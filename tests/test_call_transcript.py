"""call_transcript.py: post-call transcript file (docs/spec/call-transcript-design.md)."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import wave
from datetime import datetime
from unittest import mock

import httpx

import call_transcript as ct

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class StepClock:
    """Monotonic stand-in: returns ``value``; tests move it by hand."""

    def __init__(self, value=100.0):
        self.value = value

    def __call__(self):
        return self.value


def _import_module(value):
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    env.pop("TRANSCRIPT_ENABLED", None)
    if value is not None:
        env["TRANSCRIPT_ENABLED"] = value
    code = "import call_transcript; print(call_transcript.TRANSCRIPT_ENABLED)"
    return subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, env=env,
                          capture_output=True, text=True, timeout=120)


class TestConfig(unittest.TestCase):
    def test_default_is_on(self):
        result = _import_module(None)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split()[-1], "True")

    def test_zero_turns_it_off(self):
        result = _import_module(" 0 ")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split()[-1], "False")

    def test_unknown_value_fails_at_import(self):
        result = _import_module("yes")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("TRANSCRIPT_ENABLED must be '0' or '1', got 'yes'", result.stderr)

    def test_defaults(self):
        self.assertEqual(ct.TRANSCRIPT_DIR, "TRANSCRIPTS")
        self.assertEqual(ct.STT_MODEL, "gemini-3.5-transcribe-preview")
        self.assertEqual(ct.STT_LOCATION, "global")
        self.assertEqual((ct.STT_PRICE_AUDIO_INPUT, ct.STT_PRICE_TEXT_INPUT, ct.STT_PRICE_OUTPUT),
                         (2.00, 2.00, 12.00))


class TestCallTranscriptCapture(unittest.TestCase):
    def setUp(self):
        self.clock = StepClock(100.0)
        self.t = ct.CallTranscript(clock=self.clock, now=datetime(2026, 10, 12, 10, 0, 0, tzinfo=ct.IST))

    def test_started_at_is_ist_wall_clock(self):
        self.assertEqual(self.t.started_at, "2026-10-12 10:00:00 IST")

    def test_caller_turn_records_start_and_audio(self):
        self.clock.value = 105.0
        self.t.caller_start()
        self.t.caller_audio(b"\x01\x00\x02\x00")
        self.t.caller_audio(b"\x03\x00")
        self.t.caller_end()
        self.assertEqual(self.t.turns, [{"speaker": "user", "start_s": 5.0,
                                         "pcm": bytearray(b"\x01\x00\x02\x00\x03\x00"),
                                         "gemini_text": ""}])

    def test_audio_outside_a_turn_is_ignored(self):
        self.t.caller_audio(b"\x01\x00")
        self.t.caller_start()
        self.t.caller_end()
        self.t.caller_audio(b"\x02\x00")
        self.assertEqual(self.t.turns, [])

    def test_turn_without_audio_is_discarded(self):
        self.t.caller_start()
        self.t.caller_end()
        self.assertEqual(self.t.turns, [])

    def test_start_closes_a_turn_left_open(self):
        self.t.caller_start()
        self.t.caller_audio(b"\x01\x00")
        self.clock.value = 103.0
        self.t.caller_start()
        self.t.caller_audio(b"\x02\x00")
        self.t.caller_end()
        self.assertEqual([turn["start_s"] for turn in self.t.turns], [0.0, 3.0])

    def test_caller_end_twice_is_harmless(self):
        self.t.caller_start()
        self.t.caller_audio(b"\x01\x00")
        self.t.caller_end()
        self.t.caller_end()
        self.assertEqual(len(self.t.turns), 1)

    def test_gemini_text_goes_to_the_last_closed_caller_turn(self):
        self.t.caller_start()
        self.t.caller_audio(b"\x01\x00")
        self.t.caller_end()
        self.t.caller_start()          # caller already talking again
        self.t.caller_audio(b"\x02\x00")
        self.t.caller_gemini_text("হ্যাঁ বলুন")
        self.t.caller_gemini_text("আরও")
        self.t.caller_end()
        self.assertEqual([turn["gemini_text"] for turn in self.t.turns], ["হ্যাঁ বলুন আরও", ""])

    def test_gemini_text_without_a_closed_caller_turn_is_dropped(self):
        self.t.caller_gemini_text("orphan")
        self.assertEqual(self.t.turns, [])

    def test_agent_turn_uses_the_first_piece_time(self):
        self.clock.value = 101.0
        self.t.agent_started()
        self.clock.value = 102.0
        self.t.agent_started()          # later pieces do not move the start
        self.clock.value = 104.0
        self.t.agent_text("নমস্কার")
        self.assertEqual(self.t.turns, [{"speaker": "agent", "start_s": 1.0,
                                         "text": "নমস্কার", "interrupted": False}])

    def test_agent_turn_without_a_start_uses_the_flush_time(self):
        self.clock.value = 106.0
        self.t.agent_text("B.Tech এর fees", interrupted=True)
        self.assertEqual(self.t.turns[0]["start_s"], 6.0)
        self.assertTrue(self.t.turns[0]["interrupted"])

    def test_agent_start_resets_after_each_turn(self):
        self.clock.value = 101.0
        self.t.agent_started()
        self.t.agent_text("one")
        self.clock.value = 109.0
        self.t.agent_text("two")
        self.assertEqual([turn["start_s"] for turn in self.t.turns], [1.0, 9.0])

    def test_has_turns(self):
        self.assertFalse(self.t.has_turns())
        self.t.agent_text("hello")
        self.assertTrue(self.t.has_turns())

    def test_has_turns_counts_an_open_caller_turn(self):
        self.t.caller_start()
        self.assertFalse(self.t.has_turns(), "an open turn with no audio is not a turn")
        self.t.caller_audio(b"\x01\x00")
        self.assertTrue(self.t.has_turns())

    def test_elapsed(self):
        self.clock.value = 120.5
        self.assertEqual(self.t.elapsed_s(), 20.5)


def _caller(start_s, seconds, gemini_text=""):
    return {"speaker": "user", "start_s": start_s,
            "pcm": bytearray(b"\x00\x00" * int(ct.SAMPLE_RATE * seconds)), "gemini_text": gemini_text}


def _agent(start_s, text, interrupted=False):
    return {"speaker": "agent", "start_s": start_s, "text": text, "interrupted": interrupted}


class TestChooseLanguage(unittest.TestCase):
    def test_bengali(self):
        self.assertEqual(ct.choose_language(["আমি নেহা, Adamas University থেকে"]), "bn-IN")

    def test_hindi(self):
        self.assertEqual(ct.choose_language(["मैं नेहा, Adamas University से"]), "hi-IN")

    def test_english_or_nothing(self):
        self.assertEqual(ct.choose_language(["Hello, this is Neha"]), "en-IN")
        self.assertEqual(ct.choose_language([]), "en-IN")

    def test_majority_wins(self):
        self.assertEqual(ct.choose_language(["নমস্কার আমি নেহা বলছি", "नमस्ते"]), "bn-IN")
        self.assertEqual(ct.choose_language(["ঠিক", "मैं नेहा बोल रही हूँ"]), "hi-IN")


class TestJoinCallerAudio(unittest.TestCase):
    def test_spans_and_gaps(self):
        wav, spans, total = ct.join_caller_audio([_caller(0, 1.0), _caller(9, 0.5)])
        self.assertEqual(spans, [(0.0, 1.0), (2.5, 3.0)])
        self.assertEqual(total, 4.5)
        with wave.open(io.BytesIO(wav)) as w:
            self.assertEqual((w.getnchannels(), w.getsampwidth(), w.getframerate()), (1, 2, 16000))
            self.assertEqual(w.getnframes(), int(16000 * 4.5))

    def test_gap_is_digital_silence(self):
        turn = {"speaker": "user", "start_s": 0, "pcm": bytearray(b"\x10\x00" * 160), "gemini_text": ""}
        wav, _, _ = ct.join_caller_audio([turn])
        with wave.open(io.BytesIO(wav)) as w:
            frames = w.readframes(w.getnframes())
        self.assertEqual(frames[:320], b"\x10\x00" * 160)
        self.assertEqual(set(frames[320:]), {0})


class TestWords(unittest.TestCase):
    RESPONSE = {"candidates": [{"content": {"parts": [{"audioTranscription": {"words": [
        {"word": "হ্যাঁ", "startOffset": "0.100s", "endOffset": "0.4s"},
        {"word": "বলুন", "startOffset": "1s", "endOffset": "1.2s"},
        {"word": "এমবিএ", "startOffset": "2.600s", "endOffset": "2.9s"},
    ]}}]}}]}

    def test_extract(self):
        self.assertEqual(ct.extract_words(self.RESPONSE),
                         [("হ্যাঁ", 0.1), ("বলুন", 1.0), ("এমবিএ", 2.6)])

    def test_extract_without_words(self):
        self.assertEqual(ct.extract_words({"candidates": [{"content": {"parts": [{"text": "x"}]}}]}), [])
        self.assertEqual(ct.extract_words({}), [])

    def test_split_inside_and_in_gaps(self):
        spans = [(0.0, 1.0), (2.5, 3.0), (4.5, 5.0)]
        words = [("a", 0.2), ("b", 1.1), ("c", 2.4), ("d", 2.7), ("e", 3.6)]
        # 1.1 is 0.1 past span 0; 2.4 is 0.1 before span 1; 3.6 is nearer span 1 (0.6) than span 2 (0.9).
        self.assertEqual(ct.split_words(words, spans), [["a", "b"], ["c", "d", "e"], []])


class TestLatinize(unittest.TestCase):
    def test_exact(self):
        self.assertEqual(ct.latinize_word("এমবিএ"), "MBA")
        self.assertEqual(ct.latinize_word("बीटेक"), "B.Tech")

    def test_bengali_suffix_is_split_off(self):
        self.assertEqual(ct.latinize_word("এলএলএমটা"), "LL.M টা")
        self.assertEqual(ct.latinize_word("এমবিএটাও"), "MBA টাও")

    def test_vowel_sign_endings_stay_native(self):
        # বিটেকে / বিটেকের end in a vowel sign; splitting would leave a dangling "ে".
        self.assertEqual(ct.latinize_word("বিটেকে"), "বিটেকে")
        self.assertEqual(ct.latinize_word("বিটেকের"), "বিটেকের")

    def test_no_partial_match(self):
        self.assertEqual(ct.latinize_word("এমবিএস"), "এমবিএস")
        self.assertEqual(ct.latinize_word("ভালো"), "ভালো")

    def test_devanagari_takes_no_suffix(self):
        self.assertEqual(ct.latinize_word("एमबीएका"), "एमबीएका")


class TestBody(unittest.TestCase):
    def test_ok_words_become_user_lines(self):
        turns = [_caller(5, 1.0, "fallback text")]
        results = ct.caller_results(turns, [["এমবিএ", "টা"]])
        self.assertEqual(results, [("User", "MBA টা")])

    def test_turn_with_no_words_falls_back(self):
        results = ct.caller_results([_caller(5, 1.0, "ঠিক আছে।")], [[]])
        self.assertEqual(results, [("User (fallback)", "ঠিক আছে।")])

    def test_turn_with_no_words_and_no_fallback_is_dropped(self):
        turns = [_agent(0, "নমস্কার"), _caller(5, 1.0, "")]
        lines = ct.body_lines(turns, ct.caller_results([turns[1]], [[]]))
        self.assertEqual(lines, ["[00:00] Neha: নমস্কার"])

    def test_stt_failed_uses_gemini_text_for_every_turn(self):
        results = ct.caller_results([_caller(1, 1, "a"), _caller(2, 1, "")], None)
        self.assertEqual(results, [("User (fallback)", "a"), None])

    def test_barge_in_orders_by_start_time(self):
        # Capture order: caller turn closed first, Neha's interrupted line flushed after it.
        turns = [_caller(8.2, 1.0), _agent(7.0, "B.Tech এর fees", interrupted=True)]
        lines = ct.body_lines(turns, [("User", "MBA টা")])
        self.assertEqual(lines, ["[00:07] Neha (interrupted): B.Tech এর fees", "[00:08] User: MBA টা"])

    def test_minutes_format(self):
        lines = ct.body_lines([_agent(754.9, "bye")], [])
        self.assertEqual(lines, ["[12:34] Neha: bye"])


class TestCost(unittest.TestCase):
    USAGE = {"usageMetadata": {"promptTokenCount": 2385, "candidatesTokenCount": 158,
                               "promptTokensDetails": [{"modality": "AUDIO", "tokenCount": 2074},
                                                       {"modality": "TEXT", "tokenCount": 311}]}}

    def test_usage_tokens(self):
        self.assertEqual(ct.usage_tokens(self.USAGE), (2074, 311, 158))
        self.assertEqual(ct.usage_tokens({}), (0, 0, 0))

    def test_test8_cost(self):
        # (2074 * 2.00 + 311 * 2.00 + 158 * 12.00) / 1e6 = 0.006666
        self.assertAlmostEqual(ct.cost_usd(2074, 311, 158), 0.006666, places=9)

    def test_cost_line_ok(self):
        self.assertEqual(ct.cost_line((2074, 311, 158), "ok", ""),
                         "transcription_cost_usd: 0.0067 (audio_in=2074 text_in=311 output=158 tokens, "
                         "model=gemini-3.5-transcribe-preview)")

    def test_cost_line_failed_and_skipped(self):
        self.assertEqual(ct.cost_line(None, "failed", "http 429"),
                         "transcription_cost_usd: 0.0000 (stt failed: http 429)")
        self.assertEqual(ct.cost_line(None, "skipped", "no caller audio"),
                         "transcription_cost_usd: 0.0000 (stt skipped: no caller audio)")

    def test_status_text(self):
        self.assertEqual(ct.status_text("ok", ""), "ok")
        self.assertEqual(ct.status_text("failed", "timeout"), "failed (timeout)")


class TestRenderAndWrite(unittest.TestCase):
    def test_render(self):
        text = ct.render_file(call_uuid="u-1", started_at="2026-10-12 10:00:00 IST", duration_s=20,
                              language="bn-IN", kind="ok", reason="",
                              lines=["[00:00] Neha: hi"], tokens=(10, 0, 1))
        self.assertEqual(text, "call_uuid: u-1\nstarted_at: 2026-10-12 10:00:00 IST\nduration_sec: 20\n"
                               "language: bn-IN\nstt_model: gemini-3.5-transcribe-preview\nstt_status: ok\n"
                               "\n[00:00] Neha: hi\n\n"
                               "transcription_cost_usd: 0.0000 (audio_in=10 text_in=0 output=1 tokens, "
                               "model=gemini-3.5-transcribe-preview)\n")

    def test_append_segment_creates_the_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "u-1.txt")
            self.assertFalse(ct.append_segment(path, "নমস্কার\n"))
            with open(path, encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "নমস্কার\n")
            self.assertEqual(os.listdir(folder), ["u-1.txt"])

    def test_append_segment_keeps_the_earlier_segment(self):
        # A transferred call that comes back has the same call_uuid: append, never overwrite.
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "u-1.txt")
            ct.append_segment(path, "first leg\n")
            self.assertTrue(ct.append_segment(path, "second leg\n"))
            with open(path, encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "first leg\n" + ct.SEGMENT_SEPARATOR + "second leg\n")
            self.assertEqual(os.listdir(folder), ["u-1.txt"])


def _run(coro):
    return asyncio.run(coro)


async def _token():
    return "tok-123"


class FakeSleep:
    def __init__(self):
        self.delays = []

    async def __call__(self, seconds):
        self.delays.append(seconds)


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class TestRequest(unittest.TestCase):
    def test_endpoint(self):
        self.assertEqual(ct.stt_endpoint("proj-1"),
                         "https://aiplatform.googleapis.com/v1/projects/proj-1/locations/global"
                         "/publishers/google/models/gemini-3.5-transcribe-preview:generateContent")
        self.assertTrue(ct.stt_endpoint("p", "us").startswith("https://aiplatform.us.rep.googleapis.com/"))
        self.assertTrue(ct.stt_endpoint("p", "us-central1").startswith("https://us-central1-aiplatform.googleapis.com/"))

    def test_body(self):
        body = ct.build_request_body(b"RIFFdata", "bn-IN")
        part = body["contents"][0]["parts"][0]["inlineData"]
        self.assertEqual(part["mimeType"], "audio/wav")
        self.assertEqual(base64.b64decode(part["data"]), b"RIFFdata")
        self.assertEqual(body["generationConfig"]["audioTranscriptionConfig"],
                         {"languageCodes": ["bn-IN"], "customVocabulary": list(ct.VOCABULARY),
                          "wordTimestamp": True})

    def test_success_sends_bearer_and_json(self):
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, json={"ok": 1})

        async def go():
            async with _client(handler) as client:
                return await ct.request_transcription(client, "https://x/y", {"a": 1}, _token, sleep=FakeSleep())

        self.assertEqual(_run(go()), ({"ok": 1}, 0))
        self.assertEqual(seen[0].headers["Authorization"], "Bearer tok-123")
        self.assertEqual(json.loads(seen[0].content), {"a": 1})

    def _statuses(self, statuses):
        calls = []

        def handler(request):
            status = statuses[min(len(calls), len(statuses) - 1)]
            calls.append(status)
            return httpx.Response(status, json={"ok": 1} if status == 200 else {"error": "e"})

        sleep = FakeSleep()

        async def go():
            async with _client(handler) as client:
                return await ct.request_transcription(client, "https://x/y", {}, _token, sleep=sleep)

        return go, calls, sleep

    def test_429_then_success(self):
        go, calls, sleep = self._statuses([429, 429, 200])
        self.assertEqual(_run(go()), ({"ok": 1}, 2))
        self.assertEqual(sleep.delays, [2, 4])

    def test_500_is_retried(self):
        go, calls, _ = self._statuses([503, 200])
        self.assertEqual(_run(go())[1], 1)

    def test_400_is_not_retried(self):
        go, calls, sleep = self._statuses([400])
        with self.assertRaises(ct.SttError) as caught:
            _run(go())
        self.assertEqual(caught.exception.reason, "http 400")
        self.assertEqual(len(calls), 1)
        self.assertEqual(sleep.delays, [])

    def test_retries_stop_at_the_cap(self):
        go, calls, sleep = self._statuses([429])
        with self.assertRaises(ct.SttError) as caught:
            _run(go())
        self.assertEqual(caught.exception.reason, "http 429")
        self.assertEqual(len(calls), 7)
        self.assertEqual(sleep.delays, [2, 4, 8, 16, 32, 60])

    def test_timeout_then_success(self):
        calls = []

        def handler(request):
            calls.append(1)
            if len(calls) == 1:
                raise httpx.ReadTimeout("slow", request=request)
            return httpx.Response(200, json={"ok": 1})

        async def go():
            async with _client(handler) as client:
                return await ct.request_transcription(client, "https://x/y", {}, _token, sleep=FakeSleep())

        self.assertEqual(_run(go()), ({"ok": 1}, 1))

    def test_connection_error_reason(self):
        def handler(request):
            raise httpx.ConnectError("dns", request=request)

        async def go():
            async with _client(handler) as client:
                return await ct.request_transcription(client, "https://x/y", {}, _token, sleep=FakeSleep())

        with self.assertRaises(ct.SttError) as caught:
            _run(go())
        self.assertEqual(caught.exception.reason, "connection error")

    def test_token_error_fails_at_once(self):
        async def bad_token():
            raise RuntimeError("no key file")

        def handler(request):
            raise AssertionError("must not be called")

        async def go():
            async with _client(handler) as client:
                return await ct.request_transcription(client, "https://x/y", {}, bad_token, sleep=FakeSleep())

        with self.assertRaises(ct.SttError) as caught:
            _run(go())
        self.assertEqual(caught.exception.reason, "token error")

    def test_error_detail_is_capped(self):
        def handler(request):
            return httpx.Response(403, text="x" * 500)

        async def go():
            async with _client(handler) as client:
                return await ct.request_transcription(client, "https://x/y", {}, _token, sleep=FakeSleep())

        with self.assertRaises(ct.SttError) as caught:
            _run(go())
        self.assertEqual(len(caught.exception.detail), 200)


EXPECTED_GOLDEN = (
    "call_uuid: test-uuid\n"
    "started_at: 2026-10-12 10:00:00 IST\n"
    "duration_sec: 20\n"
    "language: bn-IN\n"
    "stt_model: gemini-3.5-transcribe-preview\n"
    "stt_status: ok\n"
    "\n"
    "[00:00] Neha: নমস্কার, আমি নেহা\n"
    "[00:05] User: হ্যাঁ বলুন\n"
    "[00:07] Neha (interrupted): B.Tech এর fees\n"
    "[00:08] User: MBA টা\n"
    "[00:12] User (fallback): ঠিক আছে।\n"
    "\n"
    "transcription_cost_usd: 0.0011 (audio_in=125 text_in=311 output=20 tokens, "
    "model=gemini-3.5-transcribe-preview)\n"
)

GOLDEN_RESPONSE = {
    "candidates": [{"content": {"parts": [{"audioTranscription": {"words": [
        {"word": "হ্যাঁ", "startOffset": "0.100s"},
        {"word": "বলুন", "startOffset": "0.500s"},
        {"word": "এমবিএ", "startOffset": "2.600s"},
        {"word": "টা", "startOffset": "2.800s"},
    ]}}]}}],
    "usageMetadata": {"candidatesTokenCount": 20, "promptTokensDetails": [
        {"modality": "AUDIO", "tokenCount": 125}, {"modality": "TEXT", "tokenCount": 311}]},
}


def golden_transcript():
    """Spans after joining: turn 1 (0, 1.0), turn 2 (2.5, 3.0), turn 3 (4.5, 5.0)."""
    clock = StepClock(100.0)
    t = ct.CallTranscript(clock=clock, now=datetime(2026, 10, 12, 10, 0, 0, tzinfo=ct.IST))
    t.agent_started()
    clock.value = 104.0
    t.agent_text("নমস্কার, আমি নেহা")
    clock.value = 105.0
    t.caller_start()
    t.caller_audio(bytes(32000))           # 1.0 s
    t.caller_end()
    clock.value = 107.0
    t.agent_started()
    clock.value = 108.0
    t.caller_start()                        # barge-in: caller starts while Neha talks
    t.caller_audio(bytes(16000))            # 0.5 s
    clock.value = 108.5
    t.agent_text("B.Tech এর fees", interrupted=True)
    t.caller_end()
    t.caller_gemini_text("MBA টা")
    clock.value = 112.0
    t.caller_start()
    t.caller_audio(bytes(16000))            # 0.5 s, the STT finds no words here
    t.caller_end()
    t.caller_gemini_text("ঠিক আছে।")
    clock.value = 120.0
    return t


class TestWriteTranscript(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)

    def _write(self, transcript, handler, **kwargs):
        async def go():
            async with _client(handler) as client:
                return await ct.write_transcript(transcript, "test-uuid", "proj-1", _token,
                                                 client=client, sleep=FakeSleep(),
                                                 out_dir=self.folder, **kwargs)
        return _run(go())

    def _read(self, path):
        with open(path, encoding="utf-8") as handle:
            return handle.read()

    def test_golden_file(self):
        bodies = []

        def handler(request):
            bodies.append(json.loads(request.content))
            return httpx.Response(200, json=GOLDEN_RESPONSE)

        with LogCaptureTranscript() as log:
            path = self._write(golden_transcript(), handler)
        self.assertEqual(path, os.path.join(self.folder, "test-uuid.txt"))
        self.assertEqual(self._read(path), EXPECTED_GOLDEN)
        config = bodies[0]["generationConfig"]["audioTranscriptionConfig"]
        self.assertEqual(config["languageCodes"], ["bn-IN"])
        self.assertEqual(os.listdir(self.folder), ["test-uuid.txt"])
        [line] = [l for l in log.lines if "[TRANSCRIPT]" in l]
        self.assertRegex(line, r"^📝 \[TRANSCRIPT\] status=ok lang=bn-IN user_turns=3 fallback=1 "
                               r"words=4 audio_s=6 latency=\d+\.\ds retries=0 cost=\$0\.0011 file=")
        self.assertNotIn("হ্যাঁ", line, "transcript text never goes into the log")

    def test_failure_falls_back_for_every_caller_line(self):
        path = self._write(golden_transcript(), lambda request: httpx.Response(400, text="bad"))
        text = self._read(path)
        self.assertIn("stt_status: failed (http 400)\n", text)
        self.assertIn("[00:08] User (fallback): MBA টা\n", text)
        self.assertIn("[00:12] User (fallback): ঠিক আছে।\n", text)
        self.assertNotIn("[00:05]", text, "turn 1 had no Gemini text, so it is dropped")
        self.assertTrue(text.endswith("transcription_cost_usd: 0.0000 (stt failed: http 400)\n"))

    def test_ok_response_without_words_is_a_failure(self):
        empty = {"candidates": [{"content": {"parts": [{"text": "x"}]}}], "usageMetadata": {}}
        text = self._read(self._write(golden_transcript(), lambda r: httpx.Response(200, json=empty)))
        self.assertIn("stt_status: failed (no words in response)\n", text)
        self.assertIn("User (fallback): MBA টা", text)

    def test_skipped_when_the_caller_never_spoke(self):
        t = ct.CallTranscript(clock=StepClock(0.0))
        t.agent_text("Hello, this is Neha")

        def handler(request):
            raise AssertionError("no request without caller audio")

        text = self._read(self._write(t, handler))
        self.assertIn("language: en-IN\nstt_model: gemini-3.5-transcribe-preview\n"
                      "stt_status: skipped (no caller audio)\n", text)
        self.assertIn("[00:00] Neha: Hello, this is Neha\n", text)
        self.assertTrue(text.endswith("transcription_cost_usd: 0.0000 (stt skipped: no caller audio)\n"))

    def test_open_caller_turn_at_teardown_is_transcribed(self):
        t = ct.CallTranscript(clock=StepClock(0.0))
        t.caller_start()
        t.caller_audio(bytes(32000))     # caller hangs up mid-sentence: no caller_end()
        response = {"candidates": [{"content": {"parts": [{"audioTranscription": {"words": [
            {"word": "আচ্ছা", "startOffset": "0.2s"}]}}]}}]}
        text = self._read(self._write(t, lambda r: httpx.Response(200, json=response)))
        self.assertIn("[00:00] User: আচ্ছা\n", text)

    def test_bug_in_split_still_writes_fallback(self):
        with mock.patch.object(ct, "split_words", side_effect=ZeroDivisionError("bug")):
            text = self._read(self._write(golden_transcript(), lambda r: httpx.Response(200, json=GOLDEN_RESPONSE)))
        self.assertIn("stt_status: failed (internal error: ZeroDivisionError)\n", text)
        self.assertIn("User (fallback): MBA টা", text)

    def test_bug_in_render_still_writes_fallback(self):
        with mock.patch.object(ct, "latinize_word", side_effect=KeyError("bug")):
            text = self._read(self._write(golden_transcript(), lambda r: httpx.Response(200, json=GOLDEN_RESPONSE)))
        self.assertIn("stt_status: failed (internal error: KeyError)\n", text)
        self.assertIn("User (fallback): MBA টা", text)

    def test_write_error_returns_none_and_raises_nothing(self):
        with mock.patch.object(ct, "append_segment", side_effect=OSError("disk full")):
            with LogCaptureTranscript() as log:
                path = self._write(golden_transcript(), lambda r: httpx.Response(200, json=GOLDEN_RESPONSE))
        self.assertIsNone(path)
        self.assertTrue(any("❌ [TRANSCRIPT] write failed" in line for line in log.lines))

    def test_two_jobs_at_once_write_two_files_and_create_the_folder(self):
        folder = os.path.join(self.folder, "fresh", "TRANSCRIPTS")

        async def go():
            async with _client(lambda r: httpx.Response(200, json=GOLDEN_RESPONSE)) as client:
                return await asyncio.gather(*[
                    ct.write_transcript(golden_transcript(), uuid, "proj-1", _token, client=client,
                                        sleep=FakeSleep(), out_dir=folder)
                    for uuid in ("call-a", "call-b")])

        paths = _run(go())
        self.assertEqual(sorted(os.listdir(folder)), ["call-a.txt", "call-b.txt"])
        self.assertTrue(all(path is not None for path in paths))

    def test_returning_leg_is_appended_after_the_first(self):
        first = self._write(golden_transcript(), lambda r: httpx.Response(200, json=GOLDEN_RESPONSE))
        first_text = self._read(first)
        with LogCaptureTranscript() as log:
            second = self._write(golden_transcript(), lambda r: httpx.Response(400, text="bad"))
        self.assertEqual(first, second)
        text = self._read(second)
        self.assertTrue(text.startswith(first_text + ct.SEGMENT_SEPARATOR + "call_uuid: test-uuid\n"))
        self.assertEqual(text.count("call_uuid: test-uuid\n"), 2)
        self.assertIn("stt_status: failed (http 400)\n", text.split(ct.SEGMENT_SEPARATOR)[1])
        [line] = [l for l in log.lines if l.startswith("📝 [TRANSCRIPT]")]
        self.assertTrue(line.endswith(" appended"))


class TestJobOrderPerCall(unittest.TestCase):
    """Two legs of one call: the later leg's STT may finish first, but it appends second."""

    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)

    def test_slow_first_leg_still_lands_first(self):
        release_first = None
        real_write = ct.write_transcript

        def leg(name):
            t = ct.CallTranscript(clock=StepClock(0.0))
            t.agent_text(name)
            return t

        async def go():
            nonlocal release_first
            release_first = asyncio.Event()

            async def write(transcript, call_uuid, project, get_token, **kwargs):
                if transcript.turns[0]["text"] == "leg one":
                    await release_first.wait()   # first leg's STT is slow
                return await real_write(transcript, call_uuid, project, get_token,
                                        out_dir=self.folder, **kwargs)

            with mock.patch.object(ct, "write_transcript", write):
                first = ct.start_job(leg("leg one"), "u-9", "p", _token)
                second = ct.start_job(leg("leg two"), "u-9", "p", _token)
                await asyncio.sleep(0.05)
                self.assertFalse(second.done(), "second leg waits for the first")
                release_first.set()
                await asyncio.gather(first, second)
            self.assertNotIn("u-9", ct._last_job)

        _run(go())
        with open(os.path.join(self.folder, "u-9.txt"), encoding="utf-8") as handle:
            text = handle.read()
        self.assertLess(text.index("Neha: leg one"), text.index("Neha: leg two"))
        self.assertEqual(text.count(ct.SEGMENT_SEPARATOR), 1)


