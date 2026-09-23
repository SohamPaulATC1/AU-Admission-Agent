"""Task 4.1 -- genuine barge-in latency and side-effect ordering.

OBSERVATION-FIRST. Every number below was read off the UNFIXED code first and
then asserted. Requirement 3.1.

Golden records observed on unfixed code (``recorded.wav`` resampled to 8 kHz,
fed one 20 ms frame at a time through ``app.stream_plivo_to_gemini``):

  assistant_speaking = True   -> ``is_speaking`` flips on fed-frame index 5,
                                 with ``rnnoise_speech_count == 4``
                                 (VAD_SPEECH_ONSET_FRAMES_WHILE_SPEAKING, 80 ms)
  assistant_speaking = False  -> flips on fed-frame index 4,
                                 with ``rnnoise_speech_count == 3``
                                 (VAD_SPEECH_ONSET_FRAMES, 60 ms)

  side-effect order on barge-in:
      1. "\N{studio microphone} [TIMING] AI Speech Interrupted at: HH:MM:SS.mmm"
      2. ``plivo_output_queue`` drained to empty
      3. ``clearAudio`` sent + "Cleared Plivo playback buffer"
      4. ``activityStart`` sent + "Sent activityStart to Gemini"
      5. preroll flushed into ``gemini_input_buffer``

A note on what "cuts at 4 frames / 80 ms" means in the code, since the spec
phrases it as a latency: it is the *onset counter*, ``rnnoise_speech_count >=
VAD_SPEECH_ONSET_FRAMES_WHILE_SPEAKING``, over frames whose average RNNoise
speech probability exceeds ``VAD_THRESHOLD_WHILE_SPEAKING``. The absolute
fed-frame index at which that happens depends on the fixture's lead-in silence,
so both the counter value and the fixture-specific index are pinned.
"""

from __future__ import annotations

import asyncio
import unittest
from datetime import timedelta

from tests.harness import scenarios
from tests.harness.appctl import LogCapture, app, live_call_state
from tests.harness.fakes import FakeClock, FakePlivoWS, FakeSession, InboundDriver

# --- golden records, observed on unfixed code --------------------------------
ONSET_FRAME_INDEX_WHILE_SPEAKING = 5
ONSET_FRAME_INDEX_WHILE_SILENT = 4
ONSET_COUNT_WHILE_SPEAKING = 4      # == VAD_SPEECH_ONSET_FRAMES_WHILE_SPEAKING
ONSET_COUNT_WHILE_SILENT = 3        # == VAD_SPEECH_ONSET_FRAMES

BARGEIN_LOG_ORDER = [
    "\U0001F3A4 User speech detected",
    "[TIMING] AI Speech Interrupted at:",
    "Cleared Plivo playback buffer",
    "Sent activityStart to Gemini",
]


def _prime_playback(call_state, clock, *, delivered_ms: float, queued_frames: int = 4):
    """Put the call in "assistant is mid-utterance" state.

    ``delivered_ms`` becomes ``current_utterance_bytes`` at 8000 bytes/s, which
    is exactly how ``send_plivo_audio`` accounts for bytes handed to Plivo.

    The playback start is **back-dated** by the same amount so the wall clock
    agrees with the byte count, i.e. ideal realtime playout. This matters because
    truncation classification now reads the wall clock rather than the byte
    count: leaving the start at "now" would make heard_ms 0 and classify every
    primed state anomalous. Back-dating rather than advancing the clock keeps
    "now" -- and every logged timestamp -- untouched.
    """
    call_state["assistant_speaking"] = True
    call_state["ai_playback_start_time"] = (
        clock.now(app.ist_tz) - timedelta(milliseconds=delivered_ms)
    )
    call_state["current_utterance_bytes"] = int(round(delivered_ms / 1000.0 * 8000))
    for index in range(queued_frames):
        call_state["plivo_output_queue"].put_nowait(bytes([index]) * app.PLIVO_ULAW_CHUNK_SIZE)


