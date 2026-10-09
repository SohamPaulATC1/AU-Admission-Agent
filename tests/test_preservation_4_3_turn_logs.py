"""Task 4.3 -- uninterrupted-turn and interrupted-turn log formats.

OBSERVATION-FIRST. Requirements 3.4, 3.5.

Golden records observed on unfixed code:

  uninterrupted turn (``stream_gemini_to_plivo`` then ``send_plivo_audio``):
      "\U0001F916 [GEMINI]: <transcript>"
      "AI speech playback ended at HH:MM:SS.mmm (calculated duration: N.NNs)"
      -- duration computed as ``current_utterance_bytes / 8000.0`` (app.py 1974)

  interrupted turn:
      "\U0001F6D1 Gemini confirmed interruption"
      "\U0001F916 [GEMINI] (Interrupted): <partial transcript>"
      and, from the *inbound* coroutine, not this one:
      "\U0001F399\uFE0F [TIMING] AI Speech Interrupted at: HH:MM:SS.mmm"

CORRECTION to the spec's framing of 4.3: the two "interrupted" lines it groups
together are emitted by two different coroutines. ``[TIMING] AI Speech
Interrupted at:`` is emitted by ``stream_plivo_to_gemini`` (app.py 1374) when the
VAD gate fires; ``\U0001F916 [GEMINI] (Interrupted):`` is emitted by
``stream_gemini_to_plivo`` (app.py 1655) when the *model* confirms the
interruption. They are pinned separately here, because a change to the barge-in
decision moves the first without touching the second.
"""

from __future__ import annotations

import asyncio
import audioop
import re
import unittest

from tests.harness import scenarios
from tests.harness.appctl import LogCapture, app, live_call_state
from tests.harness.fakes import (
    FakeClock,
    FakePlivoClient,
    FakePlivoWS,
    FakeSession,
    InboundDriver,
    SenderHarness,
    resp_audio,
    resp_input_transcription,
    resp_interrupted,
    resp_output_transcription,
    resp_turn_complete,
    run_gemini_output,
)

TRANSCRIPT = "\u0986\u09AE\u09BF \u09A6\u09C1\u0983\u0996\u09BF\u09A4, \u0986\u09AE\u09BF\u2026"

PLAYBACK_ENDED_RE = re.compile(
    r"^AI speech playback ended at \d{2}:\d{2}:\d{2}\.\d{3} "
    r"\(calculated duration: \d+\.\d{2}s\)$"
)
INTERRUPTED_TIMING_RE = re.compile(
    r"^\U0001F399\uFE0F \[TIMING\] AI Speech Interrupted at: \d{2}:\d{2}:\d{2}\.\d{3}$"
)
STARTED_TIMING_RE = re.compile(
    r"^\U0001F399\uFE0F \[TIMING\] AI Speech Started playing at: \d{2}:\d{2}:\d{2}\.\d{3}$"
)


class TestUninterruptedTurn(unittest.TestCase):
    def setUp(self):
        async def scenario():
            clock = FakeClock()
            ws = FakePlivoWS(clock)
            client = FakePlivoClient()
            session = FakeSession([
                resp_output_transcription(TRANSCRIPT),
                resp_audio(scenarios.model_audio_24k(500)),
                resp_turn_complete(),
            ])
            call_state = live_call_state(with_denoiser=False)
            with clock.install(), LogCapture() as log:
                await run_gemini_output(session, ws, call_state, client)
                queued = []
                while not call_state["plivo_output_queue"].empty():
                    queued.append(call_state["plivo_output_queue"].get_nowait())
                async with SenderHarness(call_state, clock, ws, session, client) as sender:
                    await sender.enqueue(*queued)
                    await sender.finish_utterance()
            return call_state, ws, session, log, b"".join(queued)

        (self.call_state, self.ws, self.session,
         self.log, self.queued_ulaw) = asyncio.run(scenario())

    def test_gemini_transcript_line(self):
        self.assertEqual(self.log.matching("[GEMINI]:"), [f"\U0001F916 [GEMINI]: {TRANSCRIPT}"])
        self.assertEqual(self.call_state["ai_text_buffer"], "")
        self.assertEqual(self.call_state["conversation_log"],
                         [{"role": "agent", "text": TRANSCRIPT}])

    def test_playback_ended_line_format_and_duration_arithmetic(self):
        lines = self.log.matching("AI speech playback ended at")
        self.assertEqual(len(lines), 1, self.log.lines)
        self.assertRegex(lines[0], PLAYBACK_ENDED_RE)
        duration = float(re.search(r"duration: ([\d.]+)s", lines[0]).group(1))
        self.assertAlmostEqual(duration, len(self.ws.outbound_ulaw) / 8000.0, places=2)

    def test_started_playing_line_format(self):
        lines = self.log.matching("AI Speech Started playing at")
        self.assertEqual(len(lines), 1)
        self.assertRegex(lines[0], STARTED_TIMING_RE)

    def test_full_outbound_byte_stream_matches_the_model_audio(self):
        expected = scenarios.model_audio_to_ulaw(scenarios.model_audio_24k(500))
        self.assertEqual(self.queued_ulaw, expected)
        whole_frames = len(expected) // app.PLIVO_ULAW_CHUNK_SIZE * app.PLIVO_ULAW_CHUNK_SIZE
        self.assertEqual(self.ws.outbound_ulaw, expected[:whole_frames])

    def test_turn_end_state(self):
        self.assertTrue(self.call_state["turn_complete"])
        self.assertFalse(self.call_state["assistant_speaking"])
        self.assertEqual(self.call_state["current_utterance_bytes"], 0)
        self.assertIsNone(self.call_state["ai_playback_start_time"])
        self.assertTrue(self.call_state["greeting_completed"])

    def test_log_sequence(self):
        lines = [line for line in self.log.lines
                 if "cancelled" not in line and "Terminating" not in line]
        self.assertEqual(lines, [
            "Ready to stream audio from Gemini to Plivo",
            f"\U0001F916 [GEMINI]: {TRANSCRIPT}",
            "Ready to send audio to Plivo",
            lines[3],   # started-playing, timestamp-bearing
            lines[4],   # playback-ended, timestamp-bearing
        ])
        self.assertRegex(lines[3], STARTED_TIMING_RE)
        self.assertRegex(lines[4], PLAYBACK_ENDED_RE)


