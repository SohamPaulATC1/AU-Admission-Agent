# Step 2: Call Analysis, Data Model and Full Call Cost Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** After every call, `call_history.db` holds the Haiku 5.5 lead analysis (`call_analysis` table) and all four cost parts (Gemini, STT, Haiku, Plivo) on the `calls` row, without the call path waiting for any of it.

**Architecture:** `call_store.py` owns the new columns, the `call_analysis` table and every new write (plain sqlite3 functions plus one async `run()` wrapper). `call_transcript.py` stops writing the cost line and reports its result through an `on_done` callback. `call_analysis.py` holds the Haiku request (prompt, JSON schema, parsing, school lookup) and the `Analyzer` worker queue that runs in the main process only. `plivo_cost.py` fetches Plivo call records after hangup. `app.py` wires events, the catalog lookups, the hangup and dial-action webhooks and a `before_serving` start hook.

**Tech Stack:** Python 3.12 (venv312 locally; EC2 venv), sqlite3, asyncio, Quart 0.20, `anthropic[bedrock]` (`AsyncAnthropicBedrock`, EC2 only; never imported in tests), `plivo` SDK, stdlib `unittest`.

**Spec:** `docs/spec/call-analysis-design.md` (shared decisions: `docs/spec/admin-system-overview.md`)

## Global Constraints

- New dependency allowed: `anthropic[bedrock]` only (operator decision). Add `anthropic[bedrock]>=1.0` to `requirements.txt`; nothing else in that file changes. It is NOT installed in venv312: no test may import `anthropic`; the real client is created only inside `call_analysis._default_client()`.
- Model `us.anthropic.claude-haiku-5-5`; request: `max_tokens=4000`, `output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}}`; no `temperature` / `top_p` / `top_k`, no assistant prefill.
- Defaults: `ANALYSIS_ENABLED=1`, `ANALYSIS_MODEL=us.anthropic.claude-haiku-5-5`, `ANALYSIS_CONCURRENCY=2`, `NLP_PRICE_INPUT=0.11`, `NLP_PRICE_OUTPUT=0.55` (USD per million tokens), `PLIVO_COST_ENABLED=1`. SDK `max_retries=4`; our own retry delays `(30, 120)` seconds (3 attempts); Plivo fetch delays `(60, 180, 600)` seconds after hangup, `(0, 180, 600)` for the startup catch-up.
- `interest_score` 1-10. Lead statuses exactly `hot`, `warm`, `cold`, `not_interested`, `callback_requested`, `wrong_number`, `no_conversation`.
- Log tags `🧠 [ANALYSIS]` and `💰 [PLIVO-COST]`, both added to `DIAGNOSTIC_LOG_TAGS`. Never log transcript text, names or phone numbers.
- The operator must never receive a cost: `transcript_ready` is never sent to browsers, and `web_auth.without_cost` drops `cost` and every key ending in `cost_usd`.
- The database is written by the main process (8000) only; the Analyzer, the startup catch-up and the Plivo fetches run only when `EVENT_FORWARD_URL` is empty. The schema upgrade itself runs in every process at import and must tolerate a concurrent "duplicate column" error.
- Every new `call_state` key is mirrored in `tests/harness/appctl.py` (`test_harness_smoke`). When app.py's line count changes, re-pin the `GEMINI_MODEL` list in `tests/test_preservation_4_8_resumption_recording_stats.py` (5 entries, same sites; replace the list, add one comment line).
- `@app.route("/trigger-call", methods=["POST"])` stays byte-identical. `aec.py`, `aec1.py`, `bargein.py`, prompt files untouched.
- Tests: stdlib `unittest` only; run the suite with `venv312/Scripts/python.exe -m unittest discover -s tests -t .`. Write test code with Write/Edit, never bash heredocs. `PYTHONIOENCODING=utf-8` when output holds Bengali or log lines. Tests never touch the network, `.env`, AWS or the Plivo account, and use temp databases.
- No commits unless the operator asks (CLAUDE.md). Each task ends with a full-suite run.
- Suite baseline before this plan: 705 run, 3 failures (all in `tests/test_prompt_catalog.py`, pre-existing; leave them), 2 skipped, 1 expected failure. "Green" means no failure beyond those 3.

## Review Focus

1. The operator never sees a cost even though `call_ended` now carries `cost_usd` and `transcript_ready` carries `stt_cost_usd`. Tests: Task 5 `test_without_cost_drops_every_cost_key`, `test_transcript_ready_is_queued_and_never_reaches_browsers`.
2. A transferred call (two legs, same `call_uuid`): Gemini and STT costs add up, the second transcript re-runs the analysis and replaces the row, and its Haiku cost adds to `nlp_cost_usd`. Tests: Task 1 `test_two_legs_add_up`, `test_second_save_replaces_fields_and_adds_cost`; Task 4 `test_concurrency_limit_and_dedupe` (re-queue while running).
3. Four processes start together and all run the schema upgrade: a "duplicate column" from a racing process must not crash the import. Test: Task 1 `test_column_added_by_a_racing_process_is_fine`.
4. `TRANSCRIPT_ENABLED=0` (no transcript object) must not create fake `no_conversation` rows. Test: Task 5 `test_no_transcript_object_does_nothing`.
5. Bedrock unreachable or AWS keys missing: calls are unaffected, the row ends `failed` after the retries, nothing escapes the worker. Tests: Task 4 `test_client_factory_failure_ends_failed`, `test_worker_survives_a_crash`.

## Files

| File | Change |
|---|---|
| `call_store.py` | Create (Task 1). Schema upgrade, cost writes, analysis rows, `run()`. |
| `call_transcript.py` | Modify (Task 2). No cost line; `on_done(result)`. |
| `call_analysis.py` | Create (Tasks 3-4). Request, schema, parsing, school, `Analyzer`. |
| `plivo_cost.py` | Create (Task 4). Plivo call-record cost fetch. |
| `app.py` | Modify (Task 5). Imports, `init_call_history_db` upgrade, persistence, event routing, `log_call_stats`, `catalog_found`, `finish_transcript`, webhooks, `start_background_work`. |
| `web_auth.py` | Modify (Task 5). `without_cost` drops every cost key. |
| `requirements.txt` | Modify (Task 5). `anthropic[bedrock]>=1.0`. |
| `tests/harness/appctl.py` | Modify (Task 5). `catalog_found` mirror; two log tags. |
| `tests/test_call_store.py`, `tests/test_call_analysis.py`, `tests/test_analysis_worker.py`, `tests/test_plivo_cost.py`, `tests/test_analysis_wiring.py` | Create. |
| `tests/test_call_transcript.py`, `tests/test_transcript_capture.py`, `tests/test_web_auth.py`, `tests/test_preservation_4_8_resumption_recording_stats.py` | Modify. |
| `tests/analysis_spike.py` | Create (Task 6). Not a test (not collected); the real Bedrock check run on EC2. |
| `CLAUDE.md`, `docs/spec/call-analysis-design.md`, `docs/spec/admin-system-overview.md` | Modify (Task 6). |

---

### Task 1: `call_store.py`

**Files:**
- Create: `call_store.py`
- Test: `tests/test_call_store.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - Constants `IST`, `CALL_COLUMNS: tuple[(name, sql_type)]`, `ANALYSIS_COLUMNS: tuple[(name, sql_type)]`, `ANALYSIS_FIELDS: tuple[str]` (the 26 Haiku fields, in order), `LIST_FIELDS`, `BOOL_FIELDS` (frozensets), `FINISHED = ("ok", "failed", "skipped")`.
  - `now_ist() -> str`; `upgrade(conn)`; `_add_column(conn, name, kind)`.
  - `add_leg(conn, call_uuid, cost_usd, programs) -> bool`; `add_stt(conn, call_uuid, stt_cost_usd, path)`; `stt_cost_missing(conn, call_uuid) -> bool`.
  - `add_b_leg(conn, call_uuid, b_leg_uuid) -> bool`; `b_legs(conn, call_uuid) -> list[str]`; `set_plivo_cost(conn, call_uuid, cost_usd, status)`; `calls_needing_plivo_cost(conn, since: str) -> list[str]`.
  - `call_context(conn, call_uuid) -> dict` with keys `name`, `end_reason`, `duration_sec`, `legs` (int), `programs_looked_up` (list), `b_legs` (list).
  - `mark_pending(conn, call_uuid)`; `save_analysis(conn, call_uuid, fields, *, school_code, school_name, transferred, model, input_tokens, output_tokens, cost_usd, attempts, raw_json)`; `save_failure(conn, call_uuid, error, *, attempts, status="failed", model=None, input_tokens=None, output_tokens=None, cost_usd=0.0)`; `finished(conn) -> set[str]`.
  - `async run(db_path, fn, *args, **kwargs)`: runs `fn(conn, *args, **kwargs)` on a fresh committed connection in the default thread pool and returns its result.
  - Test fixtures other tasks import from `tests/test_call_store.py`: `CALLS_TABLE` (str), `SAMPLE_FIELDS` (dict), `PROGRAM` (str, a real catalog name).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_call_store.py`:

