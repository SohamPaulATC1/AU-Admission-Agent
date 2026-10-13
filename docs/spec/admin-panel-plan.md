# Step 4: The Admin Panel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The owner (admin login) gets an `/admin` panel with five views (Overview, Calls & leads, Call detail, Campaign reports, Do-not-call), each table exportable to CSV, a re-run button for call analysis, and a date range that defaults to the last 7 days.

**Architecture:** `admin_data.py` (new) holds every query and the CSV writer as sync functions over an open sqlite3 connection (the `call_store.run` pattern), so they are tested on an in-memory database with no web layer. `app.py` gets twelve thin `/api/admin/` routes (all `ADMIN_API` in `web_auth.ENDPOINT_ACCESS`) that parse and check the request, call `admin_data` through `call_store.run`, and reply with JSON or CSV. `admin.html` becomes one vanilla-JS page with five hash-routed views. `Analyzer` (call_analysis.py) gains one small public method, `is_busy`. Three log lines stop printing full phone numbers.

**Tech Stack:** Python 3.12, sqlite3, Quart 0.20, stdlib `csv`/`io`, vanilla JS, stdlib `unittest`.

**Spec:** `docs/spec/admin-panel-design.md` (shared decisions: `docs/spec/admin-system-overview.md`).

**Dry run (Windows, 2026-10-13, throwaway worktree, discarded):** every code block in this plan was applied exactly as written on top of `4e08359` plus the uncommitted prompt-v4 files. Result: 1098 run (995 + 103 new), only the 3 known `test_prompt_catalog` failures, 4 skipped, 1 expected failure (the `GEMINI_MODEL` re-pin to `[109, 1908, 2430, 2440, 2871]` is in Task 4). The page script also passed `node --check` and a node run against a stub DOM with canned API replies, including a re-run poll that first sees `pending` and then a failed run (throwaway, not part of the suite). Revised 2026-10-13 after an Opus 5.5 review: re-run needs the transcript file (else 409), the calls CSV is built in the thread pool, the page's re-run poll waits past `pending`, the re-pin moved from Task 7 to Task 4.

## Global Constraints

- No new dependency: `requirements.txt` does not change. No new `.env` key and no kill switch (the views are read-only; the only writes are do-not-call add/remove and analysis re-run). Rollback is a revert of the commit; the one schema change is the index `calls_created_at`.
- Every `/api/admin/` route is `ADMIN_API` in `web_auth.ENDPOINT_ACCESS` (an operator gets 403, no login 401). `TestDefaultDeny` keeps that table equal to `app.url_map`. POST bodies go through the existing `json_body()` (JSON only: 415 otherwise).
- Times are IST text `YYYY-MM-DD HH:MM:SS`. A date range is inclusive of both days: `created_at >= 'from 00:00:00'` and `< 'to+1 day 00:00:00'`. No dates given means the last 7 days including today; `all=1` means no filter.
- Counting (spec decision 4): hot = `lead_status = 'hot'`; warm = `'warm'`; visits fixed = `campus_visit_agreed = 1`; calls = every call in the range; connected = `connected_at` set; cost per call = total / calls; cost per hot lead = total of all calls in the range / hot count (null when no hot lead).
- **Cost of one call** = `COALESCE(gemini_cost_usd, <old cost text as a number>) + stt + haiku + plivo`, nulls as 0. This is the spec's rule made exact: a call from before step 2 has `gemini_cost_usd` NULL, its Gemini part is the old `cost` text and is reported as "Earlier calls (no split)"; any Haiku or Plivo cost it got later (the step 2 catch-up) is still counted in its own part.
- CSV: UTF-8 with a BOM, CRLF lines, a cell starting with `=`, `+`, `-`, `@`, tab or CR gets a leading `'` (a bare phone number such as `+919876543210` is left alone), at most 20,000 data rows then a note line.
- The page never builds HTML from data: `textContent` only (no `innerHTML`). Transcripts are read only from inside `call_transcript.TRANSCRIPT_DIR`.
- A re-run is queued only when the call's transcript file exists (`admin_data.transcript_file`); otherwise 409. Without it `Analyzer.analyze` reads empty text and saves `no_conversation` over every field of a good analysis.
- 8000 also carries live call audio, so anything heavier than a few milliseconds (the database and the 20,000-row CSV) runs in the thread pool through `call_store.run`, never on the event loop.
- Phone numbers in logs are masked with `campaigns.masked` (`xxxxxx1234`). The new log tag is `🛠️ [ADMIN]`, added to `DIAGNOSTIC_LOG_TAGS` in `tests/harness/appctl.py`.
- `@app.route("/trigger-call", methods=["POST"])` stays byte-identical (only a log line inside the function changes). `aec.py`, `aec1.py`, `bargein.py` and the prompt files are untouched. No new `call_state` key.
- When app.py's line count changes, re-pin the `GEMINI_MODEL` list in `tests/test_preservation_4_8_resumption_recording_stats.py` in the same task (only Task 4 moves those lines; Tasks 5 and 7 edit below or in place). Every task ends with only the 3 known failures.
- Tests: stdlib `unittest` only. Run one file with `venv312/Scripts/python.exe -m unittest tests.test_admin_data`; run the suite with `venv312/Scripts/python.exe -m unittest discover -s tests -t .` (EC2: `venv/bin/python -m unittest discover -s tests -t .`). Write test code with the Write or Edit tool, never through bash heredocs (CLAUDE.md: escapes get mangled). Printing app log lines needs `PYTHONIOENCODING=utf-8`.
- No commits unless the operator asks (CLAUDE.md). Each task ends with the full suite. If a shell has no `.env`, `app.py` needs dummy `PLIVO_AUTH_ID`, `PLIVO_AUTH_TOKEN`, `OPERATOR_USERNAME`, `OPERATOR_PASSWORD`, `ADMIN_USERNAME`, `ADMIN_PASSWORD` and a 32+ character `APP_SECRET` in the environment to import (the real dev machine and EC2 have them).
- **Baseline before this plan** (after the prompt-v4 work is committed): 995 run, 3 failures (all in `tests/test_prompt_catalog.py`: `test_v2_is_the_default_prompt`, `test_language_mix_is_critical_with_examples`, `test_size_stays_close_to_v1`; leave them), 4 skipped, 1 expected failure. EC2 shows one more known failure, `test_gemini_backend.TestVertexEndpoint.test_defaults`.

## Review Focus

1. **An empty database or a range with no calls** must show zeros and dashes, never a division by zero or a crash. Tests: Task 1 `test_an_empty_database`, Task 3 `test_an_empty_campaign_lists_with_zeros`.
2. **A call from before step 2 that the catch-up later analysed** (old cost text plus a Haiku and a Plivo cost) must count all three, once. Tests: Task 1 `test_an_earlier_call_that_later_got_analysis_and_plivo_cost_counts_all_of_it`, `test_an_odd_cost_text_counts_as_zero`.
3. **A name or note from an uploaded Excel that starts like a formula** (`=HYPERLINK(...)`, `-5+2`, `@SUM`) must not run when the CSV opens in Excel, while a `+91...` phone stays a number. Tests: Task 4 `TestCsvCell`, `test_calls_csv`, `test_campaigns_and_dnc_csv`.
4. **A transcript path that leaves the folder**: `..\`, an absolute path elsewhere, a sibling folder whose name starts the same (`TRANSCRIPTS_evil`), an id in the URL with slashes. Tests: Task 2 `TestReadTranscript`, Task 4 `test_an_id_that_tries_to_leave_the_route_is_404`.
5. **A search for `%`, `_`, `!`, `\` or a Bengali name**, and **double-clicking Re-run or clicking "Re-run all failed" with 60 failed calls**: the search is literal, a second click gets 409 / counts as busy, and at most 50 are queued with the rest reported. A call with no transcript file is never re-run (its analysis stays). Tests: Task 2 `test_search_wildcards_are_literal`, `test_bengali_names_are_searchable`; Task 5 `TestRerun` (incl. `test_a_call_without_a_transcript_is_409_and_its_analysis_stays`), `TestRerunFailed` (incl. `test_failed_calls_without_a_transcript_are_counted_not_queued`).

## Files

| File | Change |
|---|---|
| `admin_data.py` | Create (Tasks 1-5). Range, costs, overview (1); calls table, filters, call detail, transcript reading (2); campaign reports, do-not-call (3); CSV (4); failed analyses, transcript check (5). |
| `app.py` | Modify. `import admin_data` and its `upgrade` call (4); read routes (4); write routes (5); three masked log lines (7). |
| `web_auth.py` | Modify. Twelve `ADMIN_API` entries (4: eight, 5: four). |
| `call_analysis.py` | Modify (5). `Analyzer.is_busy`. |
| `admin.html` | Replace the shell (6). |
| `tests/harness/appctl.py` | Modify (4). `[ADMIN]` log tag. |
| `tests/test_admin_data.py`, `tests/test_admin_api.py`, `tests/test_admin_page.py`, `tests/test_log_masking.py` | Create. |
| `tests/test_preservation_4_8_resumption_recording_stats.py` | Modify (4). Re-pin. |
| `CLAUDE.md`, `docs/spec/admin-panel-design.md`, `docs/spec/admin-system-overview.md` | Modify (7). |

---

### Task 1: `admin_data.py`: range, costs, overview

**Files:**
- Create: `admin_data.py`
- Test: `tests/test_admin_data.py`

**Interfaces:**
- Consumes: `call_store.IST`, `call_store.upgrade`, `call_analysis.LEAD_STATUSES`, `campaigns.upgrade`, `tests.test_call_store.CALLS_TABLE`.
- Produces (later tasks rely on these exact names):
  - `AdminError(ValueError)`, `NotFound(AdminError)`; constants `PAGE_SIZE = 50`, `CSV_MAX_ROWS = 20000`, `RERUN_MAX = 50`, `LEAD_STATUSES`, `ANALYSIS_STATUSES`.
  - `upgrade(conn)`; `parse_range(from_text, to_text, all_time, today) -> (start, end)` of `'YYYY-MM-DD'` strings, or `(None, None)`; `_range_sql(start, end, column="c.created_at") -> (sql, args)`; `EARLIER_SQL`, `COST_SQL` (SQL text over the alias `c`); `_money(value)`; `overview(conn, start, end) -> dict`.
  - Test helpers in `tests/test_admin_data.py`: `new_db()`, `add_call(conn, call_uuid, created=..., *, name, phone, connected, gemini, stt, nlp, plivo, plivo_status, cost_text, transcript_path)`, `add_analysis(conn, call_uuid, status="ok", **fields)`, `DbCase`.

The module header below already imports everything the later tasks need (`csv`, `io`, `os`, `campaigns`), so no later task edits it.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_admin_data.py` with this content (Tasks 2-4 add classes before the last two lines):

```python
"""admin_data.py: the range, the overview and its costs (docs/spec/admin-panel-design.md §1)."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from datetime import date

import admin_data
import call_store
import campaigns
from tests.test_call_store import CALLS_TABLE

TODAY = date(2026, 10, 13)


def new_db():
    conn = sqlite3.connect(":memory:")
    conn.execute(CALLS_TABLE)
    call_store.upgrade(conn)
    campaigns.upgrade(conn)
    admin_data.upgrade(conn)
    return conn


def add_call(conn, call_uuid, created="2026-10-12 10:00:00", *, name="Asha Das", phone="9876543210",
             connected=True, gemini=None, stt=None, nlp=None, plivo=None, plivo_status=None, cost_text=None,
             transcript_path=None):
    conn.execute(
        "INSERT INTO calls (call_uuid, name, phone, status, created_at, updated_at, connected_at, cost, "
        "gemini_cost_usd, stt_cost_usd, nlp_cost_usd, plivo_cost_usd, plivo_cost_status, transcript_path) "
        "VALUES (?, ?, ?, 'ended', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (call_uuid, name, phone, created, created, created if connected else None, cost_text, gemini, stt, nlp,
         plivo, plivo_status, transcript_path))


def add_analysis(conn, call_uuid, status="ok", **fields):
    call_store._upsert(conn, call_uuid, {"status": status, **fields})


class DbCase(unittest.TestCase):
    def setUp(self):
        self.conn = new_db()
        self.addCleanup(self.conn.close)


class TestParseRange(unittest.TestCase):
    def test_no_dates_is_the_last_seven_days(self):
        self.assertEqual(admin_data.parse_range(None, None, False, TODAY), ("2026-10-07", "2026-10-13"))
        self.assertEqual(admin_data.parse_range("", "", False, TODAY), ("2026-10-07", "2026-10-13"))

    def test_all_time_has_no_bounds(self):
        self.assertEqual(admin_data.parse_range(None, None, True, TODAY), (None, None))

    def test_custom_range(self):
        self.assertEqual(admin_data.parse_range("2026-10-01", "2026-10-13", False, TODAY),
                         ("2026-10-01", "2026-10-13"))
        self.assertEqual(admin_data.parse_range("2026-10-05", "2026-10-05", False, TODAY),
                         ("2026-10-05", "2026-10-05"))

    def test_bad_dates_are_refused(self):
        for start, end in (("2026-13-01", "2026-13-02"), ("20261013", "20261013"), ("yesterday", "today"),
                           ("2026-10-01", None), (None, "2026-10-01"), ("2026-10-1", "2026-10-13"),
                           ("2026-10-01 ", "2026-10-13")):
            with self.assertRaises(admin_data.AdminError, msg=(start, end)):
                admin_data.parse_range(start, end, False, TODAY)

    def test_from_after_to_is_refused(self):
        with self.assertRaises(admin_data.AdminError):
            admin_data.parse_range("2026-10-13", "2026-10-01", False, TODAY)


class TestRangeEdges(DbCase):
    def test_whole_days_are_included_at_both_ends(self):
        add_call(self.conn, "before", "2026-10-06 23:59:59")
        add_call(self.conn, "first", "2026-10-07 00:00:00")
        add_call(self.conn, "last", "2026-10-13 23:59:59")
        add_call(self.conn, "after", "2026-10-14 00:00:00")
        self.assertEqual(admin_data.overview(self.conn, "2026-10-07", "2026-10-13")["calls"], 2)

    def test_all_time_counts_everything(self):
        add_call(self.conn, "old", "2020-01-01 00:00:00")
        add_call(self.conn, "new", "2026-10-13 09:00:00")
        self.assertEqual(admin_data.overview(self.conn, None, None)["calls"], 2)


class TestOverview(DbCase):
    def test_an_empty_database(self):
        result = admin_data.overview(self.conn, None, None)
        self.assertEqual((result["calls"], result["connected"], result["hot"]), (0, 0, 0))
        self.assertEqual(result["cost"]["total"], 0)
        self.assertIsNone(result["cost_per_call"])
        self.assertIsNone(result["cost_per_hot_lead"])

    def test_counts(self):
        add_call(self.conn, "a", gemini=0.1, stt=0.01, nlp=0.002, plivo=0.02)
        add_call(self.conn, "b", gemini=0.2, stt=0.02, nlp=0.002, plivo=0.02)
        add_call(self.conn, "c", connected=False)
        add_call(self.conn, "d", gemini=0.05)
        add_analysis(self.conn, "a", lead_status="hot", campus_visit_agreed=1)
        add_analysis(self.conn, "b", lead_status="warm", campus_visit_agreed=0)
        add_analysis(self.conn, "d", lead_status="hot", campus_visit_agreed=0)
        result = admin_data.overview(self.conn, None, None)
        self.assertEqual((result["calls"], result["connected"]), (4, 3))
        self.assertEqual((result["hot"], result["warm"], result["visits"]), (2, 1, 1))

    def test_cost_parts_total_and_ratios(self):
        add_call(self.conn, "a", gemini=0.1, stt=0.01, nlp=0.002, plivo=0.02)
        add_call(self.conn, "b", gemini=0.2, stt=0.02, nlp=0.002, plivo=0.02)
        add_analysis(self.conn, "a", lead_status="hot")
        cost = admin_data.overview(self.conn, None, None)
        self.assertAlmostEqual(cost["cost"]["gemini"], 0.3)
        self.assertAlmostEqual(cost["cost"]["stt"], 0.03)
        self.assertAlmostEqual(cost["cost"]["haiku"], 0.004)
        self.assertAlmostEqual(cost["cost"]["plivo"], 0.04)
        self.assertAlmostEqual(cost["cost"]["total"], 0.374)
        self.assertAlmostEqual(cost["cost_per_call"], 0.187)
        self.assertAlmostEqual(cost["cost_per_hot_lead"], 0.374)

    def test_earlier_calls_use_the_cost_text_and_stay_out_of_the_four_parts(self):
        add_call(self.conn, "old", cost_text="$0.1404")
        add_call(self.conn, "new", gemini=0.1)
        cost = admin_data.overview(self.conn, None, None)["cost"]
        self.assertAlmostEqual(cost["earlier"], 0.1404)
        self.assertAlmostEqual(cost["gemini"], 0.1)
        self.assertAlmostEqual(cost["total"], 0.2404)

    def test_an_earlier_call_that_later_got_analysis_and_plivo_cost_counts_all_of_it(self):
        add_call(self.conn, "old", cost_text="$0.1404", nlp=0.0013, plivo=0.0166)
        cost = admin_data.overview(self.conn, None, None)["cost"]
        self.assertAlmostEqual(cost["earlier"], 0.1404)
        self.assertAlmostEqual(cost["haiku"], 0.0013)
        self.assertAlmostEqual(cost["plivo"], 0.0166)
        self.assertAlmostEqual(cost["total"], 0.1583)

    def test_an_odd_cost_text_counts_as_zero(self):
        add_call(self.conn, "x", cost_text="n/a")
        add_call(self.conn, "y", cost_text="")
        add_call(self.conn, "z")
        self.assertEqual(admin_data.overview(self.conn, None, None)["cost"]["total"], 0)

    def test_notes_count_pending_and_failed_analyses_and_missing_plivo_costs(self):
        add_call(self.conn, "a", gemini=0.1, plivo_status="ok", plivo=0.01)
        add_call(self.conn, "b", gemini=0.1, plivo_status="pending")
        add_call(self.conn, "c", gemini=0.1)
        add_call(self.conn, "d", connected=False)
        add_analysis(self.conn, "a", "pending")
        add_analysis(self.conn, "b", "failed")
        add_analysis(self.conn, "c", "failed")
        notes = admin_data.overview(self.conn, None, None)["notes"]
        self.assertEqual(notes, {"analysis_pending": 1, "analysis_failed": 2, "plivo_pending": 2})


class TestUpgrade(DbCase):
    def test_the_index_exists_and_upgrade_is_repeatable(self):
        admin_data.upgrade(self.conn)
        names = {r[1] for r in self.conn.execute("PRAGMA index_list(calls)")}
        self.assertIn("calls_created_at", names)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run them to verify they fail**

Run: `venv312/Scripts/python.exe -m unittest tests.test_admin_data`
Expected: ERROR, `ModuleNotFoundError: No module named 'admin_data'`.

- [ ] **Step 3: Write `admin_data.py`**

```python
"""Step 4 admin panel: the read queries over call_history.db and the do-not-call list.

Design: docs/spec/admin-panel-design.md §1. Sync functions that take an open sqlite3 connection (the
call_store.run pattern); app.py runs them in the thread pool. No SQL is built from request text:
values are bound parameters and filter values are checked against allow-lists first.
"""

