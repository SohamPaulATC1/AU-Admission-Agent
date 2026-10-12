# Post-call Transcript File Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** After every call, write `TRANSCRIPTS/<call_uuid>.txt`: Neha's lines from Gemini Live's output transcription, the caller's lines from one post-call `gemini-3.5-transcribe` request on Vertex, turn by turn, with the STT cost as the last line.

**Architecture:** A new module `call_transcript.py` holds a per-call `CallTranscript` (in-memory capture fed by one-line hooks in app.py) and an async post-call job started from `handle_media_stream`'s `finally`. The job joins the caller turns into one WAV, sends one REST request with word timestamps through `httpx`, splits the words back into turns, and writes the file atomically. Any failure falls back to Gemini Live's caller text, so every call gets a file.

**Tech Stack:** Python 3.12 (venv312), stdlib (`wave`, `base64`, `asyncio`, `unittest`), `httpx==0.28.1` (already pinned), Vertex AI REST `generateContent`.

**Spec:** `docs/spec/call-transcript-design.md`

## Global Constraints

- `requirements.txt` and `.env` are frozen: no new dependency, no edit. Defaults live in code; EC2 overrides go in `/etc/au-app/common.env`.
- Tests are stdlib `unittest` only (no pytest, no hypothesis). Run the suite with `venv312/Scripts/python.exe -m unittest discover -s tests -t .`
- Never write test code through bash heredocs (escapes like `\x00` get mangled); use the Write/Edit tools.
- Set `PYTHONIOENCODING=utf-8` when printing app log lines or Bengali text on Windows.
- Never read `.env` or the credential JSON. Tests never touch the network or the key.
- `aec.py` stays byte-identical (hash pinned by `test_preservation_4_7`); `bargein.py` untouched.
- Every new `call_state` key is added to the mirror in `tests/harness/appctl.py` (guarded by `test_harness_smoke`).
- When app.py's line count changes, re-pin the GEMINI_MODEL line list in `tests/test_preservation_4_8_resumption_recording_stats.py` (`test_model_identifier_is_recorded`).
- No commits unless the operator asks (CLAUDE.md). Each task ends with a full-suite run instead.
- Never log transcript text (caller PII). `TRANSCRIPTS/` is gitignored.
- Defaults (spec §7): `TRANSCRIPT_ENABLED=1`, `TRANSCRIPT_DIR=TRANSCRIPTS`, `STT_MODEL=gemini-3.5-transcribe-preview`, `STT_LOCATION=global`, `STT_PRICE_AUDIO_INPUT=2.00`, `STT_PRICE_TEXT_INPUT=2.00`, `STT_PRICE_OUTPUT=12.00` (USD per 1M tokens).

## Review Focus

1. A phantom caller turn (audio, but the STT finds no words and Gemini has no text): the line is dropped, never written as an empty `User:` line. Test: Task 2 `test_turn_with_no_words_and_no_fallback_is_dropped`.
2. A 200 response that carries no words (timestamps missing): the file says `failed (no words in response)` and every caller line falls back, rather than `ok` with all caller lines lost. Test: Task 4 `test_ok_response_without_words_is_a_failure`.
3. Barge-in: Neha's interrupted line started before the caller's turn and must come first in the file, even though it was flushed after. Test: Task 2 `test_barge_in_orders_by_start_time`.
4. The caller hangs up mid-sentence (activity still open at teardown): that last turn is transcribed, not lost. Tests: Task 1 `test_has_turns_counts_an_open_caller_turn`, Task 4 `test_open_caller_turn_at_teardown_is_transcribed`.
5. Two calls ending together in one process, first run on a fresh box (no `TRANSCRIPTS/` yet): both files written, folder created. Test: Task 4 `test_two_jobs_at_once_write_two_files_and_create_the_folder`.

---

### Task 1: `call_transcript.py` config and in-call capture

**Files:**
- Create: `call_transcript.py`
- Test: `tests/test_call_transcript.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - Module constants `TRANSCRIPT_ENABLED: bool`, `TRANSCRIPT_DIR: str`, `STT_MODEL: str`, `STT_LOCATION: str`, `STT_PRICE_AUDIO_INPUT / STT_PRICE_TEXT_INPUT / STT_PRICE_OUTPUT: float`, `IST`, `logger = logging.getLogger("call_transcript")`.
  - `class CallTranscript(clock=time.monotonic, now=None)` with attributes `t0: float`, `started_at: str` (`"YYYY-MM-DD HH:MM:SS IST"`), `turns: list[dict]`, and methods `caller_start()`, `caller_audio(pcm: bytes)`, `caller_end()`, `caller_gemini_text(text: str)`, `agent_started()`, `agent_text(text: str, interrupted: bool = False)`, `has_turns() -> bool`, `elapsed_s() -> float`.
  - Turn dicts: caller `{"speaker": "user", "start_s": float, "pcm": bytearray, "gemini_text": str}`, Neha `{"speaker": "agent", "start_s": float, "text": str, "interrupted": bool}`. `turns` is in capture order; only closed caller turns are in it.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_call_transcript.py`:

```python
"""call_transcript.py: post-call transcript file (docs/spec/call-transcript-design.md)."""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from datetime import datetime

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


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `venv312/Scripts/python.exe -m unittest tests.test_call_transcript -v`
Expected: ERROR, `ModuleNotFoundError: No module named 'call_transcript'`.

- [ ] **Step 3: Write the module**

Create `call_transcript.py`:

```python
"""Post-call transcript file: docs/spec/call-transcript-design.md.

During the call app.py feeds a CallTranscript (in-memory appends only). After teardown
start_job() sends the caller's turns to gemini-3.5-transcribe on Vertex in one request
and writes TRANSCRIPT_DIR/<call_uuid>.txt, turn by turn, with the STT cost last.
"""