class TestInterruptedTurnFromTheModel(unittest.TestCase):
    def setUp(self):
        async def scenario():
            clock = FakeClock()
            ws = FakePlivoWS(clock)
            client = FakePlivoClient()
            call_state = live_call_state(with_denoiser=False)
            session = FakeSession([
                resp_output_transcription(TRANSCRIPT),
                resp_audio(scenarios.model_audio_24k(200)),
                resp_interrupted(),
                resp_input_transcription("I'm sorry."),
                resp_audio(scenarios.model_audio_24k(100)),
                resp_turn_complete(),
            ])
            with clock.install(), LogCapture() as log:
                await run_gemini_output(session, ws, call_state, client)
            return call_state, ws, session, log

        self.call_state, self.ws, self.session, self.log = asyncio.run(scenario())

    def test_interrupted_transcript_line_format(self):
        self.assertEqual(
            self.log.matching("(Interrupted)"),
            [f"\U0001F916 [GEMINI] (Interrupted): {TRANSCRIPT}"],
        )
        self.assertEqual(self.call_state["ai_text_buffer"], "")

    def test_confirmation_line_and_queue_drain_and_clear_audio(self):
        order = self.log.order_of("Gemini confirmed interruption", "(Interrupted)")
        self.assertEqual(order, sorted(order))
        self.assertEqual(self.ws.clear_audio_count, 1)

    def test_phantom_caller_turn_is_logged_as_a_user_turn(self):
        """Recorded baseline for the concern-(b) mechanism.

        With ``assistant_speaking`` False after the interruption, the next
        ``model_turn`` flushes ``user_text_buffer`` to
        "\U0001F5E3\uFE0F [USER]: ..." -- which is how ``I'm sorry.`` reached the
        log as a caller turn. This pins the log path only, which is unchanged.
        Concern (b) is fixed upstream (the echo gate and the post-playback tail
        gate stop echo opening the turn; HANDOFF section 5.8), not here.
        """
        self.assertEqual(self.log.matching("[USER]:"), ["\U0001F5E3\uFE0F [USER]: I'm sorry."])
        self.assertIn({"role": "user", "text": "I'm sorry."}, self.call_state["conversation_log"])

    def test_log_sequence(self):
        lines = [line for line in self.log.lines if "cancelled" not in line]
        self.assertEqual(lines, [
            "Ready to stream audio from Gemini to Plivo",
            "\U0001F6D1 Gemini confirmed interruption",
            f"\U0001F916 [GEMINI] (Interrupted): {TRANSCRIPT}",
            "\U0001F5E3\uFE0F [USER]: I'm sorry.",
        ])


class TestInterruptedTimingLineFromTheInboundPath(unittest.TestCase):
    def test_timing_line_format(self):
        async def scenario():
            clock = FakeClock()
            ws = FakePlivoWS(clock)
            session = FakeSession()
            call_state = live_call_state(
                assistant_speaking=True,
                current_utterance_bytes=1040,
            )
            call_state["ai_playback_start_time"] = clock.now(app.ist_tz)
            with clock.install(), LogCapture() as log:
                async with InboundDriver(call_state, ws, session) as driver:
                    for frame in scenarios.caller_speech_frames(14):
                        await driver.feed_pcm8(frame)
            return log

        log = asyncio.run(scenario())
        lines = log.matching("AI Speech Interrupted at")
        self.assertEqual(len(lines), 1)
        self.assertRegex(lines[0], INTERRUPTED_TIMING_RE)


if __name__ == "__main__":
    unittest.main()