```python
"""call_store.py: step 2 columns, costs and analysis rows (docs/spec/call-analysis-design.md §1)."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
import tempfile
import unittest

import call_store
import course_catalog

# The calls table exactly as app.py's init_call_history_db creates it.
CALLS_TABLE = """
CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    call_uuid TEXT UNIQUE,
    name TEXT,
    phone TEXT,
    status TEXT,
    fail_reason TEXT,
    cost TEXT,
    end_reason TEXT,
    created_at TEXT,
    updated_at TEXT,
    duration_sec INTEGER,
    connected_at TEXT
)"""

PROGRAM = course_catalog.RECORDS[0]["name"]

SAMPLE_FIELDS = {
    "lead_status": "hot",
    "is_prospective": "yes",
    "prospective_reason": "Fixed a campus visit for Saturday.",
    "prospective_evidence": "শনিবার আসব",
    "interest_score": 8,
    "respondent": "parent_guardian",
    "applicant_name": "Riya Das",
    "qualification": "12th, appearing 2027",
    "board": "WBCHSE",
    "marks_pct": 82.0,
    "subjects": ["Physics", "Chemistry", "Mathematics"],
    "entrance_exams": [{"exam": "WBJEE", "result": None}],
    "programs_of_interest": [PROGRAM],
    "primary_program": PROGRAM,
    "scholarship_discussed": True,
    "scholarship_name": "Early Bird",
    "scholarship_amount_inr": 15000,
    "campus_visit_agreed": True,
    "campus_visit_day": "Saturday",
    "callback_requested": False,
    "callback_time": None,
    "objections": ["fee"],
    "language": "bn",
    "summary": "Parent of a 12th student; visit fixed for Saturday.",
    "next_action": "Confirm the Saturday visit timing.",
    "agent_issues": [],
}


def new_db():
    conn = sqlite3.connect(":memory:")
    conn.execute(CALLS_TABLE)
    conn.execute("INSERT INTO calls (call_uuid, name, phone, status, created_at, updated_at, duration_sec, end_reason) "
                 "VALUES ('u-1', 'Asha Das', '9876543210', 'ended', '2026-10-12 10:00:00', '2026-10-12 10:05:00', "
                 "95, 'call completed')")
    call_store.upgrade(conn)
    return conn


def row(conn, table, call_uuid="u-1"):
    conn.row_factory = sqlite3.Row
    try:
        found = conn.execute(f"SELECT * FROM {table} WHERE call_uuid=?", (call_uuid,)).fetchone()
        return dict(found) if found else None
    finally:
        conn.row_factory = None


def save_ok(conn, fields=None, cost=0.0006, **overrides):
    kwargs = dict(school_code="SoBAS", school_name="School of Basic and Applied Sciences", transferred=False,
                  model="us.anthropic.claude-haiku-5-5", input_tokens=3000, output_tokens=500,
                  cost_usd=cost, attempts=1, raw_json="{}")
    kwargs.update(overrides)
    call_store.save_analysis(conn, "u-1", fields or SAMPLE_FIELDS, **kwargs)


class TestUpgrade(unittest.TestCase):
    def test_adds_columns_and_keeps_rows(self):
        conn = new_db()
        names = {r[1] for r in conn.execute("PRAGMA table_info(calls)")}
        self.assertLessEqual({name for name, _ in call_store.CALL_COLUMNS}, names)
        self.assertEqual(row(conn, "calls")["name"], "Asha Das")

    def test_idempotent(self):
        conn = new_db()
        call_store.upgrade(conn)
        call_store.upgrade(conn)
        self.assertEqual(row(conn, "calls")["phone"], "9876543210")

    def test_column_added_by_a_racing_process_is_fine(self):
        conn = new_db()
        call_store._add_column(conn, "legs", "INTEGER")   # already there: duplicate column, ignored

    def test_other_alter_errors_still_raise(self):
        conn = sqlite3.connect(":memory:")   # no calls table
        with self.assertRaises(sqlite3.OperationalError):
            call_store._add_column(conn, "legs", "INTEGER")

    def test_analysis_table(self):
        conn = new_db()
        names = [r[1] for r in conn.execute("PRAGMA table_info(call_analysis)")]
        self.assertEqual(names, [name for name, _ in call_store.ANALYSIS_COLUMNS])
        self.assertLessEqual(set(call_store.ANALYSIS_FIELDS), set(names))
        self.assertEqual(len(call_store.ANALYSIS_FIELDS), 26)
        self.assertEqual(set(SAMPLE_FIELDS), set(call_store.ANALYSIS_FIELDS))


class TestLegs(unittest.TestCase):
    def test_two_legs_add_up(self):
        conn = new_db()
        self.assertTrue(call_store.add_leg(conn, "u-1", 0.0123, [PROGRAM]))
        self.assertTrue(call_store.add_leg(conn, "u-1", 0.02, [PROGRAM, "MBA"]))
        r = row(conn, "calls")
        self.assertAlmostEqual(r["gemini_cost_usd"], 0.0323)
        self.assertEqual(r["legs"], 2)
        self.assertEqual(r["cost"], "$0.0323")
        self.assertEqual(json.loads(r["programs_looked_up"]), [PROGRAM, "MBA"])

    def test_missing_cost_counts_as_zero(self):
        conn = new_db()
        call_store.add_leg(conn, "u-1", None, None)
        r = row(conn, "calls")
        self.assertEqual((r["gemini_cost_usd"], r["legs"], r["cost"]), (0.0, 1, "$0.0000"))

    def test_unknown_call(self):
        self.assertFalse(call_store.add_leg(new_db(), "nope", 0.01, []))


class TestStt(unittest.TestCase):
    def test_adds_up_and_keeps_the_path(self):
        conn = new_db()
        self.assertTrue(call_store.stt_cost_missing(conn, "u-1"))
        call_store.add_stt(conn, "u-1", 0.0011, "TRANSCRIPTS/u-1.txt")
        call_store.add_stt(conn, "u-1", 0.002, None)
        r = row(conn, "calls")
        self.assertAlmostEqual(r["stt_cost_usd"], 0.0031)
        self.assertEqual(r["transcript_path"], "TRANSCRIPTS/u-1.txt")
        self.assertFalse(call_store.stt_cost_missing(conn, "u-1"))
        self.assertFalse(call_store.stt_cost_missing(conn, "nope"))


class TestPlivo(unittest.TestCase):
    def test_b_legs_are_deduplicated(self):
        conn = new_db()
        self.assertTrue(call_store.add_b_leg(conn, "u-1", "b-1"))
        call_store.add_b_leg(conn, "u-1", "b-1")
        call_store.add_b_leg(conn, "u-1", "b-2")
        self.assertEqual(call_store.b_legs(conn, "u-1"), ["b-1", "b-2"])
        self.assertEqual(call_store.b_legs(conn, "nope"), [])
        self.assertFalse(call_store.add_b_leg(conn, "nope", "b-1"))

    def test_set_cost(self):
        conn = new_db()
        call_store.set_plivo_cost(conn, "u-1", 0.0334, "ok")
        r = row(conn, "calls")
        self.assertEqual((r["plivo_cost_usd"], r["plivo_cost_status"]), (0.0334, "ok"))

    def test_calls_needing_a_cost(self):
        conn = new_db()
        conn.execute("UPDATE calls SET connected_at='2026-10-12 10:01:00' WHERE call_uuid='u-1'")
        rows = [("u-2", "ended", "2026-10-12 10:01:00", "2026-10-12 10:05:00", "ok"),       # has a cost
                ("u-3", "failed", None, "2026-10-12 10:05:00", None),                         # never answered
                ("u-4", "ended", "2026-10-10 10:01:00", "2026-10-10 10:05:00", None),        # too old
                ("u-5", "ended", "2026-10-12 10:01:00", "2026-10-12 10:05:00", "pending")]   # unfinished
        for uuid, status, connected, updated, cost_status in rows:
            conn.execute("INSERT INTO calls (call_uuid, status, connected_at, updated_at, plivo_cost_status) "
                         "VALUES (?, ?, ?, ?, ?)", (uuid, status, connected, updated, cost_status))
        self.assertEqual(sorted(call_store.calls_needing_plivo_cost(conn, "2026-10-11 10:00:00")), ["u-1", "u-5"])


class TestAnalysis(unittest.TestCase):
    def test_save_fills_every_column(self):
        conn = new_db()
        save_ok(conn)
        r = row(conn, "call_analysis")
        self.assertEqual(r["status"], "ok")
        self.assertIsNone(r["error"])
        self.assertEqual(r["lead_status"], "hot")
        self.assertEqual(r["interest_score"], 8)
        self.assertEqual(json.loads(r["subjects"]), ["Physics", "Chemistry", "Mathematics"])
        self.assertEqual(json.loads(r["entrance_exams"]), [{"exam": "WBJEE", "result": None}])
        self.assertEqual(r["prospective_evidence"], "শনিবার আসব")
        self.assertEqual((r["scholarship_discussed"], r["campus_visit_agreed"], r["callback_requested"]), (1, 1, 0))
        self.assertEqual((r["school_code"], r["transferred"], r["input_tokens"]), ("SoBAS", 0, 3000))
        self.assertIsNotNone(r["analyzed_at"])
        self.assertAlmostEqual(row(conn, "calls")["nlp_cost_usd"], 0.0006)

    def test_second_save_replaces_fields_and_adds_cost(self):
        conn = new_db()
        save_ok(conn)
        save_ok(conn, fields=dict(SAMPLE_FIELDS, lead_status="warm", subjects=[]), cost=0.0004, attempts=2)
        r = row(conn, "call_analysis")
        self.assertEqual((r["lead_status"], r["subjects"], r["attempts"]), ("warm", "[]", 2))
        self.assertAlmostEqual(row(conn, "calls")["nlp_cost_usd"], 0.001)

    def test_partial_fields_store_empty_values(self):
        conn = new_db()
        save_ok(conn, fields={"lead_status": "no_conversation"}, model=None, input_tokens=0, output_tokens=0,
                cost=0.0, attempts=0, raw_json=None)
        r = row(conn, "call_analysis")
        self.assertEqual((r["lead_status"], r["objections"], r["campus_visit_agreed"]), ("no_conversation", "[]", None))
        self.assertIsNone(row(conn, "calls")["nlp_cost_usd"])

    def test_failure_keeps_earlier_fields_and_adds_cost(self):
        conn = new_db()
        save_ok(conn)
        call_store.save_failure(conn, "u-1", "refusal: cyber", attempts=1, input_tokens=100, output_tokens=5,
                                cost_usd=0.0001)
        r = row(conn, "call_analysis")
        self.assertEqual((r["status"], r["error"], r["lead_status"]), ("failed", "refusal: cyber", "hot"))
        self.assertAlmostEqual(row(conn, "calls")["nlp_cost_usd"], 0.0007)

    def test_pending_and_finished(self):
        conn = new_db()
        call_store.mark_pending(conn, "u-1")
        self.assertEqual(row(conn, "call_analysis")["status"], "pending")
        self.assertEqual(call_store.finished(conn), set())
        save_ok(conn)
        call_store.mark_pending(conn, "u-1")   # a re-run starts: fields stay until it finishes
        self.assertEqual(row(conn, "call_analysis")["lead_status"], "hot")
        call_store.save_failure(conn, "u-2", "transcript missing", attempts=0, status="skipped")
        self.assertEqual(call_store.finished(conn), {"u-2"})

    def test_call_context(self):
        conn = new_db()
        call_store.add_leg(conn, "u-1", 0.01, [PROGRAM])
        call_store.add_b_leg(conn, "u-1", "b-1")
        self.assertEqual(call_store.call_context(conn, "u-1"),
                         {"name": "Asha Das", "end_reason": "call completed", "duration_sec": 95, "legs": 1,
                          "programs_looked_up": [PROGRAM], "b_legs": ["b-1"]})
        self.assertEqual(call_store.call_context(conn, "nope"),
                         {"name": None, "end_reason": None, "duration_sec": None, "legs": 0,
                          "programs_looked_up": [], "b_legs": []})


class TestRun(unittest.TestCase):
    def test_runs_in_a_committed_connection(self):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        path = os.path.join(folder, "calls.db")
        conn = sqlite3.connect(path)
        with conn:
            conn.execute(CALLS_TABLE)
            conn.execute("INSERT INTO calls (call_uuid) VALUES ('u-1')")
            call_store.upgrade(conn)
        conn.close()
        self.assertTrue(asyncio.run(call_store.run(path, call_store.add_leg, "u-1", 0.5, [])))
        check = sqlite3.connect(path)
        try:
            self.assertEqual(check.execute("SELECT legs FROM calls").fetchone()[0], 1)
        finally:
            check.close()


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `venv312/Scripts/python.exe -m unittest tests.test_call_store -v`
Expected: ERROR, `ModuleNotFoundError: No module named 'call_store'`.

- [ ] **Step 3: Write `call_store.py`**

```python
"""call_history.db additions for step 2: the cost of each call and its Haiku analysis row.

Design: docs/spec/call-analysis-design.md §1. The sync functions take an open sqlite3
connection and leave the commit to the caller; run() gives one a fresh connection in the
default thread pool. Only the main process (8000) writes.
"""

import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))

# New columns on calls, added on every start when missing.
CALL_COLUMNS = (
    ("gemini_cost_usd", "REAL"),
    ("legs", "INTEGER"),
    ("stt_cost_usd", "REAL"),
    ("nlp_cost_usd", "REAL"),
    ("plivo_cost_usd", "REAL"),
    ("plivo_b_legs", "TEXT"),
    ("plivo_cost_status", "TEXT"),
    ("transcript_path", "TEXT"),
    ("programs_looked_up", "TEXT"),
)

ANALYSIS_COLUMNS = (
    ("call_uuid", "TEXT PRIMARY KEY"),
    ("status", "TEXT"),
    ("error", "TEXT"),
    ("attempts", "INTEGER"),
    ("lead_status", "TEXT"),
    ("is_prospective", "TEXT"),
    ("prospective_reason", "TEXT"),
    ("prospective_evidence", "TEXT"),
    ("interest_score", "INTEGER"),
    ("respondent", "TEXT"),
    ("applicant_name", "TEXT"),
    ("qualification", "TEXT"),
    ("board", "TEXT"),
    ("marks_pct", "REAL"),
    ("subjects", "TEXT"),
    ("entrance_exams", "TEXT"),
    ("programs_of_interest", "TEXT"),
    ("primary_program", "TEXT"),
    ("school_code", "TEXT"),
    ("school_name", "TEXT"),
    ("scholarship_discussed", "INTEGER"),
    ("scholarship_name", "TEXT"),
    ("scholarship_amount_inr", "INTEGER"),
    ("campus_visit_agreed", "INTEGER"),
    ("campus_visit_day", "TEXT"),
    ("callback_requested", "INTEGER"),
    ("callback_time", "TEXT"),
    ("objections", "TEXT"),
    ("language", "TEXT"),
    ("summary", "TEXT"),
    ("next_action", "TEXT"),
    ("agent_issues", "TEXT"),
    ("transferred", "INTEGER"),
    ("model", "TEXT"),
    ("input_tokens", "INTEGER"),
    ("output_tokens", "INTEGER"),
    ("analyzed_at", "TEXT"),
    ("raw_json", "TEXT"),
)

# The fields Haiku fills, in table order. Lists are stored as JSON text, yes/no as 0/1.
ANALYSIS_FIELDS = (
    "lead_status", "is_prospective", "prospective_reason", "prospective_evidence", "interest_score",
    "respondent", "applicant_name", "qualification", "board", "marks_pct", "subjects", "entrance_exams",
    "programs_of_interest", "primary_program", "scholarship_discussed", "scholarship_name",
    "scholarship_amount_inr", "campus_visit_agreed", "campus_visit_day", "callback_requested",
    "callback_time", "objections", "language", "summary", "next_action", "agent_issues",
)
LIST_FIELDS = frozenset({"subjects", "entrance_exams", "programs_of_interest", "objections", "agent_issues"})
BOOL_FIELDS = frozenset({"scholarship_discussed", "campus_visit_agreed", "callback_requested"})
FINISHED = ("ok", "failed", "skipped")


def now_ist():
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")


def _add_column(conn, name, kind):
    try:
        conn.execute(f"ALTER TABLE calls ADD COLUMN {name} {kind}")
    except sqlite3.OperationalError as e:
        # All four processes run this at import; one that loses the race sees "duplicate column".
        if "duplicate column" not in str(e).lower():
            raise


def upgrade(conn):
    """Add the step 2 columns and the call_analysis table. Safe on every start, in every process."""
    have = {r[1] for r in conn.execute("PRAGMA table_info(calls)")}
    for name, kind in CALL_COLUMNS:
        if name not in have:
            _add_column(conn, name, kind)
    columns = ", ".join(f"{name} {kind}" for name, kind in ANALYSIS_COLUMNS)
    conn.execute(f"CREATE TABLE IF NOT EXISTS call_analysis ({columns})")


def _json_list(text):
    try:
        value = json.loads(text) if text else []
    except ValueError:
        return []
    return value if isinstance(value, list) else []


def _dumps(value):
    return json.dumps(value, ensure_ascii=False)


def add_leg(conn, call_uuid, cost_usd, programs):
    """One call_ended: add the leg's Gemini cost, count the leg, keep the cost text in step."""
    found = conn.execute("SELECT gemini_cost_usd, legs, programs_looked_up FROM calls WHERE call_uuid=?",
                         (call_uuid,)).fetchone()
    if found is None:
        return False
    total = (found[0] or 0.0) + float(cost_usd or 0.0)
    names = _json_list(found[2])
    for name in programs or []:
        if name not in names:
            names.append(name)
    conn.execute("UPDATE calls SET gemini_cost_usd=?, legs=?, cost=?, programs_looked_up=? WHERE call_uuid=?",
                 (total, (found[1] or 0) + 1, f"${total:.4f}", _dumps(names), call_uuid))
    return True