import logging
import os
import time
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("call_transcript")

IST = timezone(timedelta(hours=5, minutes=30))


def _flag(name, default):
    value = os.getenv(name, default).strip()
    if value not in ("0", "1"):
        raise ValueError(f"{name} must be '0' or '1', got {value!r}")
    return value == "1"


# 0 = no capture during the call and no post-call job.
TRANSCRIPT_ENABLED = _flag("TRANSCRIPT_ENABLED", "1")
TRANSCRIPT_DIR = os.getenv("TRANSCRIPT_DIR", "TRANSCRIPTS")
# The GA id gemini-3.5-transcribe returned 404 for the project on 2026-10-11; switch when it serves.
STT_MODEL = os.getenv("STT_MODEL", "gemini-3.5-transcribe-preview")
STT_LOCATION = os.getenv("STT_LOCATION", "global").strip().lower()
# USD per 1M tokens (AI Studio paid prices; the text-input price is assumed equal to audio).
STT_PRICE_AUDIO_INPUT = float(os.getenv("STT_PRICE_AUDIO_INPUT", "2.00"))
STT_PRICE_TEXT_INPUT = float(os.getenv("STT_PRICE_TEXT_INPUT", "2.00"))
STT_PRICE_OUTPUT = float(os.getenv("STT_PRICE_OUTPUT", "12.00"))


class CallTranscript:
    """One call's turns, in capture order. No I/O; every method is an append or a field set."""

    def __init__(self, clock=time.monotonic, now=None):
        self._clock = clock
        self.t0 = clock()
        self.started_at = (now or datetime.now(IST)).strftime("%Y-%m-%d %H:%M:%S IST")
        self.turns = []
        self._open_caller = None
        self._agent_start = None

    def _now_s(self):
        return self._clock() - self.t0

    def elapsed_s(self):
        return self._now_s()

    def caller_start(self):
        # endCall / transferCall / reconnect reset user_activity_open without an activityEnd.
        self.caller_end()
        self._open_caller = {"speaker": "user", "start_s": self._now_s(),
                             "pcm": bytearray(), "gemini_text": ""}

    def caller_audio(self, pcm):
        if self._open_caller is not None:
            self._open_caller["pcm"] += pcm

    def caller_end(self):
        turn, self._open_caller = self._open_caller, None
        if turn is not None and turn["pcm"]:
            self.turns.append(turn)

    def caller_gemini_text(self, text):
        # Gemini flushes the caller's text after activityEnd, so it belongs to the last closed turn.
        for turn in reversed(self.turns):
            if turn["speaker"] == "user":
                turn["gemini_text"] = f"{turn['gemini_text']} {text}".strip()
                return

    def agent_started(self):
        if self._agent_start is None:
            self._agent_start = self._now_s()

    def agent_text(self, text, interrupted=False):
        start = self._agent_start if self._agent_start is not None else self._now_s()
        self._agent_start = None
        self.turns.append({"speaker": "agent", "start_s": start, "text": text,
                           "interrupted": interrupted})

    def has_turns(self):
        return bool(self.turns) or bool(self._open_caller and self._open_caller["pcm"])
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `venv312/Scripts/python.exe -m unittest tests.test_call_transcript -v`
Expected: all PASS.

- [ ] **Step 5: Run the full suite**

Run: `venv312/Scripts/python.exe -m unittest discover -s tests -t .`
Expected: OK (previous count plus the new tests; 2 skipped, 1 expected failure unchanged).

---

### Task 2: Pure transforms (language, join, split, Latin terms, cost, render, atomic write)

**Files:**
- Modify: `call_transcript.py` (append)
- Test: `tests/test_call_transcript.py` (append classes)