class TestGenuineBargeInLatency(unittest.TestCase):
    def _run(self, *, assistant_speaking: bool, delivered_ms: float = 200.0, frames: int = 10):
        async def scenario():
            clock = FakeClock()
            ws = FakePlivoWS(clock)
            session = FakeSession()
            call_state = live_call_state()
            if assistant_speaking:
                _prime_playback(call_state, clock, delivered_ms=delivered_ms)
            with clock.install(), LogCapture() as log:
                async with InboundDriver(call_state, ws, session) as driver:
                    for frame in scenarios.caller_speech_frames(frames):
                        await driver.feed_pcm8(frame)
                    onset = driver.first_frame_index_where(lambda snap: snap["is_speaking"])
            return call_state, ws, session, log, driver, onset

        return asyncio.run(scenario())

    def test_while_speaking_cuts_at_four_frames_eighty_ms(self):
        call_state, ws, session, log, driver, onset = self._run(assistant_speaking=True)
        self.assertEqual(onset, ONSET_FRAME_INDEX_WHILE_SPEAKING)
        self.assertEqual(driver.snapshots[onset]["speech_count"], ONSET_COUNT_WHILE_SPEAKING)
        self.assertEqual(ONSET_COUNT_WHILE_SPEAKING, app.VAD_SPEECH_ONSET_FRAMES_WHILE_SPEAKING)
        self.assertEqual(ONSET_COUNT_WHILE_SPEAKING * 20, 80)
        self.assertTrue(log.contains("Cleared Plivo playback buffer"))

    def test_while_silent_cuts_at_three_frames_sixty_ms(self):
        call_state, ws, session, log, driver, onset = self._run(assistant_speaking=False)
        self.assertEqual(onset, ONSET_FRAME_INDEX_WHILE_SILENT)
        self.assertEqual(driver.snapshots[onset]["speech_count"], ONSET_COUNT_WHILE_SILENT)
        self.assertEqual(ONSET_COUNT_WHILE_SILENT, app.VAD_SPEECH_ONSET_FRAMES)
        self.assertEqual(ws.clear_audio_count, 0, "no playback to clear when the assistant is silent")

    def test_while_speaking_costs_exactly_one_extra_frame(self):
        self.assertEqual(
            ONSET_FRAME_INDEX_WHILE_SPEAKING - ONSET_FRAME_INDEX_WHILE_SILENT, 1)


class TestBargeInSideEffectOrdering(unittest.TestCase):
    def setUp(self):
        async def scenario():
            clock = FakeClock()
            ws = FakePlivoWS(clock)
            session = FakeSession()
            call_state = live_call_state()
            _prime_playback(call_state, clock, delivered_ms=200.0, queued_frames=4)
            queue_sizes = []
            with clock.install(), LogCapture() as log:
                async with InboundDriver(call_state, ws, session) as driver:
                    for frame in scenarios.caller_speech_frames(8):
                        await driver.feed_pcm8(frame)
                        queue_sizes.append(call_state["plivo_output_queue"].qsize())
            return call_state, ws, session, log, driver, queue_sizes

        (self.call_state, self.ws, self.session,
         self.log, self.driver, self.queue_sizes) = asyncio.run(scenario())

    def test_queue_drained_then_clear_audio_then_activity_start(self):
        order = self.log.order_of(*BARGEIN_LOG_ORDER)
        self.assertEqual(order, sorted(order), f"log order changed: {self.log.lines}")

    def test_queue_is_empty_after_the_barge_in(self):
        self.assertTrue(self.call_state["plivo_output_queue"].empty())
        self.assertEqual(self.queue_sizes[-1], 0)
        self.assertEqual(self.queue_sizes[0], 4, "queue was primed with 4 frames")

    def test_exactly_one_clear_audio_and_one_activity_start(self):
        self.assertEqual(self.ws.clear_audio_count, 1)
        self.assertEqual(self.ws.events.count("playAudio"), 0)
        self.assertEqual(self.session.count("activityStart"), 1)
        self.assertEqual(self.session.sent_kinds[0], "activityStart")

    def test_clear_audio_frame_shape(self):
        clear_frames = [f["frame"] for f in self.ws.frames if f["event"] == "clearAudio"]
        self.assertEqual(clear_frames, [{"event": "clearAudio", "stream_id": "test-stream-id"}])

    def test_timing_trackers_reset_and_flags_set(self):
        self.assertIsNone(self.call_state["ai_playback_start_time"])
        self.assertEqual(self.call_state["current_utterance_bytes"], 0)
        self.assertFalse(self.call_state["assistant_speaking"])
        self.assertFalse(self.call_state["interrupting"])
        self.assertTrue(self.call_state["user_activity_open"])
        self.assertFalse(self.call_state["turn_complete"])

    def test_preroll_is_flushed_into_the_model_input_buffer(self):
        self.assertEqual(len(self.call_state["preroll_pcm16"]), 0)
        self.assertGreater(len(self.session.audio_bytes_sent), 0)