def add_stt(conn, call_uuid, stt_cost_usd, path):
    conn.execute("UPDATE calls SET stt_cost_usd=COALESCE(stt_cost_usd, 0) + ?, "
                 "transcript_path=COALESCE(?, transcript_path) WHERE call_uuid=?",
                 (float(stt_cost_usd or 0.0), path, call_uuid))


def stt_cost_missing(conn, call_uuid):
    found = conn.execute("SELECT stt_cost_usd FROM calls WHERE call_uuid=?", (call_uuid,)).fetchone()
    return found is not None and found[0] is None


def b_legs(conn, call_uuid):
    found = conn.execute("SELECT plivo_b_legs FROM calls WHERE call_uuid=?", (call_uuid,)).fetchone()
    return _json_list(found[0]) if found else []


def add_b_leg(conn, call_uuid, b_leg_uuid):
    found = conn.execute("SELECT plivo_b_legs FROM calls WHERE call_uuid=?", (call_uuid,)).fetchone()
    if found is None:
        return False
    legs = _json_list(found[0])
    if b_leg_uuid not in legs:
        legs.append(b_leg_uuid)
        conn.execute("UPDATE calls SET plivo_b_legs=? WHERE call_uuid=?", (_dumps(legs), call_uuid))
    return True


def set_plivo_cost(conn, call_uuid, cost_usd, status):
    conn.execute("UPDATE calls SET plivo_cost_usd=?, plivo_cost_status=? WHERE call_uuid=?",
                 (cost_usd, status, call_uuid))


def calls_needing_plivo_cost(conn, since):
    """Answered calls that ended since `since` (IST text) and have no Plivo cost yet."""
    rows = conn.execute(
        "SELECT call_uuid FROM calls WHERE status IN ('ended', 'failed') AND connected_at IS NOT NULL "
        "AND updated_at >= ? AND (plivo_cost_status IS NULL OR plivo_cost_status = 'pending')", (since,))
    return [r[0] for r in rows]


def call_context(conn, call_uuid):
    found = conn.execute("SELECT name, end_reason, duration_sec, legs, programs_looked_up, plivo_b_legs "
                         "FROM calls WHERE call_uuid=?", (call_uuid,)).fetchone()
    if found is None:
        return {"name": None, "end_reason": None, "duration_sec": None, "legs": 0,
                "programs_looked_up": [], "b_legs": []}
    return {"name": found[0], "end_reason": found[1], "duration_sec": found[2], "legs": found[3] or 0,
            "programs_looked_up": _json_list(found[4]), "b_legs": _json_list(found[5])}


def _upsert(conn, call_uuid, values):
    columns = ["call_uuid", *values]
    conn.execute(
        f"INSERT INTO call_analysis ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))}) "
        f"ON CONFLICT(call_uuid) DO UPDATE SET {', '.join(f'{c}=excluded.{c}' for c in values)}",
        [call_uuid, *values.values()])


def _add_nlp_cost(conn, call_uuid, cost_usd):
    if cost_usd:
        conn.execute("UPDATE calls SET nlp_cost_usd=COALESCE(nlp_cost_usd, 0) + ? WHERE call_uuid=?",
                     (float(cost_usd), call_uuid))


def mark_pending(conn, call_uuid):
    _upsert(conn, call_uuid, {"status": "pending"})


def save_analysis(conn, call_uuid, fields, *, school_code, school_name, transferred, model,
                  input_tokens, output_tokens, cost_usd, attempts, raw_json):
    """A finished analysis: every Haiku field is replaced, the run's cost is added to the call."""
    values = {}
    for name in ANALYSIS_FIELDS:
        value = fields.get(name)
        if name in LIST_FIELDS:
            value = _dumps(value or [])
        elif name in BOOL_FIELDS and value is not None:
            value = int(bool(value))
        values[name] = value
    values.update(status="ok", error=None, attempts=attempts, school_code=school_code,
                  school_name=school_name, transferred=int(bool(transferred)), model=model,
                  input_tokens=input_tokens, output_tokens=output_tokens, analyzed_at=now_ist(),
                  raw_json=raw_json)
    _upsert(conn, call_uuid, values)
    _add_nlp_cost(conn, call_uuid, cost_usd)


def save_failure(conn, call_uuid, error, *, attempts, status="failed", model=None, input_tokens=None,
                 output_tokens=None, cost_usd=0.0):
    """A failed or skipped run: earlier fields stay; status, error and tokens are updated."""
    _upsert(conn, call_uuid, {"status": status, "error": error, "attempts": attempts, "model": model,
                              "input_tokens": input_tokens, "output_tokens": output_tokens,
                              "analyzed_at": now_ist()})
    _add_nlp_cost(conn, call_uuid, cost_usd)


def finished(conn):
    marks = ", ".join("?" * len(FINISHED))
    return {r[0] for r in conn.execute(f"SELECT call_uuid FROM call_analysis WHERE status IN ({marks})",
                                       FINISHED)}


async def run(db_path, fn, *args, **kwargs):
    """fn(conn, *args, **kwargs) on a fresh connection in the default thread pool, committed."""
    def work():
        conn = sqlite3.connect(db_path, timeout=10)
        try:
            with conn:
                return fn(conn, *args, **kwargs)
        finally:
            conn.close()

    return await asyncio.get_running_loop().run_in_executor(None, work)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `venv312/Scripts/python.exe -m unittest tests.test_call_store -v`
Expected: all OK.

- [ ] **Step 5: Run the full suite**

Run: `venv312/Scripts/python.exe -m unittest discover -s tests -t .`
Expected: green (only the 3 known failures). app.py untouched in this task.

---

### Task 2: `call_transcript.py`: no cost line, `on_done` result

**Files:**
- Modify: `call_transcript.py` (`cost_line` lines 243-248 deleted; `render_file` 255-264; `start_job` 355-374; `write_transcript` 385-458)
- Modify: `tests/test_call_transcript.py`

**Interfaces:**
- Consumes: nothing new.
- Produces:
  - `render_file(*, call_uuid, started_at, duration_s, language, kind, reason, lines)` (no `tokens` argument, no cost line). `cost_line` no longer exists; `cost_usd` and `usage_tokens` stay.
  - `write_transcript(..., on_done=None)`: after the file is written and the `[TRANSCRIPT]` line logged, calls `on_done(result)` once, where `result = {"path": str, "stt_status": str, "audio_in": int, "text_in": int, "output": int, "stt_cost_usd": float (rounded to 6 places), "language": str, "user_turns": int}`. A raising `on_done` is logged (`⚠️ [TRANSCRIPT] on_done failed`) and swallowed. On a write failure it returns `None` and does not call `on_done`. Still returns the path.
  - `start_job(transcript, call_uuid, project, get_token, on_done=None)`: passes `on_done` to `write_transcript` only when it is not `None` (so existing fakes with the old signature keep working).

- [ ] **Step 1: Update the tests first**

In `tests/test_call_transcript.py`:

1. In `TestCost`, delete `test_cost_line_ok` and `test_cost_line_failed_and_skipped` and add:
```python
    def test_cost_line_is_gone(self):
        self.assertFalse(hasattr(ct, "cost_line"))
```

2. Replace `TestRenderAndWrite.test_render` with:
```python
    def test_render(self):
        text = ct.render_file(call_uuid="u-1", started_at="2026-10-12 10:00:00 IST", duration_s=20,
                              language="bn-IN", kind="ok", reason="", lines=["[00:00] Neha: hi"])
        self.assertEqual(text, "call_uuid: u-1\nstarted_at: 2026-10-12 10:00:00 IST\nduration_sec: 20\n"
                               "language: bn-IN\nstt_model: gemini-3.5-transcribe-preview\nstt_status: ok\n"
                               "\n[00:00] Neha: hi\n")
```

3. In `EXPECTED_GOLDEN`, delete the last three string pieces (`"\n"`, `"transcription_cost_usd: 0.0011 (audio_in=125 text_in=311 output=20 tokens, "`, `"model=gemini-3.5-transcribe-preview)\n"`), so it ends with `"[00:12] User (fallback): ঠিক আছে।\n"`.

4. In `test_failure_falls_back_for_every_caller_line`, replace the last line with:
```python
        self.assertNotIn("transcription_cost_usd", text)
```

5. In `test_skipped_when_the_caller_never_spoke`, replace the last line with:
```python
        self.assertTrue(text.endswith("[00:00] Neha: Hello, this is Neha\n"))
        self.assertNotIn("transcription_cost_usd", text)
```

6. Add to `TestWriteTranscript`:
```python
    def test_on_done_gets_the_result(self):
        results = []
        path = self._write(golden_transcript(), lambda r: httpx.Response(200, json=GOLDEN_RESPONSE),
                           on_done=results.append)
        self.assertEqual(results, [{"path": path, "stt_status": "ok", "audio_in": 125, "text_in": 311,
                                    "output": 20, "stt_cost_usd": 0.001112, "language": "bn-IN",
                                    "user_turns": 3}])

    def test_on_done_after_a_failed_stt_reports_zero_cost(self):
        results = []
        self._write(golden_transcript(), lambda r: httpx.Response(400, text="bad"), on_done=results.append)
        self.assertEqual((results[0]["stt_status"], results[0]["stt_cost_usd"], results[0]["audio_in"]),
                         ("failed (http 400)", 0.0, 0))

    def test_raising_on_done_is_logged_not_raised(self):
        def broken(result):
            raise RuntimeError("boom")

        with LogCaptureTranscript() as log:
            path = self._write(golden_transcript(), lambda r: httpx.Response(200, json=GOLDEN_RESPONSE),
                               on_done=broken)
        self.assertIsNotNone(path)
        self.assertTrue(any("⚠️ [TRANSCRIPT] on_done failed" in line for line in log.lines))

    def test_no_on_done_when_the_write_fails(self):
        results = []
        with mock.patch.object(ct, "append_segment", side_effect=OSError("disk full")):
            self._write(golden_transcript(), lambda r: httpx.Response(200, json=GOLDEN_RESPONSE),
                        on_done=results.append)
        self.assertEqual(results, [])
```

7. Add to `TestStartJob`:
```python
    def test_on_done_is_passed_through(self):
        seen = {}

        async def fake_write(transcript, call_uuid, project, get_token, **kwargs):
            seen.update(kwargs)
            return "path"

        def done(result):
            pass

        async def go():
            with mock.patch.object(ct, "write_transcript", fake_write):
                await ct.start_job(object(), "u-1", "p", _token, on_done=done)

        _run(go())
        self.assertIs(seen["on_done"], done)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `PYTHONIOENCODING=utf-8 venv312/Scripts/python.exe -m unittest tests.test_call_transcript -v`
Expected: FAIL/ERROR: `cost_line` still exists, `render_file` rejects the missing `tokens` / files still end with the cost line, `write_transcript` and `start_job` reject `on_done`.

- [ ] **Step 3: Change `call_transcript.py`**

1. Delete the whole `cost_line` function.

2. Replace `render_file` with:
```python
def render_file(*, call_uuid, started_at, duration_s, language, kind, reason, lines):
    header = [
        f"call_uuid: {call_uuid}",
        f"started_at: {started_at}",
        f"duration_sec: {duration_s}",
        f"language: {language}",
        f"stt_model: {STT_MODEL}",
        f"stt_status: {status_text(kind, reason)}",
    ]
    # The STT cost goes to the database with the transcript_ready event (step 2), not into the file.
    return "\n".join(header + [""] + lines) + "\n"
```

3. Replace `start_job`'s signature and its `kwargs` line:
```python
def start_job(transcript, call_uuid, project, get_token, on_done=None):
```
```python
    previous = _last_job.get(call_uuid)
    kwargs = {"before_write": previous} if previous is not None else {}
    if on_done is not None:
        kwargs["on_done"] = on_done
```
and add to its docstring: `on_done(result) is called once the file is written (see write_transcript).`

4. In `write_transcript`: change the signature to
```python
async def write_transcript(transcript, call_uuid, project, get_token, *,
                           client=None, sleep=asyncio.sleep, out_dir=None, before_write=None, on_done=None):
```
In the inner `render()`, drop the `tokens=tokens` argument from the `render_file(...)` call. Then replace the end of the function (from `cost = cost_usd(*tokens) if tokens else 0.0` to `return path`) with:
```python
    audio_in, text_in, output = tokens or (0, 0, 0)
    cost = cost_usd(audio_in, text_in, output)
    logger.info(
        f"📝 [TRANSCRIPT] status={status_text(kind, reason)} lang={language} "
        f"user_turns={len(caller_turns)} fallback={fallback} words={words} audio_s={audio_s:.0f} "
        f"latency={time.perf_counter() - started:.1f}s retries={retries} cost=${cost:.4f} file={path}"
        f"{' appended' if appended else ''}"
    )
    if on_done is not None:
        result = {"path": path, "stt_status": status_text(kind, reason), "audio_in": audio_in,
                  "text_in": text_in, "output": output, "stt_cost_usd": round(cost, 6),
                  "language": language, "user_turns": len(caller_turns)}
        try:
            on_done(result)
        except Exception:
            logger.exception("⚠️ [TRANSCRIPT] on_done failed")
    return path
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `PYTHONIOENCODING=utf-8 venv312/Scripts/python.exe -m unittest tests.test_call_transcript tests.test_transcript_capture -v`
Expected: all OK.

- [ ] **Step 5: Run the full suite**

Run: `venv312/Scripts/python.exe -m unittest discover -s tests -t .`
Expected: green. app.py untouched (its `start_job` call has no `on_done` yet).

