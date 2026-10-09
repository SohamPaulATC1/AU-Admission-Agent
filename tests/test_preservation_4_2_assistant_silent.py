"""Task 4.2 -- the assistant-silent inbound path, byte-for-byte.

OBSERVATION-FIRST. Requirements 3.1, 3.2, 3.3.

Golden records observed on unfixed code:

* with ``assistant_speaking`` False the gate uses ``VAD_THRESHOLD`` (0.75) and
  ``VAD_SPEECH_ONSET_FRAMES`` (6), and ``is_speaking`` flips on fed-frame index 7
  for the ``recorded.wav`` fixture.
* the bytes handed to the model are byte-for-byte reproducible across runs: the
  whole inbound chain (AEC -> ratecv 8k/48k -> RNNoise -> ratecv 48k/16k -> AGC ->
  soft limiter) is deterministic for a fixed input sequence.
* no ``clearAudio`` and no ``playAudio`` is emitted on this path at all.
* speech end: after 12 speech frames followed by silence, ``speech_ended`` fires
  on fed-frame index 22, via ``VAD_SILENCE_OFFSET_FRAMES`` (10).
"""

from __future__ import annotations

import asyncio
import hashlib
import unittest

from tests.harness import scenarios
from tests.harness.appctl import LogCapture, app, live_call_state
from tests.harness.fakes import FakeClock, FakePlivoWS, FakeSession, InboundDriver

# --- golden records, observed on unfixed code --------------------------------
ONSET_FRAME_INDEX = 7
SPEECH_END_FRAME_INDEX = 22
SPEECH_FRAMES = 12
TRAILING_SILENCE_FRAMES = 14

ASSISTANT_SILENT_LOG_SEQUENCE = [
    "Ready to stream audio from Plivo to Gemini",
    "\U0001F50A [AGC] Inbound RMS: -120.0 dB | Gain: +0.0 dB",
    "\U0001F3A4 User speech detected",
    "\u25B6\uFE0F Sent activityStart to Gemini",
    " \U0001F507 User speech end detected",
    "\u23F9\uFE0F Sent activityEnd to Gemini",
]


def _run(frames):
    async def scenario():
        clock = FakeClock()
        ws = FakePlivoWS(clock)
        session = FakeSession()
        call_state = live_call_state()
        with clock.install(), LogCapture() as log:
            async with InboundDriver(call_state, ws, session) as driver:
                for frame in frames:
                    await driver.feed_pcm8(frame)
        return call_state, ws, session, log, driver

    return asyncio.run(scenario())


class TestAssistantSilentPath(unittest.TestCase):
    def test_uses_the_six_frame_threshold_path(self):
        call_state, ws, session, log, driver = _run(scenarios.caller_speech_frames(12))
        onset = driver.first_frame_index_where(lambda snap: snap["is_speaking"])
        self.assertEqual(onset, ONSET_FRAME_INDEX)
        self.assertEqual(driver.snapshots[onset]["speech_count"], app.VAD_SPEECH_ONSET_FRAMES)
        self.assertEqual(app.VAD_SPEECH_ONSET_FRAMES, 6)
        self.assertEqual(app.VAD_THRESHOLD, 0.75)

    def test_no_outbound_plivo_traffic_at_all(self):
        call_state, ws, session, log, driver = _run(scenarios.caller_speech_frames(12))
        self.assertEqual(ws.frames, [])

    def test_bytes_to_the_model_are_byte_for_byte_reproducible(self):
        frames = scenarios.caller_speech_frames(12)
        digests = []
        for _ in range(2):
            _cs, _ws, session, _log, _driver = _run(frames)
            digests.append(hashlib.sha256(session.audio_bytes_sent).hexdigest())
        self.assertEqual(digests[0], digests[1])
        self.assertGreater(len(digests[0]), 0)

    def test_model_input_is_whole_640_byte_chunks_plus_a_flush(self):
        """app.py 1416-1421 slices ``GEMINI_PCM_CHUNK_SIZE`` at a time."""
        _cs, _ws, session, _log, _driver = _run(
            scenarios.caller_speech_frames(SPEECH_FRAMES)
            + scenarios.silence_frames(TRAILING_SILENCE_FRAMES))
        sizes = [len(blob) for blob in session.audio_sent]
        body, tail = sizes[:-1], sizes[-1]
        self.assertTrue(all(size % app.GEMINI_PCM_CHUNK_SIZE == 0 for size in body), sizes)
        self.assertGreater(tail, 0)

    def test_speech_end_uses_the_silence_offset_counter(self):
        call_state, ws, session, log, driver = _run(
            scenarios.caller_speech_frames(SPEECH_FRAMES)
            + scenarios.silence_frames(TRAILING_SILENCE_FRAMES))
        ended = driver.first_frame_index_where(
            lambda snap: not snap["is_speaking"] and snap["chunk_index"] > ONSET_FRAME_INDEX)
        self.assertEqual(ended, SPEECH_END_FRAME_INDEX)
        self.assertEqual(app.VAD_SILENCE_OFFSET_FRAMES, 10)
        self.assertFalse(call_state["user_activity_open"])
        self.assertTrue(call_state["awaiting_model"])
        self.assertEqual(session.sent_kinds[-1], "activityEnd")

    def test_existing_log_sequence(self):
        _cs, _ws, _session, log, _driver = _run(
            scenarios.caller_speech_frames(SPEECH_FRAMES)
            + scenarios.silence_frames(TRAILING_SILENCE_FRAMES))
        lines = [line for line in log.lines if "cancelled" not in line]
        self.assertEqual(lines, ASSISTANT_SILENT_LOG_SEQUENCE)

    def test_preroll_is_bounded_before_activity_opens(self):
        _cs, _ws, _session, _log, driver = _run(scenarios.silence_frames(30))
        peak = max(snap["preroll_bytes"] for snap in driver.snapshots)
        self.assertLessEqual(peak, app.PREROLL_MAX_BYTES_PCM16)
        self.assertEqual(app.PREROLL_MAX_BYTES_PCM16, 6400)

    def test_silence_only_never_opens_an_activity_window(self):
        call_state, ws, session, log, _driver = _run(scenarios.silence_frames(20))
        self.assertFalse(call_state["is_speaking"])
        self.assertFalse(call_state["user_activity_open"])
        self.assertEqual(session.sent, [])
        self.assertEqual(ws.frames, [])


if __name__ == "__main__":
    unittest.main()