import csv
import io
import os
import re
from datetime import date, timedelta

import call_analysis
import call_store
import campaigns

IST = call_store.IST
PAGE_SIZE = 50
CSV_MAX_ROWS = 20000
RERUN_MAX = 50
LEAD_STATUSES = call_analysis.LEAD_STATUSES
ANALYSIS_STATUSES = ("pending", "ok", "failed", "skipped")


class AdminError(ValueError):
    """A request the admin can fix; the message is shown to them (400)."""


class NotFound(AdminError):
    """No such call or number (404)."""


def upgrade(conn):
    """The one index the range queries need. Safe on every start, in every process."""
    conn.execute("CREATE INDEX IF NOT EXISTS calls_created_at ON calls (created_at)")


# --- the date range ---

_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def parse_range(from_text, to_text, all_time, today):
    """(from, to) as 'YYYY-MM-DD' strings, or (None, None) for all time. No dates: the last 7 days."""
    if all_time:
        return None, None
    if not from_text and not to_text:
        return (today - timedelta(days=6)).isoformat(), today.isoformat()
    try:
        if not (_DATE.fullmatch(from_text or "") and _DATE.fullmatch(to_text or "")):
            raise ValueError
        start, end = date.fromisoformat(from_text), date.fromisoformat(to_text)
    except ValueError:
        raise AdminError("from and to must both be dates like 2026-10-13") from None
    if start > end:
        raise AdminError("from must not be after to")
    return start.isoformat(), end.isoformat()


def _range_sql(start, end, column="c.created_at"):
    """(' AND ...', args) keeping rows whose IST text stamp falls on a day from start to end inclusive."""
    if start is None:
        return "", []
    stop = (date.fromisoformat(end) + timedelta(days=1)).isoformat()
    return f" AND {column} >= ? AND {column} < ?", [f"{start} 00:00:00", f"{stop} 00:00:00"]


# --- cost ---

# Calls before step 2 have no split: their Gemini part is the old `cost` text ("$0.1404").
EARLIER_SQL = "COALESCE(CAST(REPLACE(c.cost, '$', '') AS REAL), 0)"
# A call's cost: Gemini (or the old text) plus the other three parts. Null parts count as 0.
COST_SQL = (f"(COALESCE(c.gemini_cost_usd, {EARLIER_SQL}) + COALESCE(c.stt_cost_usd, 0) "
            "+ COALESCE(c.nlp_cost_usd, 0) + COALESCE(c.plivo_cost_usd, 0))")


def _money(value):
    return round(float(value or 0), 6)


# --- overview ---

def overview(conn, start, end):
    where, args = _range_sql(start, end)
    row = conn.execute(
        "SELECT COUNT(*), SUM(c.connected_at IS NOT NULL), SUM(a.lead_status = 'hot'), "
        "SUM(a.lead_status = 'warm'), SUM(a.campus_visit_agreed = 1), "
        "SUM(c.gemini_cost_usd), SUM(c.stt_cost_usd), SUM(c.nlp_cost_usd), SUM(c.plivo_cost_usd), "
        f"SUM(CASE WHEN c.gemini_cost_usd IS NULL THEN {EARLIER_SQL} ELSE 0 END), "
        "SUM(a.status = 'pending'), SUM(a.status = 'failed'), "
        "SUM(c.connected_at IS NOT NULL AND (c.plivo_cost_status IS NULL OR c.plivo_cost_status = 'pending')) "
        "FROM calls c LEFT JOIN call_analysis a ON a.call_uuid = c.call_uuid WHERE 1=1" + where, args).fetchone()
    calls, connected, hot, warm, visits = (int(v or 0) for v in row[:5])
    gemini, stt, haiku, plivo, earlier = (_money(v) for v in row[5:10])
    total = _money(gemini + stt + haiku + plivo + earlier)
    return {
        "calls": calls, "connected": connected, "hot": hot, "warm": warm, "visits": visits,
        "cost": {"gemini": gemini, "stt": stt, "haiku": haiku, "plivo": plivo, "earlier": earlier,
                 "total": total},
        "cost_per_call": _money(total / calls) if calls else None,
        "cost_per_hot_lead": _money(total / hot) if hot else None,
        "notes": {"analysis_pending": int(row[10] or 0), "analysis_failed": int(row[11] or 0),
                  "plivo_pending": int(row[12] or 0)},
    }
```

- [ ] **Step 4: Run the tests**

Run: `venv312/Scripts/python.exe -m unittest tests.test_admin_data`
Expected: `Ran 15 tests ... OK`.

- [ ] **Step 5: Full suite (no commit; the operator commits)**

Run: `venv312/Scripts/python.exe -m unittest discover -s tests -t .`
Expected: 1010 run, only the 3 known failures.

---

### Task 2: Calls table, filters, call detail, transcript reading

**Files:**
- Modify: `admin_data.py` (append)
- Test: `tests/test_admin_data.py`

**Interfaces:**
- Consumes: Task 1 (`_range_sql`, `COST_SQL`, `EARLIER_SQL`, `_money`, `AdminError`, `PAGE_SIZE`, `CSV_MAX_ROWS`, `LEAD_STATUSES`, `ANALYSIS_STATUSES`), `call_store.LIST_FIELDS`, `call_store.BOOL_FIELDS`, `call_store._json_list`.
- Produces:
  - `check_filters(args) -> dict` (keys `lead, analysis, visit, program, school, campaign, q`; raises `AdminError`), `CALL_ROW_KEYS`, `list_calls(conn, start, end, filters, page) -> {"rows", "total", "page", "page_size", "programs", "schools", "campaigns", "failed_total"}`, `export_calls(conn, start, end, filters) -> list` (up to `CSV_MAX_ROWS + 1` rows).
  - `read_transcript(path, root) -> str | None`, `TRANSCRIPT_MAX_CHARS`, `call_exists(conn, call_uuid) -> bool`, `call_detail(conn, call_uuid, transcript_root) -> {"call", "cost", "analysis", "campaign", "transcript"} | None`.
  - `_text(args, name, limit)` and `_like(text)` (used again by Task 3).
  - Test helpers: `add_campaign_row(conn, name, created, contacts)`, `NO_FILTERS`, `filters(**given)`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_admin_data.py`, insert this block immediately before the `if __name__ == "__main__":` line:

```python
def add_campaign_row(conn, name="October list", created="2026-10-12 09:00:00", contacts=()):
    """A campaign with (phone, call_uuid, dialed_at) contacts, written straight into the tables."""
    campaign_id = conn.execute("INSERT INTO campaigns (name, state, created_at) VALUES (?, 'done', ?)",
                               (name, created)).lastrowid
    for phone, call_uuid, dialed_at in contacts:
        conn.execute("INSERT INTO campaign_contacts (campaign_id, phone, status, call_uuid, dialed_at) "
                     "VALUES (?, ?, 'done', ?, ?)", (campaign_id, phone, call_uuid, dialed_at))
    return campaign_id


NO_FILTERS = admin_data.check_filters({})


def filters(**given):
    return admin_data.check_filters(given)


class TestCheckFilters(unittest.TestCase):
    def test_nothing_given_is_all_none(self):
        self.assertEqual(NO_FILTERS, {"lead": None, "analysis": None, "visit": None, "program": None,
                                      "school": None, "campaign": None, "q": None})

    def test_good_values_pass(self):
        got = filters(lead="hot", analysis="failed", visit="yes", program="B.Tech CSE", school="SoET",
                      campaign="12", q=" asha ")
        self.assertEqual((got["lead"], got["analysis"], got["visit"], got["campaign"], got["q"]),
                         ("hot", "failed", "yes", 12, "asha"))

    def test_values_outside_the_lists_are_refused(self):
        for bad in ({"lead": "boiling"}, {"analysis": "maybe"}, {"visit": "perhaps"}, {"campaign": "x"},
                    {"campaign": "-1"}, {"campaign": "1" * 10}, {"campaign": "²"}, {"q": "x" * 101},
                    {"program": "x" * 201}):
            with self.assertRaises(admin_data.AdminError, msg=bad):
                admin_data.check_filters(bad)


class TestListCalls(DbCase):
    def rows(self, **given):
        return admin_data.list_calls(self.conn, None, None, filters(**given), 1)

    def test_newest_first_in_pages_of_fifty(self):
        for i in range(120):
            add_call(self.conn, f"u{i:03d}", f"2026-10-12 10:{i // 60:02d}:{i % 60:02d}")
        first = admin_data.list_calls(self.conn, None, None, NO_FILTERS, 1)
        third = admin_data.list_calls(self.conn, None, None, NO_FILTERS, 3)
        self.assertEqual((first["total"], len(first["rows"]), len(third["rows"])), (120, 50, 20))
        self.assertEqual(first["rows"][0]["call_uuid"], "u119")
        self.assertEqual(third["rows"][-1]["call_uuid"], "u000")
        self.assertEqual(first["page_size"], 50)

    def test_a_row_carries_the_table_columns_and_the_call_cost(self):
        add_call(self.conn, "a", gemini=0.1, stt=0.01, nlp=0.002, plivo=0.02)
        add_analysis(self.conn, "a", lead_status="hot", primary_program="B.Tech CSE", school_name="SoET",
                     campus_visit_day="Saturday", interest_score=8)
        row = self.rows()["rows"][0]
        self.assertEqual(tuple(row), admin_data.CALL_ROW_KEYS)
        self.assertEqual((row["lead_status"], row["primary_program"], row["school_name"], row["campus_visit_day"],
                          row["interest_score"], row["analysis_status"]),
                         ("hot", "B.Tech CSE", "SoET", "Saturday", 8, "ok"))
        self.assertAlmostEqual(row["cost_usd"], 0.132)

    def test_a_call_without_analysis_still_lists(self):
        add_call(self.conn, "a")
        row = self.rows()["rows"][0]
        self.assertIsNone(row["lead_status"])
        self.assertIsNone(row["analysis_status"])

    def test_the_range_applies(self):
        add_call(self.conn, "in", "2026-10-12 10:00:00")
        add_call(self.conn, "out", "2026-09-01 10:00:00")
        got = admin_data.list_calls(self.conn, "2026-10-07", "2026-10-13", NO_FILTERS, 1)
        self.assertEqual([r["call_uuid"] for r in got["rows"]], ["in"])

    def test_each_filter(self):
        add_call(self.conn, "a", name="Asha", phone="9876543210")
        add_call(self.conn, "b", name="Bimal", phone="9000000002")
        add_call(self.conn, "c", name="Chitra", phone="9000000003")
        add_analysis(self.conn, "a", lead_status="hot", campus_visit_agreed=1, primary_program="CSE",
                     school_name="SoET")
        add_analysis(self.conn, "b", "failed", lead_status="cold", campus_visit_agreed=0, primary_program="BBA",
                     school_name="SoM")
        campaign_id = add_campaign_row(self.conn, contacts=[("9000000003", "c", "2026-10-12 10:00:00")])

        def ids(**given):
            return sorted(r["call_uuid"] for r in self.rows(**given)["rows"])

        self.assertEqual(ids(lead="hot"), ["a"])
        self.assertEqual(ids(analysis="failed"), ["b"])
        self.assertEqual(ids(visit="yes"), ["a"])
        self.assertEqual(ids(visit="no"), ["b", "c"])
        self.assertEqual(ids(program="BBA"), ["b"])
        self.assertEqual(ids(school="SoET"), ["a"])
        self.assertEqual(ids(campaign=str(campaign_id)), ["c"])
        self.assertEqual(ids(q="asha"), ["a"])
        self.assertEqual(ids(q="0000003"), ["c"])
        self.assertEqual(ids(lead="hot", school="SoM"), [])

    def test_search_wildcards_are_literal(self):
        add_call(self.conn, "a", name="Asha")
        add_call(self.conn, "b", name="50% off")
        add_call(self.conn, "c", name="a_b")
        add_call(self.conn, "d", name="Wow!")

        def ids(q):
            return sorted(r["call_uuid"] for r in self.rows(q=q)["rows"])

        self.assertEqual(ids("%"), ["b"])
        self.assertEqual(ids("_"), ["c"])
        self.assertEqual(ids("!"), ["d"])
        self.assertEqual(ids("\\"), [])

    def test_bengali_names_are_searchable(self):
        add_call(self.conn, "a", name="আশা দাস")
        self.assertEqual([r["call_uuid"] for r in self.rows(q="দাস")["rows"]], ["a"])

    def test_dropdown_values_and_the_failed_total_ignore_the_range_and_filters(self):
        add_call(self.conn, "a", "2020-01-01 10:00:00")
        add_analysis(self.conn, "a", "failed", primary_program="CSE", school_name="SoET")
        add_call(self.conn, "b")
        add_analysis(self.conn, "b", primary_program="BBA", school_name="SoM")
        add_campaign_row(self.conn, name="October list")
        got = admin_data.list_calls(self.conn, "2026-10-07", "2026-10-13", filters(lead="hot"), 1)
        self.assertEqual((got["programs"], got["schools"]), (["BBA", "CSE"], ["SoET", "SoM"]))
        self.assertEqual([c["name"] for c in got["campaigns"]], ["October list"])
        self.assertEqual(got["failed_total"], 1)

    def test_export_returns_one_row_past_the_cap(self):
        for i in range(5):
            add_call(self.conn, f"u{i}")
        original = admin_data.CSV_MAX_ROWS
        admin_data.CSV_MAX_ROWS = 3
        self.addCleanup(setattr, admin_data, "CSV_MAX_ROWS", original)
        self.assertEqual(len(admin_data.export_calls(self.conn, None, None, NO_FILTERS)), 4)


class TestCallDetail(DbCase):
    def setUp(self):
        super().setUp()
        self.folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)

    def detail(self, call_uuid="a"):
        return admin_data.call_detail(self.conn, call_uuid, self.folder)

    def test_an_unknown_call_is_none(self):
        self.assertIsNone(self.detail("nope"))
        self.assertFalse(admin_data.call_exists(self.conn, "nope"))

    def test_costs_lists_and_analysis_are_decoded(self):
        add_call(self.conn, "a", gemini=0.1, stt=0.01, nlp=0.002, plivo=0.02, plivo_status="ok")
        self.conn.execute("UPDATE calls SET programs_looked_up = ?, plivo_b_legs = ? WHERE call_uuid = 'a'",
                          (json.dumps(["B.Tech CSE"]), json.dumps(["leg-1"])))
        add_analysis(self.conn, "a", lead_status="hot", objections=json.dumps(["fee"]),
                     campus_visit_agreed=1, scholarship_discussed=0, summary="Visit fixed.", raw_json="{}")
        got = self.detail()
        self.assertTrue(admin_data.call_exists(self.conn, "a"))
        self.assertEqual(got["cost"], {"gemini": 0.1, "stt": 0.01, "haiku": 0.002, "plivo": 0.02, "earlier": None,
                                       "total": 0.132, "plivo_status": "ok"})
        self.assertEqual(got["call"]["programs_looked_up"], ["B.Tech CSE"])
        self.assertEqual(got["call"]["plivo_b_legs"], ["leg-1"])
        self.assertEqual(got["analysis"]["objections"], ["fee"])
        self.assertEqual(got["analysis"]["subjects"], [])
        self.assertIs(got["analysis"]["campus_visit_agreed"], True)
        self.assertIs(got["analysis"]["scholarship_discussed"], False)
        self.assertIsNone(got["analysis"]["callback_requested"])
        self.assertNotIn("raw_json", got["analysis"])
        self.assertNotIn("transcript_path", got["call"])

    def test_an_earlier_call_shows_its_text_cost_as_earlier(self):
        add_call(self.conn, "a", cost_text="$0.1404")
        cost = self.detail()["cost"]
        self.assertEqual((cost["gemini"], cost["earlier"], cost["total"]), (None, 0.1404, 0.1404))

    def test_no_analysis_and_no_campaign(self):
        add_call(self.conn, "a")
        got = self.detail()
        self.assertIsNone(got["analysis"])
        self.assertIsNone(got["campaign"])
        self.assertIsNone(got["transcript"])

    def test_the_campaign_it_belongs_to(self):
        add_call(self.conn, "a")
        campaign_id = add_campaign_row(self.conn, name="October list", contacts=[("9876543210", "a", None)])
        self.assertEqual(self.detail()["campaign"], {"id": campaign_id, "name": "October list"})

    def test_the_transcript_is_read_from_inside_the_folder(self):
        path = os.path.join(self.folder, "a.txt")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("Neha: Hello\nUser: হ্যাঁ\n")
        add_call(self.conn, "a", transcript_path=path)
        self.assertEqual(self.detail()["transcript"], "Neha: Hello\nUser: হ্যাঁ\n")


class TestReadTranscript(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.root = os.path.join(self.base, "TRANSCRIPTS")
        os.mkdir(self.root)
        self.evil = os.path.join(self.base, "TRANSCRIPTS_evil")
        os.mkdir(self.evil)
        for folder in (self.root, self.evil):
            with open(os.path.join(folder, "x.txt"), "w", encoding="utf-8") as handle:
                handle.write("secret" if folder == self.evil else "fine")

    def test_inside_the_folder(self):
        self.assertEqual(admin_data.read_transcript(os.path.join(self.root, "x.txt"), self.root), "fine")

    def test_dot_dot_out_of_the_folder_is_refused(self):
        self.assertIsNone(admin_data.read_transcript(os.path.join(self.root, "..", "TRANSCRIPTS_evil", "x.txt"),
                                                     self.root))

    def test_a_sibling_folder_with_the_same_prefix_is_refused(self):
        self.assertIsNone(admin_data.read_transcript(os.path.join(self.evil, "x.txt"), self.root))

    def test_an_absolute_path_elsewhere_is_refused(self):
        self.assertIsNone(admin_data.read_transcript(os.path.abspath(__file__), self.root))

    def test_missing_empty_and_none(self):
        self.assertIsNone(admin_data.read_transcript(os.path.join(self.root, "gone.txt"), self.root))
        self.assertIsNone(admin_data.read_transcript("", self.root))
        self.assertIsNone(admin_data.read_transcript(None, self.root))

    def test_a_long_file_is_cut(self):
        path = os.path.join(self.root, "big.txt")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("x" * (admin_data.TRANSCRIPT_MAX_CHARS + 10))
        self.assertEqual(len(admin_data.read_transcript(path, self.root)), admin_data.TRANSCRIPT_MAX_CHARS)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `venv312/Scripts/python.exe -m unittest tests.test_admin_data`
Expected: ERRORs, `AttributeError: module 'admin_data' has no attribute 'check_filters'` (and similar).

- [ ] **Step 3: Append the implementation to `admin_data.py`**

Append this to the end of the file:

```python
# --- the calls table ---