---

### Task 3: `call_analysis.py`: the Haiku request and its reply

**Files:**
- Create: `call_analysis.py` (this task: config, enums, `SCHEMA`, `SYSTEM_PROMPT`, `user_message`, `build_request`, `parse_reply`, `school_for`, `nlp_cost`, `has_caller_lines`, `old_stt_cost`, `_default_client`)
- Test: `tests/test_call_analysis.py`

**Interfaces:**
- Consumes: `call_store.ANALYSIS_FIELDS`; `course_catalog.RECORDS` (each with `name` and `school: {"name", "code"}`); test fixtures `SAMPLE_FIELDS`, `PROGRAM` from `tests/test_call_store.py`.
- Produces:
  - Constants `ANALYSIS_ENABLED: bool`, `ANALYSIS_MODEL: str`, `ANALYSIS_CONCURRENCY: int`, `NLP_PRICE_INPUT`, `NLP_PRICE_OUTPUT: float`, `MAX_TOKENS = 4000`, `RETRY_DELAYS_S = (30, 120)`, `SDK_MAX_RETRIES = 4`, `NO_RETRY_STATUS: frozenset[int]`; enums `LEAD_STATUSES`, `PROSPECTIVE`, `RESPONDENTS`, `OBJECTIONS`, `LANGUAGES`, `AGENT_ISSUES` (tuples), `PROGRAM_NAMES` (tuple), `BY_NAME` (dict name -> record); `SCHEMA` (dict); `SYSTEM_PROMPT` (str); `logger = logging.getLogger("call_analysis")`.
  - `user_message(context: dict, transcript: str) -> str`; `build_request(context, transcript, model=None) -> dict` (kwargs for `messages.create`).
  - `parse_reply(response) -> (fields: dict | None, problem: str | None, input_tokens: int, output_tokens: int)`.
  - `school_for(primary_program, programs_looked_up) -> (school_code | None, school_name | None, source)` with source `"haiku"`, `"lookup"` or `"none"`.
  - `nlp_cost(input_tokens, output_tokens) -> float`; `has_caller_lines(text) -> bool`; `old_stt_cost(text) -> float | None`; `_default_client()` (imports `anthropic` inside).
  - Test helper other tasks import from `tests/test_call_analysis.py`: `make_response(text=None, stop="end_turn", tokens=(3000, 500), category=None, blocks=None)`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_call_analysis.py`:

```python
"""call_analysis.py: the Haiku 5.5 request and its reply (docs/spec/call-analysis-design.md §3)."""

from __future__ import annotations

import inspect
import json
import os
import subprocess
import sys
import types
import unittest

import call_analysis
import call_store
import course_catalog
from tests.test_call_store import PROGRAM, SAMPLE_FIELDS

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def make_response(text=None, stop="end_turn", tokens=(3000, 500), category=None, blocks=None):
    """A stand-in for an anthropic Message: content blocks, stop reason, usage."""
    if blocks is None:
        blocks = [types.SimpleNamespace(type="thinking", thinking="")]
        if text is not None:
            blocks.append(types.SimpleNamespace(type="text", text=text))
    usage = None if tokens is None else types.SimpleNamespace(input_tokens=tokens[0], output_tokens=tokens[1])
    details = None if category is None else types.SimpleNamespace(category=category)
    return types.SimpleNamespace(content=blocks, stop_reason=stop, usage=usage, stop_details=details)


CONTEXT = {"name": "Asha Das", "end_reason": "Parent agreed to visit on Saturday.", "duration_sec": 95,
           "legs": 1, "programs_looked_up": [], "b_legs": []}
TRANSCRIPT = "call_uuid: u-1\n\n[00:00] Neha: নমস্কার\n[00:05] User: হ্যাঁ বলুন\n"


class TestConfig(unittest.TestCase):
    def _run(self, code, **env):
        clean = {k: v for k, v in os.environ.items()
                 if not k.startswith(("ANALYSIS_", "NLP_PRICE_"))}
        clean.update(env)
        return subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, env=clean,
                              capture_output=True, text=True)

    def test_defaults(self):
        result = self._run("import call_analysis as c; print(c.ANALYSIS_ENABLED, c.ANALYSIS_MODEL, "
                           "c.ANALYSIS_CONCURRENCY, c.NLP_PRICE_INPUT, c.NLP_PRICE_OUTPUT)")
        self.assertEqual(result.stdout.split(), ["True", "us.anthropic.claude-haiku-5-5", "2", "0.11", "0.55"])

    def test_bad_switch_fails_at_import(self):
        result = self._run("import call_analysis", ANALYSIS_ENABLED="yes")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ANALYSIS_ENABLED must be", result.stderr)


class TestSchema(unittest.TestCase):
    def test_fields_and_strictness(self):
        schema = call_analysis.SCHEMA
        self.assertEqual(set(schema["properties"]), set(call_store.ANALYSIS_FIELDS))
        self.assertEqual(schema["required"], list(call_store.ANALYSIS_FIELDS))
        self.assertIs(schema["additionalProperties"], False)
        json.dumps(schema)

    def test_choices(self):
        props = call_analysis.SCHEMA["properties"]
        self.assertEqual(props["lead_status"]["enum"],
                         ["hot", "warm", "cold", "not_interested", "callback_requested", "wrong_number",
                          "no_conversation"])
        self.assertEqual(props["interest_score"]["enum"], list(range(1, 11)))
        names = [r["name"] for r in course_catalog.RECORDS]
        self.assertEqual(props["primary_program"]["enum"], names + [None])
        self.assertEqual(props["programs_of_interest"]["items"]["enum"], names)
        self.assertEqual(len(names), 87)


class TestRequest(unittest.TestCase):
    def test_request_shape(self):
        request = call_analysis.build_request(CONTEXT, TRANSCRIPT)
        self.assertEqual(request["model"], "us.anthropic.claude-haiku-5-5")
        self.assertEqual(request["max_tokens"], 4000)
        self.assertEqual(request["system"], call_analysis.SYSTEM_PROMPT)
        self.assertEqual(request["output_config"],
                         {"effort": "low", "format": {"type": "json_schema", "schema": call_analysis.SCHEMA}})
        for banned in ("temperature", "top_p", "top_k"):
            self.assertNotIn(banned, request)
        [message] = request["messages"]
        self.assertEqual(message["role"], "user")
        self.assertEqual(call_analysis.build_request(CONTEXT, TRANSCRIPT, model="m")["model"], "m")

    def test_user_message(self):
        text = call_analysis.user_message(CONTEXT, TRANSCRIPT)
        self.assertIn("Contact name: Asha Das\n", text)
        self.assertIn("Call length: 95 seconds\n", text)
        self.assertIn("Agent's end-of-call note: Parent agreed to visit on Saturday.\n", text)
        for name in call_analysis.PROGRAM_NAMES:
            self.assertIn(f"- {name}\n", text)
        self.assertTrue(text.endswith("Transcript:\n" + TRANSCRIPT.strip()))

    def test_user_message_unknowns(self):
        text = call_analysis.user_message({}, "")
        self.assertIn("Contact name: unknown\n", text)
        self.assertIn("Call length: unknown seconds\n", text)
        self.assertIn("Agent's end-of-call note: none\n", text)
        self.assertTrue(text.endswith("Transcript:\n(empty)"))

    def test_system_prompt_defines_every_choice(self):
        for status in call_analysis.LEAD_STATUSES:
            self.assertIn(f"- {status}:", call_analysis.SYSTEM_PROMPT)
        for phrase in ("1 (none) to 10", "English", "null", "Never guess", "User (fallback)"):
            self.assertIn(phrase, call_analysis.SYSTEM_PROMPT)


class TestParseReply(unittest.TestCase):
    def test_good_reply(self):
        fields, problem, tin, tout = call_analysis.parse_reply(make_response(json.dumps(SAMPLE_FIELDS)))
        self.assertEqual((fields, problem, tin, tout), (SAMPLE_FIELDS, None, 3000, 500))

    def test_refusal(self):
        self.assertEqual(call_analysis.parse_reply(make_response(None, stop="refusal", category="cyber"))[1],
                         "refusal: cyber")
        self.assertEqual(call_analysis.parse_reply(make_response(None, stop="refusal"))[1], "refusal: unknown")

    def test_cut_off_or_empty(self):
        self.assertEqual(call_analysis.parse_reply(make_response("{", stop="max_tokens"))[1], "max_tokens")
        self.assertEqual(call_analysis.parse_reply(make_response(None))[1], "no text in reply")

    def test_bad_json(self):
        self.assertEqual(call_analysis.parse_reply(make_response("not json"))[1], "bad json")
        self.assertEqual(call_analysis.parse_reply(make_response("[1, 2]"))[1], "bad json")

    def test_missing_field(self):
        partial = {k: v for k, v in SAMPLE_FIELDS.items() if k not in ("summary", "language")}
        self.assertEqual(call_analysis.parse_reply(make_response(json.dumps(partial)))[1],
                         "missing fields: language, summary")

    def test_no_usage(self):
        _, _, tin, tout = call_analysis.parse_reply(make_response(json.dumps(SAMPLE_FIELDS), tokens=None))
        self.assertEqual((tin, tout), (0, 0))


class TestSchoolAndCost(unittest.TestCase):
    def test_school_from_primary_program(self):
        school = course_catalog.RECORDS[0]["school"]
        self.assertEqual(call_analysis.school_for(PROGRAM, []), (school["code"], school["name"], "haiku"))

    def test_school_from_the_lookups(self):
        last = course_catalog.RECORDS[-1]
        self.assertEqual(call_analysis.school_for(None, ["not a program", last["name"]]),
                         (last["school"]["code"], last["school"]["name"], "lookup"))

    def test_no_school(self):
        self.assertEqual(call_analysis.school_for(None, []), (None, None, "none"))
        self.assertEqual(call_analysis.school_for("not a program", ["also not"]), (None, None, "none"))

    def test_cost(self):
        # (3000 * 0.11 + 500 * 0.55) / 1e6 = 0.000605
        self.assertAlmostEqual(call_analysis.nlp_cost(3000, 500), 0.000605, places=12)


class TestTranscriptHelpers(unittest.TestCase):
    def test_has_caller_lines(self):
        self.assertTrue(call_analysis.has_caller_lines(TRANSCRIPT))
        self.assertTrue(call_analysis.has_caller_lines("[00:12] User (fallback): ঠিক আছে।\n"))
        self.assertTrue(call_analysis.has_caller_lines("[105:02] User: x\n"))
        self.assertFalse(call_analysis.has_caller_lines("[00:00] Neha: Hello\n"))
        self.assertFalse(call_analysis.has_caller_lines(""))

    def test_old_cost_lines(self):
        line = "transcription_cost_usd: {} (audio_in=1 text_in=2 output=3 tokens, model=m)"
        one = "call_uuid: u\n\n[00:00] Neha: hi\n\n" + line.format("0.0011") + "\n"
        self.assertAlmostEqual(call_analysis.old_stt_cost(one), 0.0011)
        two = one + "\n" + "=" * 60 + "\n\n" + one.replace("0.0011", "0.0020")
        self.assertAlmostEqual(call_analysis.old_stt_cost(two), 0.0031)
        self.assertIsNone(call_analysis.old_stt_cost("call_uuid: u\n\n[00:00] Neha: hi\n"))

    def test_anthropic_is_imported_only_inside_the_client_factory(self):
        with open(call_analysis.__file__, encoding="utf-8") as handle:
            source = handle.read()
        self.assertEqual(source.count("import anthropic"), 1)
        self.assertIn("import anthropic", inspect.getsource(call_analysis._default_client))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONIOENCODING=utf-8 venv312/Scripts/python.exe -m unittest tests.test_call_analysis -v`
Expected: ERROR, `ModuleNotFoundError: No module named 'call_analysis'`.

- [ ] **Step 3: Write `call_analysis.py` (request part)**

```python
"""Post-call analysis with Claude Haiku 5.5 on Amazon Bedrock.

Design: docs/spec/call-analysis-design.md §2-3. This module builds the request, parses the reply
and (Task 4) runs the Analyzer queue in the main process. The anthropic SDK is imported only in
_default_client(), so the media processes never load it.
"""

import json
import logging
import os
import re

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
        "primary_program": {"type": ["string", "null"], "enum": [*PROGRAM_NAMES, None]},
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
- programs_of_interest and primary_program must be exact names from the catalog list in the message. \
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


def user_message(context, transcript):
    duration = context.get("duration_sec")
    lines = [
        f"Contact name: {context.get('name') or 'unknown'}",
        f"Call length: {duration if duration is not None else 'unknown'} seconds",
        f"Agent's end-of-call note: {context.get('end_reason') or 'none'}",
        "",
        "Catalog program names (programs_of_interest and primary_program must be exactly one of these):",
        *(f"- {name}" for name in PROGRAM_NAMES),
        "",
        "Transcript:",
        transcript.strip() or "(empty)",
    ]
    return "\n".join(lines)


