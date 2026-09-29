"""Task 4.8 -- session resumption, debug recording and call stats.

OBSERVATION-FIRST. Requirements 3.6, 3.11.

Golden records observed on unfixed code:

* ``session_resumption_update`` with ``resumable`` and a ``new_handle`` stores the
  handle and logs "\U0001F511 Session resumption handle updated"; a
  non-resumable update stores nothing.
* ``go_away`` logs "\u26A0\uFE0F GoAway received from Gemini. Time left: <t>",
  sets ``go_away_received`` and raises ``GeminiSessionDisconnected``, which is
  what the reconnect loop in ``handle_media_stream`` catches.
* debug WAV: 10 inbound 20 ms frames produce 3040 frames at 16 kHz mono 16-bit
  (6080 bytes). Not 320 samples per inbound frame: the chain is
  ratecv 8k->48k, RNNoise, ratecv 48k->16k, and the stateful resamplers absorb a
  short startup transient. The observed value is recorded rather than derived.
* ``log_call_stats`` emits seven lines in a fixed order with the pricing
  arithmetic from app.py 724-728.

DEFERRED -- not offline-testable, with the reason:

* ``MAX_GEMINI_RECONNECTS`` retry accounting and context restoration on
  resumption live inside ``handle_media_stream`` (app.py 1041-1197), which needs
  a live Quart websocket context (``websocket.args``), a real ``genai.Client``
  and a real ``client.aio.live.connect``. There is no seam between the reconnect
  ``for`` loop and the transport, so exercising it offline would mean either
  extracting the loop (a behavioural-risk refactor this pass declines) or faking
  ``google.genai`` end to end. What would make it testable later: extracting the
  per-attempt body into a module-level coroutine taking ``(client, config,
  call_state)``, which is a pure-move refactor but out of scope here.
"""

from __future__ import annotations

import asyncio
import os
import re
import tempfile
import unittest
import wave

from tests.harness import scenarios
from tests.harness.appctl import APP_PY, REPO_ROOT, LogCapture, app, live_call_state
from tests.harness.fakes import (
    FakeClock,
    FakePlivoClient,
    FakePlivoWS,
    FakeSession,
    InboundDriver,
    resp_go_away,
    resp_resumption,
    resp_usage,
    run_gemini_output,
)

# --- golden records, observed on unfixed code --------------------------------
DEBUG_WAV_FRAMES_FOR_10_INBOUND = 3040
DEBUG_WAV_INBOUND_FRAMES = 10


