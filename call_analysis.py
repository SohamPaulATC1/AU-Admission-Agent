"""Post-call analysis with Claude Haiku 5.5 on Amazon Bedrock.

Design: docs/spec/call-analysis-design.md §2-3. This module builds the request, parses the reply
and (Task 4) runs the Analyzer queue in the main process. The anthropic SDK is imported only in
_default_client(), so the media processes never load it.
"""

import asyncio
import json
import logging
import os
import re
import time

import call_store
import course_catalog

logger = logging.getLogger("call_analysis")


def _flag(name, default):
    value = os.getenv(name, default).strip()
    if value not in ("0", "1"):
        raise ValueError(f"{name} must be '0' or '1', got {value!r}")
    return value == "1"


ANALYSIS_ENABLED = _flag("ANALYSIS_ENABLED", "1")
ANALYSIS_MODEL = os.getenv("ANALYSIS_MODEL", "us.anthropic.claude-haiku-5-5").strip()
ANALYSIS_CONCURRENCY = int(os.getenv("ANALYSIS_CONCURRENCY", "2"))
# USD per million tokens: Anthropic's Haiku 5.5 price plus Bedrock's 10% US-route premium. Check the first AWS bill.
NLP_PRICE_INPUT = float(os.getenv("NLP_PRICE_INPUT", "0.11"))
NLP_PRICE_OUTPUT = float(os.getenv("NLP_PRICE_OUTPUT", "0.55"))
MAX_TOKENS = 4000
RETRY_DELAYS_S = (30, 120)   # our own attempts after the SDK's retries: 3 in all
SDK_MAX_RETRIES = 4
NO_RETRY_STATUS = frozenset({400, 401, 403, 404, 422})   # a retry cannot fix these

LEAD_STATUSES = ("hot", "warm", "cold", "not_interested", "callback_requested", "wrong_number",
                 "no_conversation")
PROSPECTIVE = ("yes", "no", "unclear")
RESPONDENTS = ("student", "parent_guardian", "other", "unknown")
OBJECTIONS = ("fee", "distance", "parent_decides", "other_college", "not_now", "other")
LANGUAGES = ("en", "bn", "hi")
AGENT_ISSUES = ("invented_fact", "repeated_question", "ignored_question", "wrong_language")
PROGRAM_NAMES = tuple(record["name"] for record in course_catalog.RECORDS)
BY_NAME = {record["name"]: record for record in course_catalog.RECORDS}

_TEXT_OR_NULL = {"type": ["string", "null"]}


def _choice(values):
    return {"type": "string", "enum": list(values)}


def _choices(values):
    return {"type": "array", "items": _choice(values)}


SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "lead_status": _choice(LEAD_STATUSES),
        "is_prospective": _choice(PROSPECTIVE),
        "prospective_reason": {"type": "string"},
        "prospective_evidence": _TEXT_OR_NULL,
        "interest_score": {"type": "integer", "enum": list(range(1, 11))},
        "respondent": _choice(RESPONDENTS),
        "applicant_name": _TEXT_OR_NULL,
        "qualification": _TEXT_OR_NULL,
        "board": _TEXT_OR_NULL,
        "marks_pct": {"type": ["number", "null"]},
        "subjects": {"type": "array", "items": {"type": "string"}},
        "entrance_exams": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "properties": {"exam": {"type": "string"}, "result": _TEXT_OR_NULL},
            "required": ["exam", "result"]}},
        "programs_of_interest": _choices(PROGRAM_NAMES),
        # No "type": Bedrock rejects an enum holding null under type ["string", "null"] (EC2 spike 2026-10-12).
        "primary_program": {"enum": [*PROGRAM_NAMES, None]},
        "scholarship_discussed": {"type": "boolean"},
        "scholarship_name": _TEXT_OR_NULL,
        "scholarship_amount_inr": {"type": ["integer", "null"]},
        "campus_visit_agreed": {"type": "boolean"},
        "campus_visit_day": _TEXT_OR_NULL,
        "callback_requested": {"type": "boolean"},
        "callback_time": _TEXT_OR_NULL,
        "objections": _choices(OBJECTIONS),
        "language": _choice(LANGUAGES),
        "summary": {"type": "string"},
        "next_action": {"type": "string"},
        "agent_issues": _choices(AGENT_ISSUES),
    },
    "required": list(call_store.ANALYSIS_FIELDS),
}

