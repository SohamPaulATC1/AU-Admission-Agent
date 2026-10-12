"""Post-call transcript file: docs/spec/call-transcript-design.md.

During the call app.py feeds a CallTranscript (in-memory appends only). After teardown
start_job() sends the caller's turns to gemini-3.5-transcribe on Vertex in one request
and writes TRANSCRIPT_DIR/<call_uuid>.txt, turn by turn, with the STT cost last.
"""

import asyncio
import base64
import contextlib
import io
import logging
import os
import time
import wave
from datetime import datetime, timedelta, timezone

import httpx

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