class TestStartJob(unittest.TestCase):
    def test_job_runs_detached_and_is_held_until_done(self):
        seen = {}

        async def fake_write(transcript, call_uuid, project, get_token):
            seen["args"] = (call_uuid, project)
            await asyncio.sleep(0)
            return "path"

        async def go():
            with mock.patch.object(ct, "write_transcript", fake_write):
                task = ct.start_job(object(), "u-1", "proj-1", _token)
                self.assertIn(task, ct._jobs)
                await task
                await asyncio.sleep(0)
                self.assertNotIn(task, ct._jobs)

        _run(go())
        self.assertEqual(seen["args"], ("u-1", "proj-1"))

    def test_a_crash_is_logged_not_raised(self):
        async def broken(*args):
            raise RuntimeError("boom")

        async def go():
            with mock.patch.object(ct, "write_transcript", broken):
                return await ct.start_job(object(), "u-1", "p", _token)

        with LogCaptureTranscript() as log:
            self.assertIsNone(_run(go()))
        self.assertTrue(any("❌ [TRANSCRIPT] job crashed" in line for line in log.lines))


class LogCaptureTranscript:
    """Rendered messages from the call_transcript logger."""

    def __enter__(self):
        self.lines = []
        capture = self

        class _Handler(logging.Handler):
            def emit(self, record):
                capture.lines.append(record.getMessage())

        self._handler = _Handler(level=logging.INFO)
        self._level = ct.logger.level
        ct.logger.setLevel(logging.INFO)
        ct.logger.addHandler(self._handler)
        return self

    def __exit__(self, *exc_info):
        ct.logger.removeHandler(self._handler)
        ct.logger.setLevel(self._level)
        return False


if __name__ == "__main__":
    unittest.main()