class TestLongTriggerModeStillCutsPromptly(unittest.TestCase):
    """Example 4's >1 s mode (1238-11803 ms) -- genuine barge-ins.

    The unfixed barge-in path does not read ``current_utterance_bytes`` at all
    when deciding, so delivered duration cannot change the decision. This test
    records that fact rather than assuming it, because it is exactly the
    invariant a future ``delivered_ms``-aware gate must not break.
    """

    DELIVERED_MS = (1238, 1855, 3204, 5514, 11803)

    def test_every_long_mode_delivered_duration_cuts_at_the_same_frame(self):
        observed = {}
        for delivered_ms in self.DELIVERED_MS:
            async def scenario(delivered_ms=delivered_ms):
                clock = FakeClock()
                ws = FakePlivoWS(clock)
                session = FakeSession()
                call_state = live_call_state()
                _prime_playback(call_state, clock, delivered_ms=delivered_ms)
                with clock.install(), LogCapture() as log:
                    async with InboundDriver(call_state, ws, session) as driver:
                        for frame in scenarios.caller_speech_frames(8):
                            await driver.feed_pcm8(frame)
                        onset = driver.first_frame_index_where(lambda snap: snap["is_speaking"])
                return onset, ws.clear_audio_count, session.count("activityStart"), log.lines

            observed[delivered_ms] = asyncio.run(scenario())

        for delivered_ms, (onset, clears, starts, _lines) in observed.items():
            self.assertEqual(onset, ONSET_FRAME_INDEX_WHILE_SPEAKING, f"delivered_ms={delivered_ms}")
            self.assertEqual(clears, 1, f"delivered_ms={delivered_ms}")
            self.assertEqual(starts, 1, f"delivered_ms={delivered_ms}")

    def test_short_mode_is_now_distinguishable_but_existing_lines_are_unchanged(self):
        """INTENTIONAL BASELINE UPDATE — task 5.3. Defect 1.6 is closed here.

        The original form of this test asserted that the 130 ms short-trigger case
        and the 1238 ms long-trigger case produce the *identical* log sequence,
        with its own docstring naming task 5.3's ``[ANOMALY]`` line as the thing
        that would change it. 5.3 has now landed, so the equality it recorded is
        deliberately no longer true.

        What is asserted instead keeps both halves honest:

        * every **pre-existing** log line is still byte-identical between the two
          cases, in the same order -- that is preservation requirement 3.5 and it
          has not moved;
        * the **new diagnostic** lines now differ, and only the short case carries
          ``[ANOMALY]``. That difference is the whole point of concern (d).
        """
        def run(delivered_ms):
            async def scenario():
                clock = FakeClock()
                ws = FakePlivoWS(clock)
                session = FakeSession()
                call_state = live_call_state()
                _prime_playback(call_state, clock, delivered_ms=delivered_ms)
                with clock.install(), LogCapture() as log:
                    async with InboundDriver(call_state, ws, session) as driver:
                        for frame in scenarios.caller_speech_frames(8):
                            await driver.feed_pcm8(frame)
                return log, ws.events, session.sent_kinds

            return asyncio.run(scenario())

        short_log, short_events, short_sent = run(130)
        long_log, long_events, long_sent = run(1238)

        # unchanged: the existing log lines, the Plivo frames, the model sends
        self.assertEqual(short_log.existing_lines, long_log.existing_lines)
        self.assertEqual(short_events, long_events)
        self.assertEqual(short_sent, long_sent)

        # changed on purpose: the new diagnostic lines now separate the two modes
        self.assertNotEqual(short_log.diagnostic_lines, long_log.diagnostic_lines)
        self.assertTrue(any("[ANOMALY]" in line for line in short_log.diagnostic_lines))
        self.assertFalse(any("[ANOMALY]" in line for line in long_log.diagnostic_lines))
        self.assertTrue(any("classification=anomalous" in line
                            for line in short_log.diagnostic_lines))
        self.assertTrue(any("classification=normal" in line
                            for line in long_log.diagnostic_lines))


if __name__ == "__main__":
    unittest.main()