def _choice(args, name, allowed):
    value = args.get(name) or None
    if value is not None and value not in allowed:
        raise AdminError(f"{name} must be one of: {', '.join(allowed)}")
    return value


def _text(args, name, limit):
    value = (args.get(name) or "").strip()
    if len(value) > limit:
        raise AdminError(f"{name} is longer than {limit} characters")
    return value or None


def check_filters(args):
    """The calls-table filters from a mapping of strings (request.args), checked against allow-lists."""
    campaign = args.get("campaign") or None
    if campaign is not None:
        if not re.fullmatch(r"[0-9]{1,9}", campaign):
            raise AdminError("campaign must be a number")
        campaign = int(campaign)
    return {"lead": _choice(args, "lead", LEAD_STATUSES), "analysis": _choice(args, "analysis", ANALYSIS_STATUSES),
            "visit": _choice(args, "visit", ("yes", "no")), "program": _text(args, "program", 200),
            "school": _text(args, "school", 200), "campaign": campaign, "q": _text(args, "q", 100)}


def _like(text):
    """A LIKE pattern matching `text` literally anywhere ("!" escapes % and _; use with ESCAPE '!')."""
    return "%" + re.sub(r"[!%_]", lambda m: "!" + m.group(), text) + "%"


def _filters_sql(filters):
    parts, args = [], []
    if filters["lead"]:
        parts.append("a.lead_status = ?")
        args.append(filters["lead"])
    if filters["analysis"]:
        parts.append("a.status = ?")
        args.append(filters["analysis"])
    if filters["visit"] == "yes":
        parts.append("a.campus_visit_agreed = 1")
    elif filters["visit"] == "no":
        parts.append("(a.campus_visit_agreed IS NULL OR a.campus_visit_agreed = 0)")
    if filters["program"]:
        parts.append("a.primary_program = ?")
        args.append(filters["program"])
    if filters["school"]:
        parts.append("a.school_name = ?")
        args.append(filters["school"])
    if filters["campaign"] is not None:
        parts.append("c.call_uuid IN (SELECT call_uuid FROM campaign_contacts WHERE campaign_id = ?)")
        args.append(filters["campaign"])
    if filters["q"]:
        parts.append("(c.name LIKE ? ESCAPE '!' OR c.phone LIKE ? ESCAPE '!')")
        args += [_like(filters["q"])] * 2
    return "".join(f" AND {part}" for part in parts), args


CALL_ROW_KEYS = ("call_uuid", "created_at", "name", "phone", "lead_status", "primary_program", "school_name",
                 "campus_visit_day", "interest_score", "cost_usd", "analysis_status")


def _select_calls(conn, start, end, filters, limit, offset):
    where, args = _range_sql(start, end)
    more, more_args = _filters_sql(filters)
    rows = conn.execute(
        "SELECT c.call_uuid, c.created_at, c.name, c.phone, a.lead_status, a.primary_program, a.school_name, "
        f"a.campus_visit_day, a.interest_score, {COST_SQL}, a.status "
        f"FROM calls c LEFT JOIN call_analysis a ON a.call_uuid = c.call_uuid WHERE 1=1{where}{more} "
        "ORDER BY c.created_at DESC, c.id DESC LIMIT ? OFFSET ?", [*args, *more_args, limit, offset]).fetchall()
    out = []
    for row in rows:
        item = dict(zip(CALL_ROW_KEYS, row))
        item["cost_usd"] = _money(item["cost_usd"])
        out.append(item)
    return out


def _distinct(conn, column):
    """The values of a call_analysis column (a fixed name from this file, never request text)."""
    return [r[0] for r in conn.execute(
        f"SELECT DISTINCT {column} FROM call_analysis WHERE {column} IS NOT NULL ORDER BY 1")]


def list_calls(conn, start, end, filters, page):
    """One page of the calls table, plus what the page needs to fill its filter dropdowns."""
    where, args = _range_sql(start, end)
    more, more_args = _filters_sql(filters)
    total = conn.execute("SELECT COUNT(*) FROM calls c LEFT JOIN call_analysis a ON a.call_uuid = c.call_uuid "
                         f"WHERE 1=1{where}{more}", [*args, *more_args]).fetchone()[0]
    rows = _select_calls(conn, start, end, filters, PAGE_SIZE, (page - 1) * PAGE_SIZE)
    return {"rows": rows, "total": total, "page": page, "page_size": PAGE_SIZE,
            "programs": _distinct(conn, "primary_program"), "schools": _distinct(conn, "school_name"),
            "campaigns": [{"id": r[0], "name": r[1]} for r in conn.execute(
                "SELECT id, name FROM campaigns ORDER BY id DESC")],
            "failed_total": conn.execute("SELECT COUNT(*) FROM call_analysis WHERE status = 'failed'").fetchone()[0]}


def export_calls(conn, start, end, filters):
    """Every matching row for the CSV, one more than the cap so the caller can tell it was cut."""
    return _select_calls(conn, start, end, filters, CSV_MAX_ROWS + 1, 0)


# --- one call ---

TRANSCRIPT_MAX_CHARS = 500_000


def read_transcript(path, root):
    """The transcript text, or None when there is none, it is missing, or it is outside `root`."""
    if not path:
        return None
    try:
        base = os.path.realpath(root)
        target = os.path.realpath(path)
        if os.path.commonpath([base, target]) != base:   # ValueError on another drive
            return None
        with open(target, encoding="utf-8", errors="replace") as handle:
            return handle.read(TRANSCRIPT_MAX_CHARS)
    except (OSError, ValueError):
        return None


def call_exists(conn, call_uuid):
    return conn.execute("SELECT 1 FROM calls WHERE call_uuid = ?", (call_uuid,)).fetchone() is not None


def _decoded_analysis(conn, call_uuid):
    cursor = conn.execute("SELECT * FROM call_analysis WHERE call_uuid = ?", (call_uuid,))
    row = cursor.fetchone()
    if row is None:
        return None
    analysis = dict(zip([d[0] for d in cursor.description], row))
    analysis.pop("raw_json", None)
    for name in call_store.LIST_FIELDS:
        analysis[name] = call_store._json_list(analysis.get(name))
    for name in call_store.BOOL_FIELDS:
        if analysis.get(name) is not None:
            analysis[name] = bool(analysis[name])
    return analysis


def call_detail(conn, call_uuid, transcript_root):
    """Everything about one call, or None for an unknown id. The admin sees costs and the transcript."""
    cursor = conn.execute("SELECT * FROM calls WHERE call_uuid = ?", (call_uuid,))
    row = cursor.fetchone()
    if row is None:
        return None
    call = dict(zip([d[0] for d in cursor.description], row))
    path = call.pop("transcript_path", None)
    for name in ("programs_looked_up", "plivo_b_legs"):
        call[name] = call_store._json_list(call.get(name))
    parts = conn.execute(
        "SELECT c.gemini_cost_usd, c.stt_cost_usd, c.nlp_cost_usd, c.plivo_cost_usd, "
        f"CASE WHEN c.gemini_cost_usd IS NULL THEN {EARLIER_SQL} END, {COST_SQL} "
        "FROM calls c WHERE c.call_uuid = ?", (call_uuid,)).fetchone()
    names = ("gemini", "stt", "haiku", "plivo", "earlier", "total")
    cost = {name: (None if value is None else _money(value)) for name, value in zip(names, parts)}
    cost["plivo_status"] = call.get("plivo_cost_status")
    campaign = conn.execute("SELECT cp.id, cp.name FROM campaign_contacts k JOIN campaigns cp ON cp.id = k.campaign_id "
                            "WHERE k.call_uuid = ? LIMIT 1", (call_uuid,)).fetchone()
    return {"call": call, "cost": cost, "analysis": _decoded_analysis(conn, call_uuid),
            "campaign": {"id": campaign[0], "name": campaign[1]} if campaign else None,
            "transcript": read_transcript(path, transcript_root)}
```

- [ ] **Step 4: Run the tests**

Run: `venv312/Scripts/python.exe -m unittest tests.test_admin_data`
Expected: `Ran 39 tests ... OK`.

- [ ] **Step 5: Full suite**

Run: `venv312/Scripts/python.exe -m unittest discover -s tests -t .`
Expected: 1034 run, only the 3 known failures.

---

### Task 3: Campaign reports and the do-not-call list

**Files:**
- Modify: `admin_data.py` (append)
- Test: `tests/test_admin_data.py`

**Interfaces:**
- Consumes: Tasks 1-2 (`_range_sql`, `COST_SQL`, `_money`, `_text`, `_like`, `AdminError`, `NotFound`), `campaigns.normalise_phone`, `call_store.now_ist`.
- Produces:
  - `campaign_reports(conn, start, end) -> list` of dicts with keys `id, name, state, created_at, contacts, dialed, reached, hot, visits, cost_usd, cost_per_hot_lead`.
  - `check_search(args) -> str | None`, `list_dnc(conn, q=None) -> list` of `{"phone", "added_at", "note"}`, `add_dnc(conn, phone, note, now_text=None) -> number`, `remove_dnc(conn, phone) -> number` (raises `NotFound`).

- [ ] **Step 1: Write the failing tests**

In `tests/test_admin_data.py`, insert this block immediately before the `if __name__ == "__main__":` line (after the Task 2 block):

```python
class TestCampaignReports(DbCase):
    def test_funnel_and_cost(self):
        add_call(self.conn, "c1", gemini=0.1, stt=0.01, nlp=0.002, plivo=0.02)
        add_call(self.conn, "c2", gemini=0.2)
        add_call(self.conn, "c3", connected=False)
        add_analysis(self.conn, "c1", lead_status="hot", campus_visit_agreed=1)
        add_analysis(self.conn, "c2", lead_status="warm", campus_visit_agreed=0)
        campaign_id = add_campaign_row(self.conn, contacts=[
            ("9000000001", "c1", "2026-10-12 10:00:00"), ("9000000002", "c2", "2026-10-12 10:01:00"),
            ("9000000003", "c3", "2026-10-12 10:02:00"), ("9000000004", None, None)])
        (row,) = admin_data.campaign_reports(self.conn, None, None)
        self.assertEqual((row["id"], row["name"], row["state"]), (campaign_id, "October list", "done"))
        self.assertEqual((row["contacts"], row["dialed"], row["reached"], row["hot"], row["visits"]), (4, 3, 2, 1, 1))
        self.assertAlmostEqual(row["cost_usd"], 0.332)
        self.assertAlmostEqual(row["cost_per_hot_lead"], 0.332)

    def test_no_hot_lead_means_no_cost_per_hot_lead(self):
        add_call(self.conn, "c1", gemini=0.1)
        add_campaign_row(self.conn, contacts=[("9000000001", "c1", "2026-10-12 10:00:00")])
        self.assertIsNone(admin_data.campaign_reports(self.conn, None, None)[0]["cost_per_hot_lead"])

    def test_an_empty_campaign_lists_with_zeros(self):
        add_campaign_row(self.conn)
        (row,) = admin_data.campaign_reports(self.conn, None, None)
        self.assertEqual((row["contacts"], row["dialed"], row["reached"], row["hot"], row["cost_usd"]), (0, 0, 0, 0, 0))

    def test_the_range_is_the_campaign_creation_day_and_newest_comes_first(self):
        add_campaign_row(self.conn, "old", "2026-09-01 09:00:00")
        add_campaign_row(self.conn, "mid", "2026-10-08 09:00:00")
        add_campaign_row(self.conn, "new", "2026-10-12 09:00:00")
        got = admin_data.campaign_reports(self.conn, "2026-10-07", "2026-10-13")
        self.assertEqual([r["name"] for r in got], ["new", "mid"])