def build_request(context, transcript, model=None):
    """Keyword arguments for messages.create. No sampling parameters: non-default values 400 on Haiku 5.5."""
    return {
        "model": model or ANALYSIS_MODEL,
        "max_tokens": MAX_TOKENS,
        "system": SYSTEM_PROMPT,
        "output_config": {"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
        "messages": [{"role": "user", "content": user_message(context, transcript)}],
    }


def parse_reply(response):
    """(fields, problem, input_tokens, output_tokens). problem is None when fields is usable."""
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


def nlp_cost(input_tokens, output_tokens):
    return (input_tokens * NLP_PRICE_INPUT + output_tokens * NLP_PRICE_OUTPUT) / 1_000_000


_CALLER_LINE = re.compile(r"^\[\d+:\d\d\] User(?: \(fallback\))?: ", re.MULTILINE)
_OLD_COST_LINE = re.compile(r"^transcription_cost_usd: ([0-9.]+)", re.MULTILINE)


def has_caller_lines(text):
    return bool(_CALLER_LINE.search(text or ""))


def old_stt_cost(text):
    """The STT cost written into transcript files before step 2 (one line per segment), or None."""
    amounts = [float(value) for value in _OLD_COST_LINE.findall(text or "")]
    return sum(amounts) if amounts else None


def _default_client():
    import anthropic   # only here: the media processes and the tests never load the SDK
    return anthropic.AsyncAnthropicBedrock(max_retries=SDK_MAX_RETRIES)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `PYTHONIOENCODING=utf-8 venv312/Scripts/python.exe -m unittest tests.test_call_analysis -v`
Expected: all OK.

- [ ] **Step 5: Run the full suite**

Run: `venv312/Scripts/python.exe -m unittest discover -s tests -t .`
Expected: green.

---

### Task 4: the `Analyzer` worker and `plivo_cost.py`

**Files:**
- Modify: `call_analysis.py` (append the worker)
- Create: `plivo_cost.py`
- Test: `tests/test_analysis_worker.py`, `tests/test_plivo_cost.py`

**Interfaces:**
- Consumes: Task 1's `call_store` functions and `run()`; Task 3's `build_request`, `parse_reply`, `school_for`, `nlp_cost`, `has_caller_lines`, `old_stt_cost`, `_default_client`, `RETRY_DELAYS_S`, `NO_RETRY_STATUS`, `ANALYSIS_CONCURRENCY`, `ANALYSIS_MODEL`. Test fixtures: `CALLS_TABLE`, `SAMPLE_FIELDS`, `PROGRAM` (`tests.test_call_store`), `make_response` (`tests.test_call_analysis`).
- Produces:
  - `class Analyzer(db_path, *, client_factory=_default_client, concurrency=None, sleep=asyncio.sleep, retry_delays=RETRY_DELAYS_S, model=None)` with `enqueue(call_uuid, path) -> bool` (False when already queued; a call being analysed right now can be queued again), `start(backfill_dir=None)`, `async analyze(call_uuid, path) -> str` (`"ok"`, `"no_conversation"`, `"failed"`, `"skipped"`), `async backfill(transcript_dir) -> int`, `async join()`, `async stop()`.
  - `plivo_cost`: `PLIVO_COST_ENABLED: bool`, `FETCH_DELAYS_S = (60, 180, 600)`, `amount(record) -> float | None`, `async fetch_cost(db_path, call_uuid, get_call, *, delays=FETCH_DELAYS_S, sleep=asyncio.sleep) -> float | None`, `schedule(db_path, call_uuid, get_call, **kwargs) -> asyncio.Task`, module set `_tasks`, module flag `_logged_fields`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_analysis_worker.py`:

```python
"""call_analysis.Analyzer: the Haiku queue in the main process (docs/spec/call-analysis-design.md §2)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sqlite3
import tempfile
import unittest
from unittest import mock

import call_analysis
import call_store
import course_catalog
from tests.test_call_analysis import make_response
from tests.test_call_store import CALLS_TABLE, PROGRAM, SAMPLE_FIELDS

TRANSCRIPT = "call_uuid: u-1\n\n[00:00] Neha: নমস্কার\n[00:05] User: হ্যাঁ বলুন, B.Tech নিয়ে জানতে চাই\n"
NEHA_ONLY = "call_uuid: u-1\n\n[00:00] Neha: Hello, this is Neha\n"


def run(coro):
    return asyncio.run(coro)


def good_reply(fields=None):
    return make_response(json.dumps(fields or SAMPLE_FIELDS))


class FakeStatusError(Exception):
    def __init__(self, status):
        super().__init__(f"status {status}")
        self.status_code = status


class FakeMessages:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []
        self.active = 0
        self.max_active = 0
        self.gate = None

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.gate is not None:
                await self.gate.wait()
            reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
            if isinstance(reply, Exception):
                raise reply
            return reply
        finally:
            self.active -= 1


class FakeClient:
    def __init__(self, replies):
        self.messages = FakeMessages(replies)


class FakeSleep:
    def __init__(self):
        self.delays = []

    async def __call__(self, delay):
        self.delays.append(delay)


class LogCaptureFor:
    def __init__(self, logger):
        self.logger = logger

    def __enter__(self):
        self.lines = []
        capture = self

        class _Handler(logging.Handler):
            def emit(self, record):
                capture.lines.append(record.getMessage())

        self._handler = _Handler(level=logging.INFO)
        self._level = self.logger.level
        self.logger.setLevel(logging.INFO)
        self.logger.addHandler(self._handler)
        return self

    def __exit__(self, *exc_info):
        self.logger.removeHandler(self._handler)
        self.logger.setLevel(self._level)
        return False


def make_db(folder, rows=("u-1",)):
    path = os.path.join(folder, "calls.db")
    conn = sqlite3.connect(path)
    with conn:
        conn.execute(CALLS_TABLE)
        for uuid in rows:
            conn.execute("INSERT INTO calls (call_uuid, name, phone, status, duration_sec, end_reason) "
                         "VALUES (?, 'Asha Das', '9876543210', 'ended', 95, 'call completed')", (uuid,))
        call_store.upgrade(conn)
    conn.close()
    return path


def select(db, sql, args=()):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        found = conn.execute(sql, args).fetchone()
        return dict(found) if found else None
    finally:
        conn.close()


def write_sync(db, fn, *args, **kwargs):
    conn = sqlite3.connect(db)
    try:
        with conn:
            return fn(conn, *args, **kwargs)
    finally:
        conn.close()


class WorkerCase(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)
        self.db = make_db(self.folder)

    def transcript(self, call_uuid="u-1", text=TRANSCRIPT):
        path = os.path.join(self.folder, f"{call_uuid}.txt")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def analyzer(self, client, **kwargs):
        self.sleep = FakeSleep()
        return call_analysis.Analyzer(self.db, client_factory=lambda: client, sleep=self.sleep, **kwargs)

    def analysis(self, call_uuid="u-1"):
        return select(self.db, "SELECT * FROM call_analysis WHERE call_uuid=?", (call_uuid,))

    def call(self, call_uuid="u-1"):
        return select(self.db, "SELECT * FROM calls WHERE call_uuid=?", (call_uuid,))


class TestAnalyze(WorkerCase):
    def test_good_reply_fills_the_row(self):
        client = FakeClient([good_reply()])
        with LogCaptureFor(call_analysis.logger) as log:
            status = run(self.analyzer(client).analyze("u-1", self.transcript()))
        self.assertEqual(status, "ok")
        row = self.analysis()
        school = course_catalog.RECORDS[0]["school"]
        self.assertEqual((row["status"], row["lead_status"], row["school_code"], row["school_name"]),
                         ("ok", "hot", school["code"], school["name"]))
        self.assertEqual((row["transferred"], row["attempts"], row["input_tokens"]), (0, 1, 3000))
        self.assertEqual(json.loads(row["raw_json"]), SAMPLE_FIELDS)
        self.assertAlmostEqual(self.call()["nlp_cost_usd"], 0.000605)
        [request] = client.messages.calls
        self.assertEqual(request["model"], "us.anthropic.claude-haiku-5-5")
        self.assertIn("Contact name: Asha Das", request["messages"][0]["content"])
        [line] = [l for l in log.lines if "[ANALYSIS]" in l]
        self.assertIn("🧠 [ANALYSIS] call=u-1 status=ok lead=hot prospective=yes tokens=3000/500 "
                      "cost=$0.0006", line)
        self.assertIn("school_from=haiku", line)
        for private in ("হ্যাঁ", "Asha", "9876543210"):
            self.assertNotIn(private, line)

    def test_no_caller_speech_needs_no_haiku(self):
        client = FakeClient([good_reply()])
        analyzer = self.analyzer(client)
        self.assertEqual(run(analyzer.analyze("u-1", self.transcript(text=NEHA_ONLY))), "no_conversation")
        self.assertEqual(run(analyzer.analyze("u-1", None)), "no_conversation")
        self.assertEqual(client.messages.calls, [])
        row = self.analysis()
        self.assertEqual((row["status"], row["lead_status"], row["input_tokens"]), ("ok", "no_conversation", 0))

    def test_missing_file_is_skipped(self):
        status = run(self.analyzer(FakeClient([good_reply()])).analyze("u-1", os.path.join(self.folder, "gone.txt")))
        self.assertEqual(status, "skipped")
        self.assertEqual((self.analysis()["status"], self.analysis()["error"]), ("skipped", "transcript missing"))

    def test_school_falls_back_to_the_lookups(self):
        last = course_catalog.RECORDS[-1]
        write_sync(self.db, call_store.add_leg, "u-1", 0.01, [last["name"]])
        client = FakeClient([good_reply(dict(SAMPLE_FIELDS, primary_program=None))])
        with LogCaptureFor(call_analysis.logger) as log:
            run(self.analyzer(client).analyze("u-1", self.transcript()))
        row = self.analysis()
        self.assertEqual((row["primary_program"], row["school_code"]), (None, last["school"]["code"]))
        self.assertTrue(any("school_from=lookup" in l for l in log.lines))

    def test_refusal_is_failed_and_still_costed(self):
        client = FakeClient([make_response(None, stop="refusal", category="cyber", tokens=(1000, 10))])
        self.assertEqual(run(self.analyzer(client).analyze("u-1", self.transcript())), "failed")
        self.assertEqual((self.analysis()["status"], self.analysis()["error"]), ("failed", "refusal: cyber"))
        self.assertAlmostEqual(self.call()["nlp_cost_usd"], (1000 * 0.11 + 10 * 0.55) / 1e6)
        self.assertEqual(len(client.messages.calls), 1, "a refusal is not retried")

    def test_retry_then_success(self):
        client = FakeClient([FakeStatusError(529), good_reply()])
        analyzer = self.analyzer(client)
        self.assertEqual(run(analyzer.analyze("u-1", self.transcript())), "ok")
        self.assertEqual((self.sleep.delays, self.analysis()["attempts"]), ([30], 2))

    def test_retries_exhausted(self):
        client = FakeClient([FakeStatusError(500), FakeStatusError(500), FakeStatusError(500), good_reply()])
        analyzer = self.analyzer(client)
        self.assertEqual(run(analyzer.analyze("u-1", self.transcript())), "failed")
        self.assertEqual(self.sleep.delays, [30, 120])
        row = self.analysis()
        self.assertEqual((row["status"], row["error"], row["attempts"]), ("failed", "FakeStatusError 500", 3))

    def test_bad_request_is_not_retried(self):
        analyzer = self.analyzer(FakeClient([FakeStatusError(400)]))
        self.assertEqual(run(analyzer.analyze("u-1", self.transcript())), "failed")
        self.assertEqual((self.sleep.delays, self.analysis()["attempts"]), ([], 1))

    def test_client_factory_failure_ends_failed(self):
        def no_credentials():
            raise ValueError("no region")

        analyzer = call_analysis.Analyzer(self.db, client_factory=no_credentials, sleep=FakeSleep())
        self.assertEqual(run(analyzer.analyze("u-1", self.transcript())), "failed")
        self.assertEqual((self.analysis()["error"], self.analysis()["attempts"]), ("ValueError", 3))

    def test_transferred_from_the_legs(self):
        write_sync(self.db, call_store.add_leg, "u-1", 0.01, [])
        write_sync(self.db, call_store.add_leg, "u-1", 0.01, [])
        run(self.analyzer(FakeClient([good_reply()])).analyze("u-1", self.transcript()))
        self.assertEqual(self.analysis()["transferred"], 1)

    def test_call_without_a_calls_row(self):
        path = self.transcript(call_uuid="u-x")
        self.assertEqual(run(self.analyzer(FakeClient([good_reply()])).analyze("u-x", path)), "ok")
        self.assertEqual(self.analysis("u-x")["lead_status"], "hot")


class TestQueue(WorkerCase):
    def setUp(self):
        super().setUp()   # its database already has u-1
        write_sync(self.db, lambda conn: conn.executemany(
            "INSERT INTO calls (call_uuid, name, status) VALUES (?, 'X', 'ended')", [("u-2",), ("u-3",)]))

    def test_concurrency_limit_and_dedupe(self):
        paths = {u: self.transcript(call_uuid=u) for u in ("u-1", "u-2", "u-3")}
        client = FakeClient([good_reply()])

        async def go():
            client.messages.gate = asyncio.Event()
            analyzer = self.analyzer(client, concurrency=2)
            analyzer.start()
            self.assertTrue(analyzer.enqueue("u-1", paths["u-1"]))
            self.assertFalse(analyzer.enqueue("u-1", paths["u-1"]), "already queued")
            analyzer.enqueue("u-2", paths["u-2"])
            analyzer.enqueue("u-3", paths["u-3"])
            for _ in range(500):
                if client.messages.active == 2:
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(client.messages.active, 2)
            self.assertTrue(analyzer.enqueue("u-1", paths["u-1"]), "running now: a new transcript re-queues it")
            client.messages.gate.set()
            await asyncio.wait_for(analyzer.join(), 5)
            await analyzer.stop()

        run(go())
        self.assertEqual(client.messages.max_active, 2)
        self.assertEqual(len(client.messages.calls), 4)

    def test_worker_survives_a_crash(self):
        async def go():
            analyzer = self.analyzer(FakeClient([good_reply()]))
            analyzer.analyze = mock.AsyncMock(side_effect=[RuntimeError("boom"), "ok"])
            analyzer.start()
            analyzer.enqueue("u-1", "a")
            analyzer.enqueue("u-2", "b")
            await asyncio.wait_for(analyzer.join(), 5)
            await analyzer.stop()
            return analyzer.analyze.await_count

        with LogCaptureFor(call_analysis.logger) as log:
            self.assertEqual(run(go()), 2)
        self.assertTrue(any("❌ [ANALYSIS] crashed call=u-1" in l for l in log.lines))


class TestBackfill(WorkerCase):
    def test_old_transcripts_are_queued_once(self):
        old_line = "transcription_cost_usd: 0.0011 (audio_in=125 text_in=311 output=20 tokens, model=m)\n"
        folder = os.path.join(self.folder, "TRANSCRIPTS")
        os.makedirs(folder)
        for uuid in ("u-1", "u-2"):
            with open(os.path.join(folder, f"{uuid}.txt"), "w", encoding="utf-8") as handle:
                handle.write(TRANSCRIPT + "\n" + old_line)
        with open(os.path.join(folder, "notes.md"), "w", encoding="utf-8") as handle:
            handle.write("not a transcript")
        write_sync(self.db, lambda conn: conn.execute("INSERT INTO calls (call_uuid) VALUES ('u-2')"))
        write_sync(self.db, call_store.save_failure, "u-2", "x", attempts=1)   # finished: not queued

        async def go():
            analyzer = self.analyzer(FakeClient([good_reply()]))
            first = await analyzer.backfill(folder)
            second = await analyzer.backfill(folder)   # u-1 still queued: not twice
            return first, second, analyzer._queue.qsize()

        with LogCaptureFor(call_analysis.logger) as log:
            self.assertEqual(run(go()), (1, 0, 1))
        self.assertAlmostEqual(self.call()["stt_cost_usd"], 0.0011)
        self.assertEqual(self.call()["transcript_path"], os.path.join(folder, "u-1.txt"))
        self.assertTrue(any("🧠 [ANALYSIS] backfill queued=1 of 2" in l for l in log.lines))

    def test_missing_folder(self):
        self.assertEqual(run(self.analyzer(FakeClient([good_reply()])).backfill(os.path.join(self.folder, "no"))), 0)


if __name__ == "__main__":
    unittest.main()
```

Create `tests/test_plivo_cost.py`:

```python
"""plivo_cost.py: Plivo telephony cost from call records (docs/spec/call-analysis-design.md §2.4)."""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import types
import unittest
from unittest import mock

import call_store
import plivo_cost
from tests.test_analysis_worker import FakeSleep, LogCaptureFor, make_db, select, write_sync


def run(coro):
    return asyncio.run(coro)


class TestAmount(unittest.TestCase):
    def test_amount(self):
        self.assertEqual(plivo_cost.amount({"total_amount": "0.01667"}), 0.01667)
        self.assertEqual(plivo_cost.amount(types.SimpleNamespace(total_amount="0.5")), 0.5)
        for missing in ({"total_amount": ""}, {"total_amount": None}, {}, {"total_amount": "abc"}, None):
            self.assertIsNone(plivo_cost.amount(missing), missing)


class TestFetch(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)
        self.db = make_db(self.folder)
        flag = mock.patch.object(plivo_cost, "_logged_fields", True)   # the one-time field log is tested below
        flag.start()
        self.addCleanup(flag.stop)

    def fetch(self, records, **kwargs):
        """records: call uuid -> list of replies (dict or Exception), consumed per lookup."""
        self.sleep = FakeSleep()
        lookups = []

        def get_call(uuid):
            lookups.append(uuid)
            reply = records[uuid].pop(0) if len(records[uuid]) > 1 else records[uuid][0]
            if isinstance(reply, Exception):
                raise reply
            return reply

        result = run(plivo_cost.fetch_cost(self.db, "u-1", get_call, sleep=self.sleep, **kwargs))
        return result, lookups

    def cost_row(self):
        return select(self.db, "SELECT plivo_cost_usd, plivo_cost_status FROM calls WHERE call_uuid='u-1'")

    def test_a_leg_only(self):
        result, _ = self.fetch({"u-1": [{"total_amount": "0.01667"}]})
        self.assertEqual(result, 0.01667)
        self.assertEqual(self.cost_row(), {"plivo_cost_usd": 0.01667, "plivo_cost_status": "ok"})
        self.assertEqual(self.sleep.delays, [60])

    def test_human_legs_are_added(self):
        write_sync(self.db, call_store.add_b_leg, "u-1", "b-1")
        result, lookups = self.fetch({"u-1": [{"total_amount": "0.02"}], "b-1": [{"total_amount": "0.03"}]})
        self.assertAlmostEqual(result, 0.05)
        self.assertEqual(lookups, ["u-1", "b-1"])

    def test_retried_until_the_record_has_a_cost(self):
        result, _ = self.fetch({"u-1": [{"total_amount": ""}, RuntimeError("404"), {"total_amount": "0.01"}]})
        self.assertEqual(result, 0.01)
        self.assertEqual(self.sleep.delays, [60, 180, 600])

    def test_gives_up(self):
        with LogCaptureFor(plivo_cost.logger) as log:
            result, _ = self.fetch({"u-1": [{"total_amount": ""}]})
        self.assertIsNone(result)
        self.assertEqual(self.cost_row(), {"plivo_cost_usd": None, "plivo_cost_status": "failed"})
        self.assertTrue(any("💰 [PLIVO-COST] call=u-1 status=failed" in l for l in log.lines))

    def test_record_fields_are_logged_once(self):
        with mock.patch.object(plivo_cost, "_logged_fields", False), LogCaptureFor(plivo_cost.logger) as log:
            self.fetch({"u-1": [{"total_amount": "0.01", "to_number": "9876543210"}]})
            self.fetch({"u-1": [{"total_amount": "0.01"}]})
        field_lines = [l for l in log.lines if "record fields" in l]
        self.assertEqual(field_lines, ["💰 [PLIVO-COST] record fields: ['to_number', 'total_amount']"])
        self.assertFalse(any("9876543210" in l for l in log.lines))

    def test_schedule_holds_the_task(self):
        async def go():
            task = plivo_cost.schedule(self.db, "u-1", lambda uuid: {"total_amount": "0.01"},
                                       delays=(0,), sleep=FakeSleep())
            self.assertIn(task, plivo_cost._tasks)
            await task
            await asyncio.sleep(0)
            return task in plivo_cost._tasks

        self.assertFalse(run(go()))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONIOENCODING=utf-8 venv312/Scripts/python.exe -m unittest tests.test_analysis_worker tests.test_plivo_cost -v`
Expected: ERROR, `AttributeError: module 'call_analysis' has no attribute 'Analyzer'` and `ModuleNotFoundError: No module named 'plivo_cost'`.

- [ ] **Step 3: Append the worker to `call_analysis.py`**

Add `import asyncio` and `import time` to the imports at the top (`json`, `os`, `re` are already there), then append:

```python
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
        self._tasks = []

    def enqueue(self, call_uuid, path):
        if not call_uuid or call_uuid in self._queued:
            return False
        self._queued.add(call_uuid)
        self._queue.put_nowait((call_uuid, path))
        return True

    def start(self, backfill_dir=None):
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
            try:
                await self.analyze(call_uuid, path)
            except Exception:
                logger.exception(f"❌ [ANALYSIS] crashed call={call_uuid[:8]}")
            finally:
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
                response = await self._get_client().messages.create(**request)
                break
            except Exception as e:
                status = getattr(e, "status_code", None)
                error = type(e).__name__ + (f" {status}" if status is not None else "")
                if status in NO_RETRY_STATUS or attempts > len(self.retry_delays):
                    await self._db(call_store.save_failure, call_uuid, error, attempts=attempts, model=self.model)
                    logger.warning(f"🧠 [ANALYSIS] call={tag} status=failed error={error} attempts={attempts} "
                                   f"latency={time.perf_counter() - started:.1f}s")
                    return "failed"
                logger.warning(f"🧠 [ANALYSIS] call={tag} attempt {attempts} failed ({error}), retrying")
                await self.sleep(self.retry_delays[attempts - 1])

        fields, problem, tin, tout = parse_reply(response)
        cost = nlp_cost(tin, tout)
        latency = time.perf_counter() - started
        if problem:
            await self._db(call_store.save_failure, call_uuid, problem, attempts=attempts, model=self.model,
                           input_tokens=tin, output_tokens=tout, cost_usd=cost)
            logger.warning(f"🧠 [ANALYSIS] call={tag} status=failed error={problem} tokens={tin}/{tout} "
                           f"cost=${cost:.4f} latency={latency:.1f}s attempts={attempts}")
            return "failed"
        code, name, source = school_for(fields.get("primary_program"), context["programs_looked_up"])
        await self._db(call_store.save_analysis, call_uuid, fields, school_code=code, school_name=name,
                       transferred=transferred, model=self.model, input_tokens=tin, output_tokens=tout,
                       cost_usd=cost, attempts=attempts, raw_json=json.dumps(fields, ensure_ascii=False))
        logger.info(f"🧠 [ANALYSIS] call={tag} status=ok lead={fields.get('lead_status')} "
                    f"prospective={fields.get('is_prospective')} tokens={tin}/{tout} cost=${cost:.4f} "
                    f"latency={latency:.1f}s attempts={attempts} school_from={source}")
        return "ok"

    async def backfill(self, transcript_dir):
        """Queue every transcript without a finished analysis; read a pre-step-2 cost line once."""
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
```

- [ ] **Step 4: Create `plivo_cost.py`**

```python
"""Plivo telephony cost per call, from Plivo's call records.

Design: docs/spec/call-analysis-design.md §2.4. The hangup callback carries no reliable cost, so
a minute after hangup the call record (calls.get, field total_amount) is read for the call and for
every human leg of a transfer (DialBLegUUID), and the sum is stored. Main process only.
"""

import asyncio
import logging
import os

import call_store

logger = logging.getLogger("plivo_cost")


def _flag(name, default):
    value = os.getenv(name, default).strip()
    if value not in ("0", "1"):
        raise ValueError(f"{name} must be '0' or '1', got {value!r}")
    return value == "1"


PLIVO_COST_ENABLED = _flag("PLIVO_COST_ENABLED", "1")
FETCH_DELAYS_S = (60, 180, 600)   # first look a minute after hangup, then 3 and 10 minutes later

_tasks = set()            # running fetches, held so they are not garbage-collected
_logged_fields = False    # the first record's field names are logged once, to confirm them live


def _get(record, name):
    if isinstance(record, dict):
        return record.get(name)
    return getattr(record, name, None)


def _field_names(record):
    return sorted(record.keys()) if isinstance(record, dict) else sorted(vars(record))


def amount(record):
    """total_amount as a float, or None when the record has none (yet)."""
    if record is None:
        return None
    value = _get(record, "total_amount")
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


async def fetch_cost(db_path, call_uuid, get_call, *, delays=FETCH_DELAYS_S, sleep=asyncio.sleep):
    global _logged_fields
    tag = call_uuid[:8]
    await call_store.run(db_path, call_store.set_plivo_cost, call_uuid, None, "pending")
    legs = 1
    for delay in delays:
        await sleep(delay)
        uuids = [call_uuid] + await call_store.run(db_path, call_store.b_legs, call_uuid)
        legs = len(uuids)
        amounts = []
        for uuid in uuids:
            try:
                record = await asyncio.to_thread(get_call, uuid)
            except Exception as e:
                logger.warning(f"⚠️ [PLIVO-COST] call={tag} lookup failed: {type(e).__name__}")
                record = None
            if record is not None and not _logged_fields:
                _logged_fields = True
                logger.info(f"💰 [PLIVO-COST] record fields: {_field_names(record)}")
            amounts.append(amount(record))
        if all(value is not None for value in amounts):
            total = sum(amounts)
            await call_store.run(db_path, call_store.set_plivo_cost, call_uuid, total, "ok")
            logger.info(f"💰 [PLIVO-COST] call={tag} status=ok total=${total:.5f} legs={legs}")
            return total
    await call_store.run(db_path, call_store.set_plivo_cost, call_uuid, None, "failed")
    logger.warning(f"💰 [PLIVO-COST] call={tag} status=failed legs={legs}")
    return None


def schedule(db_path, call_uuid, get_call, **kwargs):
    task = asyncio.get_running_loop().create_task(fetch_cost(db_path, call_uuid, get_call, **kwargs))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `PYTHONIOENCODING=utf-8 venv312/Scripts/python.exe -m unittest tests.test_analysis_worker tests.test_plivo_cost tests.test_call_analysis tests.test_call_store -v`
Expected: all OK.

- [ ] **Step 6: Run the full suite**

Run: `venv312/Scripts/python.exe -m unittest discover -s tests -t .`
Expected: green.

---

### Task 5: wire it into `app.py`

**Files:**
- Modify: `app.py` (imports after `import web_auth`; `init_call_history_db` ~1050; `persist_call_event_sync` ~1072; `emit_call_event_local` ~1171; `log_call_stats` ~1873; call_state literal ~2103; the `finally` of `handle_media_stream` ~2485; catalog branch ~2996; `dial_action`; `plivo_hangup`; new `finish_transcript`, `ANALYZER`, `start_background_work`)
- Modify: `web_auth.py` (`without_cost`), `requirements.txt`, `tests/harness/appctl.py`, `tests/test_transcript_capture.py`, `tests/test_web_auth.py`, `tests/test_preservation_4_8_resumption_recording_stats.py`
- Test: `tests/test_analysis_wiring.py`

**Interfaces:**
- Consumes: Tasks 1-4 (`call_store`, `call_transcript.start_job(..., on_done=)`, `call_analysis.Analyzer`, `call_analysis.ANALYSIS_ENABLED`, `plivo_cost.schedule`, `plivo_cost.PLIVO_COST_ENABLED`). Step 1 test helpers: none needed.
- Produces: `app.ANALYZER` (None or the Analyzer); `app.finish_transcript(call_state)`; `app.start_background_work()` (registered `@app.before_serving`); `call_state["catalog_found"]` (list of catalog names); `call_ended` data gains `cost_usd` (float, 6 places) and `programs_looked_up` (list); new internal event `transcript_ready` (persisted, queued, never sent to browsers); `web_auth.without_cost` drops `cost` and every `*cost_usd` key.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_analysis_wiring.py`:

```python
"""Step 2 wiring in app.py: events, costs, catalog lookups, webhooks, startup (call-analysis-design.md §2)."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from unittest import mock

import course_catalog
from tests.harness.appctl import DIAGNOSTIC_LOG_TAGS, LogCapture, app, live_call_state
from tests.test_catalog_tool_handler import run_tool_calls

web_auth = app.web_auth


def run(coro):
    return asyncio.run(coro)


class DbCase(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)
        patcher = mock.patch.object(app, "CALL_HISTORY_DB_PATH", os.path.join(self.folder, "calls.db"))
        patcher.start()
        self.addCleanup(patcher.stop)
        app.init_call_history_db()

    def row(self, call_uuid="u-1"):
        conn = sqlite3.connect(app.CALL_HISTORY_DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            found = conn.execute("SELECT * FROM calls WHERE call_uuid=?", (call_uuid,)).fetchone()
            return dict(found) if found else None
        finally:
            conn.close()

    def queued(self, call_uuid="u-1"):
        app.persist_call_event_sync(call_uuid, "call_queued", {"name": "Asha Das", "phone": "9876543210"})


class TestPersistence(DbCase):
    def test_init_upgrades_the_schema(self):
        conn = sqlite3.connect(app.CALL_HISTORY_DB_PATH)
        try:
            names = {r[1] for r in conn.execute("PRAGMA table_info(calls)")}
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            conn.close()
        self.assertIn("gemini_cost_usd", names)
        self.assertIn("call_analysis", tables)

    def test_two_call_ended_events_add_up(self):
        self.queued()
        app.persist_call_event_sync("u-1", "call_ended", {"cost": "$0.0100", "cost_usd": 0.01,
                                                          "reason": "transferred", "programs_looked_up": ["A"]})
        app.persist_call_event_sync("u-1", "call_ended", {"cost": "$0.0200", "cost_usd": 0.02,
                                                          "reason": "done", "programs_looked_up": ["B"]})
        r = self.row()
        self.assertEqual((r["legs"], r["cost"], r["end_reason"], r["status"]), (2, "$0.0300", "done", "ended"))
        self.assertAlmostEqual(r["gemini_cost_usd"], 0.03)
        self.assertEqual(json.loads(r["programs_looked_up"]), ["A", "B"])

    def test_transcript_ready_adds_the_stt_cost(self):
        self.queued()
        app.persist_call_event_sync("u-1", "transcript_ready", {"path": "TRANSCRIPTS/u-1.txt", "stt_cost_usd": 0.0011})
        r = self.row()
        self.assertAlmostEqual(r["stt_cost_usd"], 0.0011)
        self.assertEqual(r["transcript_path"], "TRANSCRIPTS/u-1.txt")


class TestEvents(unittest.TestCase):
    def test_transcript_ready_is_queued_and_never_reaches_browsers(self):
        fake = mock.Mock()

        async def go():
            subscriber = asyncio.Queue()
            app.call_event_subscribers.add(subscriber)
            try:
                with mock.patch.object(app, "ANALYZER", fake), \
                        mock.patch.object(app, "persist_call_event_sync") as persist:
                    app.emit_call_event_local("u-1", "transcript_ready",
                                              {"path": "TRANSCRIPTS/u-1.txt", "stt_cost_usd": 0.001})
                    await asyncio.sleep(0.05)
                return subscriber.qsize(), persist.call_args
            finally:
                app.call_event_subscribers.discard(subscriber)

        size, persisted = run(go())
        self.assertEqual(size, 0)
        fake.enqueue.assert_called_once_with("u-1", "TRANSCRIPTS/u-1.txt")
        self.assertEqual(persisted.args[:2], ("u-1", "transcript_ready"))

    def test_without_an_analyzer_nothing_breaks(self):
        async def go():
            with mock.patch.object(app, "ANALYZER", None), mock.patch.object(app, "persist_call_event_sync"):
                app.emit_call_event_local("u-1", "transcript_ready", {"path": None})
                await asyncio.sleep(0.01)

        run(go())

    def test_without_cost_drops_every_cost_key(self):
        payload = json.dumps({"call_id": "u-1", "type": "call_ended",
                              "data": {"cost": "$0.03", "cost_usd": 0.03, "stt_cost_usd": 0.001, "reason": "done"}})
        self.assertEqual(json.loads(web_auth.without_cost(payload))["data"], {"reason": "done"})

    def test_call_ended_carries_the_numbers(self):
        call_state = live_call_state(with_denoiser=False, with_aec=False, tokens_text_in=2_000_000,
                                     catalog_found=["A"])
        with mock.patch.object(app, "emit_call_event") as emit, LogCapture():
            app.log_call_stats(call_state)
        call_uuid, event_type, data = emit.call_args.args
        self.assertEqual(event_type, "call_ended")
        self.assertEqual(data["programs_looked_up"], ["A"])
        expected = round(2 * app.PRICE_TEXT_INPUT, 6)   # 2M text-in tokens, nothing else
        self.assertAlmostEqual(data["cost_usd"], expected)
        self.assertEqual(data["cost"], f"${expected:.4f}")

    def test_log_tags(self):
        self.assertIn("[ANALYSIS]", DIAGNOSTIC_LOG_TAGS)
        self.assertIn("[PLIVO-COST]", DIAGNOSTIC_LOG_TAGS)


class TestCatalogFound(unittest.TestCase):
    def test_found_programs_are_recorded_once(self):
        args = {"programs": ["cse"], "fields": ["fees"]}
        call_state, _, _ = run_tool_calls(("get_program_details", args), ("get_program_details", args),
                                          ("list_programs", {}))
        expected = course_catalog.handle_tool_call("get_program_details", args)["results"][0]["name"]
        self.assertEqual(call_state["catalog_found"], [expected])


class TestFinishTranscript(unittest.TestCase):
    def test_job_with_on_done(self):
        transcript = mock.Mock()
        transcript.has_turns.return_value = True
        with mock.patch.object(app.call_transcript, "start_job") as job, \
                mock.patch.object(app, "emit_call_event") as emit:
            app.finish_transcript({"transcript": transcript, "call_uuid": "u-1"})
            args = job.call_args
            self.assertEqual(args.args, (transcript, "u-1", app.VERTEX_PROJECT, app.vertex_access_token))
            args.kwargs["on_done"]({"path": "p", "stt_cost_usd": 0.001})
        emit.assert_called_once_with("u-1", "transcript_ready", {"path": "p", "stt_cost_usd": 0.001})

    def test_nothing_said_still_reports(self):
        transcript = mock.Mock()
        transcript.has_turns.return_value = False
        with mock.patch.object(app.call_transcript, "start_job") as job, \
                mock.patch.object(app, "emit_call_event") as emit:
            app.finish_transcript({"transcript": transcript, "call_uuid": "u-1"})
        job.assert_not_called()
        emit.assert_called_once_with("u-1", "transcript_ready", {"path": None, "user_turns": 0, "stt_cost_usd": 0.0})

    def test_no_transcript_object_does_nothing(self):
        with mock.patch.object(app.call_transcript, "start_job") as job, \
                mock.patch.object(app, "emit_call_event") as emit:
            app.finish_transcript({"transcript": None, "call_uuid": "u-1"})
        job.assert_not_called()
        emit.assert_not_called()


class TestWebhooks(DbCase):
    def setUp(self):
        super().setUp()
        for patcher in (mock.patch.object(web_auth, "PLIVO_SIGNATURE_CHECK", "off"),
                        mock.patch.object(app, "EVENT_FORWARD_URL", ""),
                        mock.patch.object(app, "PUBLIC_BASE_URL", "https://agent.example.com")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def post(self, path, form):
        async def go():
            return (await app.app.test_client().post(path, form=form)).status_code
        return run(go())

    def test_hangup_schedules_the_plivo_cost(self):
        with mock.patch.object(app.plivo_cost, "schedule") as schedule, \
                mock.patch.object(app, "emit_call_event"):
            self.assertEqual(self.post("/plivo-hangup", {"CallUUID": "h-1", "HangupCause": "NORMAL_CLEARING"}), 200)
        schedule.assert_called_once_with(app.CALL_HISTORY_DB_PATH, "h-1", app.plivo_client.calls.get)

    def test_hangup_in_a_media_process_schedules_nothing(self):
        with mock.patch.object(app, "EVENT_FORWARD_URL", "http://127.0.0.1:8000/internal/call-event"), \
                mock.patch.object(app.plivo_cost, "schedule") as schedule:
            self.post("/plivo-hangup", {"CallUUID": "h-1", "HangupCause": "NORMAL_CLEARING"})
        schedule.assert_not_called()

    def test_dial_action_stores_the_human_leg(self):
        self.queued("c-1")
        form = {"DialBLegStatus": "completed", "CallUUID": "c-1", "From": "+919876543210", "DialBLegUUID": "b-1"}
        self.assertEqual(self.post("/dial-action?call_uuid=c-1", form), 200)
        self.assertEqual(json.loads(self.row("c-1")["plivo_b_legs"]), ["b-1"])

    def test_dial_action_without_a_human_leg(self):
        self.queued("c-1")
        self.post("/dial-action?call_uuid=c-1", {"DialBLegStatus": "no-answer", "CallUUID": "c-1", "DialBLegUUID": ""})
        self.assertIsNone(self.row("c-1")["plivo_b_legs"])


class FakeAnalyzer:
    def __init__(self, db_path, **kwargs):
        self.db_path = db_path
        self.started = "never"

    def start(self, backfill_dir=None):
        self.started = backfill_dir


class TestStartup(DbCase):
    def setUp(self):
        super().setUp()
        for patcher in (mock.patch.object(app, "ANALYZER", None),
                        mock.patch.object(app.call_analysis, "Analyzer", FakeAnalyzer),
                        mock.patch.object(app.call_analysis, "ANALYSIS_ENABLED", True),
                        mock.patch.object(app.plivo_cost, "PLIVO_COST_ENABLED", True)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.queued("u-1")
        app.persist_call_event_sync("u-1", "call_connected", {})
        app.persist_call_event_sync("u-1", "call_ended", {"cost_usd": 0.01})

    def test_main_process_starts_the_worker_and_catches_up(self):
        with mock.patch.object(app, "EVENT_FORWARD_URL", ""), \
                mock.patch.object(app.plivo_cost, "schedule") as schedule:
            run(app.start_background_work())
            self.assertIsInstance(app.ANALYZER, FakeAnalyzer)
            self.assertEqual(app.ANALYZER.started, app.call_transcript.TRANSCRIPT_DIR)
        schedule.assert_called_once_with(app.CALL_HISTORY_DB_PATH, "u-1", app.plivo_client.calls.get,
                                         delays=(0, 180, 600))

    def test_media_process_starts_nothing(self):
        with mock.patch.object(app, "EVENT_FORWARD_URL", "http://127.0.0.1:8000/internal/call-event"), \
                mock.patch.object(app.plivo_cost, "schedule") as schedule:
            run(app.start_background_work())
            self.assertIsNone(app.ANALYZER)
        schedule.assert_not_called()

    def test_switches_off(self):
        with mock.patch.object(app, "EVENT_FORWARD_URL", ""), \
                mock.patch.object(app.call_analysis, "ANALYSIS_ENABLED", False), \
                mock.patch.object(app.plivo_cost, "PLIVO_COST_ENABLED", False), \
                mock.patch.object(app.plivo_cost, "schedule") as schedule:
            run(app.start_background_work())
            self.assertIsNone(app.ANALYZER)
        schedule.assert_not_called()

    def test_registered_before_serving(self):
        self.assertIn(app.start_background_work, app.app.before_serving_funcs)


if __name__ == "__main__":
    unittest.main()
```

Update the existing tests:

1. `tests/test_transcript_capture.py`, replace `test_job_starts_in_the_finally_after_the_stats` with:
```python
    def test_job_starts_in_the_finally_after_the_stats(self):
        final = self.media.rindex("    finally:\n")
        stats = self.media.index("log_call_stats(call_state)", final)
        job = self.media.index("finish_transcript(call_state)", final)
        self.assertLess(stats, job)
        helper = inspect.getsource(app.finish_transcript)
        self.assertIn("call_transcript.start_job(", helper)
        self.assertIn("VERTEX_PROJECT, vertex_access_token", helper)
```

2. `tests/test_web_auth.py`, in `TestWithoutCost` add:
```python
    def test_every_cost_key_is_removed(self):
        payload = json.dumps({"call_id": "u-1", "type": "call_ended",
                              "data": {"cost": "$1", "cost_usd": 1.0, "stt_cost_usd": 0.1, "reason": "x"}})
        self.assertEqual(json.loads(web_auth.without_cost(payload))["data"], {"reason": "x"})
```

- [ ] **Step 2: Run them to verify they fail**

Run: `PYTHONIOENCODING=utf-8 venv312/Scripts/python.exe -m unittest tests.test_analysis_wiring tests.test_transcript_capture tests.test_web_auth -v`
Expected: ERROR/FAIL: `app` has no `ANALYZER`, `finish_transcript`, `start_background_work`, `plivo_cost`; `without_cost` keeps `cost_usd`.

- [ ] **Step 3: `web_auth.without_cost`**

Replace the function with:
```python
def without_cost(event_payload):
    """A dashboard event (JSON text) without its cost fields (cost, *cost_usd), for operator browsers."""
    event = json.loads(event_payload)
    data = event.get("data")
    if isinstance(data, dict):
        costly = [key for key in data if key == "cost" or key.endswith("cost_usd")]
        if costly:
            for key in costly:
                del data[key]
            return json.dumps(event)
    return event_payload
```

- [ ] **Step 4: `app.py` edits**

1. After the lines
```python
# Logins and Plivo request checks (web_auth.py). Imported after load_dotenv() so its config sees .env.
import web_auth
```
add
```python

# Step 2: per-call costs and the Haiku analysis (call_store.py, call_analysis.py, plivo_cost.py).
import call_store
import call_analysis
import plivo_cost
```

2. In `init_call_history_db`, after the `CREATE TABLE IF NOT EXISTS calls (...)` execute and before `conn.commit()`, add:
```python
        call_store.upgrade(conn)   # step 2 columns and call_analysis; every process, race-safe
```

3. In `persist_call_event_sync`, in the `elif event_type in ("call_ended", "call_failed"):` branch, after its `conn.execute("""UPDATE calls SET status=?, ...""", (...))` statement add:
```python
                if event_type == "call_ended":
                    call_store.add_leg(conn, call_uuid, data.get("cost_usd"), data.get("programs_looked_up"))
```
and add a new branch before `conn.commit()`:
```python
            elif event_type == "transcript_ready":
                call_store.add_stt(conn, call_uuid, data.get("stt_cost_usd"), data.get("path"))
```

4. Directly above `def emit_call_event(call_id, event_type, data=None):` add:
```python
# call_analysis.Analyzer, started by start_background_work in the main process; None elsewhere.
ANALYZER = None
```
In `emit_call_event_local`, change the persisted-types list to
`["call_queued", "call_ringing", "call_connected", "call_ended", "call_failed", "transcript_ready"]`,
and directly after that `if call_id and event_type in [...]` block (before the `try:` that builds the SSE payload) add:
```python
    if event_type == "transcript_ready":
        # Internal: carries costs, so it never goes to dashboards. The main process queues the analysis.
        if ANALYZER is not None and call_id:
            ANALYZER.enqueue(str(call_id), (data or {}).get("path"))
        return
```

5. In `log_call_stats`, replace the `emit_call_event(call_state.get("call_uuid"), "call_ended", {...})` call with:
```python
    emit_call_event(call_state.get("call_uuid"), "call_ended", {
        "reason": reason,
        "cost": f"${total_cost:.4f}",
        "cost_usd": round(total_cost, 6),
        "phone": call_state.get("from_number", ""),
        "programs_looked_up": list(call_state.get("catalog_found", [])),
    })
```

6. In the `call_state` literal in `handle_media_stream`, after `"catalog_chars": 0,` add:
```python
        # Catalog names get_program_details found on this call (step 2: school fallback for the analysis).
        "catalog_found": [],
```

7. In the catalog tool branch, after `call_state["catalog_chars"] += chars` add:
```python
                                    if call.name == "get_program_details":
                                        for entry in result.get("results", []):
                                            name = entry.get("name")
                                            if entry.get("status") == "found" and name not in call_state["catalog_found"]:
                                                call_state["catalog_found"].append(name)
```

8. In the `finally` of `handle_media_stream`, replace
```python
        if call_state["transcript"] and call_state["transcript"].has_turns():
            call_transcript.start_job(call_state["transcript"],
                                      call_state.get("call_uuid") or f"no-uuid-{int(time.time())}",
                                      VERTEX_PROJECT, vertex_access_token)
```
with
```python
        finish_transcript(call_state)
```
and add this function directly above `async def stream_plivo_to_gemini(`:
```python
def finish_transcript(call_state):
    """After a call leg: start the transcript job, which reports transcript_ready when its file is
    written, or report a leg where nobody spoke. Transcripts switched off: nothing (no fake rows)."""
    transcript = call_state.get("transcript")
    if transcript is None:
        return None
    call_uuid = call_state.get("call_uuid") or f"no-uuid-{int(time.time())}"
    if transcript.has_turns():
        return call_transcript.start_job(
            transcript, call_uuid, VERTEX_PROJECT, vertex_access_token,
            on_done=lambda result: emit_call_event(call_uuid, "transcript_ready", result))
    emit_call_event(call_uuid, "transcript_ready", {"path": None, "user_turns": 0, "stt_cost_usd": 0.0})
    return None
```

9. In `dial_action`, in the POST branch after `plivo_call_uuid = form_data.get('CallUUID', '')` add `b_leg_uuid = form_data.get('DialBLegUUID', '')`; in the GET branch after `plivo_call_uuid = request.args.get('CallUUID', '')` add `b_leg_uuid = request.args.get('DialBLegUUID', '')`. After the line `logger.info(f"📲 DialStatus={dial_status}, call_uuid={call_uuid}")` add:
```python
    if b_leg_uuid and call_uuid and not EVENT_FORWARD_URL:
        try:   # the human leg is billed as its own call: its cost is added after hangup
            await call_store.run(CALL_HISTORY_DB_PATH, call_store.add_b_leg, call_uuid, b_leg_uuid)
        except Exception as e:
            logger.error(f"❌ [PLIVO-COST] could not store the human leg: {type(e).__name__}")
```

10. In `plivo_hangup`, directly before `if hangup_cause and hangup_cause != "NORMAL_CLEARING":` add:
```python
    if call_uuid and plivo_cost.PLIVO_COST_ENABLED and not EVENT_FORWARD_URL:
        plivo_cost.schedule(CALL_HISTORY_DB_PATH, call_uuid, plivo_client.calls.get)
```

11. Directly above `if __name__ == "__main__":` add:
```python
@app.before_serving
async def start_background_work():
    """Main process only: the Haiku worker (with its catch-up of old transcripts) and the Plivo
    costs that a restart may have missed (spec §2.5)."""
    global ANALYZER
    if EVENT_FORWARD_URL:
        return
    if call_analysis.ANALYSIS_ENABLED:
        ANALYZER = call_analysis.Analyzer(CALL_HISTORY_DB_PATH)
        ANALYZER.start(backfill_dir=call_transcript.TRANSCRIPT_DIR)
        logger.info(f"🧠 [ANALYSIS] worker started model={call_analysis.ANALYSIS_MODEL} "
                    f"concurrency={call_analysis.ANALYSIS_CONCURRENCY}")
    if plivo_cost.PLIVO_COST_ENABLED:
        since = (datetime.now(ist_tz) - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
        missing = await call_store.run(CALL_HISTORY_DB_PATH, call_store.calls_needing_plivo_cost, since)
        for call_uuid in missing:
            plivo_cost.schedule(CALL_HISTORY_DB_PATH, call_uuid, plivo_client.calls.get, delays=(0, 180, 600))
```
(`plivo_client` is defined at module level above the routes, so it exists here.)

- [ ] **Step 5: Harness, requirements**

1. `tests/harness/appctl.py`: in `new_call_state`, after `"catalog_chars": 0,` add `"catalog_found": [],`. In `DIAGNOSTIC_LOG_TAGS`, after the `"[AUTH]",` line add:
```python
    "[ANALYSIS]",     # step 2: Haiku analysis worker (call_analysis.py)
    "[PLIVO-COST]",   # step 2: Plivo call-record cost fetch (plivo_cost.py)
```
2. `requirements.txt`: add the line `anthropic[bedrock]>=1.0` directly after `annotated-doc==0.0.4` (the operator pins the exact version after installing it on EC2).

- [ ] **Step 6: Run the new tests**

Run: `PYTHONIOENCODING=utf-8 venv312/Scripts/python.exe -m unittest tests.test_analysis_wiring tests.test_transcript_capture tests.test_web_auth tests.test_harness_smoke tests.test_catalog_tool_handler tests.test_operator_view -v`
Expected: all OK.

- [ ] **Step 7: Re-pin the GEMINI_MODEL lines**

Run: `venv312/Scripts/python.exe -m unittest tests.test_preservation_4_8_resumption_recording_stats -v`
Expected: `test_model_identifier_is_recorded` fails with the new list of 5. Check the 5 sites (definition, `log_call_stats`, connect call, `🧬 [MODEL]` line, `[ANOMALY]` line), replace the list and add one comment line:
```python
        # And again for step 2 (call_store / call_analysis / plivo_cost imports, call_ended numbers, catalog_found).
```
Run again. Expected: OK.

- [ ] **Step 8: Run the full suite**

Run: `venv312/Scripts/python.exe -m unittest discover -s tests -t .`
Expected: green (only the 3 known failures).

---

### Task 6: the real-call spike script and the docs

**Files:**
- Create: `tests/analysis_spike.py` (tracked so it reaches EC2 with `git pull`; not collected by `unittest discover`, whose pattern is `test*.py`)
- Modify: `CLAUDE.md`, `docs/spec/call-analysis-design.md` (status line), `docs/spec/admin-system-overview.md` (build order item 2)

**Interfaces:**
- Consumes: `call_analysis._default_client`, `build_request`, `parse_reply`, `nlp_cost`.
- Produces: the operator's EC2 check; docs that match the code.

- [ ] **Step 1: Create `tests/analysis_spike.py`**

```python
"""One real Haiku 5.5 analysis on an existing transcript (docs/spec/call-analysis-design.md §4).

Not a unit test: it calls Amazon Bedrock (about $0.001). Run on EC2 with the AWS keys loaded:
    set -a; . /etc/au-app/common.env; set +a
    venv/bin/python tests/analysis_spike.py TRANSCRIPTS/<call_uuid>.txt
Prints the stop reason, tokens, cost and the parsed fields. A schema error from Bedrock shows here
first: fix call_analysis.SCHEMA before deploying.
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import call_analysis  # noqa: E402


async def main(path):
    with open(path, encoding="utf-8") as handle:
        transcript = handle.read()
    client = call_analysis._default_client()
    request = call_analysis.build_request({"name": None, "end_reason": None, "duration_sec": None}, transcript)
    response = await client.messages.create(**request)
    fields, problem, tin, tout = call_analysis.parse_reply(response)
    print(f"model: {request['model']}")
    print(f"stop_reason: {response.stop_reason}  problem: {problem}")
    print(f"tokens: in={tin} out={tout}  cost: ${call_analysis.nlp_cost(tin, tout):.5f}")
    print(json.dumps(fields, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: analysis_spike.py TRANSCRIPTS/<call_uuid>.txt")
    asyncio.run(main(sys.argv[1]))
```

- [ ] **Step 2: Update the docs**

`<date>` below is the day this task runs (`YYYY-MM-DD`).

1. `docs/spec/call-analysis-design.md`, first status line: append ` Implemented <date>, plan docs/spec/call-analysis-plan.md; not yet on EC2.`
2. `docs/spec/admin-system-overview.md`, build order item 2: append ` Implemented <date>; not yet on EC2.`
3. `CLAUDE.md`:
   - Under "Resume here", add a new first item:
```
0000. **Step 2 of the admin system (call analysis + full call cost), built and tested offline <date>, not yet on EC2.** `call_store.py`, `call_analysis.py`, `plivo_cost.py`, app.py wiring; transcript files no longer carry the cost line. Spec `docs/spec/call-analysis-design.md`, plan `docs/spec/call-analysis-plan.md`. Next (operator, EC2): `venv/bin/pip install "anthropic[bedrock]"` and pin the installed version in requirements.txt; AWS keys + `AWS_DEFAULT_REGION` in `/etc/au-app/common.env`; run `tests/analysis_spike.py` on one transcript; deploy, restart idle; check the startup `[ANALYSIS] worker started` / `backfill queued=` lines, one live call's `call_analysis` row and the four costs; compare the Plivo amount and currency with the Plivo console. Kill switches `ANALYSIS_ENABLED=0`, `PLIVO_COST_ENABLED=0`.
```
   - Update the suite line to the new count (from Step 3).
   - Add a session-log entry at the end, `### <date>: step 2, call analysis and full call cost (uncommitted)`: the three new modules and what each owns; the `calls` columns and the `call_analysis` table; the `transcript_ready` event (internal, never sent to browsers) and the widened `without_cost`; the transfer cost fix (legs summed); the startup catch-up of old transcripts; the new test files and their counts; the 4_8 re-pin; the new `call_state` key `catalog_found`.

- [ ] **Step 3: Run the full suite and note the count**

Run: `venv312/Scripts/python.exe -m unittest discover -s tests -t .`
Expected: green (only the 3 known failures). Put the run count into CLAUDE.md's suite line. Confirm `tests/analysis_spike.py` did not run (it is not in the run list).