**Interfaces:**
- Consumes: turn dicts from Task 1; `STT_MODEL`, `STT_PRICE_*`.
- Produces:
  - `SAMPLE_RATE = 16000`, `GAP_S = 1.5`, `VOCABULARY: tuple[str, ...]`, `LATIN_TERMS: dict[str, str]`, `BENGALI_SUFFIXES: frozenset[str]`
  - `choose_language(agent_texts: list[str]) -> str` (`"bn-IN" | "hi-IN" | "en-IN"`)
  - `join_caller_audio(caller_turns: list[dict]) -> tuple[bytes, list[tuple[float, float]], float]` (WAV bytes, spans, joined seconds)
  - `extract_words(data: dict) -> list[tuple[str, float]]`
  - `split_words(words, spans) -> list[list[str]]`
  - `latinize_word(word: str) -> str`
  - `caller_results(caller_turns, buckets: list[list[str]] | None) -> list[tuple[str, str] | None]`
  - `body_lines(turns, results) -> list[str]`
  - `usage_tokens(data: dict) -> tuple[int, int, int]` (audio_in, text_in, output)
  - `cost_usd(audio_in, text_in, output) -> float`
  - `cost_line(tokens: tuple | None, kind: str, reason: str) -> str`
  - `status_text(kind: str, reason: str) -> str`
  - `render_file(*, call_uuid, started_at, duration_s, language, kind, reason, lines, tokens) -> str`
  - `write_atomic(path: str, text: str) -> None`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_call_transcript.py`, before the `if __name__ == "__main__":` block (add `import io`, `import tempfile`, `import wave` and `from unittest import mock` to the imports at the top):

```python
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

    def test_write_atomic(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "u-1.txt")
            ct.write_atomic(path, "নমস্কার\n")
            with open(path, encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "নমস্কার\n")
            self.assertEqual(os.listdir(folder), ["u-1.txt"])

    def test_write_failure_leaves_no_tmp(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "u-1.txt")
            with mock.patch.object(ct.os, "replace", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    ct.write_atomic(path, "x")
            self.assertEqual(os.listdir(folder), [])
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `venv312/Scripts/python.exe -m unittest tests.test_call_transcript -v`
Expected: the new classes ERROR with `AttributeError: module 'call_transcript' has no attribute ...`; Task 1 tests still pass.

- [ ] **Step 3: Write the transforms**

Add to the imports at the top of `call_transcript.py`: `import contextlib`, `import io`, `import wave`. Append to the module:

```python
SAMPLE_RATE = 16000   # the 16 kHz PCM16 app.py sends to Gemini
GAP_S = 1.5           # silence between joined caller turns

# Sent as customVocabulary. Short and specific: a loose list inserted words the caller never said (spike).
VOCABULARY = (
    "Adamas", "Adamas University", "Neha", "B.Tech", "M.Tech", "BBA", "MBA", "BCA", "MCA",
    "B.Sc", "M.Sc", "LLB", "LL.M", "B.Pharm", "AUAT", "AUJET", "WBJEE", "JEE Main", "CAT",
    "PCM", "PCB", "CGPA", "Barasat", "Kolkata", "CSE", "Computer Science", "Data Science",
)

# Whole-call STT writes English terms in native script; map the known ones back to Latin.
LATIN_TERMS = {
    "এমবিএ": "MBA", "एमबीए": "MBA",
    "বিবিএ": "BBA", "बीबीए": "BBA",
    "বিসিএ": "BCA", "बीसीए": "BCA",
    "এমসিএ": "MCA", "एमसीए": "MCA",
    "বিটেক": "B.Tech", "বি.টেক": "B.Tech", "बीटेक": "B.Tech", "बी.टेक": "B.Tech",
    "এমটেক": "M.Tech", "এম.টেক": "M.Tech", "एमटेक": "M.Tech",
    "এলএলবি": "LLB", "एलएलबी": "LLB",
    "এলএলএম": "LL.M", "एलएलएम": "LL.M",
    "সিজিপিএ": "CGPA", "सीजीपीए": "CGPA",
    "পিসিএম": "PCM", "पीसीएम": "PCM",
    "পিসিবি": "PCB", "पीसीबी": "PCB",
    "এআই": "AI", "एआई": "AI",
}
# Bengali classifier particles that attach to a term (এলএলএমটা = LL.M + টা). Endings that start
# with a vowel sign (বিটেকে) stay native: split, they leave a dangling sign. Devanagari takes none.
BENGALI_SUFFIXES = frozenset({"টা", "টি", "টার", "টাই", "টাও"})


def _is_bengali(text):
    return any("ঀ" <= ch <= "৿" for ch in text)


def choose_language(agent_texts):
    bengali = devanagari = 0
    for text in agent_texts:
        for ch in text:
            if "ঀ" <= ch <= "৿":
                bengali += 1
            elif "ऀ" <= ch <= "ॿ":
                devanagari += 1
    if bengali == devanagari == 0:
        return "en-IN"
    return "bn-IN" if bengali >= devanagari else "hi-IN"


def join_caller_audio(caller_turns):
    """One WAV of every caller turn, GAP_S of zeros after each; spans are (begin_s, end_s)."""
    gap = bytes(2 * int(SAMPLE_RATE * GAP_S))
    pcm = bytearray()
    spans = []
    for turn in caller_turns:
        begin = len(pcm) / 2 / SAMPLE_RATE
        pcm += turn["pcm"]
        spans.append((begin, len(pcm) / 2 / SAMPLE_RATE))
        pcm += gap
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(bytes(pcm))
    return buffer.getvalue(), spans, len(pcm) / 2 / SAMPLE_RATE


def extract_words(data):
    """(word, start seconds) from candidates[0].content.parts[*].audioTranscription.words."""
    words = []
    for candidate in (data.get("candidates") or [])[:1]:
        for part in (candidate.get("content") or {}).get("parts") or []:
            for item in (part.get("audioTranscription") or {}).get("words") or []:
                words.append((str(item["word"]), float(str(item["startOffset"]).rstrip("s"))))
    return words


def split_words(words, spans):
    """Each word goes to the span holding its start, else the nearest span edge."""
    buckets = [[] for _ in spans]
    for word, start in words:
        def distance(index):
            begin, end = spans[index]
            return 0.0 if begin <= start < end else min(abs(start - begin), abs(start - end))
        buckets[min(range(len(spans)), key=distance)].append(word)
    return buckets


def latinize_word(word):
    if word in LATIN_TERMS:
        return LATIN_TERMS[word]
    for term, latin in LATIN_TERMS.items():
        if _is_bengali(term) and word.startswith(term) and word[len(term):] in BENGALI_SUFFIXES:
            return f"{latin} {word[len(term):]}"
    return word


def caller_results(caller_turns, buckets):
    """Per caller turn: ("User", text), ("User (fallback)", gemini text) or None (dropped).
    buckets None means the STT failed."""
    results = []
    for index, turn in enumerate(caller_turns):
        words = buckets[index] if buckets is not None else []
        if words:
            results.append(("User", " ".join(latinize_word(word) for word in words)))
        elif turn["gemini_text"]:
            results.append(("User (fallback)", turn["gemini_text"]))
        else:
            results.append(None)
    return results


def body_lines(turns, results):
    """Both speakers sorted by start time (stable, so ties keep capture order)."""
    pending = iter(results)
    rows = []
    for turn in turns:
        if turn["speaker"] == "agent":
            label = "Neha (interrupted)" if turn["interrupted"] else "Neha"
            rows.append((turn["start_s"], label, turn["text"]))
        else:
            result = next(pending)
            if result is not None:
                rows.append((turn["start_s"], *result))
    rows.sort(key=lambda row: row[0])
    lines = []
    for start_s, label, text in rows:
        minutes, seconds = divmod(int(start_s), 60)
        lines.append(f"[{minutes:02d}:{seconds:02d}] {label}: {text}")
    return lines


def usage_tokens(data):
    usage = data.get("usageMetadata") or {}
    audio_in = text_in = 0
    for detail in usage.get("promptTokensDetails") or []:
        if detail.get("modality") == "AUDIO":
            audio_in += detail.get("tokenCount", 0)
        else:
            text_in += detail.get("tokenCount", 0)
    return audio_in, text_in, usage.get("candidatesTokenCount", 0)


def cost_usd(audio_in, text_in, output):
    return (audio_in * STT_PRICE_AUDIO_INPUT + text_in * STT_PRICE_TEXT_INPUT
            + output * STT_PRICE_OUTPUT) / 1_000_000


def cost_line(tokens, kind, reason):
    if tokens is None:
        return f"transcription_cost_usd: 0.0000 (stt {kind}: {reason})"
    audio_in, text_in, output = tokens
    return (f"transcription_cost_usd: {cost_usd(audio_in, text_in, output):.4f} "
            f"(audio_in={audio_in} text_in={text_in} output={output} tokens, model={STT_MODEL})")


def status_text(kind, reason):
    return kind if not reason else f"{kind} ({reason})"


def render_file(*, call_uuid, started_at, duration_s, language, kind, reason, lines, tokens):
    header = [
        f"call_uuid: {call_uuid}",
        f"started_at: {started_at}",
        f"duration_sec: {duration_s}",
        f"language: {language}",
        f"stt_model: {STT_MODEL}",
        f"stt_status: {status_text(kind, reason)}",
    ]
    return "\n".join(header + [""] + lines + ["", cost_line(tokens, kind, reason)]) + "\n"


def write_atomic(path, text):
    """Write path.tmp then os.replace, so a reader never sees a half-written file."""
    temporary = path + ".tmp"
    try:
        with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(temporary)
        raise
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `venv312/Scripts/python.exe -m unittest tests.test_call_transcript -v`
Expected: all PASS.

- [ ] **Step 5: Run the full suite**

Run: `venv312/Scripts/python.exe -m unittest discover -s tests -t .`
Expected: OK.

---

### Task 3: STT request with retries

**Files:**
- Modify: `call_transcript.py` (append)
- Test: `tests/test_call_transcript.py` (append classes)

**Interfaces:**
- Consumes: `VOCABULARY`, `STT_MODEL`, `STT_LOCATION` (Task 1/2).
- Produces:
  - `class SttError(Exception)` with `.reason: str` and `.detail: str`
  - `RETRY_DELAYS_S = (2, 4, 8, 16, 32, 60)`, `REQUEST_TIMEOUT_S = 120`
  - `stt_endpoint(project: str, location: str | None = None, model: str | None = None) -> str`
  - `build_request_body(wav: bytes, language: str) -> dict`
  - `async request_transcription(client: httpx.AsyncClient, url: str, body: dict, get_token, *, sleep=asyncio.sleep) -> tuple[dict, int]` (response JSON, retries); raises `SttError`. `get_token` is an async callable returning the bearer token string.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_call_transcript.py` (add `import asyncio`, `import base64`, `import json`, `import httpx` to the imports):

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `venv312/Scripts/python.exe -m unittest tests.test_call_transcript -v`
Expected: `TestRequest` tests ERROR with `AttributeError` (`stt_endpoint`, `SttError`, ...).

- [ ] **Step 3: Write the request code**

Add `import asyncio`, `import base64` and `import httpx` to the imports of `call_transcript.py`. Append:

```python
RETRY_DELAYS_S = (2, 4, 8, 16, 32, 60)   # 7 attempts, about 2 minutes
REQUEST_TIMEOUT_S = 120


class SttError(Exception):
    """``reason`` goes into stt_status; ``detail`` (capped) only into the warning log."""

    def __init__(self, reason, detail=""):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


def stt_endpoint(project, location=None, model=None):
    location = location or STT_LOCATION
    model = model or STT_MODEL
    if location == "global":
        host = "aiplatform.googleapis.com"
    elif location in ("us", "eu"):
        host = f"aiplatform.{location}.rep.googleapis.com"
    else:
        host = f"{location}-aiplatform.googleapis.com"
    return (f"https://{host}/v1/projects/{project}/locations/{location}"
            f"/publishers/google/models/{model}:generateContent")


def build_request_body(wav, language):
    # google-genai 1.66 has no audio_transcription_config, hence plain REST.
    return {
        "contents": [{"role": "user", "parts": [
            {"inlineData": {"mimeType": "audio/wav", "data": base64.b64encode(wav).decode("ascii")}}]}],
        "generationConfig": {"audioTranscriptionConfig": {
            "languageCodes": [language], "customVocabulary": list(VOCABULARY), "wordTimestamp": True}},
    }


async def request_transcription(client, url, body, get_token, *, sleep=asyncio.sleep):
    """POST with retries on 429 / 5xx / timeout / connection errors. Returns (json, retries)."""
    for attempt in range(len(RETRY_DELAYS_S) + 1):
        try:
            token = await get_token()
        except Exception as e:
            raise SttError("token error", type(e).__name__) from e
        try:
            response = await client.post(url, json=body, timeout=REQUEST_TIMEOUT_S,
                                         headers={"Authorization": f"Bearer {token}"})
        except httpx.TimeoutException:
            reason, detail = "timeout", ""
        except httpx.TransportError as e:
            reason, detail = "connection error", type(e).__name__
        else:
            if response.status_code == 200:
                return response.json(), attempt
            reason, detail = f"http {response.status_code}", response.text[:200]
            if response.status_code != 429 and response.status_code < 500:
                raise SttError(reason, detail)
        if attempt == len(RETRY_DELAYS_S):
            raise SttError(reason, detail)
        await sleep(RETRY_DELAYS_S[attempt])
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `venv312/Scripts/python.exe -m unittest tests.test_call_transcript -v`
Expected: all PASS.

- [ ] **Step 5: Run the full suite**

Run: `venv312/Scripts/python.exe -m unittest discover -s tests -t .`
Expected: OK.

---

### Task 4: The post-call job (`write_transcript`, `start_job`)

**Files:**
- Modify: `call_transcript.py` (append)
- Test: `tests/test_call_transcript.py` (append classes)

**Interfaces:**
- Consumes: everything from Tasks 1-3.
- Produces:
  - `async write_transcript(transcript: CallTranscript, call_uuid: str, project: str, get_token, *, client=None, sleep=asyncio.sleep, out_dir=None) -> str | None` (file path, or None when the write failed). Never raises `Exception`.
  - `start_job(transcript: CallTranscript, call_uuid: str, project: str, get_token) -> asyncio.Task` (kept in the module-level set `_jobs` until done).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_call_transcript.py` (add `import logging` and `import shutil` to the imports):

```python
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
        with mock.patch.object(ct, "write_atomic", side_effect=OSError("disk full")):
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
```

Check of the golden numbers: turn starts are 5.0, 8.0, 12.0 (after `t0 = 100`); Neha's starts are 0.0 and 7.0; `duration_sec` is 120 - 100 = 20; joined audio is 1.0 + 1.5 + 0.5 + 1.5 + 0.5 + 1.5 = 6.5 s (`audio_s=6` with `:.0f` rounding half to even); cost (125 × 2 + 311 × 2 + 20 × 12) / 1e6 = 0.001112, printed 0.0011.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `venv312/Scripts/python.exe -m unittest tests.test_call_transcript -v`
Expected: `TestWriteTranscript` / `TestStartJob` ERROR with `AttributeError: ... 'write_transcript'` / `'start_job'`.

- [ ] **Step 3: Write the job**

Append to `call_transcript.py`:

```python
_jobs = set()   # running jobs, held so they are not garbage-collected mid-run


def start_job(transcript, call_uuid, project, get_token):
    """Start the post-call job detached from the call. It inherits the call's log tag."""
    task = asyncio.create_task(_guarded(write_transcript(transcript, call_uuid, project, get_token)))
    _jobs.add(task)
    task.add_done_callback(_jobs.discard)
    return task


async def _guarded(coroutine):
    try:
        return await coroutine
    except Exception:
        logger.exception("❌ [TRANSCRIPT] job crashed")
        return None


async def write_transcript(transcript, call_uuid, project, get_token, *,
                           client=None, sleep=asyncio.sleep, out_dir=None):
    started = time.perf_counter()
    transcript.caller_end()   # the caller may have hung up mid-turn
    turns = list(transcript.turns)
    caller_turns = [turn for turn in turns if turn["speaker"] == "user"]
    language = choose_language([turn["text"] for turn in turns if turn["speaker"] == "agent"])
    out_dir = out_dir or TRANSCRIPT_DIR
    path = os.path.join(out_dir, f"{call_uuid}.txt")
    kind, reason = "ok", ""
    buckets = tokens = None
    retries = words = 0
    audio_s = 0.0

    try:
        if not caller_turns:
            kind, reason = "skipped", "no caller audio"
        else:
            wav, spans, audio_s = join_caller_audio(caller_turns)
            body = build_request_body(wav, language)
            owned = client is None
            http = client or httpx.AsyncClient()
            try:
                data, retries = await request_transcription(http, stt_endpoint(project), body,
                                                            get_token, sleep=sleep)
            finally:
                if owned:
                    await http.aclose()
            found = extract_words(data)
            if not found:
                raise SttError("no words in response")
            buckets = split_words(found, spans)
            words = len(found)
            tokens = usage_tokens(data)
    except SttError as e:
        kind, reason, buckets, tokens = "failed", e.reason, None, None
        logger.warning(f"⚠️ [TRANSCRIPT] stt failed: {e.reason} {e.detail}".rstrip())
    except Exception as e:
        kind, reason, buckets, tokens = "failed", f"internal error: {type(e).__name__}", None, None
        logger.exception("⚠️ [TRANSCRIPT] internal error before render")

    def render():
        results = caller_results(caller_turns, buckets)
        text = render_file(call_uuid=call_uuid, started_at=transcript.started_at,
                           duration_s=int(transcript.elapsed_s()), language=language,
                           kind=kind, reason=reason, lines=body_lines(turns, results), tokens=tokens)
        return text, sum(1 for result in results if result and result[0] == "User (fallback)")

    try:
        text, fallback = render()
    except Exception as e:
        logger.exception("⚠️ [TRANSCRIPT] render failed, writing the fallback file")
        kind, reason, buckets, tokens = "failed", f"internal error: {type(e).__name__}", None, None
        text, fallback = render()   # buckets None: no latinize / no STT words involved

    try:
        os.makedirs(out_dir, exist_ok=True)
        write_atomic(path, text)
    except Exception as e:
        logger.error(f"❌ [TRANSCRIPT] write failed: {type(e).__name__}: {e} file={path}")
        return None

    cost = cost_usd(*tokens) if tokens else 0.0
    logger.info(
        f"📝 [TRANSCRIPT] status={status_text(kind, reason)} lang={language} "
        f"user_turns={len(caller_turns)} fallback={fallback} words={words} audio_s={audio_s:.0f} "
        f"latency={time.perf_counter() - started:.1f}s retries={retries} cost=${cost:.4f} file={path}"
    )
    return path
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `venv312/Scripts/python.exe -m unittest tests.test_call_transcript -v`
Expected: all PASS.

- [ ] **Step 5: Run the full suite**

Run: `venv312/Scripts/python.exe -m unittest discover -s tests -t .`
Expected: OK.

---

### Task 5: Wire it into app.py

**Files:**
- Modify: `app.py` (import after the DEBUG_RECORDING block ~line 49; `vertex_access_token` after `ensure_vertex_token` ~line 1593; `"transcript": None` in the `call_state` literal ~line 1976; creation after `call_log_tag.set` ~line 1985; hooks at the sites below; `start_job` after `log_call_stats(call_state)` ~line 2330)
- Modify: `tests/harness/appctl.py` (mirror key; `DIAGNOSTIC_LOG_TAGS`)
- Modify: `tests/test_preservation_4_8_resumption_recording_stats.py` (re-pin)
- Modify: `.gitignore`
- Test: `tests/test_transcript_capture.py` (new)

**Interfaces:**
- Consumes: `call_transcript.TRANSCRIPT_ENABLED`, `CallTranscript` and its methods (Task 1), `start_job(transcript, call_uuid, project, get_token)` (Task 4).
- Produces: `app.vertex_access_token() -> str` (async); `call_state["transcript"]: CallTranscript | None`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_transcript_capture.py`:

```python
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
        job = self.media.index("call_transcript.start_job(", final)
        self.assertLess(stats, job)
        self.assertIn("VERTEX_PROJECT, vertex_access_token)", self.media[job:job + 300])

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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `venv312/Scripts/python.exe -m unittest tests.test_transcript_capture -v`
Expected: `test_caller_turn_holds_exactly_what_gemini_was_sent` FAILS (no turns captured, so the joined pcm is empty and differs from the audio sent); the Gemini-side capture tests FAIL (no turns / empty `gemini_text`); `TestWiring` ERRORS with `ValueError: substring not found` and `AttributeError: ... 'vertex_access_token'`. `test_capture_does_not_change_what_gemini_is_sent` and `test_disabled_capture_is_harmless` already pass (nothing reads the key yet).

- [ ] **Step 3: Edit app.py**

3a. After the `DEBUG_RECORDING = DEBUG_RECORDING == "1"` line, add:

```python

# Post-call transcript files (call_transcript.py). Imported after load_dotenv() so its config sees .env.
import call_transcript
```

3b. After `ensure_vertex_token()` (ends with `logger.info("🔐 Vertex AI access token refreshed")`), add:

```python


async def vertex_access_token():
    """Bearer token for call_transcript's REST request, refreshed off the event loop."""
    await ensure_vertex_token()
    return load_vertex_credentials().token
```

3c. In the `call_state` literal in `handle_media_stream`, after `"commit_gate_committed": False,`, add:

```python
        "transcript": None,
```

3d. Right after `call_log_tag.set(f"[{(call_state.get('call_uuid') or 'no-uuid')[:8]}] ")`, add:

```python
        if call_transcript.TRANSCRIPT_ENABLED:
            call_state["transcript"] = call_transcript.CallTranscript()
```

3e. In `stream_plivo_to_gemini`, the activityStart site:

```python
                            await session.send_realtime_input(
                                activity_start=types.ActivityStart()
                            )
                            logger.info("▶️ Sent activityStart to Gemini")
```
becomes
```python
                            await session.send_realtime_input(
                                activity_start=types.ActivityStart()
                            )
                            if call_state["transcript"]: call_state["transcript"].caller_start()
                            logger.info("▶️ Sent activityStart to Gemini")
```

3f. The streaming send:

```python
                        if not call_state["tool_call_in_progress"]:
                            await session.send_realtime_input(
                                audio=types.Blob(data=audio_chunk, mime_type="audio/pcm;rate=16000")
                            )
```
becomes
```python
                        if not call_state["tool_call_in_progress"]:
                            await session.send_realtime_input(
                                audio=types.Blob(data=audio_chunk, mime_type="audio/pcm;rate=16000")
                            )
                            if call_state["transcript"]: call_state["transcript"].caller_audio(audio_chunk)
```

3g. The remainder flush before activityEnd:

```python
                    if call_state["gemini_input_buffer"]:
                        await session.send_realtime_input(
                            audio=types.Blob(
                                data=bytes(call_state["gemini_input_buffer"]),
                                mime_type="audio/pcm;rate=16000"
                            )
                        )
                        call_state["gemini_input_buffer"].clear()

                    await session.send_realtime_input(
                        activity_end=types.ActivityEnd()
                    )
```
becomes
```python
                    if call_state["gemini_input_buffer"]:
                        remainder = bytes(call_state["gemini_input_buffer"])
                        await session.send_realtime_input(
                            audio=types.Blob(
                                data=remainder,
                                mime_type="audio/pcm;rate=16000"
                            )
                        )
                        if call_state["transcript"]: call_state["transcript"].caller_audio(remainder)
                        call_state["gemini_input_buffer"].clear()

                    await session.send_realtime_input(
                        activity_end=types.ActivityEnd()
                    )
                    if call_state["transcript"]: call_state["transcript"].caller_end()
```

3h. In `stream_gemini_to_plivo`, the deferred activityStart and its flush:

```python
                            await session.send_realtime_input(
                                activity_start=types.ActivityStart()
                            )
                            logger.info("▶️ Sent deferred activityStart to Gemini (tool call finished)")
```
becomes
```python
                            await session.send_realtime_input(
                                activity_start=types.ActivityStart()
                            )
                            if call_state["transcript"]: call_state["transcript"].caller_start()
                            logger.info("▶️ Sent deferred activityStart to Gemini (tool call finished)")
```
and
```python
                            if call_state["gemini_input_buffer"]:
                                await session.send_realtime_input(
                                    audio=types.Blob(
                                        mime_type="audio/pcm;rate=16000",
                                        data=bytes(call_state["gemini_input_buffer"]),
                                    )
                                )
                                call_state["gemini_input_buffer"].clear()
```
becomes
```python
                            if call_state["gemini_input_buffer"]:
                                deferred = bytes(call_state["gemini_input_buffer"])
                                await session.send_realtime_input(
                                    audio=types.Blob(
                                        mime_type="audio/pcm;rate=16000",
                                        data=deferred,
                                    )
                                )
                                if call_state["transcript"]: call_state["transcript"].caller_audio(deferred)
                                call_state["gemini_input_buffer"].clear()
```

3i. Output transcription: before the `record_text_delta(` call inside `if output_transcription and getattr(output_transcription, 'text', None):`, add:

```python
                        if call_state["transcript"]: call_state["transcript"].agent_started()
```

3j. Interrupted flush:

```python
                        if call_state["ai_text_buffer"].strip():
                            logger.info(f"🤖 [GEMINI] (Interrupted): {call_state['ai_text_buffer'].strip()}")
                            call_state["ai_text_buffer"] = ""
```
becomes
```python
                        if call_state["ai_text_buffer"].strip():
                            logger.info(f"🤖 [GEMINI] (Interrupted): {call_state['ai_text_buffer'].strip()}")
                            if call_state["transcript"]: call_state["transcript"].agent_text(call_state["ai_text_buffer"].strip(), interrupted=True)
                            call_state["ai_text_buffer"] = ""
```

3k. turn_complete flush: after `emit_call_event(call_state.get("call_uuid"), "ai_transcript", {"text": ai_transcript})` add:

```python
                            if call_state["transcript"]: call_state["transcript"].agent_text(ai_transcript)
```

3l. User flush: after `emit_call_event(call_state.get("call_uuid"), "user_transcript", {"text": user_transcript})` add:

```python
                            if call_state["transcript"]: call_state["transcript"].caller_gemini_text(user_transcript)
```

3m. In `handle_media_stream`'s final `finally`, after `log_call_stats(call_state)`:

```python
        if call_state["transcript"] and call_state["transcript"].has_turns():
            call_transcript.start_job(call_state["transcript"],
                                      call_state.get("call_uuid") or f"no-uuid-{int(time.time())}",
                                      VERTEX_PROJECT, vertex_access_token)
```

- [ ] **Step 4: Update the harness and the guards**

4a. `tests/harness/appctl.py`, in `new_call_state`, after `"commit_gate_committed": False,`:

```python
        "transcript": None,
```

4b. `tests/harness/appctl.py`, in `DIAGNOSTIC_LOG_TAGS`, after `"[LATCH]",`:

```python
    "[TRANSCRIPT]",   # post-call transcript job (call_transcript.py)
```

4c. `.gitignore`: add a line `TRANSCRIPTS/` (caller PII, like `CALL_RECORDINGS/`).

- [ ] **Step 5: Run the new tests**

Run: `venv312/Scripts/python.exe -m unittest tests.test_transcript_capture -v`
Expected: all PASS.

- [ ] **Step 6: Run the full suite and re-pin 4_8**

Run: `venv312/Scripts/python.exe -m unittest discover -s tests -t .`
Expected: one FAIL in `test_model_identifier_is_recorded` showing the new GEMINI_MODEL line list (it was `[84, 1691, 2208, 2218, 2631]`; app.py gained lines above the later sites). Replace the list in `tests/test_preservation_4_8_resumption_recording_stats.py` with the printed one and add to the comment above it: `And again for the post-call transcript wiring (import call_transcript, vertex_access_token, the transcript call_state key and its creation).` Rerun the suite: OK, 2 skipped, 1 expected failure. Also confirm `test_harness_smoke` and `test_preservation_4_7` (aec.py hash) pass.

---

### Task 6: Real-audio check and docs

**Files:**
- Create: `TEST_FILES/_transcript_check.py` (gitignored, throwaway)
- Modify: `CLAUDE.md` (Current state, Resume here, session log)
- Modify: `docs/spec/call-transcript-design.md` (status line only, if results differ from §9)

**Interfaces:**
- Consumes: `call_transcript.CallTranscript`, `write_transcript`; `TEST_FILES/_stt_spike.py`'s `cut_clips`.
- Produces: `TEST_FILES/_transcripts/<name>.txt` for the operator to read.

- [ ] **Step 1: Write the check script**

Create `TEST_FILES/_transcript_check.py`:

```python
"""THROWAWAY: run the real call_transcript job on a recorded call's caller audio, against real Vertex.

  PYTHONIOENCODING=utf-8 venv312/Scripts/python.exe TEST_FILES/_transcript_check.py TEST8

Caller turns are cut from <TEST>/*_aecout.wav (8 kHz, upsampled to 16 kHz like app.py's path);
Neha's lines are the [GEMINI] lines of the call log, placed at their log times. Those are turn END
times, so the Neha/User order here is approximate; the live app uses turn start times.
"""
import asyncio
import audioop
import glob
import os
import re
import sys
import time
import wave

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [REPO, os.path.join(REPO, "TEST_FILES")]
import call_transcript as ct  # noqa: E402
import _stt_spike as spike  # noqa: E402


class Clock:
    value = 0.0

    def __call__(self):
        return self.value


async def main(name):
    folder = os.path.join(REPO, "TEST_FILES", name)
    with wave.open(glob.glob(os.path.join(folder, "*_aecout.wav"))[0]) as w:
        rate, pcm = w.getframerate(), w.readframes(w.getnframes())
    anchors, _ = spike.load_log(folder)
    log = open(glob.glob(os.path.join(folder, "*.log"))[0], encoding="utf-8", errors="replace").read().splitlines()
    t_first = spike.frame_to_wall(anchors, 1)
    events = []
    for line in log:
        m = re.search(r"\[GEMINI\]( \(Interrupted\))?: (.*)$", line)
        if m and spike.wall(line):
            events.append((spike.wall(line) - t_first, "agent", m.group(2), bool(m.group(1))))
    clips, frames = spike.cut_clips(pcm, rate, pad_ms=400)
    for s, e in clips:
        clip, _ = audioop.ratecv(b"".join(frames[s:e + 1]), 2, 1, rate, 16000, None)
        events.append((spike.frame_to_wall(anchors, s + 1) - t_first, "user", clip, False))
    events.sort(key=lambda event: event[0])

    clock = Clock()
    transcript = ct.CallTranscript(clock=clock)
    for at, speaker, payload, interrupted in events:
        clock.value = at
        if speaker == "agent":
            transcript.agent_text(payload, interrupted=interrupted)
        else:
            transcript.caller_start()
            transcript.caller_audio(payload)
            transcript.caller_end()
    clock.value = events[-1][0] + 5

    out_dir = os.path.join(REPO, "TEST_FILES", "_transcripts")
    started = time.perf_counter()
    path = await ct.write_transcript(transcript, name, spike.PROJECT, _token, out_dir=out_dir)
    print(f"wrote {path} in {time.perf_counter() - started:.1f}s")
    print(open(path, encoding="utf-8").read())


async def _token():
    return await asyncio.to_thread(spike.token)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
```

- [ ] **Step 2: Run it on TEST8**

Run: `PYTHONIOENCODING=utf-8 venv312/Scripts/python.exe TEST_FILES/_transcript_check.py TEST8`
Expected: `wrote ...TEST_FILES/_transcripts/TEST8.txt`, `stt_status: ok`, `language: bn-IN`, about 22 `User:` lines in Bengali with `MBA` / `LL.M` / `CGPA` in Latin, Neha's lines interleaved by time, a cost line near `0.0067`. If it prints `failed (http 429)` after the full retry cap, wait a minute and rerun (preview quota). Show the file to the operator; they judge accuracy.

- [ ] **Step 3: Update CLAUDE.md**

In `CLAUDE.md`, update the suite count in "Current state" to the new total, add a "Resume here" item, and append a session-log entry:

```markdown
### 2026-10-12: post-call transcript files (uncommitted)
- Spec `docs/spec/call-transcript-design.md`, plan `docs/spec/call-transcript-plan.md`. After each call,
  `TRANSCRIPTS/<call_uuid>.txt`: Neha from `output_transcription`, caller from one post-call
  `gemini-3.5-transcribe-preview` request on Vertex `global` (REST via httpx, word timestamps, split back
  into turns), cost on the last line. Fallback to Gemini Live's caller text, marked `User (fallback)`.
- `call_transcript.py` (new); app.py one-line hooks at the caller audio sends, activityStart/End and
  the Neha/caller text flushes, `vertex_access_token`, `start_job` after `log_call_stats`.
  `call_state["transcript"]` mirrored. Kill switch `TRANSCRIPT_ENABLED=0`.
- Spike (2026-10-11, TEST8): per-turn requests hit 429 at ~12/min; one whole-call request 3.5-5 s;
  GA id 404s, `-preview` works; `bn-IN` only is the best language setting; English terms come back in
  Bengali script, mapped by `LATIN_TERMS`.
- Next: commit when asked, deploy to EC2 (git pull, restart idle), one live call, check the file and
  the `[TRANSCRIPT]` line; confirm Vertex prices against `STT_PRICE_*`; extend `LATIN_TERMS` from live files.
```

- [ ] **Step 4: Final suite run**

Run: `venv312/Scripts/python.exe -m unittest discover -s tests -t .`
Expected: OK, 2 skipped, 1 expected failure, total = previous total + new tests.