SYSTEM_PROMPT = """\
You analyse one phone call between Neha, the admission counselor voice agent of Adamas University \
(Kolkata), and a contact, for the university's admission team. Fill every field of the required JSON \
from what was actually said on the call.

Rules:
- Use only what was said in the transcript. If something was not stated, use null (an empty list for \
list fields, false for the yes/no fields). Never guess.
- Write every text field in English, whatever language the call was in. The one exception is \
prospective_evidence: quote the caller's own words as they were said.
- Lines marked "User (fallback)" come from a less accurate transcription: use them, with care.
- programs_of_interest and primary_program must be exact names from the catalog program list at the end \
of these instructions. \
primary_program is the one program the caller was most interested in; null if no specific program \
was named, or recommended and accepted.

lead_status (one value):
- hot: a campus visit day was fixed, or the caller clearly intends to apply.
- warm: interested and engaged, but no commitment.
- cold: little interest, short or non-committal answers.
- not_interested: clearly said no.
- callback_requested: asked to be called back later and nothing more was decided.
- wrong_number: not the applicant or the applicant's family, or unrelated to admissions.
- no_conversation: no real conversation (voicemail, silence, hung up at the greeting).

is_prospective: yes if the applicant could join an Adamas program for the coming session and the call \
gives no reason they will not; no if they clearly will not (not interested, admitted elsewhere, wrong \
number, not eligible by their own account); unclear otherwise. prospective_reason: one line why. \
prospective_evidence: a short quote that supports it, or null.

interest_score: 1 (none) to 10 (ready to join), from the caller's own words and engagement.
respondent: who Neha spoke with. applicant_name: only if the applicant's name differs from the contact \
name given.
qualification, board, marks_pct (a percentage, only if stated), subjects, entrance_exams (the exam and \
the score or rank as said; result null if not stated).
scholarship_discussed, scholarship_name, scholarship_amount_inr: whether Neha shared a scholarship, its \
name and the rupee amount she quoted.
campus_visit_agreed, campus_visit_day: whether a visit day was agreed, and the day as said. \
callback_requested, callback_time: whether the caller asked to be called back, and when.
objections: only objections the caller raised.
language: the main language the caller spoke (en English, bn Bengali, hi Hindi).
summary: 2-3 sentences for the admission team. next_action: one line, the team's next step.
agent_issues: only clear cases where Neha stated a fact that looks invented, asked a question again \
after it was answered, ignored a direct question, or spoke a language other than the caller's.
"""

PROGRAM_LIST = "\n".join(
    ["Catalog program names (programs_of_interest and primary_program must be exactly one of these):",
     *(f"- {name}" for name in PROGRAM_NAMES)])

# The fixed part of every request (instructions + program list) is one byte-identical system block with a
# cache marker (Bedrock has no automatic caching). Analyses starting less than 5 minutes apart read it at
# 0.1x the input price instead of paying for it again; a cold start writes it at 1.25x.
SYSTEM_BLOCK = {"type": "text", "text": SYSTEM_PROMPT + "\n" + PROGRAM_LIST, "cache_control": {"type": "ephemeral"}}
CACHE_WRITE_MULTIPLIER = 1.25   # 5-minute TTL
CACHE_READ_MULTIPLIER = 0.1


def user_message(context, transcript):
    """The per-call part: who was called, how long, and the transcript."""
    duration = context.get("duration_sec")
    lines = [
        f"Contact name: {context.get('name') or 'unknown'}",
        f"Call length: {duration if duration is not None else 'unknown'} seconds",
        f"Agent's end-of-call note: {context.get('end_reason') or 'none'}",
        "",
        "Transcript:",
        # Pre-step-2 files end each segment with a cost line; spec §2.5: the analysis ignores it.
        _OLD_COST_LINE_FULL.sub("", transcript).strip() or "(empty)",
    ]
    return "\n".join(lines)