class TestSessionResumptionHandleFlow(unittest.TestCase):
    def _run(self, script):
        async def scenario():
            clock = FakeClock()
            ws = FakePlivoWS(clock)
            client = FakePlivoClient()
            call_state = live_call_state(with_denoiser=False, with_aec=False)
            raised = None
            with clock.install(), LogCapture() as log:
                try:
                    await run_gemini_output(FakeSession(script), ws, call_state, client)
                except app.GeminiSessionDisconnected as exc:
                    raised = exc
            return call_state, log, raised

        return asyncio.run(scenario())

    def test_resumable_handle_is_stored_and_logged(self):
        call_state, log, raised = self._run([resp_resumption("handle-abc")])
        self.assertEqual(call_state["session_resumption_handle"], "handle-abc")
        self.assertEqual(log.matching("Session resumption"),
                         ["\U0001F511 Session resumption handle updated"])
        self.assertIsNone(raised)

    def test_non_resumable_update_is_ignored(self):
        call_state, log, _raised = self._run(
            [resp_resumption("handle-xyz", resumable=False)])
        self.assertIsNone(call_state["session_resumption_handle"])
        self.assertEqual(log.matching("Session resumption"), [])

    def test_go_away_raises_for_the_reconnect_loop(self):
        call_state, log, raised = self._run([resp_go_away("3s")])
        self.assertTrue(call_state["go_away_received"])
        self.assertIsInstance(raised, app.GeminiSessionDisconnected)
        self.assertEqual(str(raised), "GoAway received, time_left=3s")
        self.assertEqual(log.matching("GoAway"),
                         ["\u26A0\uFE0F GoAway received from Gemini. Time left: 3s"])
        self.assertTrue(log.contains("GeminiSessionDisconnected raised, propagating"))

    def test_handle_survives_up_to_the_go_away(self):
        call_state, _log, raised = self._run([resp_resumption("h1"), resp_go_away("1s")])
        self.assertEqual(call_state["session_resumption_handle"], "h1")
        self.assertIsInstance(raised, app.GeminiSessionDisconnected)

    def test_reconnect_limit_and_context_restoration_are_not_offline_testable(self):
        """Recorded gap, with the source evidence for why."""
        with open(APP_PY, "r", encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("for _attempt in range(MAX_GEMINI_RECONNECTS + 1):", source)
        self.assertIn("async with connect_live_with_timeout(client, GEMINI_MODEL, config)", source)
        self.assertIn("user_name = websocket.args.get", source)
        self.assertEqual(app.MAX_GEMINI_RECONNECTS, 5)
        self.skipTest(
            "deferred: the reconnect loop and context restoration are inline in "
            "handle_media_stream, which requires a live Quart websocket context and a real "
            "genai.Client. No seam exists; extracting one is a refactor this pass declines. "
            "Offline coverage stops at GeminiSessionDisconnected being raised correctly."
        )


class TestUsageMetadataAccounting(unittest.TestCase):
    def test_token_accounting_and_log_lines(self):
        async def scenario():
            clock = FakeClock()
            ws = FakePlivoWS(clock)
            client = FakePlivoClient()
            call_state = live_call_state(with_denoiser=False, with_aec=False)
            session = FakeSession([
                resp_usage(100, [("TEXT", 10), ("AUDIO", 20)], [("TEXT", 3), ("AUDIO", 5)]),
                resp_usage(250, [("AUDIO", 40)], [("AUDIO", 7)]),
                resp_usage(200),
            ])
            with clock.install(), LogCapture() as log:
                await run_gemini_output(session, ws, call_state, client)
            return call_state, log

        call_state, log = asyncio.run(scenario())
        self.assertEqual(call_state["tokens_text_in"], 10)
        self.assertEqual(call_state["tokens_audio_in"], 60)
        self.assertEqual(call_state["tokens_text_out"], 3)
        self.assertEqual(call_state["tokens_audio_out"], 12)
        self.assertEqual(call_state["last_usage_total_token_count"], 200)
        self.assertEqual(call_state["usage_total_updates"], 3)
        self.assertEqual(call_state["usage_total_non_monotonic_count"], 1)
        self.assertEqual(log.matching("total_token_count"), [
            "\U0001F4CA usage_metadata.total_token_count: current=100, prev=None, delta=None",
            "\U0001F4CA usage_metadata.total_token_count: current=250, prev=100, delta=150",
            "\U0001F4C9 usage_metadata.total_token_count decreased: prev=250, current=200",
        ])


class TestDebugRecording(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(prefix="bgsf_debugwav_"), "inbound.wav")

    def tearDown(self):
        if os.path.exists(self.path):
            os.remove(self.path)
        os.rmdir(os.path.dirname(self.path))

    def test_byte_count_and_wav_header(self):
        async def scenario():
            clock = FakeClock()
            ws = FakePlivoWS(clock)
            session = FakeSession()
            writer = wave.open(self.path, "wb")
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(app.GEMINI_INPUT_RATE)
            call_state = live_call_state(debug_wav_writer=writer)
            with clock.install():
                async with InboundDriver(call_state, ws, session) as driver:
                    for frame in scenarios.caller_speech_frames(DEBUG_WAV_INBOUND_FRAMES):
                        await driver.feed_pcm8(frame)
            writer.close()

        asyncio.run(scenario())
        with wave.open(self.path, "rb") as reader:
            self.assertEqual(reader.getnchannels(), 1)
            self.assertEqual(reader.getsampwidth(), 2)
            self.assertEqual(reader.getframerate(), app.GEMINI_INPUT_RATE)
            self.assertEqual(reader.getframerate(), 16000)
            self.assertEqual(reader.getnframes(), DEBUG_WAV_FRAMES_FOR_10_INBOUND)

    def test_writer_absent_is_a_no_op(self):
        async def scenario():
            clock = FakeClock()
            ws = FakePlivoWS(clock)
            session = FakeSession()
            call_state = live_call_state(debug_wav_writer=None)
            with clock.install():
                async with InboundDriver(call_state, ws, session) as driver:
                    for frame in scenarios.caller_speech_frames(4):
                        await driver.feed_pcm8(frame)
            return call_state

        call_state = asyncio.run(scenario())
        self.assertEqual(call_state["chunk_count"], 4)

    def test_far_end_recording_is_paired_with_the_inbound_one(self):
        """INTENTIONAL BASELINE UPDATE — task 5.4a has landed.

        This test previously asserted ``assertNotIn("_farend.wav", source)`` and
        ``wave.open(`` twice, as the recorded baseline for a task that was
        authorised but not yet written. The far-end writer now exists. What is
        asserted instead is the pairing and the rates, because a far-end recorded
        at the wrong declared rate would silently corrupt every future lag
        estimate: the inbound writer records post-resample ``clean_pcm_16k`` at
        ``GEMINI_INPUT_RATE`` (16 kHz), the far-end writer records pre-resample
        telephony audio at ``PLIVO_SAMPLE_RATE`` (8 kHz), and both derive their
        filenames from the same ``safe_name`` / ``timestamp_str``.
        """
        with open(APP_PY, "r", encoding="utf-8") as handle:
            source = handle.read()
        # disclaimer read + inbound debug write + far-end debug write + the one
        # loop that opens the aligned nearraw/farref/aecout writers (BASELINE
        # UPDATE, TEST1 follow-up: was 3 and 2 before the aligned recordings).
        self.assertEqual(source.count("wave.open("), 4)
        self.assertEqual(len(re.findall(r'debug_recordings/\{safe_name\}', source)), 3)
        self.assertIn(
            'aligned_path = f"debug_recordings/{safe_name}_{timestamp_str}_{kind}.wav"',
            source,
        )
        self.assertIn("aligned_wf.setframerate(PLIVO_SAMPLE_RATE)", source)
        self.assertIn('debug_wav_path = f"debug_recordings/{safe_name}_{timestamp_str}.wav"', source)
        self.assertIn(
            'farend_wav_path = f"debug_recordings/{safe_name}_{timestamp_str}_farend.wav"',
            source,
        )
        self.assertIn("debug_wf.setframerate(GEMINI_INPUT_RATE)", source)
        self.assertIn("farend_wf.setframerate(PLIVO_SAMPLE_RATE)", source)
        self.assertNotEqual(app.GEMINI_INPUT_RATE, app.PLIVO_SAMPLE_RATE)

    def test_recordings_are_gitignored(self):
        """Call audio is caller PII and must not enter version control."""
        with open(os.path.join(REPO_ROOT, ".gitignore"), "r", encoding="utf-8") as handle:
            ignored = handle.read().splitlines()
        self.assertIn("debug_recordings/", ignored)
        self.assertIn("*.wav", ignored)


class TestCallStats(unittest.TestCase):
    def test_log_call_stats_golden_record(self):
        call_state = live_call_state(
            with_denoiser=False, with_aec=False,
            tokens_text_in=1000, tokens_audio_in=2000,
            tokens_text_out=300, tokens_audio_out=400,
            last_usage_total_token_count=3700,
            usage_total_updates=5, usage_total_non_monotonic_count=1,
        )
        with LogCapture() as log:
            app.log_call_stats(call_state)

        # INTENTIONAL BASELINE UPDATE — task 5.2 inserts one 🧬 [MODEL] line after
        # the header. Every pre-existing line keeps its exact format and relative
        # order, which ``existing_lines`` asserts below.
        self.assertEqual(log.existing_lines, [
            "=== CALL ENDED : STATS & PRICING ===",
            "Tokens Used - Text In: 1000, Audio In: 2000, Text Out: 300, Audio Out: 400",
            "Last usage_metadata.total_token_count seen: 3700",
            "usage_metadata.total_token_count updates: 5, non-monotonic transitions: 1",
            "Estimated Call Cost (lower-bound): $0.012900",
            "\u26A0\uFE0F  Note: Actual cost is higher due to compounding \u2014 past tokens are "
            "re-billed each turn.",
            "====================================",
        ])
        self.assertEqual(log.lines[1], f"🧬 [MODEL] {app.GEMINI_MODEL} | "
                                      "gemini_reconnect_count: 0")

    def test_model_identifier_is_recorded(self):
        """INTENTIONAL BASELINE UPDATE — task 5.2 / defect 1.8 is closed.

        This previously asserted the opposite, as the recorded baseline: no log
        line anywhere carried the model identifier, so the next affected call
        would have been as unattributable from its own log as the reported one.
        The reported call was pinned to ``gemini-3.1-flash-live-preview`` only by
        external evidence that will not exist next time.
        """
        call_state = live_call_state(with_denoiser=False, with_aec=False)
        with LogCapture() as log:
            app.log_call_stats(call_state)
        self.assertTrue(any(app.GEMINI_MODEL in line for line in log.lines))

        with open(APP_PY, "r", encoding="utf-8") as handle:
            source = handle.read()
        references = [index + 1 for index, line in enumerate(source.splitlines())
                      if "GEMINI_MODEL" in line]
        # definition, log_call_stats, connect call, the 🧬 [MODEL] line at session
        # establishment, and the ⚠️ [ANOMALY] line so a truncation record is
        # self-attributing without cross-referencing the call's other lines.
        # Re-pinned after the wall-clock classifier and the wall-clock-continuous
        # far-end recorder were added. The count (5) and the fact that every
        # reference is a log/stats site rather than a behavioural branch are the
        # invariants; the absolute line numbers are incidental.
        # Re-pinned after the audio-pipeline redesign added imports, constants
        # and the reset_far_reference helper above these sites. Count (5) and the
        # fact that each is a log/stats/connect site rather than a behavioural
        # branch are the invariants; absolute line numbers are incidental.
        # Re-pinned again after the corroborated barge-in gate (bargein.py
        # wiring, playout-paced far reference) grew app.py above these sites.
        # Same five sites, same kinds. Shifted +6 when the far echo window's
        # constants (ECHO_FAR_MS / ECHO_FAR_BYTES_8K) were documented above them,
        # and again when the echo latch (ECHO_LATCH_BREAK_DB, apply_echo_latch)
        # was added, and again for the echo return ceiling and the latch
        # hold/end field logging, and again for the post-playback echo tail
        # gate (ECHO_TAIL_FRAMES, far_window_has_playback), and again for the
        # aligned inbound-loop recordings (ALIGNED_RECORDING_KINDS,
        # write_aligned_frames), and again for the AEC output guard
        # (AEC_OUTPUT_GUARD_ENABLED, guard_aec_output), and again for the
        # dashboard / persistent call-history work (hangup webhook, SQLite
        # call history, /api/call-history), and again for the AEC_IMPL switch
        # (aec / aec1) that now sits after load_dotenv(), above the definition,
        # and again when its comment grew a line for the default flip to aec1,
        # and again for the GEMINI_BACKEND switch (studio / vertex) and its
        # credential helpers.
        self.assertEqual(references, [64, 1721, 2233, 2243, 2659])


if __name__ == "__main__":
    unittest.main()