class TestDoNotCall(DbCase):
    def numbers(self, q=None):
        return [r["phone"] for r in admin_data.list_dnc(self.conn, q)]

    def test_add_normalises_and_lists_newest_first(self):
        self.assertEqual(admin_data.add_dnc(self.conn, "+91 98765-43210", "asked us to stop", "2026-10-12 10:00:00"),
                         "9876543210")
        admin_data.add_dnc(self.conn, 9000000001, "", "2026-10-13 10:00:00")
        self.assertEqual(self.numbers(), ["9000000001", "9876543210"])
        self.assertEqual(admin_data.list_dnc(self.conn)[1]["note"], "asked us to stop")

    def test_a_bad_number_or_long_note_is_refused(self):
        for bad in ("12345", "", None, "abcdefghij"):
            with self.assertRaises(admin_data.AdminError, msg=bad):
                admin_data.add_dnc(self.conn, bad, "")
        with self.assertRaises(admin_data.AdminError):
            admin_data.add_dnc(self.conn, "9876543210", "x" * 201)
        self.assertEqual(self.numbers(), [])

    def test_adding_again_keeps_the_first_row_and_a_given_note_replaces_the_note(self):
        admin_data.add_dnc(self.conn, "9876543210", "first", "2026-10-12 10:00:00")
        admin_data.add_dnc(self.conn, "9876543210", "", "2026-10-13 10:00:00")
        self.assertEqual(admin_data.list_dnc(self.conn)[0]["note"], "first")
        admin_data.add_dnc(self.conn, "09876543210", "second", "2026-10-13 11:00:00")
        (row,) = admin_data.list_dnc(self.conn)
        self.assertEqual((row["note"], row["added_at"]), ("second", "2026-10-12 10:00:00"))

    def test_a_number_added_by_hand_in_another_format_is_matched(self):
        self.conn.execute("INSERT INTO do_not_call (phone, added_at) VALUES ('+91 90000 00001', '2026-10-01 10:00:00')")
        admin_data.add_dnc(self.conn, "9000000001", "dup")
        self.assertEqual(len(admin_data.list_dnc(self.conn)), 1)
        self.assertEqual(admin_data.remove_dnc(self.conn, "9000000001"), "9000000001")
        self.assertEqual(self.numbers(), [])

    def test_remove(self):
        admin_data.add_dnc(self.conn, "9876543210", "")
        admin_data.remove_dnc(self.conn, "98765 43210")
        self.assertEqual(self.numbers(), [])
        with self.assertRaises(admin_data.NotFound):
            admin_data.remove_dnc(self.conn, "9876543210")
        with self.assertRaises(admin_data.AdminError):
            admin_data.remove_dnc(self.conn, "junk")

    def test_search_matches_number_or_note_literally(self):
        admin_data.add_dnc(self.conn, "9876543210", "parent called")
        admin_data.add_dnc(self.conn, "9000000001", "100% sure")
        self.assertEqual(self.numbers("43210"), ["9876543210"])
        self.assertEqual(self.numbers("parent"), ["9876543210"])
        self.assertEqual(self.numbers("%"), ["9000000001"])
        self.assertEqual(self.numbers("zzz"), [])

    def test_search_text_is_checked(self):
        self.assertIsNone(admin_data.check_search({}))
        self.assertEqual(admin_data.check_search({"q": " 98 "}), "98")
        with self.assertRaises(admin_data.AdminError):
            admin_data.check_search({"q": "x" * 101})
```

- [ ] **Step 2: Run them to verify they fail**

Run: `venv312/Scripts/python.exe -m unittest tests.test_admin_data`
Expected: ERRORs, `AttributeError: module 'admin_data' has no attribute 'campaign_reports'`.

- [ ] **Step 3: Append the implementation to `admin_data.py`**

Append this to the end of the file:

```python
# --- campaign reports ---

def campaign_reports(conn, start, end):
    """One row per campaign created in the range: the funnel and what its calls cost.

    campaign_contacts.call_uuid holds a contact's last attempt only; an earlier unanswered try costs ~0.
    """
    where, args = _range_sql(start, end, "cp.created_at")
    rows = conn.execute(
        "SELECT cp.id, cp.name, cp.state, cp.created_at, COUNT(k.id), SUM(k.dialed_at IS NOT NULL), "
        f"SUM(c.connected_at IS NOT NULL), SUM(a.lead_status = 'hot'), SUM(a.campus_visit_agreed = 1), SUM({COST_SQL}) "
        "FROM campaigns cp LEFT JOIN campaign_contacts k ON k.campaign_id = cp.id "
        "LEFT JOIN calls c ON c.call_uuid = k.call_uuid LEFT JOIN call_analysis a ON a.call_uuid = k.call_uuid "
        f"WHERE 1=1{where} GROUP BY cp.id ORDER BY cp.created_at DESC, cp.id DESC", args).fetchall()
    out = []
    for row in rows:
        contacts, dialed, reached, hot, visits = (int(v or 0) for v in row[4:9])
        cost = _money(row[9])
        out.append({"id": row[0], "name": row[1], "state": row[2], "created_at": row[3], "contacts": contacts,
                    "dialed": dialed, "reached": reached, "hot": hot, "visits": visits, "cost_usd": cost,
                    "cost_per_hot_lead": _money(cost / hot) if hot else None})
    return out


# --- do-not-call ---

def check_search(args):
    """The search box of the do-not-call list: text up to 100 characters, or None."""
    return _text(args, "q", 100)


def list_dnc(conn, q=None):
    sql = "SELECT phone, added_at, note FROM do_not_call"
    args = []
    if q:
        sql += " WHERE phone LIKE ? ESCAPE '!' OR COALESCE(note, '') LIKE ? ESCAPE '!'"
        args = [_like(q)] * 2
    return [{"phone": r[0], "added_at": r[1], "note": r[2]}
            for r in conn.execute(sql + " ORDER BY added_at DESC, phone", args)]


def _dnc_rows(conn, number):
    """The stored rows for a number. A row added by hand may hold +91 or spaces; match on the normal form."""
    return [r[0] for r in conn.execute("SELECT phone FROM do_not_call") if campaigns.normalise_phone(r[0]) == number]


def _dnc_number(phone):
    number = campaigns.normalise_phone(phone)
    if number is None:
        raise AdminError("Enter a 10-digit Indian mobile number")
    return number


def add_dnc(conn, phone, note, now_text=None):
    """Add a number (kept as 10 digits). Already listed: the first row stays and a given note replaces its note."""
    number = _dnc_number(phone)
    note = str(note or "").strip()
    if len(note) > 200:
        raise AdminError("note is longer than 200 characters")
    stored = _dnc_rows(conn, number)
    if not stored:
        conn.execute("INSERT INTO do_not_call (phone, added_at, note) VALUES (?, ?, ?)",
                     (number, now_text or call_store.now_ist(), note))
    elif note:
        conn.executemany("UPDATE do_not_call SET note = ? WHERE phone = ?", [(note, p) for p in stored])
    return number


def remove_dnc(conn, phone):
    number = _dnc_number(phone)
    stored = _dnc_rows(conn, number)
    if not stored:
        raise NotFound("That number is not on the list")
    conn.executemany("DELETE FROM do_not_call WHERE phone = ?", [(p,) for p in stored])
    return number
```

- [ ] **Step 4: Run the tests**

Run: `venv312/Scripts/python.exe -m unittest tests.test_admin_data`
Expected: `Ran 50 tests ... OK`.

- [ ] **Step 5: Full suite**

Run: `venv312/Scripts/python.exe -m unittest discover -s tests -t .`
Expected: 1045 run, only the 3 known failures.

---

### Task 4: CSV and the read routes

**Files:**
- Modify: `admin_data.py` (append), `app.py`, `web_auth.py`, `tests/harness/appctl.py`, `tests/test_preservation_4_8_resumption_recording_stats.py`
- Test: `tests/test_admin_data.py`, `tests/test_admin_api.py` (create)

**Interfaces:**
- Consumes: Tasks 1-3 (`parse_range`, `check_filters`, `list_calls`, `export_calls`, `call_detail`, `campaign_reports`, `check_search`, `list_dnc`, `AdminError`, `NotFound`), `call_store.run`, `call_transcript.TRANSCRIPT_DIR`, app.py's `ist_tz`, `Response`, `request`, `CALL_HISTORY_DB_PATH`.
- Produces:
  - `admin_data`: `csv_cell(value)`, `csv_bytes(headers, rows, note=None) -> bytes`, `calls_csv(rows)`, `export_calls_csv(conn, start, end, filters) -> bytes` (query and file in one thread-pool call), `campaigns_csv(rows)`, `dnc_csv(rows)`, `CSV_CUT_NOTE`, `CALLS_CSV_HEADERS`, `CAMPAIGNS_CSV_HEADERS`, `DNC_CSV_HEADERS`.
  - app.py helpers `admin_range()`, `admin_page_number()`, `admin_error(e)`, `csv_reply(body, prefix, start, end)`; routes (endpoint = function name): `admin_overview_api`, `admin_calls_api`, `admin_calls_csv`, `admin_call_detail_api`, `admin_campaigns_api`, `admin_campaigns_csv`, `admin_dnc_api`, `admin_dnc_csv`.
  - Test helper in `tests/test_admin_api.py`: `AdminApiCase` (`seed(fn)`, `call(role, method, path, **kwargs) -> (status, json)`, `raw(role, path) -> (status, headers, bytes)`, `get(role, path) -> (status, json)`), `now_text()`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_admin_data.py`, insert this block immediately before the `if __name__ == "__main__":` line (after the Task 3 block):

```python
class TestCsvCell(unittest.TestCase):
    def test_formula_starts_get_a_leading_quote(self):
        for text in ("=1+1", "+1+1", "-5+2", "@SUM(A1)", "\tcmd", "\rcmd", "=", "+", "-", "+91 98765"):
            self.assertEqual(admin_data.csv_cell(text), "'" + text, text)

    def test_plain_text_numbers_and_phones_pass_through(self):
        for value, shown in (("Asha", "Asha"), ("a=b", "a=b"), ("", ""), (None, ""), (12, "12"), (0.132, "0.132"),
                             ("9876543210", "9876543210"), ("+919876543210", "+919876543210"),
                             ("আশা", "আশা"), (True, "True")):
            self.assertEqual(admin_data.csv_cell(value), shown, value)

    def test_export_calls_csv_queries_and_writes_in_one_step(self):
        conn = new_db()
        self.addCleanup(conn.close)
        add_call(conn, "a", name="Asha")
        body = admin_data.export_calls_csv(conn, None, None, NO_FILTERS)
        rows = body.decode("utf-8-sig").split("\r\n")
        self.assertEqual((rows[0].split(",")[0], rows[1].split(",")[:3]), ("Call ID", ["a", "2026-10-12 10:00:00", "Asha"]))

    def test_the_file_has_a_bom_crlf_lines_and_quotes_commas(self):
        body = admin_data.csv_bytes(["A", "B"], [["x,y", 'he said "hi"'], ["line\nbreak", None]], note="cut")
        self.assertTrue(body.startswith(b"\xef\xbb\xbf"))
        text = body.decode("utf-8-sig")
        self.assertEqual(text, 'A,B\r\n"x,y","he said ""hi"""\r\n"line\nbreak",\r\ncut\r\n')
```

Create `tests/test_admin_api.py` with this content (Task 5 adds classes before the last two lines):

```python
"""The /api/admin/ routes: access, ranges, filters, CSV (docs/spec/admin-panel-design.md §2)."""

from __future__ import annotations

import csv
import io
import os
import shutil
import sqlite3
import tempfile
import unittest
from datetime import datetime
from unittest import mock

import admin_data
import call_analysis
from tests.harness.appctl import LogCapture, app
from tests.test_access_control import AuthTestCase, run
from tests.test_admin_data import add_analysis, add_call, add_campaign_row

web_auth = app.web_auth


def now_text():
    return datetime.now(app.ist_tz).strftime("%Y-%m-%d %H:%M:%S")


class AdminApiCase(AuthTestCase):
    def seed(self, fn):
        conn = sqlite3.connect(app.CALL_HISTORY_DB_PATH)
        try:
            with conn:
                return fn(conn)
        finally:
            conn.close()

    def call(self, role, method, path, **kwargs):
        async def go():
            client = await self.client(role)
            response = await getattr(client, method)(path, **kwargs)
            return response.status_code, await response.get_json()
        return run(go())

    def raw(self, role, path):
        async def go():
            client = await self.client(role)
            response = await client.get(path)
            return response.status_code, response.headers, await response.get_data()
        return run(go())

    def get(self, role, path):   # noqa: D102 (AuthTestCase.get returns text; the API tests want JSON)
        return self.call(role, "get", path)


class TestAccess(AdminApiCase):
    PATHS = ("/api/admin/overview", "/api/admin/calls", "/api/admin/calls.csv", "/api/admin/calls/u1",
             "/api/admin/campaigns", "/api/admin/campaigns.csv", "/api/admin/dnc", "/api/admin/dnc.csv")

    def setUp(self):
        super().setUp()
        self.seed(lambda conn: add_call(conn, "u1", now_text()))

    def test_no_login_is_401_an_operator_is_403_and_an_admin_gets_in(self):
        for path in self.PATHS:
            self.assertEqual(self.raw(None, path)[0], 401, path)
            self.assertEqual(self.raw("operator", path)[0], 403, path)
            self.assertEqual(self.raw("admin", path)[0], 200, path)


class TestOverviewApi(AdminApiCase):
    def test_the_default_range_is_the_last_seven_days_and_all_time_is_everything(self):
        self.seed(lambda conn: (add_call(conn, "new", now_text(), gemini=0.1),
                                add_call(conn, "old", "2020-01-01 10:00:00", gemini=0.2)))
        status, body = self.get("admin", "/api/admin/overview")
        self.assertEqual((status, body["calls"]), (200, 1))
        self.assertIsNotNone(body["range"]["from"])
        self.assertAlmostEqual(body["cost"]["total"], 0.1)
        status, body = self.get("admin", "/api/admin/overview?all=1")
        self.assertEqual((body["calls"], body["range"]), (2, {"from": None, "to": None}))

    def test_a_custom_range(self):
        self.seed(lambda conn: add_call(conn, "a", "2026-10-05 10:00:00"))
        status, body = self.get("admin", "/api/admin/overview?from=2026-10-05&to=2026-10-05")
        self.assertEqual((status, body["calls"]), (200, 1))
        self.assertEqual(body["range"], {"from": "2026-10-05", "to": "2026-10-05"})

    def test_a_bad_range_is_400_with_a_message(self):
        for query in ("from=yesterday&to=today", "from=2026-10-13&to=2026-10-01", "from=2026-10-01"):
            status, body = self.get("admin", "/api/admin/overview?" + query)
            self.assertEqual(status, 400, query)
            self.assertEqual(body["status"], "error")
            self.assertTrue(body["message"])


class TestCallsApi(AdminApiCase):
    def setUp(self):
        super().setUp()

        def seed(conn):
            add_call(conn, "a", now_text(), name="Asha", gemini=0.1)
            add_call(conn, "b", now_text(), name="Bimal", gemini=0.2)
            add_analysis(conn, "a", lead_status="hot", primary_program="CSE")
        self.seed(seed)

    def test_a_page_with_a_filter(self):
        status, body = self.get("admin", "/api/admin/calls?lead=hot")
        self.assertEqual(status, 200)
        self.assertEqual([r["call_uuid"] for r in body["rows"]], ["a"])
        self.assertEqual((body["total"], body["page"], body["programs"]), (1, 1, ["CSE"]))

    def test_bad_filters_and_pages_are_400(self):
        for query in ("lead=boiling", "analysis=maybe", "visit=x", "campaign=abc", "page=two", "from=bad&to=bad"):
            self.assertEqual(self.get("admin", "/api/admin/calls?" + query)[0], 400, query)

    def test_an_out_of_range_page_is_empty_not_an_error(self):
        status, body = self.get("admin", "/api/admin/calls?page=99999999999")
        self.assertEqual((status, body["rows"]), (200, []))


class TestCsv(AdminApiCase):
    def parse(self, body):
        self.assertTrue(body.startswith(b"\xef\xbb\xbf"), "the file starts with a UTF-8 BOM")
        return list(csv.reader(io.StringIO(body.decode("utf-8-sig"))))

    def test_calls_csv(self):
        self.seed(lambda conn: (add_call(conn, "a", now_text(), name="আশা দাস", gemini=0.1, phone="+919876543210"),
                                add_call(conn, "b", now_text(), name="=HYPERLINK(1)", gemini=0.2)))
        status, headers, body = self.raw("admin", "/api/admin/calls.csv?from=2026-01-01&to=2099-12-31")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/csv; charset=utf-8")
        self.assertEqual(headers["Content-Disposition"], 'attachment; filename="calls_2026-01-01_2099-12-31.csv"')
        rows = self.parse(body)
        self.assertEqual(rows[0], list(admin_data.CALLS_CSV_HEADERS))
        by_id = {r[0]: r for r in rows[1:]}
        self.assertEqual(by_id["a"][2], "আশা দাস")
        self.assertEqual(by_id["a"][3], "+919876543210")   # a phone is not escaped
        self.assertEqual(by_id["b"][2], "'=HYPERLINK(1)")   # a formula is
        self.assertEqual(len(rows), 3)

    def test_the_all_time_file_name(self):
        headers = self.raw("admin", "/api/admin/calls.csv?all=1")[1]
        self.assertEqual(headers["Content-Disposition"], 'attachment; filename="calls_all.csv"')

    def test_filters_apply_to_the_export_and_a_bad_one_is_400(self):
        self.seed(lambda conn: (add_call(conn, "a", now_text()), add_call(conn, "b", now_text()),
                                add_analysis(conn, "a", lead_status="hot")))
        rows = self.parse(self.raw("admin", "/api/admin/calls.csv?lead=hot")[2])
        self.assertEqual([r[0] for r in rows[1:]], ["a"])
        self.assertEqual(self.raw("admin", "/api/admin/calls.csv?lead=boiling")[0], 400)

    def test_a_long_export_is_cut_with_a_note(self):
        self.seed(lambda conn: [add_call(conn, f"u{i}", now_text()) for i in range(5)])
        with mock.patch.object(admin_data, "CSV_MAX_ROWS", 3):
            rows = self.parse(self.raw("admin", "/api/admin/calls.csv")[2])
        self.assertEqual(len(rows), 1 + 3 + 1)
        self.assertEqual(rows[-1], [admin_data.CSV_CUT_NOTE])

    def test_campaigns_and_dnc_csv(self):
        def seed(conn):
            add_call(conn, "c1", now_text(), gemini=0.1)
            add_campaign_row(conn, "October list", now_text(), contacts=[("9000000001", "c1", now_text())])
            admin_data.add_dnc(conn, "9876543210", "=cmd|' /C calc'!A0")
        self.seed(seed)
        status, headers, body = self.raw("admin", "/api/admin/campaigns.csv")
        self.assertEqual(headers["Content-Disposition"].split("filename=")[1].startswith('"campaigns_'), True)
        rows = self.parse(body)
        self.assertEqual((rows[0], rows[1][1], rows[1][4]), (list(admin_data.CAMPAIGNS_CSV_HEADERS),
                                                              "October list", "1"))
        status, headers, body = self.raw("admin", "/api/admin/dnc.csv")
        self.assertEqual(headers["Content-Disposition"], 'attachment; filename="do_not_call_all.csv"')
        rows = self.parse(body)
        self.assertEqual((rows[0], rows[1][0], rows[1][2]), (list(admin_data.DNC_CSV_HEADERS), "9876543210",
                                                              "'=cmd|' /C calc'!A0"))


class TestDetailApi(AdminApiCase):
    def setUp(self):
        super().setUp()
        self.folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)
        patcher = mock.patch.object(app.call_transcript, "TRANSCRIPT_DIR", self.folder)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_call_with_its_transcript(self):
        path = os.path.join(self.folder, "a.txt")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("Neha: Hello\n")
        self.seed(lambda conn: (add_call(conn, "a", now_text(), gemini=0.1, transcript_path=path),
                                add_analysis(conn, "a", lead_status="hot", summary="Visit fixed.")))
        status, body = self.get("admin", "/api/admin/calls/a")
        self.assertEqual(status, 200)
        self.assertEqual(body["transcript"], "Neha: Hello\n")
        self.assertEqual(body["analysis"]["lead_status"], "hot")
        self.assertAlmostEqual(body["cost"]["total"], 0.1)

    def test_an_unknown_call_is_404(self):
        status, body = self.get("admin", "/api/admin/calls/nope")
        self.assertEqual((status, body["status"]), (404, "error"))

    def test_an_id_that_tries_to_leave_the_route_is_404(self):
        self.assertEqual(self.get("admin", "/api/admin/calls/..%2F..%2Fetc%2Fpasswd")[0], 404)


class TestCampaignsAndDncApi(AdminApiCase):
    def test_campaign_reports(self):
        def seed(conn):
            add_call(conn, "c1", now_text(), gemini=0.1)
            add_analysis(conn, "c1", lead_status="hot")
            add_campaign_row(conn, "October list", now_text(), contacts=[("9000000001", "c1", now_text())])
        self.seed(seed)
        status, body = self.get("admin", "/api/admin/campaigns")
        self.assertEqual(status, 200)
        self.assertEqual((body["campaigns"][0]["name"], body["campaigns"][0]["hot"]), ("October list", 1))
        self.assertEqual(self.get("admin", "/api/admin/campaigns?from=bad&to=bad")[0], 400)

    def test_dnc_list_and_search(self):
        self.seed(lambda conn: (admin_data.add_dnc(conn, "9876543210", "parent called"),
                                admin_data.add_dnc(conn, "9000000001", "")))
        self.assertEqual(len(self.get("admin", "/api/admin/dnc")[1]["numbers"]), 2)
        self.assertEqual([n["phone"] for n in self.get("admin", "/api/admin/dnc?q=parent")[1]["numbers"]],
                         ["9876543210"])
        self.assertEqual(self.get("admin", "/api/admin/dnc?q=" + "x" * 101)[0], 400)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run them to verify they fail**

Run: `venv312/Scripts/python.exe -m unittest tests.test_admin_data tests.test_admin_api`
Expected: failures: `AttributeError: module 'admin_data' has no attribute 'csv_cell'`, and the API tests get 404 for the missing routes.

- [ ] **Step 3: Append the CSV code to `admin_data.py`**

Append this to the end of the file:

```python
# --- CSV ---