def build_request(context, transcript, model=None):
    """Keyword arguments for messages.create. No sampling parameters: non-default values 400 on Haiku 5.5."""
    return {
        "model": model or ANALYSIS_MODEL,
        "max_tokens": MAX_TOKENS,
        "system": [dict(SYSTEM_BLOCK)],
        "output_config": {"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
        "messages": [{"role": "user", "content": user_message(context, transcript)}],
    }


def parse_reply(response):
    """(fields, problem, input_tokens, output_tokens). problem is None when fields is usable.
    input_tokens is usage.input_tokens, the uncached remainder; see cache_tokens for the rest."""
    usage = getattr(response, "usage", None)
    tin = getattr(usage, "input_tokens", 0) or 0
    tout = getattr(usage, "output_tokens", 0) or 0
    stop = getattr(response, "stop_reason", None)
    if stop == "refusal":
        category = getattr(getattr(response, "stop_details", None), "category", None)
        return None, f"refusal: {category or 'unknown'}", tin, tout
    if stop == "max_tokens":
        return None, "max_tokens", tin, tout
    text = next((block.text for block in getattr(response, "content", None) or []
                 if getattr(block, "type", None) == "text"), None)
    if text is None:
        return None, "no text in reply", tin, tout
    try:
        fields = json.loads(text)
    except ValueError:
        return None, "bad json", tin, tout
    if not isinstance(fields, dict):
        return None, "bad json", tin, tout
    missing = sorted(name for name in call_store.ANALYSIS_FIELDS if name not in fields)
    if missing:
        return None, f"missing fields: {', '.join(missing)}", tin, tout
    return fields, None, tin, tout


def school_for(primary_program, programs_looked_up):
    """School from the catalog record, never from Haiku: primary_program first, then Neha's lookups."""
    record = BY_NAME.get(primary_program)
    source = "haiku"
    if record is None:
        record = next((BY_NAME[name] for name in programs_looked_up or [] if name in BY_NAME), None)
        source = "lookup"
    if record is None:
        return None, None, "none"
    return record["school"]["code"], record["school"]["name"], source


def cache_tokens(response):
    """(cache_write, cache_read) prompt tokens of a reply; 0 when the response reports none."""
    usage = getattr(response, "usage", None)
    return (getattr(usage, "cache_creation_input_tokens", 0) or 0,
            getattr(usage, "cache_read_input_tokens", 0) or 0)


def nlp_cost(input_tokens, output_tokens, cache_write=0, cache_read=0):
    """USD for one request. input_tokens is the uncached remainder (usage.input_tokens)."""
    prompt = (input_tokens + cache_write * CACHE_WRITE_MULTIPLIER + cache_read * CACHE_READ_MULTIPLIER)
    return (prompt * NLP_PRICE_INPUT + output_tokens * NLP_PRICE_OUTPUT) / 1_000_000


_CALLER_LINE = re.compile(r"^\[\d+:\d\d\] User(?: \(fallback\))?: ", re.MULTILINE)
_OLD_COST_LINE = re.compile(r"^transcription_cost_usd: ([0-9.]+)", re.MULTILINE)
_OLD_COST_LINE_FULL = re.compile(r"^transcription_cost_usd: .*$\n?", re.MULTILINE)


def has_caller_lines(text):
    return bool(_CALLER_LINE.search(text or ""))


def old_stt_cost(text):
    """The STT cost written into transcript files before step 2 (one line per segment), or None."""
    amounts = [float(value) for value in _OLD_COST_LINE.findall(text or "")]
    return sum(amounts) if amounts else None


def _default_client():
    import anthropic   # only here: the media processes and the tests never load the SDK
    return anthropic.AsyncAnthropicBedrock(max_retries=SDK_MAX_RETRIES)


def _read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _transcript_files(folder):
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return []
    return [(name[:-4], os.path.join(folder, name)) for name in names if name.endswith(".txt")]


class Analyzer:
    """At most `concurrency` Haiku requests at once, in the main process only (spec §2.3)."""

    def __init__(self, db_path, *, client_factory=_default_client, concurrency=None, sleep=asyncio.sleep,
                 retry_delays=RETRY_DELAYS_S, model=None):
        self.db_path = db_path
        self.client_factory = client_factory
        self.concurrency = concurrency or ANALYSIS_CONCURRENCY
        self.sleep = sleep
        self.retry_delays = tuple(retry_delays)
        self.model = model or ANALYSIS_MODEL
        self._client = None
        self._queue = asyncio.Queue()
        self._queued = set()   # waiting in the queue; a call being analysed right now can be queued again
        self._running = {}     # call_uuid -> [Lock, users]: a re-queued call waits for its earlier run
        self._tasks = []

    def enqueue(self, call_uuid, path):
        if not call_uuid or call_uuid in self._queued:
            return False
        self._queued.add(call_uuid)
        self._queue.put_nowait((call_uuid, path))
        return True

    def is_busy(self, call_uuid):
        """True while the call waits in the queue or a worker is analysing it (the admin panel's re-run check)."""
        return call_uuid in self._queued or call_uuid in self._running

    def start(self, backfill_dir=None):
        if not (os.getenv("AWS_ACCESS_KEY_ID") or os.getenv("AWS_PROFILE")):
            # spec §3: one error at start; runs then end failed with error="aws credentials".
            logger.error("❌ [ANALYSIS] no AWS credentials in the environment (AWS_ACCESS_KEY_ID)")
        self._tasks = [asyncio.create_task(self._worker()) for _ in range(self.concurrency)]
        if backfill_dir:
            self._tasks.append(asyncio.create_task(self.backfill(backfill_dir)))

    async def join(self):
        await self._queue.join()

    async def stop(self):
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []

    async def _worker(self):
        while True:
            call_uuid, path = await self._queue.get()
            self._queued.discard(call_uuid)
            entry = self._running.setdefault(call_uuid, [asyncio.Lock(), 0])
            entry[1] += 1   # workers holding or waiting for this call's lock
            try:
                async with entry[0]:
                    await self.analyze(call_uuid, path)
            except Exception:
                logger.exception(f"❌ [ANALYSIS] crashed call={call_uuid[:8]}")
            finally:
                entry[1] -= 1
                if entry[1] == 0:
                    self._running.pop(call_uuid, None)
                self._queue.task_done()

    def _get_client(self):
        if self._client is None:
            self._client = self.client_factory()
        return self._client

    async def _db(self, fn, *args, **kwargs):
        return await call_store.run(self.db_path, fn, *args, **kwargs)

    async def analyze(self, call_uuid, path):
        started = time.perf_counter()
        tag = call_uuid[:8]
        context = await self._db(call_store.call_context, call_uuid)
        transferred = context["legs"] > 1 or bool(context["b_legs"])
        if not path:
            # A leg where nobody spoke (e.g. the return after a transfer) must not wipe an earlier
            # leg's analysis: re-read the call's file if one was written.
            path = await self._db(call_store.transcript_path, call_uuid)
        text = ""
        if path:
            try:
                text = await asyncio.to_thread(_read, path)
            except OSError:
                await self._db(call_store.save_failure, call_uuid, "transcript missing", attempts=0,
                               status="skipped")
                logger.warning(f"🧠 [ANALYSIS] call={tag} status=skipped error=transcript missing")
                return "skipped"
        if not has_caller_lines(text):
            await self._db(call_store.save_analysis, call_uuid, {"lead_status": "no_conversation"},
                           school_code=None, school_name=None, transferred=transferred, model=None,
                           input_tokens=0, output_tokens=0, cost_usd=0.0, attempts=0, raw_json=None)
            logger.info(f"🧠 [ANALYSIS] call={tag} status=ok lead=no_conversation tokens=0/0 cost=$0.0000")
            return "no_conversation"

        await self._db(call_store.mark_pending, call_uuid)
        request = build_request(context, text, model=self.model)
        attempts = 0
        while True:
            attempts += 1
            try:
                # First use: loading the SDK and building the client (~1 s) runs off the event loop, which
                # also serves live media streams on 8000.
                client = self._client or await asyncio.to_thread(self._get_client)
                response = await client.messages.create(**request)
                break
            except Exception as e:
                status = getattr(e, "status_code", None)
                error = type(e).__name__ + (f" {status}" if status is not None else "")
                no_keys = status is None and "credentials" in str(e).lower()
                if no_keys:
                    error = "aws credentials"   # spec §3: missing keys, a retry cannot fix it
                elif isinstance(e, ImportError):
                    no_keys, error = True, "anthropic not installed"
                if no_keys or status in NO_RETRY_STATUS or attempts > len(self.retry_delays):
                    await self._db(call_store.save_failure, call_uuid, error, attempts=attempts, model=self.model)
                    logger.warning(f"🧠 [ANALYSIS] call={tag} status=failed error={error} attempts={attempts} "
                                   f"latency={time.perf_counter() - started:.1f}s")
                    return "failed"
                logger.warning(f"🧠 [ANALYSIS] call={tag} attempt {attempts} failed ({error}), retrying")
                await self.sleep(self.retry_delays[attempts - 1])

        fields, problem, tin, tout = parse_reply(response)
        cache_write, cache_read = cache_tokens(response)
        cost = nlp_cost(tin, tout, cache_write, cache_read)
        prompt = tin + cache_write + cache_read   # stored and logged: the whole prompt, cached or not
        cache = f"cache_write={cache_write} cache_read={cache_read}"
        latency = time.perf_counter() - started
        if problem:
            await self._db(call_store.save_failure, call_uuid, problem, attempts=attempts, model=self.model,
                           input_tokens=prompt, output_tokens=tout, cost_usd=cost)
            logger.warning(f"🧠 [ANALYSIS] call={tag} status=failed error={problem} tokens={prompt}/{tout} "
                           f"cost=${cost:.4f} latency={latency:.1f}s attempts={attempts} {cache}")
            return "failed"
        code, name, source = school_for(fields.get("primary_program"), context["programs_looked_up"])
        await self._db(call_store.save_analysis, call_uuid, fields, school_code=code, school_name=name,
                       transferred=transferred, model=self.model, input_tokens=prompt, output_tokens=tout,
                       cost_usd=cost, attempts=attempts, raw_json=json.dumps(fields, ensure_ascii=False))
        logger.info(f"🧠 [ANALYSIS] call={tag} status=ok lead={fields.get('lead_status')} "
                    f"prospective={fields.get('is_prospective')} tokens={prompt}/{tout} cost=${cost:.4f} "
                    f"latency={latency:.1f}s attempts={attempts} school_from={source} {cache}")
        return "ok"

    async def backfill(self, transcript_dir):
        """Queue every transcript without a finished analysis; read a pre-step-2 cost line once."""
        try:
            return await self._backfill(transcript_dir)
        except Exception as e:
            logger.error(f"❌ [ANALYSIS] backfill failed: {type(e).__name__}")
            return 0

    async def _backfill(self, transcript_dir):
        files = await asyncio.to_thread(_transcript_files, transcript_dir)
        done = await self._db(call_store.finished)
        queued = 0
        for call_uuid, path in files:
            if call_uuid in done:
                continue
            try:
                old = old_stt_cost(await asyncio.to_thread(_read, path))
            except OSError:
                old = None
            if old is not None and await self._db(call_store.stt_cost_missing, call_uuid):
                await self._db(call_store.add_stt, call_uuid, old, path)
            if self.enqueue(call_uuid, path):
                queued += 1
        logger.info(f"🧠 [ANALYSIS] backfill queued={queued} of {len(files)}")
        return queued
