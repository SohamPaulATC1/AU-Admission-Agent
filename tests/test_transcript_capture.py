"""app.py wiring for the post-call transcript (docs/spec/call-transcript-design.md §3-4)."""

from __future__ import annotations

import asyncio
import inspect
import unittest

import call_transcript
from tests.harness import scenarios
from tests.harness.appctl import app, live_call_state
from tests.harness.fakes import (
    FakeClock,
    FakePlivoClient,
    FakePlivoWS,
    FakeSession,
    InboundDriver,
    resp_audio,
    resp_input_transcription,
    resp_interrupted,
    resp_output_transcription,
    resp_turn_complete,
    run_gemini_output,
)

SPEECH_FRAMES = 100    # 2 s of the disclaimer recording: enough to open a caller turn
SILENCE_FRAMES = 60    # enough for the VAD to end it


def _feed_caller(transcript):
    """Run speech then silence through the real inbound path; return what Gemini was sent."""
    async def scenario():
        clock = FakeClock()   # virtual app clock, so two runs make identical decisions
        ws = FakePlivoWS(clock)
        session = FakeSession()
        call_state = live_call_state()
        call_state["transcript"] = transcript
        with clock.install():
            async with InboundDriver(call_state, ws, session) as driver:
                for frame in scenarios.caller_speech_frames(SPEECH_FRAMES) + scenarios.silence_frames(SILENCE_FRAMES):
                    await driver.feed_pcm8(frame)
        return session.sent

    return asyncio.run(scenario())


class TestCallerCapture(unittest.TestCase):
    def test_caller_turn_holds_exactly_what_gemini_was_sent(self):
        transcript = call_transcript.CallTranscript()
        sent = _feed_caller(transcript)
        transcript.caller_end()
        kinds = [kind for kind, _ in sent]
        self.assertIn("activityStart", kinds)
        audio = b"".join(payload for kind, payload in sent if kind == "audio")
        self.assertGreater(len(audio), 0)
        self.assertEqual(b"".join(bytes(t["pcm"]) for t in transcript.turns), audio)
        self.assertEqual(len(transcript.turns), kinds.count("activityStart"))

    def test_capture_does_not_change_what_gemini_is_sent(self):
        self.assertEqual(_feed_caller(call_transcript.CallTranscript()), _feed_caller(None))


class TestGeminiSideCapture(unittest.TestCase):
    def _run(self, script, transcript):
        async def scenario():
            call_state = live_call_state(with_denoiser=False)
            call_state["transcript"] = transcript
            await run_gemini_output(FakeSession(script), FakePlivoWS(FakeClock()), call_state, FakePlivoClient())
        asyncio.run(scenario())

    def test_turn_complete_line(self):
        transcript = call_transcript.CallTranscript()
        self._run([resp_output_transcription("নমস্কার,"), resp_output_transcription(" আমি নেহা"),
                   resp_turn_complete()], transcript)
        self.assertEqual([(t["speaker"], t["text"], t["interrupted"]) for t in transcript.turns],
                         [("agent", "নমস্কার, আমি নেহা", False)])

    def test_interrupted_line(self):
        transcript = call_transcript.CallTranscript()
        self._run([resp_output_transcription("B.Tech এর fees"), resp_interrupted()], transcript)
        self.assertEqual([(t["text"], t["interrupted"]) for t in transcript.turns],
                         [("B.Tech এর fees", True)])

    def test_gemini_caller_text_attaches_to_the_caller_turn(self):
        transcript = call_transcript.CallTranscript()
        transcript.caller_start()
        transcript.caller_audio(b"\x01\x00" * 160)
        transcript.caller_end()
        self._run([resp_input_transcription("হ্যাঁ বলুন"), resp_audio(scenarios.model_audio_24k(100))],
                  transcript)
        self.assertEqual(transcript.turns[0]["gemini_text"], "হ্যাঁ বলুন")

    def test_disabled_capture_is_harmless(self):
        self._run([resp_output_transcription("hi"), resp_turn_complete()], None)


class TestWiring(unittest.TestCase):
    """handle_media_stream is not drivable offline (tests/harness/README.md): pin it by source."""

    @classmethod
    def setUpClass(cls):
        cls.media = inspect.getsource(app.handle_media_stream)
        cls.inbound = inspect.getsource(app.stream_plivo_to_gemini)
        cls.outbound = inspect.getsource(app.stream_gemini_to_plivo)

    def test_created_only_when_enabled_after_the_log_tag(self):
        tag = self.media.index("call_log_tag.set(")
        create = self.media.index("call_state[\"transcript\"] = call_transcript.CallTranscript()")
        gate = self.media.rindex("if call_transcript.TRANSCRIPT_ENABLED:", 0, create)
        self.assertLess(tag, gate)

    def test_job_starts_in_the_finally_after_the_stats(self):
        final = self.media.rindex("    finally:\n")
        stats = self.media.index("log_call_stats(call_state)", final)
        job = self.media.index("finish_transcript(call_state)", final)
        self.assertLess(stats, job)
        helper = inspect.getsource(app.finish_transcript)
        self.assertIn("call_transcript.start_job(", helper)
        self.assertIn("VERTEX_PROJECT, vertex_access_token", helper)

    def test_hook_counts(self):
        source = self.inbound + self.outbound
        self.assertEqual(source.count(".caller_start()"), 2)    # activityStart + deferred activityStart
        self.assertEqual(source.count(".caller_audio("), 3)     # every caller audio send
        self.assertEqual(source.count(".caller_end()"), 1)      # activityEnd
        self.assertEqual(source.count(".caller_gemini_text("), 1)
        self.assertEqual(source.count(".agent_started()"), 1)
        self.assertEqual(source.count(".agent_text("), 2)       # turn_complete + interrupted
        self.assertEqual(source.count("audio=types.Blob("), 3,
                         "a new caller audio send needs its own caller_audio hook")

    def test_vertex_access_token_refreshes_first(self):
        source = inspect.getsource(app.vertex_access_token)
        self.assertLess(source.index("await ensure_vertex_token()"),
                        source.index("load_vertex_credentials().token"))


if __name__ == "__main__":
    unittest.main()