CSV_CUT_NOTE = "Export stopped at 20,000 rows. Narrow the range or the filters to get the rest."
_PHONE_CELL = re.compile(r"\+?[0-9]+")


def csv_cell(value):
    """A cell as text. One that starts like a formula gets a leading ' (names and extra columns come
    from uploaded Excel files); a bare phone number, which may start with +, is left alone."""
    if value is None:
        return ""
    text = str(value)
    if text and text[0] in "=+-@\t\r" and not _PHONE_CELL.fullmatch(text):
        text = "'" + text
    return text


def csv_bytes(headers, rows, note=None):
    """UTF-8 with a BOM (Excel then shows Bengali text), CRLF lines, an optional last line of note."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(headers)
    for row in rows:
        writer.writerow([csv_cell(value) for value in row])
    if note:
        writer.writerow([note])
    return out.getvalue().encode("utf-8-sig")


CALLS_CSV_HEADERS = ("Call ID", "Time", "Name", "Phone", "Lead", "Program", "School", "Visit day", "Score",
                     "Cost (USD)", "Analysis")
CAMPAIGNS_CSV_HEADERS = ("ID", "Campaign", "State", "Created", "Contacts", "Dialed", "Reached", "Hot", "Visits",
                         "Cost (USD)", "Cost per hot lead (USD)")
DNC_CSV_HEADERS = ("Phone", "Added", "Note")


def calls_csv(rows):
    """`rows` is export_calls(): up to one row past the cap, which is cut off and noted."""
    cut = len(rows) > CSV_MAX_ROWS
    return csv_bytes(CALLS_CSV_HEADERS, [[r[key] for key in CALL_ROW_KEYS] for r in rows[:CSV_MAX_ROWS]],
                     CSV_CUT_NOTE if cut else None)


def export_calls_csv(conn, start, end, filters):
    """Query and file in one call, so app.py runs both in the thread pool: 20,000 rows take ~0.1 s,
    too long for the event loop of 8000, which also carries live call audio."""
    return calls_csv(export_calls(conn, start, end, filters))


def campaigns_csv(rows):
    keys = ("id", "name", "state", "created_at", "contacts", "dialed", "reached", "hot", "visits", "cost_usd",
            "cost_per_hot_lead")
    return csv_bytes(CAMPAIGNS_CSV_HEADERS, [[r[key] for key in keys] for r in rows])


def dnc_csv(rows):
    return csv_bytes(DNC_CSV_HEADERS, [[r["phone"], r["added_at"], r["note"]] for r in rows])
```

- [ ] **Step 4: Wire the routes**

In `app.py`, after the line `import campaigns` (in the step 2/3 import block) add:

```python
import admin_data   # step 4: the admin panel's queries
```

In `init_call_history_db`, after the line `campaigns.upgrade(conn)    # step 3 campaign tables; every process, safe on every start` add (same indentation):

```python
        admin_data.upgrade(conn)   # step 4 index on calls.created_at; same
```

In `app.py`, immediately before the line `@app.before_serving` that precedes `async def start_background_work():`, insert:

```python
# --- Step 4: admin panel (docs/spec/admin-panel-design.md). Every route is ADMIN_API in web_auth. ---

def admin_range():
    """(from, to) of the request: ?from=&to= (IST dates), ?all=1, or the last 7 days."""
    args = request.args
    return admin_data.parse_range(args.get("from"), args.get("to"), args.get("all") == "1",
                                  datetime.now(ist_tz).date())


def admin_page_number():
    try:
        return min(max(1, int(request.args.get("page", "1"))), 10**6)   # a huge OFFSET overflows SQLite
    except ValueError:
        raise admin_data.AdminError("page must be a number") from None


def admin_error(e):
    return {"status": "error", "message": str(e)}, 404 if isinstance(e, admin_data.NotFound) else 400


def csv_reply(body, prefix, start, end):
    name = f"{prefix}_all.csv" if start is None else f"{prefix}_{start}_{end}.csv"
    return Response(body, content_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.route("/api/admin/overview", methods=["GET"])
async def admin_overview_api():
    try:
        start, end = admin_range()
    except admin_data.AdminError as e:
        return admin_error(e)
    data = await call_store.run(CALL_HISTORY_DB_PATH, admin_data.overview, start, end)
    return {"range": {"from": start, "to": end}, **data}


@app.route("/api/admin/calls", methods=["GET"])
async def admin_calls_api():
    try:
        start, end = admin_range()
        filters = admin_data.check_filters(request.args)
        page = admin_page_number()
    except admin_data.AdminError as e:
        return admin_error(e)
    data = await call_store.run(CALL_HISTORY_DB_PATH, admin_data.list_calls, start, end, filters, page)
    return {"range": {"from": start, "to": end}, **data}


@app.route("/api/admin/calls.csv", methods=["GET"])
async def admin_calls_csv():
    try:
        start, end = admin_range()
        filters = admin_data.check_filters(request.args)
    except admin_data.AdminError as e:
        return admin_error(e)
    body = await call_store.run(CALL_HISTORY_DB_PATH, admin_data.export_calls_csv, start, end, filters)
    return csv_reply(body, "calls", start, end)


@app.route("/api/admin/calls/<call_uuid>", methods=["GET"])
async def admin_call_detail_api(call_uuid):
    found = await call_store.run(CALL_HISTORY_DB_PATH, admin_data.call_detail, call_uuid,
                                 call_transcript.TRANSCRIPT_DIR)
    if found is None:
        return admin_error(admin_data.NotFound("No such call"))
    return found


@app.route("/api/admin/campaigns", methods=["GET"])
async def admin_campaigns_api():
    try:
        start, end = admin_range()
    except admin_data.AdminError as e:
        return admin_error(e)
    rows = await call_store.run(CALL_HISTORY_DB_PATH, admin_data.campaign_reports, start, end)
    return {"range": {"from": start, "to": end}, "campaigns": rows}


@app.route("/api/admin/campaigns.csv", methods=["GET"])
async def admin_campaigns_csv():
    try:
        start, end = admin_range()
    except admin_data.AdminError as e:
        return admin_error(e)
    rows = await call_store.run(CALL_HISTORY_DB_PATH, admin_data.campaign_reports, start, end)
    return csv_reply(admin_data.campaigns_csv(rows), "campaigns", start, end)


@app.route("/api/admin/dnc", methods=["GET"])
async def admin_dnc_api():
    try:
        q = admin_data.check_search(request.args)
    except admin_data.AdminError as e:
        return admin_error(e)
    return {"numbers": await call_store.run(CALL_HISTORY_DB_PATH, admin_data.list_dnc, q)}


@app.route("/api/admin/dnc.csv", methods=["GET"])
async def admin_dnc_csv():
    rows = await call_store.run(CALL_HISTORY_DB_PATH, admin_data.list_dnc, None)
    return csv_reply(admin_data.dnc_csv(rows), "do_not_call", None, None)
```

In `web_auth.py`, in `ENDPOINT_ACCESS`, after the `"campaign_settings_api": OPERATOR_API,` line add:

```python
    "admin_overview_api": ADMIN_API,
    "admin_calls_api": ADMIN_API,
    "admin_calls_csv": ADMIN_API,
    "admin_call_detail_api": ADMIN_API,
    "admin_campaigns_api": ADMIN_API,
    "admin_campaigns_csv": ADMIN_API,
    "admin_dnc_api": ADMIN_API,
    "admin_dnc_csv": ADMIN_API,
```

In `tests/harness/appctl.py`, in `DIAGNOSTIC_LOG_TAGS`, after the `"[CAMPAIGN]", ...` line add:

```python
    "[ADMIN]",        # step 4: admin panel actions (re-run analysis, do-not-call changes)
```

- [ ] **Step 5: Run the tests**

Run: `venv312/Scripts/python.exe -m unittest tests.test_admin_data tests.test_admin_api tests.test_access_control`
Expected: all OK (54 + 17 admin tests; `TestDefaultDeny` passes because `ENDPOINT_ACCESS` and `app.url_map` still match).

- [ ] **Step 6: Re-pin the `GEMINI_MODEL` lines**

The import and the upgrade call move app.py's `GEMINI_MODEL` sites down by 1 and 2 lines. In `tests/test_preservation_4_8_resumption_recording_stats.py`, `test_model_identifier_is_recorded`, replace:

```python
        self.assertEqual(references, [108, 1906, 2428, 2438, 2869])
```

with:

```python
        # And again for the admin panel (import admin_data, its upgrade call in init_call_history_db).
        self.assertEqual(references, [109, 1908, 2430, 2440, 2871])
```

If the baseline differs from the one in Global Constraints, run the test once, read the "Lists differ" message and use the printed actual list. Tasks 5 and 7 do not move these lines (Task 5 adds routes far below them; Task 7 edits lines in place).

- [ ] **Step 7: Full suite**

Run: `venv312/Scripts/python.exe -m unittest discover -s tests -t .`
Expected: 1066 run, only the 3 known failures.

---

### Task 5: Re-run analysis and do-not-call writes

**Files:**
- Modify: `admin_data.py`, `app.py`, `web_auth.py`, `call_analysis.py`
- Test: `tests/test_admin_api.py`

**Interfaces:**
- Consumes: Task 4's routes and helpers, `app.ANALYZER`, `app.json_body()`, `call_analysis.Analyzer.enqueue`, `admin_data.add_dnc/remove_dnc/call_exists/RERUN_MAX`.
- Produces: `Analyzer.is_busy(call_uuid) -> bool`; `admin_data.failed_analyses(conn) -> list[(call_uuid, path or None)]` (newest first, all dates; `None` when the transcript file is missing); `admin_data.transcript_file(conn, call_uuid) -> path | None`; the rerun-failed reply `{"status", "queued", "busy", "remaining", "no_transcript"}` (202); a single re-run of a call without a transcript file is 409; routes `admin_dnc_add_api` (`POST /api/admin/dnc`), `admin_dnc_remove_api` (`POST /api/admin/dnc/remove`), `admin_rerun_api` (`POST /api/admin/calls/<call_uuid>/rerun`), `admin_rerun_failed_api` (`POST /api/admin/rerun-failed`).

- [ ] **Step 1: Write the failing tests**

In `tests/test_admin_api.py`, insert this block immediately before the `if __name__ == "__main__":` line:

```python
class WriteCase(AdminApiCase):
    def setUp(self):
        super().setUp()
        self.analyzer = call_analysis.Analyzer(app.CALL_HISTORY_DB_PATH)
        patcher = mock.patch.object(app, "ANALYZER", self.analyzer)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)

    def transcript(self, call_uuid):
        """A transcript file for the call; returns its path (store it as transcript_path)."""
        path = os.path.join(self.folder, f"{call_uuid}.txt")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("Neha: Hello\nUser: Yes, tell me\n")
        return path

    def post(self, role, path, body=None):
        return self.call(role, "post", path, json={} if body is None else body)

    def queued(self):
        items = []
        while not self.analyzer._queue.empty():
            items.append(self.analyzer._queue.get_nowait())
        return items


class TestAnalyzerIsBusy(unittest.TestCase):
    def test_queued_running_and_idle(self):
        analyzer = call_analysis.Analyzer(":memory:")
        self.assertFalse(analyzer.is_busy("a"))
        analyzer.enqueue("a", None)
        self.assertTrue(analyzer.is_busy("a"))
        analyzer._queued.discard("a")
        analyzer._running["a"] = [None, 1]
        self.assertTrue(analyzer.is_busy("a"))
        analyzer._running.pop("a")
        self.assertFalse(analyzer.is_busy("a"))


class TestRerun(WriteCase):
    PATH = "/api/admin/calls/a/rerun"

    def setUp(self):
        super().setUp()
        self.path = self.transcript("a")
        self.seed(lambda conn: (add_call(conn, "a", now_text(), transcript_path=self.path),
                                add_analysis(conn, "a", lead_status="hot", summary="Visit fixed.")))

    def analysis(self, call_uuid):
        return self.seed(lambda conn: conn.execute("SELECT lead_status, summary FROM call_analysis WHERE call_uuid=?",
                                                   (call_uuid,)).fetchone())

    def test_only_the_admin_may(self):
        self.assertEqual(self.post(None, self.PATH)[0], 401)
        self.assertEqual(self.post("operator", self.PATH)[0], 403)
        self.assertEqual(self.analyzer._queue.qsize(), 0)

    def test_a_non_json_post_is_415(self):
        async def go():
            client = await self.client("admin")
            return (await client.post(self.PATH, data="x=1")).status_code
        self.assertEqual(run(go()), 415)

    def test_unknown_call_is_404_and_a_server_without_a_worker_is_503(self):
        self.assertEqual(self.post("admin", "/api/admin/calls/nope/rerun")[0], 404)
        with mock.patch.object(app, "ANALYZER", None):
            status, body = self.post("admin", self.PATH)
        self.assertEqual((status, body["status"]), (503, "error"))

    def test_it_queues_once_with_the_transcript_and_leaves_the_old_analysis_alone(self):
        self.assertEqual(self.post("admin", self.PATH), (202, {"status": "queued"}))
        status, body = self.post("admin", self.PATH)
        self.assertEqual(status, 409)
        self.assertEqual(self.queued(), [("a", self.path)])
        self.assertEqual(self.analysis("a"), ("hot", "Visit fixed."))

    def test_a_call_without_a_transcript_is_409_and_its_analysis_stays(self):
        # A run with no transcript text would save no_conversation over every field (Analyzer.analyze).
        self.seed(lambda conn: (add_call(conn, "b", now_text()),
                                add_analysis(conn, "b", lead_status="warm", summary="Kept.")))
        status, body = self.post("admin", "/api/admin/calls/b/rerun")
        self.assertEqual(status, 409)
        self.assertIn("transcript", body["message"])
        self.assertEqual(self.analyzer._queue.qsize(), 0)
        self.assertEqual(self.analysis("b"), ("warm", "Kept."))

    def test_a_transcript_file_that_is_gone_is_409(self):
        os.remove(self.path)
        self.assertEqual(self.post("admin", self.PATH)[0], 409)
        self.assertEqual(self.analyzer._queue.qsize(), 0)

    def test_a_call_being_analysed_right_now_is_409(self):
        self.analyzer._running["a"] = [None, 1]
        self.assertEqual(self.post("admin", self.PATH)[0], 409)
        self.assertEqual(self.analyzer._queue.qsize(), 0)


class TestRerunFailed(WriteCase):
    PATH = "/api/admin/rerun-failed"

    def seed_failed(self, count, status="failed", prefix=None, transcript=True):
        prefix = prefix or status

        def seed(conn):
            for i in range(count):
                call_uuid = f"{prefix}{i}"
                path = self.transcript(call_uuid) if transcript else None
                add_call(conn, call_uuid, f"2026-10-12 10:00:{i:02d}", transcript_path=path)
                add_analysis(conn, call_uuid, status)
        self.seed(seed)

    def test_only_the_admin_may(self):
        self.assertEqual(self.post(None, self.PATH)[0], 401)
        self.assertEqual(self.post("operator", self.PATH)[0], 403)

    def test_no_worker_is_503(self):
        with mock.patch.object(app, "ANALYZER", None):
            self.assertEqual(self.post("admin", self.PATH)[0], 503)

    def test_it_queues_the_failed_ones_newest_first_and_not_the_skipped(self):
        self.seed_failed(3)
        self.seed_failed(2, "skipped")
        self.seed_failed(1, "ok")
        status, body = self.post("admin", self.PATH)
        self.assertEqual((status, body["queued"], body["busy"], body["remaining"]), (202, 3, 0, 0))
        self.assertEqual([item[0] for item in self.queued()], ["failed2", "failed1", "failed0"])

    def test_at_most_fifty_per_click_and_it_says_how_many_remain(self):
        self.seed_failed(60)
        status, body = self.post("admin", self.PATH)
        self.assertEqual((body["queued"], body["remaining"]), (50, 10))
        self.assertEqual(len(self.queued()), 50)
        status, body = self.post("admin", self.PATH)   # the first 50 are still queued, so they count as busy
        self.assertEqual((body["queued"], body["busy"], body["remaining"]), (10, 50, 0))

    def test_a_cap_below_the_count(self):
        self.seed_failed(5)
        with mock.patch.object(admin_data, "RERUN_MAX", 2):
            body = self.post("admin", self.PATH)[1]
        self.assertEqual((body["queued"], body["remaining"]), (2, 3))

    def test_nothing_failed_queues_nothing(self):
        body = self.post("admin", self.PATH)[1]
        self.assertEqual((body["queued"], body["busy"], body["remaining"], body["no_transcript"]), (0, 0, 0, 0))

    def test_failed_calls_without_a_transcript_are_counted_not_queued(self):
        self.seed_failed(2)
        self.seed_failed(3, prefix="nofile", transcript=False)
        body = self.post("admin", self.PATH)[1]
        self.assertEqual((body["queued"], body["remaining"], body["no_transcript"]), (2, 0, 3))
        self.assertEqual(sorted(item[0] for item in self.queued()), ["failed0", "failed1"])

    def test_the_calls_list_reports_the_failed_total_for_the_button(self):
        self.seed_failed(3)
        self.assertEqual(self.get("admin", "/api/admin/calls")[1]["failed_total"], 3)


class TestDncWrites(WriteCase):
    def test_only_the_admin_may(self):
        for path in ("/api/admin/dnc", "/api/admin/dnc/remove"):
            self.assertEqual(self.post(None, path, {"phone": "9876543210"})[0], 401, path)
            self.assertEqual(self.post("operator", path, {"phone": "9876543210"})[0], 403, path)

    def test_add_then_list_then_remove_and_the_log_is_masked(self):
        with LogCapture() as log:
            self.assertEqual(self.post("admin", "/api/admin/dnc", {"phone": "+91 98765-43210", "note": "asked"}),
                             (200, {"status": "success", "phone": "9876543210"}))
            self.assertEqual([n["phone"] for n in self.get("admin", "/api/admin/dnc")[1]["numbers"]], ["9876543210"])
            self.assertEqual(self.post("admin", "/api/admin/dnc/remove", {"phone": "9876543210"})[0], 200)
        self.assertEqual(self.get("admin", "/api/admin/dnc")[1]["numbers"], [])
        admin_lines = [line for line in log.lines if "[ADMIN]" in line]
        self.assertEqual(len(admin_lines), 2)
        self.assertTrue(all("xxxxxx3210" in line and "9876543210" not in line for line in admin_lines), admin_lines)

    def test_bad_input(self):
        for body in ({"phone": "12345"}, {"phone": ""}, {}, {"phone": "9876543210", "note": "x" * 201}):
            self.assertEqual(self.post("admin", "/api/admin/dnc", body)[0], 400, body)
        self.assertEqual(self.post("admin", "/api/admin/dnc/remove", {"phone": "junk"})[0], 400)
        self.assertEqual(self.post("admin", "/api/admin/dnc/remove", {"phone": "9876543210"})[0], 404)

    def test_a_non_json_post_is_415_and_a_list_body_is_400(self):
        async def go(**kwargs):
            client = await self.client("admin")
            return (await client.post("/api/admin/dnc", **kwargs)).status_code
        self.assertEqual(run(go(data="phone=9876543210")), 415)
        self.assertEqual(run(go(json=["9876543210"])), 400)

    def test_a_number_added_here_stops_a_campaign_upload(self):
        self.post("admin", "/api/admin/dnc", {"phone": "9876543210"})
        conn = sqlite3.connect(app.CALL_HISTORY_DB_PATH)
        self.addCleanup(conn.close)
        listed = {app.campaigns.normalise_phone(r[0]) for r in conn.execute("SELECT phone FROM do_not_call")}
        self.assertIn("9876543210", listed)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `venv312/Scripts/python.exe -m unittest tests.test_admin_api`
Expected: failures: `AttributeError: 'Analyzer' object has no attribute 'is_busy'`, and 404 for the missing POST routes.

- [ ] **Step 3: `Analyzer.is_busy`, `failed_analyses` and `transcript_file`**

In `call_analysis.py`, in class `Analyzer`, add this method right after `enqueue`:

```python
    def is_busy(self, call_uuid):
        """True while the call waits in the queue or a worker is analysing it (the admin panel's re-run check)."""
        return call_uuid in self._queued or call_uuid in self._running
```

In `admin_data.py`, insert this immediately before the line `# --- campaign reports ---`:

```python
def failed_analyses(conn):
    """(call_uuid, transcript path or None) for every failed analysis, newest first, across all dates.
    A skipped one is not retried (its transcript is missing)."""
    rows = conn.execute(
        "SELECT a.call_uuid, c.transcript_path FROM call_analysis a LEFT JOIN calls c ON c.call_uuid = a.call_uuid "
        "WHERE a.status = 'failed' ORDER BY c.created_at DESC, a.call_uuid").fetchall()
    return [(call_uuid, path if path and os.path.isfile(path) else None) for call_uuid, path in rows]


def transcript_file(conn, call_uuid):
    """The call's stored transcript path if the file is there, else None. A re-run needs it: with no
    transcript, Analyzer.analyze reads empty text and saves no_conversation over a good analysis."""
    path = call_store.transcript_path(conn, call_uuid)
    return path if path and os.path.isfile(path) else None
```

- [ ] **Step 4: The four write routes**

In `app.py`, immediately before the line `@app.before_serving` that precedes `async def start_background_work():` (after the `admin_dnc_csv` route), insert:

```python
@app.route("/api/admin/dnc", methods=["POST"])
async def admin_dnc_add_api():
    data, error = await json_body()
    if error:
        return error
    try:
        number = await call_store.run(CALL_HISTORY_DB_PATH, admin_data.add_dnc, data.get("phone"), data.get("note"))
    except admin_data.AdminError as e:
        return admin_error(e)
    logger.info(f"🛠️ [ADMIN] do-not-call added {campaigns.masked(number)}")
    return {"status": "success", "phone": number}


@app.route("/api/admin/dnc/remove", methods=["POST"])
async def admin_dnc_remove_api():
    data, error = await json_body()
    if error:
        return error
    try:
        number = await call_store.run(CALL_HISTORY_DB_PATH, admin_data.remove_dnc, data.get("phone"))
    except admin_data.AdminError as e:
        return admin_error(e)
    logger.info(f"🛠️ [ADMIN] do-not-call removed {campaigns.masked(number)}")
    return {"status": "success", "phone": number}


ANALYSIS_OFF = ({"status": "error", "message": "Analysis is switched off on this server"}, 503)


@app.route("/api/admin/calls/<call_uuid>/rerun", methods=["POST"])
async def admin_rerun_api(call_uuid):
    _, error = await json_body()
    if error:
        return error
    if not await call_store.run(CALL_HISTORY_DB_PATH, admin_data.call_exists, call_uuid):
        return admin_error(admin_data.NotFound("No such call"))
    if ANALYZER is None:
        return ANALYSIS_OFF
    # Without the transcript the worker reads empty text and saves no_conversation over a good analysis.
    path = await call_store.run(CALL_HISTORY_DB_PATH, admin_data.transcript_file, call_uuid)
    if path is None:
        return {"status": "error", "message": "No transcript to analyse for this call"}, 409
    if ANALYZER.is_busy(call_uuid):
        return {"status": "error", "message": "An analysis for this call is already queued or running"}, 409
    ANALYZER.enqueue(call_uuid, path)
    logger.info(f"🛠️ [ADMIN] analysis re-run queued call={call_uuid[:8]}")
    return {"status": "queued"}, 202


@app.route("/api/admin/rerun-failed", methods=["POST"])
async def admin_rerun_failed_api():
    _, error = await json_body()
    if error:
        return error
    if ANALYZER is None:
        return ANALYSIS_OFF
    failed = await call_store.run(CALL_HISTORY_DB_PATH, admin_data.failed_analyses)
    ready = [(call_uuid, path) for call_uuid, path in failed if path]   # no transcript: a re-run cannot help
    queued = busy = checked = 0
    for call_uuid, path in ready:
        if queued >= admin_data.RERUN_MAX:
            break
        checked += 1
        if ANALYZER.is_busy(call_uuid):
            busy += 1
        else:
            ANALYZER.enqueue(call_uuid, path)
            queued += 1
    remaining, no_transcript = len(ready) - checked, len(failed) - len(ready)
    logger.info(f"🛠️ [ADMIN] re-run of failed analyses queued={queued} busy={busy} remaining={remaining} "
                f"no_transcript={no_transcript}")
    return {"status": "queued", "queued": queued, "busy": busy, "remaining": remaining,
            "no_transcript": no_transcript}, 202
```

In `web_auth.py`, after the `"admin_dnc_csv": ADMIN_API,` line add:

```python
    "admin_dnc_add_api": ADMIN_API,
    "admin_dnc_remove_api": ADMIN_API,
    "admin_rerun_api": ADMIN_API,
    "admin_rerun_failed_api": ADMIN_API,
```

- [ ] **Step 5: Run the tests**

Run: `venv312/Scripts/python.exe -m unittest tests.test_admin_data tests.test_admin_api tests.test_access_control tests.test_analysis_worker`
Expected: all OK (`test_admin_api` now 38 tests).

- [ ] **Step 6: Full suite**

Run: `venv312/Scripts/python.exe -m unittest discover -s tests -t .`
Expected: 1087 run, only the 3 known failures.

---

### Task 6: The page, `admin.html`

**Files:**
- Modify: `admin.html` (replace the whole file)
- Test: `tests/test_admin_page.py` (create)

**Interfaces:**
- Consumes: the Task 4-5 routes by path: `GET /api/admin/overview|calls|calls.csv|calls/<uuid>|campaigns|campaigns.csv|dnc|dnc.csv` and `POST /api/admin/calls/<uuid>/rerun|rerun-failed|dnc|dnc/remove`.
- Produces: the five views (`#overview`, `#calls`, `#call/<uuid>`, `#campaigns`, `#dnc`), the range bar writing `from`/`to`/`all` into the hash.

- [ ] **Step 1: Write the failing test**

Create `tests/test_admin_page.py`:

```python
"""admin.html: static checks (docs/spec/admin-panel-design.md §3). No browser here, so the page is checked as text."""

from __future__ import annotations

import os
import re
import unittest

from tests.harness.appctl import app
from tests.test_access_control import AuthTestCase

web_auth = app.web_auth
PAGE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "admin.html")
with open(PAGE_PATH, encoding="utf-8") as handle:
    PAGE = handle.read()


class TestAdminPage(unittest.TestCase):
    def test_no_value_ever_becomes_markup(self):
        for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
            self.assertNotIn(banned, PAGE, banned)

    def test_every_element_the_script_looks_up_exists(self):
        ids = set(re.findall(r'\bid="([A-Za-z-]+)"', PAGE))
        wanted = set(re.findall(r'\$\("([A-Za-z-]+)"\)', PAGE))
        self.assertEqual(sorted(wanted - ids), [])

    def test_the_five_views_exist(self):
        self.assertIn('const VIEWS = ["overview", "calls", "call", "campaigns", "dnc"];', PAGE)
        for view in ("overview", "calls", "call", "campaigns", "dnc"):
            self.assertIn(f'id="view-{view}"', PAGE)

    def test_every_api_path_it_calls_is_an_admin_only_route(self):
        paths = set(re.findall(r'"(/api/admin/[^"?]*)', PAGE))
        self.assertTrue(paths)
        rules = {rule.rule: rule.endpoint for rule in app.app.url_map.iter_rules()}
        for path in paths:
            matches = [endpoint for rule, endpoint in rules.items()
                       if rule == path or (path.endswith("/") and rule.startswith(path))]
            self.assertTrue(matches, f"no route for {path}")
            for endpoint in matches:
                self.assertEqual(web_auth.ENDPOINT_ACCESS[endpoint], web_auth.ADMIN_API, endpoint)

    def test_the_export_links_point_at_real_csv_routes(self):
        for path in ("/api/admin/calls.csv", "/api/admin/campaigns.csv", "/api/admin/dnc.csv"):
            self.assertIn(path, PAGE)

    def test_the_rerun_poll_waits_past_pending(self):
        # The worker marks the row pending before it calls Haiku; stopping on any change said "Done." too early.
        self.assertIn('a.status !== "pending" && a.analyzed_at !== beforeAt', PAGE)

    def test_the_old_placeholder_is_gone(self):
        self.assertNotIn("will appear here", PAGE)


class TestAdminPageServing(AuthTestCase):
    def test_the_admin_gets_the_page_and_an_operator_does_not(self):
        status, _, body = self.get("admin", "/admin")
        self.assertEqual(status, 200)
        self.assertIn("Do-not-call", body)
        self.assertEqual(self.get("operator", "/admin")[:2], (302, "/"))
        self.assertEqual(self.get(None, "/admin")[:2], (302, "/login"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `venv312/Scripts/python.exe -m unittest tests.test_admin_page`
Expected: FAIL: the old shell has no `view-overview` ids, no `/api/admin/` paths, and still says "will appear here".

- [ ] **Step 3: Replace `admin.html`**

Write the whole file with this content:

```html
<!DOCTYPE html>
<html lang="en">

<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Admin | AU Admission Assist</title>
    <link rel="icon" href="/atc.png?v=2" type="image/png">
    <style>
        :root {
            --bg-primary: #f4f7fb;
            --bg-card: #ffffff;
            --text-primary: #0a1b2d;
            --text-secondary: #4a627d;
            --border-color: #d6e2ee;
            --accent: #005aab;
            --accent-light: #e8f2fc;
            --danger: #b42318;
        }

        * { box-sizing: border-box; }

        body {
            margin: 0;
            background: var(--bg-primary);
            color: var(--text-primary);
            font-family: "Montserrat", system-ui, sans-serif;
        }

        nav {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 12px 24px;
            background: var(--bg-card);
            border-bottom: 1px solid var(--border-color);
        }

        nav img { height: 40px; }
        .actions { display: flex; gap: 10px; align-items: center; }
        .actions form { margin: 0; }

        .nav-link, .btn {
            height: 36px;
            padding: 0 14px;
            display: inline-flex;
            align-items: center;
            background: var(--accent-light);
            border: 1px solid var(--border-color);
            border-radius: 10px;
            color: var(--text-primary);
            font: inherit;
            font-size: 0.85rem;
            font-weight: 600;
            text-decoration: none;
            cursor: pointer;
        }

        .btn.primary { background: var(--accent); color: #fff; border-color: var(--accent); }
        .btn.danger { color: var(--danger); }
        .btn:disabled { opacity: 0.5; cursor: default; }

        main { max-width: 1280px; margin: 24px auto; padding: 0 24px; }
        section {
            background: var(--bg-card);
            border: 1px solid var(--border-color);
            border-radius: 14px;
            padding: 18px 20px;
            margin-bottom: 20px;
        }

        h1 { font-size: 1.3rem; margin: 0 0 12px; }
        h2 { font-size: 1.05rem; margin: 0 0 12px; }
        p, .muted { color: var(--text-secondary); font-size: 0.9rem; }
        .error { color: var(--danger); font-weight: 600; }

        .tabs { display: flex; gap: 8px; margin-bottom: 16px; flex-wrap: wrap; }
        .tabs a { height: 36px; padding: 0 16px; display: inline-flex; align-items: center; border-radius: 10px;
                  border: 1px solid var(--border-color); background: var(--bg-card); color: var(--text-primary);
                  font-size: 0.85rem; font-weight: 600; text-decoration: none; }
        .tabs a.active { background: var(--accent); color: #fff; border-color: var(--accent); }

        .grid { display: flex; flex-wrap: wrap; gap: 12px 18px; align-items: flex-end; }
        label { display: flex; flex-direction: column; gap: 4px; font-size: 0.8rem; font-weight: 600; }
        input, select { height: 36px; padding: 0 10px; border: 1px solid var(--border-color); border-radius: 8px; font: inherit; }
        .row { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin: 10px 0; }

        .cards { display: flex; flex-wrap: wrap; gap: 14px; margin-bottom: 8px; }
        .card { min-width: 150px; padding: 14px 18px; border: 1px solid var(--border-color); border-radius: 12px; background: var(--bg-primary); }
        .card .value { font-size: 1.6rem; font-weight: 700; }
        .card .label { font-size: 0.78rem; color: var(--text-secondary); font-weight: 600; }

        .table-wrap { overflow-x: auto; }
        table { border-collapse: collapse; width: 100%; font-size: 0.85rem; }
        th, td { text-align: left; padding: 7px 9px; border-bottom: 1px solid var(--border-color); vertical-align: top; }
        th { color: var(--text-secondary); font-weight: 600; white-space: nowrap; }
        tr.clickable { cursor: pointer; }
        tr.clickable:hover { background: var(--accent-light); }
        td.num, th.num { text-align: right; white-space: nowrap; }

        dl { display: grid; grid-template-columns: max-content 1fr; gap: 6px 18px; font-size: 0.88rem; margin: 0; }
        dt { color: var(--text-secondary); font-weight: 600; }
        dd { margin: 0; overflow-wrap: anywhere; }
        pre { white-space: pre-wrap; overflow-wrap: anywhere; background: var(--bg-primary); padding: 12px;
              border-radius: 10px; font-size: 0.85rem; max-height: 480px; overflow: auto; }
        .issues { color: var(--danger); }
    </style>
</head>

<body>
    <nav>
        <img src="/AU_Logo.png?v=2" alt="Adamas University">
        <div class="actions">
            <a class="nav-link" href="/">Calls</a>
            <a class="nav-link" href="/campaigns">Campaigns</a>
            <form method="post" action="/logout"><button class="nav-link" type="submit">Logout</button></form>
        </div>
    </nav>

    <main>
        <h1>Admin</h1>
        <div class="tabs" id="tabs"></div>

        <section id="rangeBar">
            <div class="row">
                <button class="btn" type="button" data-quick="today">Today</button>
                <button class="btn" type="button" data-quick="7">7 days</button>
                <button class="btn" type="button" data-quick="30">30 days</button>
                <button class="btn" type="button" data-quick="month">This month</button>
                <button class="btn" type="button" data-quick="all">All time</button>
                <label>From <input type="date" id="fromDate"></label>
                <label>To <input type="date" id="toDate"></label>
                <button class="btn primary" type="button" id="applyRange">Apply</button>
                <span class="muted" id="rangeText"></span>
            </div>
        </section>

        <p id="message" class="error" hidden></p>

        <section id="view-overview" hidden>
            <div class="cards" id="kpis"></div>
            <h2>Cost</h2>
            <div class="table-wrap"><table><tbody id="costRows"></tbody></table></div>
            <ul id="notes" class="muted"></ul>
        </section>

        <section id="view-calls" hidden>
            <form id="filterForm" class="grid">
                <label>Lead <select name="lead"></select></label>
                <label>Analysis <select name="analysis"></select></label>
                <label>Visit <select name="visit"></select></label>
                <label>Program <select name="program"></select></label>
                <label>School <select name="school"></select></label>
                <label>Campaign <select name="campaign"></select></label>
                <label>Name or phone <input name="q" maxlength="100"></label>
                <button class="btn primary" type="submit">Filter</button>
            </form>
            <div class="row">
                <button class="btn" type="button" id="prevPage">Previous</button>
                <span class="muted" id="pageInfo"></span>
                <button class="btn" type="button" id="nextPage">Next</button>
                <a class="btn" id="exportCalls" href="#">Export CSV</a>
                <button class="btn danger" type="button" id="rerunFailed">Re-run all failed</button>
            </div>
            <div class="table-wrap"><table><thead id="callHead"></thead><tbody id="callRows"></tbody></table></div>
        </section>

        <section id="view-call" hidden>
            <div class="row"><a class="btn" id="backToCalls" href="#calls">Back to calls</a></div>
            <h2 id="callTitle"></h2>
            <dl id="callMeta"></dl>
            <h2 style="margin-top:18px">Cost</h2>
            <dl id="callCost"></dl>
            <h2 style="margin-top:18px">Analysis</h2>
            <div class="row">
                <button class="btn primary" type="button" id="rerunOne">Re-run analysis</button>
                <span class="muted" id="rerunStatus"></span>
            </div>
            <p id="callSummary"></p>
            <p id="callNext"></p>
            <p id="callIssues" class="issues"></p>
            <dl id="callAnalysis"></dl>
            <h2 style="margin-top:18px">Transcript</h2>
            <pre id="callTranscript"></pre>
        </section>

        <section id="view-campaigns" hidden>
            <div class="row"><a class="btn" id="exportCampaigns" href="#">Export CSV</a></div>
            <p id="noCampaigns" hidden>No campaigns in this range.</p>
            <div class="table-wrap"><table><thead id="campaignHead"></thead><tbody id="campaignRows"></tbody></table></div>
        </section>

        <section id="view-dnc" hidden>
            <form id="dncForm" class="grid">
                <label>Mobile number <input name="phone" required maxlength="20"></label>
                <label>Note <input name="note" maxlength="200"></label>
                <button class="btn primary" type="submit">Add to the list</button>
            </form>
            <div class="row">
                <label>Search <input id="dncSearch" maxlength="100"></label>
                <a class="btn" href="/api/admin/dnc.csv">Export CSV</a>
                <span class="muted" id="dncStatus"></span>
            </div>
            <div class="table-wrap"><table><thead id="dncHead"></thead><tbody id="dncRows"></tbody></table></div>
        </section>
    </main>

    <script>
        // Every value from the server is set with textContent: names, extra columns and the transcript
        // come from callers and uploaded files and must never become markup.
        const VIEWS = ["overview", "calls", "call", "campaigns", "dnc"];
        const TABS = [["overview", "Overview"], ["calls", "Calls & leads"], ["campaigns", "Campaigns"], ["dnc", "Do-not-call"]];
        const RANGE_VIEWS = ["overview", "calls", "campaigns"];
        const LEADS = ["hot", "warm", "cold", "not_interested", "callback_requested", "wrong_number", "no_conversation"];
        const ANALYSES = ["pending", "ok", "failed", "skipped"];
        const FIELD_LABELS = [
            ["lead_status", "Lead"], ["interest_score", "Interest (1-10)"], ["is_prospective", "Prospective"],
            ["prospective_reason", "Why"], ["respondent", "Spoke with"], ["applicant_name", "Applicant"],
            ["qualification", "Qualification"], ["board", "Board"], ["marks_pct", "Marks %"], ["subjects", "Subjects"],
            ["entrance_exams", "Entrance exams"], ["programs_of_interest", "Programs of interest"],
            ["primary_program", "Primary program"], ["school_name", "School"], ["scholarship_discussed", "Scholarship discussed"],
            ["scholarship_name", "Scholarship"], ["scholarship_amount_inr", "Scholarship (INR)"],
            ["campus_visit_agreed", "Visit agreed"], ["campus_visit_day", "Visit day"],
            ["callback_requested", "Callback requested"], ["callback_time", "Callback time"], ["objections", "Objections"],
            ["language", "Language"], ["transferred", "Transferred"], ["status", "Analysis status"], ["error", "Error"],
            ["analyzed_at", "Analysed at"]];
        const POLL_MS = 3000, POLL_MAX = 20;

        const $ = (id) => document.getElementById(id);
        let route = { view: "overview", arg: null, params: new URLSearchParams() };
        let pollTimer = null, loadToken = 0;

        function el(tag, text, cls) {
            const node = document.createElement(tag);
            if (text !== undefined && text !== null) node.textContent = String(text);
            if (cls) node.className = cls;
            return node;
        }

        async function api(path, options) {
            const response = await fetch(path, options);
            if (response.status === 401) {
                window.location.href = "/login";
                throw new Error("Login required");
            }
            const body = await response.json().catch(() => ({}));
            if (!response.ok) throw new Error(body.message || ("Request failed (" + response.status + ")"));
            return body;
        }

        function post(path, data) {
            return api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data || {}) });
        }

        function showMessage(text) {
            $("message").textContent = text || "";
            $("message").hidden = !text;
        }

        function usd(value) { return value === null || value === undefined ? "–" : "$" + Number(value).toFixed(4); }
        function plain(value) {
            if (value === null || value === undefined || value === "") return "–";
            if (Array.isArray(value)) return value.length ? value.join(", ") : "–";
            if (value === true) return "Yes";
            if (value === false) return "No";
            return String(value);
        }

        // --- the route: #view/arg?params ---

        function parseHash() {
            const raw = window.location.hash.replace(/^#/, "");
            const cut = raw.indexOf("?");
            const path = cut === -1 ? raw : raw.slice(0, cut);
            const params = new URLSearchParams(cut === -1 ? "" : raw.slice(cut + 1));
            const slash = path.indexOf("/");
            const view = slash === -1 ? path : path.slice(0, slash);
            const arg = slash === -1 ? null : decodeURIComponent(path.slice(slash + 1));
            route = { view: VIEWS.includes(view) ? view : "overview", arg, params };
        }

        function hashFor(view, arg, params) {
            const query = params && params.toString() ? "?" + params.toString() : "";
            return "#" + view + (arg ? "/" + encodeURIComponent(arg) : "") + query;
        }

        function rangeParams() {
            const out = new URLSearchParams();
            for (const key of ["from", "to", "all"]) if (route.params.get(key)) out.set(key, route.params.get(key));
            return out;
        }

        function go(view, arg, params) { window.location.hash = hashFor(view, arg, params); }

        // --- the range bar ---

        function istDate(offsetDays) {
            return new Date(Date.now() + offsetDays * 86400000).toLocaleDateString("en-CA", { timeZone: "Asia/Kolkata" });
        }

        function quickRange(kind) {
            const params = new URLSearchParams();
            if (kind === "all") params.set("all", "1");
            else {
                const today = istDate(0);
                params.set("to", today);
                params.set("from", kind === "today" ? today : kind === "month" ? today.slice(0, 8) + "01" : istDate(1 - Number(kind)));
            }
            return params;
        }

        function showRange(range) {
            $("fromDate").value = range.from || "";
            $("toDate").value = range.to || "";
            $("rangeText").textContent = range.from ? range.from + " to " + range.to + " (IST)" : "All time";
        }

        document.querySelectorAll("[data-quick]").forEach((button) => button.addEventListener("click", () => {
            const params = quickRange(button.dataset.quick);
            for (const [key, value] of route.params) if (!["from", "to", "all", "page"].includes(key)) params.set(key, value);
            go(route.view, route.arg, params);
        }));

        $("applyRange").addEventListener("click", () => {
            const from = $("fromDate").value, to = $("toDate").value;
            if (!from || !to) { showMessage("Pick both dates."); return; }
            const params = new URLSearchParams(route.params);
            params.delete("all"); params.delete("page");
            params.set("from", from); params.set("to", to);
            go(route.view, route.arg, params);
        });

        // --- tables ---

        function table(head, headers, body, rows, onRow, numeric) {
            head.replaceChildren(); body.replaceChildren();
            const tr = el("tr");
            headers.forEach((text, i) => tr.append(el("th", text, numeric && numeric.includes(i) ? "num" : "")));
            head.append(tr);
            rows.forEach((cells, index) => {
                const row = el("tr", null, onRow ? "clickable" : "");
                cells.forEach((value, i) => row.append(el("td", value, numeric && numeric.includes(i) ? "num" : "")));
                if (onRow) row.addEventListener("click", () => onRow(index));
                body.append(row);
            });
        }

        function fillSelect(select, first, values, current) {
            select.replaceChildren();
            select.append(new Option(first, ""));
            for (const value of values) {
                const [v, label] = Array.isArray(value) ? value : [value, value];
                select.append(new Option(label, v));
            }
            select.value = current || "";
        }

        // --- views ---

        async function loadOverview() {
            const data = await api("/api/admin/overview?" + rangeParams());
            showRange(data.range);
            const kpis = $("kpis");
            kpis.replaceChildren();
            for (const [label, value] of [["Calls", data.calls], ["Connected", data.connected], ["Hot leads", data.hot],
                                          ["Warm leads", data.warm], ["Visits fixed", data.visits],
                                          ["Cost per call", usd(data.cost_per_call)], ["Cost per hot lead", usd(data.cost_per_hot_lead)]]) {
                const card = el("div", null, "card");
                card.append(el("div", value, "value"), el("div", label, "label"));
                kpis.append(card);
            }
            const c = data.cost;
            const rows = [["Total", usd(c.total)], ["Gemini", usd(c.gemini)], ["Speech to text", usd(c.stt)],
                          ["Haiku analysis", usd(c.haiku)], ["Plivo", usd(c.plivo)], ["Earlier calls (no split)", usd(c.earlier)]];
            const body = $("costRows");
            body.replaceChildren();
            for (const [label, value] of rows) {
                const tr = el("tr");
                tr.append(el("td", label), el("td", value, "num"));
                body.append(tr);
            }
            const notes = $("notes");
            notes.replaceChildren();
            const n = data.notes;
            if (n.analysis_failed) {
                const li = el("li");
                const link = el("a", n.analysis_failed + " analyses failed");
                const params = rangeParams();
                params.set("analysis", "failed");
                link.href = hashFor("calls", null, params);
                li.append(link);
                notes.append(li);
            }
            if (n.analysis_pending) notes.append(el("li", n.analysis_pending + " analyses still running"));
            if (n.plivo_pending) notes.append(el("li", n.plivo_pending + " Plivo costs not fetched yet, so totals may rise"));
        }

        const FILTER_KEYS = ["lead", "analysis", "visit", "program", "school", "campaign", "q"];

        function callsQuery() {
            const params = rangeParams();
            for (const key of FILTER_KEYS.concat(["page"])) if (route.params.get(key)) params.set(key, route.params.get(key));
            return params;
        }

        let callRows = [];

        async function loadCalls() {
            const data = await api("/api/admin/calls?" + callsQuery());
            showRange(data.range);
            const form = $("filterForm").elements;
            fillSelect(form.lead, "Any", LEADS, route.params.get("lead"));
            fillSelect(form.analysis, "Any", ANALYSES, route.params.get("analysis"));
            fillSelect(form.visit, "Any", [["yes", "Visit fixed"], ["no", "No visit"]], route.params.get("visit"));
            fillSelect(form.program, "Any", data.programs, route.params.get("program"));
            fillSelect(form.school, "Any", data.schools, route.params.get("school"));
            fillSelect(form.campaign, "Any", data.campaigns.map((c) => [String(c.id), c.name]), route.params.get("campaign"));
            form.q.value = route.params.get("q") || "";
            callRows = data.rows;
            table($("callHead"), ["Time", "Name", "Phone", "Lead", "Program", "School", "Visit day", "Score", "Cost", "Analysis"],
                  $("callRows"), data.rows.map((r) => [r.created_at, r.name, r.phone, plain(r.lead_status), plain(r.primary_program),
                      plain(r.school_name), plain(r.campus_visit_day), plain(r.interest_score), usd(r.cost_usd), plain(r.analysis_status)]),
                  (i) => go("call", callRows[i].call_uuid, rangeParams()), [7, 8]);
            const pages = Math.max(1, Math.ceil(data.total / data.page_size));
            $("pageInfo").textContent = "Page " + data.page + " of " + pages + " (" + data.total + " calls)";
            $("prevPage").disabled = data.page <= 1;
            $("nextPage").disabled = data.page >= pages;
            const exportParams = callsQuery();
            exportParams.delete("page");
            $("exportCalls").href = "/api/admin/calls.csv?" + exportParams;
            $("rerunFailed").textContent = "Re-run all failed (" + data.failed_total + ")";
            $("rerunFailed").disabled = data.failed_total === 0;
        }

        function turnPage(step) {
            const params = new URLSearchParams(route.params);
            params.set("page", String(Math.max(1, Number(route.params.get("page") || 1) + step)));
            go("calls", null, params);
        }

        $("prevPage").addEventListener("click", () => turnPage(-1));
        $("nextPage").addEventListener("click", () => turnPage(1));

        $("filterForm").addEventListener("submit", (event) => {
            event.preventDefault();
            const params = rangeParams();
            for (const key of FILTER_KEYS) if (event.target.elements[key].value) params.set(key, event.target.elements[key].value);
            go("calls", null, params);
        });

        $("rerunFailed").addEventListener("click", async () => {
            if (!window.confirm("Re-run the analysis of failed calls? Up to 50 are queued per click and each costs about $0.001.")) return;
            try {
                const result = await post("/api/admin/rerun-failed");
                showMessage("");
                $("pageInfo").textContent = "Queued " + result.queued + " (" + result.busy + " already running, " + result.remaining +
                    " more failed, " + result.no_transcript + " without a transcript). Reload in a minute.";
            } catch (error) { showMessage(error.message); }
        });

        function fillList(list, pairs) {
            list.replaceChildren();
            for (const [label, value] of pairs) list.append(el("dt", label), el("dd", value));
        }

        let detailUuid = null, detailAt = null;

        function drawDetail(data) {
            const call = data.call, a = data.analysis;
            $("callTitle").textContent = (call.name || "Unknown") + " · " + (call.phone || "");
            fillList($("callMeta"), [["Time", call.created_at], ["Status", plain(call.status)], ["Ended because", plain(call.end_reason)],
                ["Duration (s)", plain(call.duration_sec)], ["Legs", plain(call.legs)], ["Programs looked up", plain(call.programs_looked_up)],
                ["Campaign", data.campaign ? data.campaign.name : "–"]]);
            const cost = data.cost;
            fillList($("callCost"), [["Total", usd(cost.total)], ["Gemini", usd(cost.gemini)], ["Speech to text", usd(cost.stt)],
                ["Haiku analysis", usd(cost.haiku)], ["Plivo", usd(cost.plivo) + (cost.plivo_status ? " (" + cost.plivo_status + ")" : "")],
                ["Earlier call (no split)", usd(cost.earlier)]]);
            $("callSummary").textContent = a ? "Summary: " + plain(a.summary) : "No analysis yet.";
            $("callNext").textContent = a ? "Next action: " + plain(a.next_action) : "";
            $("callIssues").textContent = a && a.agent_issues.length ? "Agent issues: " + a.agent_issues.join("; ") : "";
            fillList($("callAnalysis"), a ? FIELD_LABELS.map(([key, label]) => [label, plain(a[key])]) : []);
            $("callTranscript").textContent = data.transcript === null ? "Transcript not available." : data.transcript;
            // No transcript: the server refuses a re-run (it would wipe the analysis). Pending: one is already running.
            $("rerunOne").disabled = data.transcript === null || Boolean(a && a.status === "pending");
            detailAt = a ? a.analyzed_at : null;
        }

        async function loadCall() {
            detailUuid = route.arg;
            $("backToCalls").href = hashFor("calls", null, rangeParams());
            $("rerunStatus").textContent = "";
            drawDetail(await api("/api/admin/calls/" + encodeURIComponent(detailUuid)));
        }

        $("rerunOne").addEventListener("click", async () => {
            const uuid = detailUuid, beforeAt = detailAt;
            try {
                await post("/api/admin/calls/" + encodeURIComponent(uuid) + "/rerun");
            } catch (error) { $("rerunStatus").textContent = error.message; return; }
            $("rerunOne").disabled = true;
            $("rerunStatus").textContent = "Queued…";
            let tries = 0;
            clearInterval(pollTimer);
            pollTimer = setInterval(async () => {
                tries += 1;
                try {
                    const data = await api("/api/admin/calls/" + encodeURIComponent(uuid));
                    if (uuid !== detailUuid) { clearInterval(pollTimer); return; }
                    // The worker first marks the row "pending" (analyzed_at unchanged); finished means a new
                    // analyzed_at with a status other than pending.
                    const a = data.analysis;
                    const finished = Boolean(a) && a.status !== "pending" && a.analyzed_at !== beforeAt;
                    if (finished || tries >= POLL_MAX) {
                        clearInterval(pollTimer);
                        drawDetail(data);
                        $("rerunStatus").textContent = !finished ? "Still running; reload later."
                            : a.status === "failed" ? "Failed: " + (a.error || "unknown error") : "Done.";
                    }
                } catch (error) { clearInterval(pollTimer); $("rerunStatus").textContent = error.message; $("rerunOne").disabled = false; }
            }, POLL_MS);
        });

        async function loadCampaigns() {
            const data = await api("/api/admin/campaigns?" + rangeParams());
            showRange(data.range);
            $("noCampaigns").hidden = data.campaigns.length > 0;
            table($("campaignHead"), ["Campaign", "State", "Created", "Contacts", "Dialed", "Reached", "Hot", "Visits", "Cost", "Cost per hot lead"],
                  $("campaignRows"), data.campaigns.map((c) => [c.name, c.state, c.created_at, c.contacts, c.dialed, c.reached, c.hot,
                      c.visits, usd(c.cost_usd), usd(c.cost_per_hot_lead)]), null, [3, 4, 5, 6, 7, 8, 9]);
            $("exportCampaigns").href = "/api/admin/campaigns.csv?" + rangeParams();
        }

        let dncRows = [];

        async function loadDnc() {
            const q = $("dncSearch").value.trim();
            const data = await api("/api/admin/dnc" + (q ? "?q=" + encodeURIComponent(q) : ""));
            dncRows = data.numbers;
            table($("dncHead"), ["Number", "Added", "Note", ""], $("dncRows"), dncRows.map((r) => [r.phone, r.added_at, plain(r.note), ""]));
            [...$("dncRows").children].forEach((row, i) => {
                const button = el("button", "Remove", "btn danger");
                button.type = "button";
                button.addEventListener("click", async () => {
                    if (!window.confirm("Remove " + dncRows[i].phone + " from the do-not-call list?")) return;
                    try {
                        await post("/api/admin/dnc/remove", { phone: dncRows[i].phone });
                        $("dncStatus").textContent = "Removed.";
                        await loadDnc();
                    } catch (error) { showMessage(error.message); }
                });
                row.lastChild.append(button);
            });
        }

        $("dncForm").addEventListener("submit", async (event) => {
            event.preventDefault();
            const form = event.target.elements;
            try {
                const result = await post("/api/admin/dnc", { phone: form.phone.value, note: form.note.value });
                showMessage("");
                $("dncStatus").textContent = "Added " + result.phone + ".";
                event.target.reset();
                await loadDnc();
            } catch (error) { showMessage(error.message); }
        });

        $("dncSearch").addEventListener("input", () => loadDnc().catch((error) => showMessage(error.message)));

        // --- the page ---

        const LOADERS = { overview: loadOverview, calls: loadCalls, call: loadCall, campaigns: loadCampaigns, dnc: loadDnc };

        function drawTabs() {
            const tabs = $("tabs");
            tabs.replaceChildren();
            for (const [view, label] of TABS) {
                const link = el("a", label, route.view === view || (route.view === "call" && view === "calls") ? "active" : "");
                link.href = hashFor(view, null, RANGE_VIEWS.includes(view) ? rangeParams() : null);
                tabs.append(link);
            }
        }

        async function render() {
            parseHash();
            clearInterval(pollTimer);
            const token = ++loadToken;
            drawTabs();
            showMessage("");
            for (const view of VIEWS) $("view-" + view).hidden = view !== route.view;
            $("rangeBar").hidden = !RANGE_VIEWS.includes(route.view);
            try {
                await LOADERS[route.view]();
            } catch (error) {
                if (token === loadToken) showMessage(error.message);
            }
        }

        window.addEventListener("hashchange", render);
        render();
    </script>
</body>

</html>
```

- [ ] **Step 4: Run the test**

Run: `venv312/Scripts/python.exe -m unittest tests.test_admin_page`
Expected: `Ran 8 tests ... OK`.

- [ ] **Step 5: Syntax-check the script (optional, when Node is installed)**

There is no browser check in this project (the operator looks at EC2 only), so if `node` is on the PATH, extract the `<script>` body to a temp file outside the repo and run `node --check <file>`. Expected: no output. Skip this step when Node is absent; EC2 does not need it.

- [ ] **Step 6: Full suite**

Run: `venv312/Scripts/python.exe -m unittest discover -s tests -t .`
Expected: 1095 run, only the 3 known failures.

---

### Task 7: Mask the phone numbers in the logs, docs

**Files:**
- Modify: `app.py` (three log lines, edited in place: no line count change), `CLAUDE.md`, `docs/spec/admin-panel-design.md`, `docs/spec/admin-system-overview.md`
- Test: `tests/test_log_masking.py` (create)

**Interfaces:**
- Consumes: `campaigns.masked(phone)` (last four characters after `xxxxxx`), `tests.test_plivo_checks.PlivoCase`, `tests.test_access_control.AuthTestCase`, `tests.harness.appctl.APP_PY`, `LogCapture`.
- Produces: no full number or contact name in `/outbound-webhook`, `/trigger-call` or `/media-stream` log lines.

- [ ] **Step 1: Write the failing test**

Create `tests/test_log_masking.py`:

```python
"""No full phone number or contact name in the call logs (docs/spec/admin-panel-design.md §4)."""

from __future__ import annotations

import unittest
from unittest import mock

from tests.harness.appctl import APP_PY, LogCapture, app
from tests.test_access_control import AuthTestCase, run
from tests.test_plivo_checks import PlivoCase

web_auth = app.web_auth
NUMBER = "9876543210"


class TestOutboundWebhookLog(PlivoCase):
    def test_the_answered_line_shows_the_last_four_digits_and_no_name(self):
        with mock.patch.object(web_auth, "PLIVO_SIGNATURE_CHECK", "off"), LogCapture() as log:
            status, _ = self.get(f"/outbound-webhook?user_name=Asha+Das&phone_number={NUMBER}", {})
        self.assertEqual(status, 200)
        (line,) = [l for l in log.lines if "Outbound call answered by" in l]
        self.assertIn("xxxxxx3210", line)
        self.assertFalse([l for l in log.lines if NUMBER in l or "Asha" in l], log.lines)


class TestTriggerCallLog(AuthTestCase):
    def test_the_initiating_line_is_masked(self):
        async def go():
            client = await self.client("operator")
            return (await client.post("/trigger-call", json={"user_name": "Asha Das", "phone_number": NUMBER})).status_code
        with mock.patch.object(app, "place_call", mock.AsyncMock(return_value="uuid-1")), LogCapture() as log:
            self.assertEqual(run(go()), 200)
        (line,) = [l for l in log.lines if "Initiating outbound call" in l]
        self.assertIn("xxxxxx3210", line)
        self.assertFalse([l for l in log.lines if NUMBER in l], log.lines)


class TestMediaStreamLogSource(unittest.TestCase):
    def test_the_connected_line_is_masked(self):
        with open(APP_PY, encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("Caller: {campaigns.masked(phone_number)}", source)
        self.assertNotIn("Caller: {phone_number}", source)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `venv312/Scripts/python.exe -m unittest tests.test_log_masking`
Expected: 3 failures (the lines still print `9876543210` and the name).

- [ ] **Step 3: Mask the three lines in `app.py`**

Replace, in `outbound_webhook`:

```python
    logger.info(f"📞 Outbound call answered by: {phone_number} ({user_name})")
```

with:

```python
    logger.info(f"📞 Outbound call answered by: {campaigns.masked(phone_number)}")
```

Replace, in `handle_media_stream`:

```python
    logger.info(f'Client connected to Quart WebSocket. Caller: {phone_number}')
```

with:

```python
    logger.info(f'Client connected to Quart WebSocket. Caller: {campaigns.masked(phone_number)}')
```

Replace, in `trigger_call` (the `@app.route("/trigger-call", methods=["POST"])` line itself does not change):

```python
        logger.info(f"Initiating outbound call to {target_phone_number} via Dashboard...")
```

with:

```python
        logger.info(f"Initiating outbound call to {campaigns.masked(target_phone_number)} via Dashboard...")
```

- [ ] **Step 4: Run the test**

Run: `venv312/Scripts/python.exe -m unittest tests.test_log_masking`
Expected: `Ran 3 tests ... OK`.

- [ ] **Step 5: Documentation**

In `CLAUDE.md`, in "Resume here", add as the first entry (above the current item 0000000):

```
00000000. **Step 4 of the admin system (admin panel): BUILT on <date>, uncommitted / <commit>; not yet deployed or checked on EC2.** `admin_data.py` (queries, CSV), twelve `/api/admin/` routes in app.py (all `ADMIN_API`), `admin.html` (five hash-routed views: Overview, Calls & leads, Call detail, Campaign reports, Do-not-call; range default last 7 days IST), `Analyzer.is_busy`, one index `calls_created_at`, and the three phone-number log lines masked. Spec `docs/spec/admin-panel-design.md`, plan `docs/spec/admin-panel-plan.md`. Cost of a call = `COALESCE(gemini, old cost text)` + STT + Haiku + Plivo (old calls show as "Earlier calls (no split)"). No kill switch; rollback is a revert. Next: deploy (`git pull`, idle restart), then the operator's look at `/admin` on EC2 (report §5 in the handoff).
```

Replace `<date>` and `<commit>` with the real values. In the "Suite" lines of "Current state", add the new totals from the last run.

In `docs/spec/admin-panel-design.md`, in the Decisions list item 4 ("Counting"), add this sentence at its end: `A call's cost is COALESCE(gemini_cost_usd, the old cost text) + STT + Haiku + Plivo, so an old call that the step 2 catch-up analysed still counts its Haiku and Plivo cost.` In §2 "Re-run", add: `A call is re-run only when its transcript file exists (else 409 "No transcript to analyse"); "Re-run all failed" skips such calls and reports them as no_transcript. Without the file the worker would save no_conversation over a good analysis.` In §2 "CSV", add: `The calls CSV is built in the thread pool with its query (8000 also carries live audio).` Change the line `Status: design approved ... Spec awaiting the operator's review; no plan yet.` to say the spec is approved and the plan is `docs/spec/admin-panel-plan.md`.

In `docs/spec/admin-system-overview.md`, in the step 4 entry, add one line: `Step 4 built (plan docs/spec/admin-panel-plan.md); see CLAUDE.md "Resume here" for its state.`

- [ ] **Step 6: Full suite**

Run: `venv312/Scripts/python.exe -m unittest discover -s tests -t .`
Expected: 1098 run, only the 3 known failures (4 on EC2), 4 skipped, 1 expected failure. Do not commit; report the totals to the operator.
