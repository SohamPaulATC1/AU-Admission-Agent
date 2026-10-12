# Step 3: Bulk Campaigns and the Dispatcher Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The calling team uploads an .xlsx of contacts, starts a campaign, and the main process (8000) calls them at most N at once inside calling hours, retries no-answer and busy, survives a restart, and shows per-contact status and a short lead result on a new `/campaigns` page, with no cost anywhere on it.

**Architecture:** `campaigns.py` (new) holds everything that is not I/O: the tables, settings checks, state moves, the .xlsx upload parsing and screening, and the dispatcher's decisions (`due_contacts`, `on_hangup`, `expire_stuck`, ...), all as sync functions on a `sqlite3` connection, run through `call_store.run`. `app.py` owns the asyncio side: a shared `place_call()` taken out of `/trigger-call`, a 2 s polling loop started in `start_background_work` (main process only), a background hook in `/plivo-hangup`, and six routes. `campaigns.html` (new) is the page.

**Tech Stack:** Python 3.12, sqlite3 (WAL), asyncio, Quart 0.20, `plivo` SDK (mocked in tests), stdlib `zipfile` + `xml.etree` via `catalog/build_catalog.read_sheets`, vanilla JS, stdlib `unittest`.

**Spec:** `docs/spec/campaigns-design.md` (shared decisions: `docs/spec/admin-system-overview.md`)

**Dry run (Windows, 2026-10-12, throwaway worktree, discarded):** every code block in this plan was applied exactly as written on top of `2448772`. Result: 908 run (808 + 100 new), the 3 known `test_prompt_catalog` failures, and the one expected `GEMINI_MODEL` re-pin failure (`[108, 1894, 2420, 2430, 2861]`, Task 4 Step 10). Nothing else failed. So a failure you see while executing is real (a different base, a typo in the copy), not a known flaw in the plan.

## Global Constraints

- No new dependency. `requirements.txt` does not change.
- `.env` defaults (all optional): `CAMPAIGNS_ENABLED=1`, `CAMPAIGN_MAX_CONCURRENT=8`, `CAMPAIGN_DEFAULT_CONCURRENCY=5`, `CAMPAIGN_HOURS_START=09:00`, `CAMPAIGN_HOURS_END=21:00`, `CAMPAIGN_MAX_RETRIES=3`, `CAMPAIGN_MIN_RETRY_GAP_MIN=30`, `CAMPAIGN_RECENT_DAYS=30`, `CAMPAIGN_TICK_S=2`, `CAMPAIGN_DIAL_GAP_S=1`, `CAMPAIGN_CALL_TIMEOUT_MIN=30`. New-campaign defaults: concurrency 5, 09:00-21:00, 2 retries, 120 min gap.
- Upload limits: .xlsx only, 2 MB file, 50 MB unpacked, 5,000 data rows, 20 extra columns, 200 chars per cell. Phone valid = `^[6-9][0-9]{9}$` after normalisation.
- Campaign states exactly `draft`, `running`, `paused`, `stopped`, `done`. Contact statuses exactly `pending`, `calling`, `retry_wait`, `done`, `failed`, `cancelled`. Rejection reasons exactly `missing_number`, `bad_number`, `duplicate_in_file`, `on_do_not_call`, `called_recently`.
- Times stored as IST text `YYYY-MM-DD HH:MM:SS` (same as `call_store.now_ist()`); every `campaigns` function that needs the time takes `now` (an aware `datetime`) so tests use a fixed clock.
- The database is written by the main process (8000) only: the dispatcher loop, the hangup hook and WAL run only when `EVENT_FORWARD_URL` is empty. The tables are created in every process at import (`campaigns.upgrade` from `app.init_call_history_db`).
- Log tag `📣 [CAMPAIGN]`, added to `DIAGNOSTIC_LOG_TAGS` in `tests/harness/appctl.py`. Phone numbers in logs masked to the last 4 digits (`campaigns.masked`). Never log names, extras or full numbers.
- No cost column or key in any campaign API reply, for either role. The page never builds HTML from data (`textContent` only; no `innerHTML`).
- Every new route gets an entry in `web_auth.ENDPOINT_ACCESS` (default deny; `TestDefaultDeny` keeps it equal to `app.url_map`). All campaign routes are `OPERATOR_PAGE` / `OPERATOR_API` (both roles). JSON bodies only (CSRF), as `/trigger-call`.
- `@app.route("/trigger-call", methods=["POST"])` stays byte-identical, and `/trigger-call`'s replies and logs stay the same. `aec.py`, `aec1.py`, `bargein.py`, prompt files untouched. The contact's name and extras never reach the Gemini prompt.
- No new `call_state` key is planned. If one appears, mirror it in `tests/harness/appctl.py` (`test_harness_smoke`). When app.py's line count changes, re-pin the `GEMINI_MODEL` list in `tests/test_preservation_4_8_resumption_recording_stats.py` (replace the list, add one comment line).
- Tests: stdlib `unittest` only; run the suite with `venv312/Scripts/python.exe -m unittest discover -s tests -t .` (EC2: `venv/bin/python -m unittest discover -s tests -t .`). Write test code with Write/Edit, never bash heredocs. Tests never touch the network, `.env`, Plivo or the real `call_history.db`; they patch the `campaigns` bounds (so a `.env` with `CAMPAIGN_*` keys does not change them).
- No commits unless the operator asks (CLAUDE.md). Each task ends with a full-suite run.
- Suite baseline before this plan (Windows): 808 run, 3 failures (all in `tests/test_prompt_catalog.py`, pre-existing; leave them), 2 skipped, 1 expected failure. EC2 shows more skips and one more known failure (`test_gemini_backend.TestVertexEndpoint.test_defaults`). "Green" means no failure beyond those.

## Review Focus

1. A campaign started at night (or a retry that falls due at 23:00) must not dial until the next window, and the `.env` bounds win over a campaign's own hours. Tests: Task 3 `test_outside_hours_dials_nothing`, `test_env_bounds_win_over_campaign_hours`, `test_retry_due_at_night_waits_for_the_window`.
2. A real calling-team Excel: phone cells as numbers (`9876543210`, `9.87654321E9`, `9876543210.0`), `+91 98765-43210`, `09876543210`, Bengali names, blank rows. Tests: Task 2 `test_normalise_phone`, `test_numeric_and_text_cells`, `test_blank_rows_are_skipped`.
3. A restart mid-campaign: rows in `calling` keep their slots until their hangup arrives or the 30-minute backstop, and nobody is dialled twice. Tests: Task 3 `test_calling_contacts_hold_their_slots`, `test_no_double_pick`, `test_backstop_ends_a_lost_call_without_retry`; Task 4 `test_hangup_webhook_settles_the_contact`.
4. A hostile or broken upload: not a zip, a zip bomb, an HTML/script name. Nothing is stored, the server answers 400, and the page shows names as text. Tests: Task 2 `test_not_an_xlsx`, `test_unpacked_size_limit`; Task 5 `test_page_never_builds_html_from_data`.
5. A Plivo outage: three dial errors in a row pause the campaign with a reason, no contact loses an attempt, and the loop keeps running. Tests: Task 3 `test_three_dial_errors_pause_the_campaign`; Task 4 `test_three_dial_errors_pause_and_spare_the_contacts`, `test_loop_survives_a_failing_tick`.

## Files

| File | Change |
|---|---|
| `campaigns.py` | Create (Tasks 1-3). Tables, settings, state moves, reads (1); upload parsing and screening (2); dispatcher decisions (3). |
| `app.py` | Modify (Task 4). `import campaigns`; `init_call_history_db` (tables + WAL); `place_call`; dispatcher loop and tick; hangup hook; six routes; `start_background_work`. |
| `web_auth.py` | Modify (Task 4). Six `ENDPOINT_ACCESS` entries. |
| `tests/harness/appctl.py` | Modify (Task 4). `[CAMPAIGN]` log tag. |
| `campaigns.html` | Create (Task 5). |
| `index.html` | Modify (Task 5). "Campaigns" nav link. |
| `tests/test_campaigns_store.py`, `tests/test_campaign_upload.py`, `tests/test_campaign_dispatch.py`, `tests/test_campaign_wiring.py`, `tests/test_campaign_page.py` | Create. |
| `tests/test_preservation_4_8_resumption_recording_stats.py` | Modify (Task 4). Re-pin. |
| `CLAUDE.md`, `docs/spec/campaigns-design.md`, `docs/spec/admin-system-overview.md` | Modify (Task 6). |

---

### Task 1: `campaigns.py`: tables, settings, state moves, reads

**Files:**
- Create: `campaigns.py`
- Test: `tests/test_campaigns_store.py`

**Interfaces:**
- Consumes: `call_store.IST`, `call_store.upgrade(conn)`; `catalog.build_catalog.read_sheets` (imported now, used in Task 2); fixture `tests.test_call_store.CALLS_TABLE`.
- Produces:
  - Constants: `CAMPAIGNS_ENABLED`, `MAX_CONCURRENT`, `DEFAULT_CONCURRENCY`, `HOURS_START`, `HOURS_END`, `MAX_RETRIES`, `MIN_RETRY_GAP_MIN`, `RECENT_DAYS`, `TICK_S`, `DIAL_GAP_S`, `CALL_TIMEOUT_MIN`, `DEFAULT_RETRIES`, `DEFAULT_RETRY_GAP_MIN`, `DIAL_ERRORS_TO_PAUSE`, `PAGE_SIZE`, `MAX_FILE_BYTES`, `MAX_UNPACKED_BYTES`, `MAX_ROWS`, `MAX_EXTRAS`, `MAX_CELL_CHARS`, `STATES`, `CONTACT_STATUSES`, `OPEN_STATUSES`, `ACTIONS`, `SETTING_KEYS`.
  - Errors: `CampaignError(ValueError)` (400), `NotFound(CampaignError)` (404), `BadMove(CampaignError)` (409).
  - `upgrade(conn)`; `masked(phone) -> str`; `limits() -> dict`; `check_name(raw) -> str`; `check_settings(raw: dict, require_all=False) -> dict` (the five `SETTING_KEYS`).
  - `create_campaign(conn, name, settings, *, created_by, file_name, contacts, rejected_count, now) -> int` (contacts: dicts `sheet_row`, `name`, `phone`, `extra`).
  - `apply_action(conn, campaign_id, action, now) -> str` (new state); `update_settings(conn, campaign_id, settings)`.
  - `list_campaigns(conn) -> list[dict]`; `campaign_detail(conn, campaign_id, page=1, status=None) -> dict` with keys `campaign`, `contacts`, `extra_columns`, `page`, `page_size`, `total`.
  - Internal helpers later tasks use: `_stamp(when) -> str`, `_clock(when) -> "HH:MM"`, `_counts(conn, campaign_id) -> dict`.
  - Test fixtures other tasks import from `tests/test_campaigns_store.py`: `NOW`, `BOUNDS`, `SETTINGS`, `new_db()`, `add_campaign(conn, phones, settings=None, state=None, now=NOW) -> int`, `CampaignCase`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_campaigns_store.py`:

```python
"""campaigns.py: tables, settings, state moves and reads (docs/spec/campaigns-design.md §1, §2, §4)."""

from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime
from unittest import mock

import call_store
import campaigns
from call_store import IST
from tests.test_call_store import CALLS_TABLE

NOW = datetime(2026, 10, 13, 10, 0, tzinfo=IST)   # a Tuesday, 10:00 IST

# The spec's defaults, patched in so a .env with CAMPAIGN_* keys cannot change the tests.
BOUNDS = dict(MAX_CONCURRENT=8, DEFAULT_CONCURRENCY=5, HOURS_START="09:00", HOURS_END="21:00",
              MAX_RETRIES=3, MIN_RETRY_GAP_MIN=30, RECENT_DAYS=30, CALL_TIMEOUT_MIN=30, DIAL_GAP_S=0)

SETTINGS = {"concurrency": 2, "hours_start": "09:00", "hours_end": "21:00", "max_retries": 2, "retry_gap_min": 120}


def new_db():
    conn = sqlite3.connect(":memory:")
    conn.execute(CALLS_TABLE)
    call_store.upgrade(conn)
    campaigns.upgrade(conn)
    return conn


def add_campaign(conn, phones, settings=None, state=None, now=NOW):
    contacts = [{"sheet_row": i + 2, "name": f"Contact {i}", "phone": phone, "extra": {"City": "Kolkata"}}
                for i, phone in enumerate(phones)]
    campaign_id = campaigns.create_campaign(conn, "Test list", {**SETTINGS, **(settings or {})},
                                            created_by="operator", file_name="list.xlsx", contacts=contacts,
                                            rejected_count=0, now=now)
    if state:
        conn.execute("UPDATE campaigns SET state=? WHERE id=?", (state, campaign_id))
    return campaign_id


class CampaignCase(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.multiple(campaigns, **BOUNDS)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.conn = new_db()
        self.addCleanup(self.conn.close)

    def contact(self, phone):
        found = self.conn.execute("SELECT status, attempts, last_cause, next_attempt_at, call_uuid, finished_at "
                                  "FROM campaign_contacts WHERE phone=?", (phone,)).fetchone()
        return dict(zip(("status", "attempts", "last_cause", "next_attempt_at", "call_uuid", "finished_at"), found))

    def campaign(self, campaign_id):
        found = self.conn.execute("SELECT state, state_reason, started_at, finished_at, dial_errors "
                                  "FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
        return dict(zip(("state", "state_reason", "started_at", "finished_at", "dial_errors"), found))


class TestSchema(CampaignCase):
    def test_upgrade_twice_is_fine(self):
        campaigns.upgrade(self.conn)
        tables = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"campaigns", "campaign_contacts", "do_not_call"} <= tables)

    def test_same_phone_twice_in_one_campaign_is_refused(self):
        with self.assertRaises(sqlite3.IntegrityError):
            add_campaign(self.conn, ["9876543210", "9876543210"])

    def test_new_campaign_is_a_draft_with_pending_contacts(self):
        campaign_id = add_campaign(self.conn, ["9876543210"])
        self.assertEqual(self.campaign(campaign_id)["state"], "draft")
        self.assertEqual(self.contact("9876543210")["status"], "pending")
        self.assertEqual(self.contact("9876543210")["attempts"], 0)

    def test_masked(self):
        self.assertEqual(campaigns.masked("9876543210"), "xxxxxx3210")


class TestSettings(CampaignCase):
    def test_defaults(self):
        self.assertEqual(campaigns.check_settings({}), {"concurrency": 5, "hours_start": "09:00", "hours_end": "21:00",
                                                        "max_retries": 2, "retry_gap_min": 120})

    def test_limits_carry_bounds_and_defaults(self):
        limits = campaigns.limits()
        self.assertEqual((limits["max_concurrent"], limits["hours_start"], limits["hours_end"]), (8, "09:00", "21:00"))
        self.assertEqual(limits["defaults"], campaigns.check_settings({}))

    def test_concurrency_bounds(self):
        self.assertEqual(campaigns.check_settings({"concurrency": 8})["concurrency"], 8)
        self.assertEqual(campaigns.check_settings({"concurrency": "3"})["concurrency"], 3)
        for bad in (0, 9, -1, True, 2.5, "two", []):
            with self.assertRaises(campaigns.CampaignError, msg=repr(bad)):
                campaigns.check_settings({"concurrency": bad})

    def test_hours_inside_the_env_bounds(self):
        self.assertEqual(campaigns.check_settings({"hours_start": "10:00", "hours_end": "18:30"})["hours_end"], "18:30")
        for start, end in (("08:00", "12:00"), ("10:00", "21:30"), ("12:00", "12:00"), ("14:00", "10:00"),
                           ("9:00", "12:00"), ("10:00", "24:00"), ("ten", "12:00")):
            with self.assertRaises(campaigns.CampaignError, msg=(start, end)):
                campaigns.check_settings({"hours_start": start, "hours_end": end})

    def test_retries_and_gap(self):
        self.assertEqual(campaigns.check_settings({"max_retries": 0})["max_retries"], 0)
        with self.assertRaises(campaigns.CampaignError):
            campaigns.check_settings({"max_retries": 4})
        with self.assertRaises(campaigns.CampaignError):
            campaigns.check_settings({"retry_gap_min": 29})

    def test_require_all_for_an_edit(self):
        with self.assertRaises(campaigns.CampaignError):
            campaigns.check_settings({"concurrency": 2}, require_all=True)
        self.assertEqual(campaigns.check_settings(SETTINGS, require_all=True), SETTINGS)

    def test_name(self):
        self.assertEqual(campaigns.check_name("  October list "), "October list")
        for bad in ("", "   ", None, "x" * 101):
            with self.assertRaises(campaigns.CampaignError):
                campaigns.check_name(bad)


class TestStateMoves(CampaignCase):
    def test_start(self):
        campaign_id = add_campaign(self.conn, ["9876543210"])
        self.assertEqual(campaigns.apply_action(self.conn, campaign_id, "start", NOW), "running")
        self.assertEqual(self.campaign(campaign_id)["started_at"], "2026-10-13 10:00:00")
        with self.assertRaises(campaigns.BadMove):
            campaigns.apply_action(self.conn, campaign_id, "start", NOW)

    def test_pause_and_resume_clear_the_reason(self):
        campaign_id = add_campaign(self.conn, ["9876543210"], state="running")
        self.assertEqual(campaigns.apply_action(self.conn, campaign_id, "pause", NOW), "paused")
        self.conn.execute("UPDATE campaigns SET state_reason='dial_errors', dial_errors=3 WHERE id=?", (campaign_id,))
        self.assertEqual(campaigns.apply_action(self.conn, campaign_id, "resume", NOW), "running")
        self.assertEqual((self.campaign(campaign_id)["state_reason"], self.campaign(campaign_id)["dial_errors"]),
                         (None, 0))

    def test_stop_cancels_only_what_has_not_been_dialled(self):
        campaign_id = add_campaign(self.conn, ["9000000001", "9000000002", "9000000003", "9000000004"],
                                   state="running")
        for phone, status in (("9000000002", "retry_wait"), ("9000000003", "calling"), ("9000000004", "done")):
            self.conn.execute("UPDATE campaign_contacts SET status=? WHERE phone=?", (status, phone))
        self.assertEqual(campaigns.apply_action(self.conn, campaign_id, "stop", NOW), "stopped")
        self.assertEqual([self.contact(p)["status"] for p in ("9000000001", "9000000002", "9000000003", "9000000004")],
                         ["cancelled", "cancelled", "calling", "done"])
        self.assertEqual(self.contact("9000000001")["last_cause"], "stopped")
        self.assertEqual(self.campaign(campaign_id)["finished_at"], "2026-10-13 10:00:00")
        for action in campaigns.ACTIONS:
            with self.assertRaises(campaigns.BadMove):
                campaigns.apply_action(self.conn, campaign_id, action, NOW)

    def test_unknown_campaign_and_action(self):
        with self.assertRaises(campaigns.NotFound):
            campaigns.apply_action(self.conn, 999, "start", NOW)
        campaign_id = add_campaign(self.conn, ["9876543210"])
        with self.assertRaises(campaigns.CampaignError) as caught:
            campaigns.apply_action(self.conn, campaign_id, "explode", NOW)
        self.assertNotIsInstance(caught.exception, campaigns.BadMove)

    def test_settings_edit_only_in_draft_or_paused(self):
        campaign_id = add_campaign(self.conn, ["9876543210"])
        changed = {**SETTINGS, "concurrency": 4}
        campaigns.update_settings(self.conn, campaign_id, changed)
        self.assertEqual(self.conn.execute("SELECT concurrency FROM campaigns WHERE id=?", (campaign_id,)).fetchone()[0], 4)
        campaigns.apply_action(self.conn, campaign_id, "start", NOW)
        with self.assertRaises(campaigns.BadMove):
            campaigns.update_settings(self.conn, campaign_id, changed)
        campaigns.apply_action(self.conn, campaign_id, "pause", NOW)
        campaigns.update_settings(self.conn, campaign_id, {**changed, "concurrency": 1})
        with self.assertRaises(campaigns.NotFound):
            campaigns.update_settings(self.conn, 999, changed)


class TestReads(CampaignCase):
    def setUp(self):
        super().setUp()
        self.campaign_id = add_campaign(self.conn, ["9000000001", "9000000002", "9000000003"])
        self.conn.execute("UPDATE campaign_contacts SET status='done', call_uuid='u-1' WHERE phone='9000000001'")
        self.conn.execute("UPDATE campaign_contacts SET status='done', call_uuid='u-2' WHERE phone='9000000002'")
        self.conn.execute("INSERT INTO call_analysis (call_uuid, status, lead_status, campus_visit_day, summary) "
                          "VALUES ('u-1', 'ok', 'hot', 'Saturday', 'Wants B.Tech CSE.')")
        self.conn.execute("INSERT INTO call_analysis (call_uuid, status, lead_status) VALUES ('u-2', 'failed', 'cold')")

    def test_list_has_counts_per_status(self):
        [item] = campaigns.list_campaigns(self.conn)
        self.assertEqual(item["name"], "Test list")
        self.assertEqual(item["counts"], {"pending": 1, "calling": 0, "retry_wait": 0, "done": 2, "failed": 0,
                                          "cancelled": 0})

    def test_detail_joins_only_finished_analyses(self):
        detail = campaigns.campaign_detail(self.conn, self.campaign_id)
        self.assertEqual(detail["total"], 3)
        self.assertEqual(detail["extra_columns"], ["City"])
        first, second, third = detail["contacts"]
        self.assertEqual((first["lead_status"], first["campus_visit_day"], first["summary"]),
                         ("hot", "Saturday", "Wants B.Tech CSE."))
        self.assertIsNone(second["lead_status"])   # the analysis failed: nothing shown
        self.assertEqual(third["extra"], {"City": "Kolkata"})
        self.assertEqual(third["sheet_row"], 4)

    def test_detail_filter_and_pages(self):
        self.assertEqual(campaigns.campaign_detail(self.conn, self.campaign_id, status="pending")["total"], 1)
        with mock.patch.object(campaigns, "PAGE_SIZE", 2):
            self.assertEqual(len(campaigns.campaign_detail(self.conn, self.campaign_id, page=2)["contacts"]), 1)
        with self.assertRaises(campaigns.CampaignError):
            campaigns.campaign_detail(self.conn, self.campaign_id, status="bogus")
        with self.assertRaises(campaigns.NotFound):
            campaigns.campaign_detail(self.conn, 999)

    def test_no_cost_in_any_reply(self):
        self.conn.execute("UPDATE calls SET cost='$1.00'")   # calls is not read; this must stay true
        text = json.dumps([campaigns.list_campaigns(self.conn), campaigns.campaign_detail(self.conn, self.campaign_id)])
        self.assertNotIn("cost", text)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to see them fail**

Run: `venv312/Scripts/python.exe -m unittest tests.test_campaigns_store -v`
Expected: ERROR, `ModuleNotFoundError: No module named 'campaigns'`.

- [ ] **Step 3: Write `campaigns.py`**

Create `campaigns.py`:

```python
"""Step 3: bulk campaigns, their contacts, the upload and the dispatcher's decisions.

Design: docs/spec/campaigns-design.md. Sync functions take an open sqlite3 connection and leave
the commit to the caller (call_store.run gives them one). app.py owns the asyncio loop, Plivo and
the routes; nothing here does I/O beyond the database. Only the main process (8000) writes.
"""

import io
import json
import os
import re
from datetime import timedelta
from decimal import Decimal, InvalidOperation
import zipfile

from call_store import IST
from catalog.build_catalog import read_sheets


def _env_int(name, default):
    return int(os.getenv(name, str(default)))


_HHMM = re.compile(r"([01][0-9]|2[0-3]):[0-5][0-9]")

CAMPAIGNS_ENABLED = os.getenv("CAMPAIGNS_ENABLED", "1").strip() != "0"
MAX_CONCURRENT = _env_int("CAMPAIGN_MAX_CONCURRENT", 8)          # all campaign calls; Plivo's cap is 10
DEFAULT_CONCURRENCY = _env_int("CAMPAIGN_DEFAULT_CONCURRENCY", 5)
HOURS_START = os.getenv("CAMPAIGN_HOURS_START", "09:00").strip()  # IST; TRAI bounds for every campaign
HOURS_END = os.getenv("CAMPAIGN_HOURS_END", "21:00").strip()
MAX_RETRIES = _env_int("CAMPAIGN_MAX_RETRIES", 3)
MIN_RETRY_GAP_MIN = _env_int("CAMPAIGN_MIN_RETRY_GAP_MIN", 30)
RECENT_DAYS = _env_int("CAMPAIGN_RECENT_DAYS", 30)
TICK_S = float(os.getenv("CAMPAIGN_TICK_S", "2"))
DIAL_GAP_S = float(os.getenv("CAMPAIGN_DIAL_GAP_S", "1"))         # Plivo's calls-per-second limit
CALL_TIMEOUT_MIN = _env_int("CAMPAIGN_CALL_TIMEOUT_MIN", 30)      # backstop for a lost hangup

if not (_HHMM.fullmatch(HOURS_START) and _HHMM.fullmatch(HOURS_END) and HOURS_START < HOURS_END):
    raise ValueError("CAMPAIGN_HOURS_START/END must be HH:MM with start before end")

DEFAULT_RETRIES = 2
DEFAULT_RETRY_GAP_MIN = 120
MAX_RETRY_GAP_MIN = 24 * 60
DIAL_ERRORS_TO_PAUSE = 3
PAGE_SIZE = 100

MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_UNPACKED_BYTES = 50 * 1024 * 1024   # a small zip can unpack to gigabytes
MAX_ROWS = 5000
MAX_EXTRAS = 20
MAX_CELL_CHARS = 200

STATES = ("draft", "running", "paused", "stopped", "done")
CONTACT_STATUSES = ("pending", "calling", "retry_wait", "done", "failed", "cancelled")
OPEN_STATUSES = ("pending", "calling", "retry_wait")
ACTIONS = ("start", "pause", "resume", "stop")
MOVES = {("draft", "start"): "running", ("running", "pause"): "paused", ("paused", "resume"): "running",
         ("draft", "stop"): "stopped", ("running", "stop"): "stopped", ("paused", "stop"): "stopped"}
EDITABLE_STATES = ("draft", "paused")
SETTING_KEYS = ("concurrency", "hours_start", "hours_end", "max_retries", "retry_gap_min")


class CampaignError(ValueError):
    """A request the operator can fix; the message is shown to them (400)."""


class NotFound(CampaignError):
    """No such campaign (404)."""


class BadMove(CampaignError):
    """The campaign's state does not allow it (409)."""


# --- tables ---

CAMPAIGN_COLUMNS = (
    ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
    ("name", "TEXT NOT NULL"),
    ("state", "TEXT NOT NULL"),
    ("state_reason", "TEXT"),
    ("created_by", "TEXT"),
    ("created_at", "TEXT"),
    ("started_at", "TEXT"),
    ("finished_at", "TEXT"),
    ("concurrency", "INTEGER"),
    ("hours_start", "TEXT"),
    ("hours_end", "TEXT"),
    ("max_retries", "INTEGER"),
    ("retry_gap_min", "INTEGER"),
    ("file_name", "TEXT"),
    ("rejected_count", "INTEGER"),
    ("last_dial_at", "TEXT"),
    ("dial_errors", "INTEGER DEFAULT 0"),
)

CONTACT_COLUMNS = (
    ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
    ("campaign_id", "INTEGER NOT NULL"),
    ("sheet_row", "INTEGER"),
    ("name", "TEXT"),
    ("phone", "TEXT NOT NULL"),
    ("extra", "TEXT"),
    ("status", "TEXT NOT NULL"),
    ("attempts", "INTEGER DEFAULT 0"),
    ("next_attempt_at", "TEXT"),
    ("call_uuid", "TEXT"),
    ("last_cause", "TEXT"),
    ("dialed_at", "TEXT"),
    ("finished_at", "TEXT"),
)


def _create(conn, table, columns):
    conn.execute(f"CREATE TABLE IF NOT EXISTS {table} ({', '.join(f'{n} {k}' for n, k in columns)})")


def upgrade(conn):
    """The campaign tables and indexes. Safe on every start, in every process."""
    _create(conn, "campaigns", CAMPAIGN_COLUMNS)
    _create(conn, "campaign_contacts", CONTACT_COLUMNS)
    conn.execute("CREATE TABLE IF NOT EXISTS do_not_call (phone TEXT PRIMARY KEY, added_at TEXT, note TEXT)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS campaign_contacts_phone ON campaign_contacts (campaign_id, phone)")
    conn.execute("CREATE INDEX IF NOT EXISTS campaign_contacts_status ON campaign_contacts (status, campaign_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS campaign_contacts_call ON campaign_contacts (call_uuid)")


# --- small helpers ---

def _stamp(when):
    return when.astimezone(IST).strftime("%Y-%m-%d %H:%M:%S")


def _clock(when):
    return when.astimezone(IST).strftime("%H:%M")


def masked(phone):
    """For log lines: never a full number."""
    return "xxxxxx" + str(phone or "")[-4:]


def _counts(conn, campaign_id):
    counts = dict.fromkeys(CONTACT_STATUSES, 0)
    for status, n in conn.execute("SELECT status, COUNT(*) FROM campaign_contacts WHERE campaign_id=? "
                                  "GROUP BY status", (campaign_id,)):
        counts[status] = n
    return counts


# --- settings ---

def limits():
    """Bounds and defaults for the campaign form (GET /api/campaigns)."""
    return {
        "max_concurrent": MAX_CONCURRENT,
        "hours_start": HOURS_START,
        "hours_end": HOURS_END,
        "max_retries": MAX_RETRIES,
        "min_retry_gap_min": MIN_RETRY_GAP_MIN,
        "max_rows": MAX_ROWS,
        "max_file_bytes": MAX_FILE_BYTES,
        "defaults": {
            "concurrency": min(DEFAULT_CONCURRENCY, MAX_CONCURRENT),
            "hours_start": HOURS_START,
            "hours_end": HOURS_END,
            "max_retries": min(DEFAULT_RETRIES, MAX_RETRIES),
            "retry_gap_min": max(DEFAULT_RETRY_GAP_MIN, MIN_RETRY_GAP_MIN),
        },
    }


def _whole(value, key, low, high):
    text = str(value).strip()
    if isinstance(value, bool) or not re.fullmatch(r"[0-9]+", text):
        raise CampaignError(f"{key} must be a whole number")
    number = int(text)
    if not low <= number <= high:
        raise CampaignError(f"{key} must be between {low} and {high}")
    return number


def check_settings(raw, require_all=False):
    """The five settings from a request, checked against the .env bounds (spec §3). A missing key
    takes the default, unless require_all (a settings edit sends all five)."""
    given = {k: raw[k] for k in SETTING_KEYS if raw.get(k) is not None}
    if require_all and len(given) < len(SETTING_KEYS):
        raise CampaignError(f"All of {', '.join(SETTING_KEYS)} are required")
    values = {**limits()["defaults"], **given}
    hours = []
    for key in ("hours_start", "hours_end"):
        text = str(values[key]).strip()
        if not _HHMM.fullmatch(text):
            raise CampaignError(f"{key} must be HH:MM")
        hours.append(text)
    if not HOURS_START <= hours[0] < hours[1] <= HOURS_END:
        raise CampaignError(f"Calling hours must be inside {HOURS_START}-{HOURS_END} IST, start before end")
    return {
        "concurrency": _whole(values["concurrency"], "concurrency", 1, MAX_CONCURRENT),
        "hours_start": hours[0],
        "hours_end": hours[1],
        "max_retries": _whole(values["max_retries"], "max_retries", 0, MAX_RETRIES),
        "retry_gap_min": _whole(values["retry_gap_min"], "retry_gap_min", MIN_RETRY_GAP_MIN, MAX_RETRY_GAP_MIN),
    }


def check_name(raw):
    name = str(raw or "").strip()
    if not 1 <= len(name) <= 100:
        raise CampaignError("Campaign name must be 1-100 characters")
    return name


# --- campaigns ---

def create_campaign(conn, name, settings, *, created_by, file_name, contacts, rejected_count, now):
    """A draft campaign with its contacts (dicts: sheet_row, name, phone, extra). Returns its id."""
    cursor = conn.execute(
        "INSERT INTO campaigns (name, state, created_by, created_at, concurrency, hours_start, hours_end, "
        "max_retries, retry_gap_min, file_name, rejected_count, dial_errors) "
        "VALUES (?, 'draft', ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
        (name, created_by, _stamp(now), settings["concurrency"], settings["hours_start"], settings["hours_end"],
         settings["max_retries"], settings["retry_gap_min"], file_name, rejected_count))
    campaign_id = cursor.lastrowid
    conn.executemany(
        "INSERT INTO campaign_contacts (campaign_id, sheet_row, name, phone, extra, status, attempts) "
        "VALUES (?, ?, ?, ?, ?, 'pending', 0)",
        [(campaign_id, c["sheet_row"], c["name"], c["phone"], json.dumps(c["extra"], ensure_ascii=False))
         for c in contacts])
    return campaign_id


def _state(conn, campaign_id):
    found = conn.execute("SELECT state FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
    if found is None:
        raise NotFound("No such campaign")
    return found[0]


def apply_action(conn, campaign_id, action, now):
    """start / pause / resume / stop (spec §2 state moves). Returns the new state."""
    if action not in ACTIONS:
        raise CampaignError("Unknown action")
    state = _state(conn, campaign_id)
    target = MOVES.get((state, action))
    if target is None:
        raise BadMove(f"Cannot {action} a {state} campaign")
    stamp = _stamp(now)
    if action == "start":
        conn.execute("UPDATE campaigns SET state='running', started_at=?, state_reason=NULL WHERE id=?",
                     (stamp, campaign_id))
    elif action == "stop":
        conn.execute("UPDATE campaigns SET state='stopped', finished_at=?, state_reason=NULL WHERE id=?",
                     (stamp, campaign_id))
        # Live calls finish and get their hangup result; nothing new is dialled.
        conn.execute("UPDATE campaign_contacts SET status='cancelled', last_cause='stopped', finished_at=? "
                     "WHERE campaign_id=? AND status IN ('pending', 'retry_wait')", (stamp, campaign_id))
    else:
        conn.execute("UPDATE campaigns SET state=?, state_reason=NULL, dial_errors=0 WHERE id=?",
                     (target, campaign_id))
    return target


def update_settings(conn, campaign_id, settings):
    state = _state(conn, campaign_id)
    if state not in EDITABLE_STATES:
        raise BadMove(f"Settings can only change while the campaign is draft or paused, not {state}")
    conn.execute("UPDATE campaigns SET concurrency=?, hours_start=?, hours_end=?, max_retries=?, retry_gap_min=? "
                 "WHERE id=?", (*(settings[k] for k in SETTING_KEYS), campaign_id))


# --- reads (no cost anywhere: spec §4) ---

SUMMARY_FIELDS = ("id", "name", "state", "state_reason", "created_by", "created_at", "started_at", "finished_at",
                  "concurrency", "hours_start", "hours_end", "max_retries", "retry_gap_min", "file_name",
                  "rejected_count")
CONTACT_FIELDS = ("id", "sheet_row", "name", "phone", "extra", "status", "attempts", "last_cause",
                  "next_attempt_at", "dialed_at", "finished_at")
LEAD_FIELDS = ("lead_status", "campus_visit_day", "summary")   # all the operator sees of the analysis


def list_campaigns(conn):
    rows = conn.execute(f"SELECT {', '.join(SUMMARY_FIELDS)} FROM campaigns ORDER BY id DESC").fetchall()
    return [dict(zip(SUMMARY_FIELDS, row), counts=_counts(conn, row[0])) for row in rows]


def campaign_detail(conn, campaign_id, page=1, status=None):
    found = conn.execute(f"SELECT {', '.join(SUMMARY_FIELDS)} FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
    if found is None:
        raise NotFound("No such campaign")
    if status is not None and status not in CONTACT_STATUSES:
        raise CampaignError("Unknown status filter")
    campaign = dict(zip(SUMMARY_FIELDS, found), counts=_counts(conn, campaign_id))
    where, args = "c.campaign_id=?", [campaign_id]
    if status:
        where += " AND c.status=?"
        args.append(status)
    total = conn.execute(f"SELECT COUNT(*) FROM campaign_contacts c WHERE {where}", args).fetchone()[0]
    columns = ", ".join([f"c.{f}" for f in CONTACT_FIELDS] + [f"a.{f}" for f in LEAD_FIELDS])
    rows = conn.execute(
        f"SELECT {columns} FROM campaign_contacts c "
        f"LEFT JOIN call_analysis a ON a.call_uuid = c.call_uuid AND a.status = 'ok' "
        f"WHERE {where} ORDER BY c.id LIMIT ? OFFSET ?", [*args, PAGE_SIZE, (page - 1) * PAGE_SIZE]).fetchall()
    contacts = []
    for row in rows:
        item = dict(zip(CONTACT_FIELDS + LEAD_FIELDS, row))
        item["extra"] = json.loads(item["extra"] or "{}")
        contacts.append(item)
    first = conn.execute("SELECT extra FROM campaign_contacts WHERE campaign_id=? ORDER BY id LIMIT 1",
                         (campaign_id,)).fetchone()
    return {"campaign": campaign, "contacts": contacts, "extra_columns": list(json.loads(first[0] or "{}")) if first else [],
            "page": page, "page_size": PAGE_SIZE, "total": total}
```

- [ ] **Step 4: Run the tests to see them pass**

Run: `venv312/Scripts/python.exe -m unittest tests.test_campaigns_store -v`
Expected: all OK.

- [ ] **Step 5: Run the full suite**

Run: `venv312/Scripts/python.exe -m unittest discover -s tests -t .`
Expected: the baseline plus the new tests, no new failure.

- [ ] **Step 6: Commit (only if the operator asked for commits)**

```bash
git add campaigns.py tests/test_campaigns_store.py
git commit -m "Step 3 Task 1: campaigns tables, settings, state moves, reads"
```

---

### Task 2: upload parsing and screening

**Files:**
- Modify: `campaigns.py` (append)
- Test: `tests/test_campaign_upload.py`

**Interfaces:**
- Consumes: Task 1 (`CampaignError`, `create_campaign`, `_stamp`, the limits constants, `RECENT_DAYS`); `read_sheets(file)` (accepts a file-like object; returns every sheet as a list of rows of cell strings).
- Produces:
  - `NothingToCall(CampaignError)` with attribute `rejected` (list).
  - `REJECT_REASONS`, `NAME_HEADERS`, `PHONE_HEADERS`.
  - `normalise_phone(text) -> str | None` (10 digits).
  - `parse_upload(data: bytes) -> (rows, extra_headers, extras_dropped)`; each row a dict `sheet_row`, `name`, `phone_text`, `extra` (dict header -> cell text).
  - `screen_rows(conn, rows, now) -> (contacts, rejected)`; contact dicts `sheet_row`, `name`, `phone`, `extra`; rejected dicts `sheet_row`, `name`, `phone` (as written), `reason`.
  - `create_from_upload(conn, name, settings, rows, *, created_by, file_name, now) -> {"id", "accepted", "rejected"}`; raises `NothingToCall` if no row survives.
  - Test helpers other tasks import from `tests/test_campaign_upload.py`: `make_xlsx(rows) -> bytes`, `Num` (a `str` subclass written as a raw number cell).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_campaign_upload.py`:

```python
"""campaigns.py upload: .xlsx parsing, phone normalisation, row screening (docs/spec/campaigns-design.md §3)."""

from __future__ import annotations

import io
import unittest
import zipfile
from datetime import timedelta
from unittest import mock
from xml.sax.saxutils import escape

import campaigns
from tests.test_campaigns_store import NOW, SETTINGS, CampaignCase, add_campaign

_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


class Num(str):
    """A cell written as a raw number (<v>), the way Excel stores a phone typed as a number."""


def _column(i):
    letters = ""
    i += 1
    while i:
        i, rest = divmod(i - 1, 26)
        letters = chr(65 + rest) + letters
    return letters


def make_xlsx(rows):
    """A one-sheet .xlsx: str -> inline text, int/float/Num -> number cell, None -> no cell."""
    sheet_rows = []
    for r, row in enumerate(rows, start=1):
        cells = []
        for c, value in enumerate(row):
            ref = f"{_column(c)}{r}"
            if value is None:
                continue
            if isinstance(value, (Num, int, float)):
                cells.append(f'<c r="{ref}"><v>{value}</v></c>')
            else:
                cells.append(f'<c r="{ref}" t="inlineStr"><is><t>{escape(value)}</t></is></c>')
        sheet_rows.append(f'<row r="{r}">{"".join(cells)}</row>')
    sheet = f'<worksheet xmlns="{_MAIN}"><sheetData>{"".join(sheet_rows)}</sheetData></worksheet>'
    workbook = (f'<workbook xmlns="{_MAIN}" xmlns:r="{_REL}"><sheets>'
                f'<sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>')
    rels = ('<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("xl/workbook.xml", workbook)
        z.writestr("xl/_rels/workbook.xml.rels", rels)
        z.writestr("xl/worksheets/sheet1.xml", sheet)
    return buffer.getvalue()


class TestNormalisePhone(unittest.TestCase):
    def test_normalise_phone(self):
        good = ["9876543210", "+919876543210", "919876543210", "09876543210", "+91 98765-43210",
                "(987) 654-3210", "98765 43210", "9.87654321E9", "9876543210.0", " 6000000000 "]
        for text in good:
            self.assertIn(campaigns.normalise_phone(text), ("9876543210", "6000000000"), text)
        bad = ["", "12345", "5876543210", "98765432101", "abcdefghij", "९८७६५४३२१०", "98765.4321",
               "+1 9876543210", "1e10", None]
        for text in bad:
            self.assertIsNone(campaigns.normalise_phone(text), text)


class TestParseUpload(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.multiple(campaigns, MAX_FILE_BYTES=2 * 1024 * 1024, MAX_UNPACKED_BYTES=50 * 1024 * 1024,
                                      MAX_ROWS=5000, MAX_EXTRAS=20, MAX_CELL_CHARS=200)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_header_variants_and_extras(self):
        data = make_xlsx([["Student  Name", "Mobile Number", "City", "Course Interest"],
                          ["অনিতা দাস", "9876543210", "Kolkata", "B.Tech"]])
        rows, extras, dropped = campaigns.parse_upload(data)
        self.assertEqual(extras, ["City", "Course Interest"])
        self.assertEqual(dropped, 0)
        self.assertEqual(rows, [{"sheet_row": 2, "name": "অনিতা দাস", "phone_text": "9876543210",
                                 "extra": {"City": "Kolkata", "Course Interest": "B.Tech"}}])

    def test_every_accepted_header(self):
        for name in campaigns.NAME_HEADERS:
            for phone in campaigns.PHONE_HEADERS:
                rows, _, _ = campaigns.parse_upload(make_xlsx([[name.upper(), phone.title()], ["A", "9876543210"]]))
                self.assertEqual(rows[0]["phone_text"], "9876543210", (name, phone))

    def test_numeric_and_text_cells(self):
        data = make_xlsx([["Name", "Phone"], ["A", 9876543210], ["B", Num("9.87654321E9")], ["C", "+91 98765 43210"]])
        rows, _, _ = campaigns.parse_upload(data)
        self.assertEqual([campaigns.normalise_phone(r["phone_text"]) for r in rows], ["9876543210"] * 3)

    def test_blank_rows_are_skipped(self):
        data = make_xlsx([[None], ["Name", "Phone"], [None, None], ["A", "9876543210"], [], ["B", "9876543211"]])
        rows, _, _ = campaigns.parse_upload(data)
        self.assertEqual([(r["sheet_row"], r["name"]) for r in rows], [(2, "A"), (3, "B")])

    def test_short_rows_and_missing_cells(self):
        rows, _, _ = campaigns.parse_upload(make_xlsx([["Name", "Phone", "City"], ["A"], [None, "9876543210"]]))
        self.assertEqual(rows[0], {"sheet_row": 2, "name": "A", "phone_text": "", "extra": {"City": ""}})
        self.assertEqual(rows[1]["name"], "")

    def test_cells_are_cut(self):
        rows, _, _ = campaigns.parse_upload(make_xlsx([["Name", "Phone", "Note"], ["x" * 300, "9876543210", "y" * 300]]))
        self.assertEqual((len(rows[0]["name"]), len(rows[0]["extra"]["Note"])), (200, 200))

    def test_extra_columns_capped_and_duplicates_dropped(self):
        header = ["Name", "Phone"] + [f"Col {i}" for i in range(25)] + ["col 0"]
        rows, extras, dropped = campaigns.parse_upload(make_xlsx([header, ["A", "9876543210"]]))
        self.assertEqual(len(extras), 20)
        self.assertEqual(dropped, 5)
        self.assertEqual(extras[0], "Col 0")

    def test_missing_columns(self):
        for header in (["Name", "City"], ["Phone", "City"], ["Naam", "Number"]):
            with self.assertRaises(campaigns.CampaignError):
                campaigns.parse_upload(make_xlsx([header, ["A", "9876543210"]]))

    def test_empty_sheet(self):
        with self.assertRaises(campaigns.CampaignError):
            campaigns.parse_upload(make_xlsx([]))

    def test_not_an_xlsx(self):
        for data in (b"", b"name,phone\nA,9876543210\n", b"PK\x03\x04garbage"):
            with self.assertRaises(campaigns.CampaignError):
                campaigns.parse_upload(data)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as z:
            z.writestr("hello.txt", "not a workbook")
        with self.assertRaises(campaigns.CampaignError):
            campaigns.parse_upload(buffer.getvalue())

    def test_file_size_limit(self):
        data = make_xlsx([["Name", "Phone"], ["A", "9876543210"]])
        with mock.patch.object(campaigns, "MAX_FILE_BYTES", len(data) - 1):
            with self.assertRaises(campaigns.CampaignError):
                campaigns.parse_upload(data)

    def test_unpacked_size_limit(self):
        data = make_xlsx([["Name", "Phone"]] + [["A" * 100, "9876543210"]] * 50)
        with mock.patch.object(campaigns, "MAX_UNPACKED_BYTES", 1000):
            with self.assertRaises(campaigns.CampaignError) as caught:
                campaigns.parse_upload(data)
        self.assertIn("50 MB", str(caught.exception))

    def test_row_limit(self):
        data = make_xlsx([["Name", "Phone"]] + [["A", f"98765432{i:02d}"] for i in range(4)])
        with mock.patch.object(campaigns, "MAX_ROWS", 3):
            with self.assertRaises(campaigns.CampaignError):
                campaigns.parse_upload(data)


def parsed(*phones):
    return [{"sheet_row": i + 2, "name": f"N{i}", "phone_text": p, "extra": {}} for i, p in enumerate(phones)]


class TestScreenRows(CampaignCase):
    def test_each_reason(self):
        self.conn.execute("INSERT INTO do_not_call (phone, added_at) VALUES ('9000000005', '2026-10-01 10:00:00')")
        self.conn.execute("INSERT INTO calls (call_uuid, phone, created_at) VALUES ('u-1', '9000000006', ?)",
                          (campaigns._stamp(NOW - timedelta(days=3)),))
        contacts, rejected = campaigns.screen_rows(self.conn, parsed(
            "9000000001", "", "12345", "+91 90000 00001", "9000000005", "9000000006"), NOW)
        self.assertEqual([c["phone"] for c in contacts], ["9000000001"])
        self.assertEqual([(r["sheet_row"], r["reason"]) for r in rejected],
                         [(3, "missing_number"), (4, "bad_number"), (5, "duplicate_in_file"),
                          (6, "on_do_not_call"), (7, "called_recently")])
        self.assertEqual(rejected[2]["phone"], "+91 90000 00001")   # as written, for the operator

    def test_recent_window(self):
        self.conn.execute("INSERT INTO calls (call_uuid, phone, created_at) VALUES ('u-1', '+919000000001', ?)",
                          (campaigns._stamp(NOW - timedelta(days=40)),))
        contacts, rejected = campaigns.screen_rows(self.conn, parsed("9000000001"), NOW)
        self.assertEqual((len(contacts), rejected), (1, []))

    def test_other_campaigns_count_but_not_their_cancelled_contacts(self):
        add_campaign(self.conn, ["9000000001", "9000000002"])
        self.conn.execute("UPDATE campaign_contacts SET status='cancelled' WHERE phone='9000000002'")
        contacts, rejected = campaigns.screen_rows(self.conn, parsed("9000000001", "9000000002"), NOW)
        self.assertEqual([c["phone"] for c in contacts], ["9000000002"])
        self.assertEqual(rejected[0]["reason"], "called_recently")

    def test_create_from_upload(self):
        result = campaigns.create_from_upload(self.conn, "Oct", SETTINGS, parsed("9000000001", "123"),
                                              created_by="operator", file_name="list.xlsx", now=NOW)
        self.assertEqual((result["accepted"], [r["reason"] for r in result["rejected"]]), (1, ["bad_number"]))
        state, rejected_count = self.conn.execute("SELECT state, rejected_count FROM campaigns WHERE id=?",
                                                  (result["id"],)).fetchone()
        self.assertEqual((state, rejected_count), ("draft", 1))

    def test_all_rejected_stores_nothing(self):
        with self.assertRaises(campaigns.NothingToCall) as caught:
            campaigns.create_from_upload(self.conn, "Oct", SETTINGS, parsed("123", ""), created_by="operator",
                                         file_name="list.xlsx", now=NOW)
        self.assertEqual(len(caught.exception.rejected), 2)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM campaigns").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to see them fail**

Run: `venv312/Scripts/python.exe -m unittest tests.test_campaign_upload -v`
Expected: errors, `AttributeError: module 'campaigns' has no attribute 'normalise_phone'` (and `parse_upload`, `screen_rows`, `NAME_HEADERS`, ...).

- [ ] **Step 3: Append the upload code to `campaigns.py`**

Append to `campaigns.py`:

```python
# --- upload (spec §3) ---

NAME_HEADERS = ("name", "student name", "full name")
PHONE_HEADERS = ("phone", "mobile", "phone number", "mobile number", "contact")
REJECT_REASONS = ("missing_number", "bad_number", "duplicate_in_file", "on_do_not_call", "called_recently")
_PHONE = re.compile(r"[6-9][0-9]{9}")
_NUMBER_CELL = re.compile(r"[0-9]+(\.[0-9]+)?([eE]\+?[0-9]+)?")


class NothingToCall(CampaignError):
    """Every row was rejected; `rejected` lists them for the operator."""

    def __init__(self, rejected):
        super().__init__("No row in the file can be called")
        self.rejected = rejected


def normalise_phone(text):
    """A 10-digit Indian mobile number from a cell's text, or None."""
    text = str(text or "").strip()
    if _NUMBER_CELL.fullmatch(text) and ("." in text or "e" in text.lower()):
        # A number cell: Excel writes 9876543210 as "9876543210", "9876543210.0" or "9.87654321E9".
        try:
            value = Decimal(text)
        except InvalidOperation:
            value = None
        if value is not None and value == value.to_integral_value():
            text = str(int(value))
    digits = re.sub(r"[\s\-().]", "", text)
    if digits.startswith("+"):
        digits = digits[1:]
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    return digits if _PHONE.fullmatch(digits) else None


def _header_key(text):
    return " ".join(str(text).split()).lower()


def _cell(row, i):
    return str(row[i]).strip()[:MAX_CELL_CHARS] if i < len(row) else ""


def parse_upload(data):
    """The first sheet of an .xlsx upload: (rows, extra_headers, extras_dropped). Raises
    CampaignError (nothing stored) for a file it cannot use."""
    if len(data) > MAX_FILE_BYTES:
        raise CampaignError(f"File is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            if sum(info.file_size for info in z.infolist()) > MAX_UNPACKED_BYTES:
                raise CampaignError("File unpacks to more than 50 MB")
        sheets = read_sheets(io.BytesIO(data))
    except CampaignError:
        raise
    except Exception as e:   # any damage in a file from outside is the same answer to the operator
        raise CampaignError("Not a readable .xlsx file") from e
    rows = [row for row in (sheets[0] if sheets else []) if any(str(c).strip() for c in row)]
    if not rows:
        raise CampaignError("The first sheet is empty")
    header = [str(c).strip() for c in rows[0]]
    keys = [_header_key(c) for c in header]
    name_col = next((i for i, k in enumerate(keys) if k in NAME_HEADERS), None)
    phone_col = next((i for i, k in enumerate(keys) if k in PHONE_HEADERS), None)
    if name_col is None or phone_col is None:
        raise CampaignError("The first row must have a name column (Name / Student Name / Full Name) and a "
                            "phone column (Phone / Mobile / Phone Number / Mobile Number / Contact)")
    extras, seen = [], set()
    for i, text in enumerate(header):
        if i in (name_col, phone_col) or not text or keys[i] in seen:
            continue
        seen.add(keys[i])
        extras.append((i, text))
    dropped = max(0, len(extras) - MAX_EXTRAS)
    extras = extras[:MAX_EXTRAS]
    data_rows = rows[1:]
    if len(data_rows) > MAX_ROWS:
        raise CampaignError(f"More than {MAX_ROWS} rows")
    parsed = [{"sheet_row": n, "name": _cell(row, name_col), "phone_text": _cell(row, phone_col),
               "extra": {text: _cell(row, i) for i, text in extras}}
              for n, row in enumerate(data_rows, start=2)]
    return parsed, [text for _, text in extras], dropped


def screen_rows(conn, rows, now):
    """Split parsed rows into contacts to store and rejected rows with a reason (spec §3)."""
    since = _stamp(now - timedelta(days=RECENT_DAYS))
    dnc = {normalise_phone(r[0]) for r in conn.execute("SELECT phone FROM do_not_call")}
    recent = {normalise_phone(r[0]) for r in conn.execute("SELECT phone FROM calls WHERE created_at >= ?", (since,))}
    recent |= {r[0] for r in conn.execute(
        "SELECT c.phone FROM campaign_contacts c JOIN campaigns k ON k.id = c.campaign_id "
        "WHERE k.created_at >= ? AND c.status != 'cancelled'", (since,))}
    contacts, rejected, seen = [], [], set()
    for row in rows:
        phone = normalise_phone(row["phone_text"])
        if not row["phone_text"]:
            reason = "missing_number"
        elif phone is None:
            reason = "bad_number"
        elif phone in seen:
            reason = "duplicate_in_file"
        elif phone in dnc:
            reason = "on_do_not_call"
        elif phone in recent:
            reason = "called_recently"
        else:
            seen.add(phone)
            contacts.append({"sheet_row": row["sheet_row"], "name": row["name"], "phone": phone, "extra": row["extra"]})
            continue
        rejected.append({"sheet_row": row["sheet_row"], "name": row["name"], "phone": row["phone_text"],
                         "reason": reason})
    return contacts, rejected


def create_from_upload(conn, name, settings, rows, *, created_by, file_name, now):
    """Screen the parsed rows and store a draft. NothingToCall if no row survives (nothing stored)."""
    contacts, rejected = screen_rows(conn, rows, now)
    if not contacts:
        raise NothingToCall(rejected)
    campaign_id = create_campaign(conn, name, settings, created_by=created_by, file_name=file_name,
                                  contacts=contacts, rejected_count=len(rejected), now=now)
    return {"id": campaign_id, "accepted": len(contacts), "rejected": rejected}
```

- [ ] **Step 4: Run the tests to see them pass**

Run: `venv312/Scripts/python.exe -m unittest tests.test_campaign_upload tests.test_campaigns_store -v`
Expected: all OK. Check `test_normalise_phone`'s Devanagari case passes: `_PHONE` uses `[0-9]`, not `\d`.

- [ ] **Step 5: Run the full suite**

Run: `venv312/Scripts/python.exe -m unittest discover -s tests -t .`
Expected: no new failure.

- [ ] **Step 6: Commit (only if the operator asked for commits)**

```bash
git add campaigns.py tests/test_campaign_upload.py
git commit -m "Step 3 Task 2: campaign upload parsing and screening"
```

---

### Task 3: dispatcher decisions

**Files:**
- Modify: `campaigns.py` (append)
- Test: `tests/test_campaign_dispatch.py`

**Interfaces:**
- Consumes: Tasks 1-2 (`_stamp`, `_clock`, `_counts`, `normalise_phone`, the constants).
- Produces (all sync, `conn` first; app.py runs them through `call_store.run`):
  - `due_contacts(conn, now) -> list[dict]` (keys `id`, `campaign_id`, `name`, `phone`, `attempt`); the picked contacts are already `calling` with `attempts + 1`.
  - `record_dial(conn, contact_id, call_uuid)`; `release(conn, contact_id)`; `dial_failed(conn, contact_id) -> bool` (True = the campaign just auto-paused).
  - `RETRY_CAUSES`, `RETRY_CALL_STATUSES`; `hangup_outcome(form: dict) -> "answered" | "retry" | "failed"`.
  - `on_hangup(conn, call_uuid, form: dict, now) -> dict | None` (keys `contact_id`, `campaign_id`, `status`, `cause`, `attempts`; None = not a campaign call).
  - `expire_stuck(conn, now) -> int`; `finish_done(conn, now) -> list[dict]` (keys `id`, `counts`).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_campaign_dispatch.py`:

```python
"""campaigns.py dispatcher: picking, slots, hours, retries, hangups, backstop (docs/spec/campaigns-design.md §2)."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import campaigns
from call_store import IST
from tests.test_campaigns_store import NOW, CampaignCase, add_campaign


def phones(n, start=9000000001):
    return [str(start + i) for i in range(n)]


def at(hour, minute=0):
    return datetime(2026, 10, 13, hour, minute, tzinfo=IST)


class DispatchCase(CampaignCase):
    def due(self, now=NOW):
        return campaigns.due_contacts(self.conn, now)

    def set_contact(self, phone, **values):
        columns = ", ".join(f"{k}=?" for k in values)
        self.conn.execute(f"UPDATE campaign_contacts SET {columns} WHERE phone=?", (*values.values(), phone))


class TestPicking(DispatchCase):
    def test_picks_up_to_the_campaign_concurrency(self):
        add_campaign(self.conn, phones(5), state="running")
        picked = self.due()
        self.assertEqual([p["phone"] for p in picked], ["9000000001", "9000000002"])
        self.assertEqual(picked[0]["attempt"], 1)
        self.assertEqual(self.contact("9000000001")["status"], "calling")
        self.assertEqual(self.contact("9000000001")["attempts"], 1)
        self.assertEqual(self.contact("9000000003")["status"], "pending")

    def test_global_cap_and_round_robin(self):
        first = add_campaign(self.conn, phones(5), {"concurrency": 5}, state="running")
        second = add_campaign(self.conn, phones(5, 9100000001), {"concurrency": 5}, state="running")
        with mock.patch.object(campaigns, "MAX_CONCURRENT", 3):
            picked = self.due()
        self.assertEqual([p["campaign_id"] for p in picked], [first, second, first])

    def test_least_recently_dialled_campaign_goes_first(self):
        first = add_campaign(self.conn, phones(2), state="running")
        second = add_campaign(self.conn, phones(2, 9100000001), state="running")
        self.conn.execute("UPDATE campaigns SET last_dial_at='2026-10-13 09:59:00' WHERE id=?", (first,))
        with mock.patch.object(campaigns, "MAX_CONCURRENT", 1):
            self.assertEqual([p["campaign_id"] for p in self.due()], [second])

    def test_calling_contacts_hold_their_slots(self):
        add_campaign(self.conn, phones(4), state="running")
        self.set_contact("9000000001", status="calling", dialed_at="2026-10-13 09:58:00")
        self.set_contact("9000000002", status="calling", dialed_at="2026-10-13 09:58:00")
        self.assertEqual(self.due(), [])   # after a restart too: the rows are what counts

    def test_live_calls_of_other_campaigns_use_the_global_cap(self):
        paused = add_campaign(self.conn, phones(2), state="paused")
        add_campaign(self.conn, phones(2, 9100000001), state="running")
        self.set_contact("9000000001", status="calling", dialed_at="2026-10-13 09:58:00")
        with mock.patch.object(campaigns, "MAX_CONCURRENT", 2):
            picked = self.due()
        self.assertEqual(len(picked), 1)
        self.assertNotEqual(picked[0]["campaign_id"], paused)

    def test_only_running_campaigns_dial(self):
        for state in ("draft", "paused", "stopped", "done"):
            add_campaign(self.conn, phones(1, 9000000001 + 10 * len(state)), state=state)
        self.assertEqual(self.due(), [])

    def test_no_double_pick(self):
        add_campaign(self.conn, phones(2), state="running")
        self.assertEqual(len(self.due()), 2)
        self.assertEqual(self.due(), [])

    def test_do_not_call_is_cancelled_and_the_next_one_taken(self):
        add_campaign(self.conn, phones(3), {"concurrency": 1}, state="running")
        self.conn.execute("INSERT INTO do_not_call (phone) VALUES ('+91 90000 00001')")
        self.assertEqual([p["phone"] for p in self.due()], ["9000000002"])
        self.assertEqual((self.contact("9000000001")["status"], self.contact("9000000001")["last_cause"]),
                         ("cancelled", "dnc"))

    def test_retry_wait_only_when_due(self):
        add_campaign(self.conn, phones(1), state="running")
        self.set_contact("9000000001", status="retry_wait", attempts=1, next_attempt_at="2026-10-13 10:30:00")
        self.assertEqual(self.due(at(10, 29)), [])
        self.assertEqual(self.due(at(10, 30))[0]["attempt"], 2)


class TestHours(DispatchCase):
    def test_outside_hours_dials_nothing(self):
        add_campaign(self.conn, phones(2), state="running")
        for when in (at(8, 59), at(21, 0), at(23, 30), datetime(2026, 10, 13, 2, 0, tzinfo=IST)):
            self.assertEqual(self.due(when), [], when)
        self.assertEqual(len(self.due(at(20, 59))), 2)

    def test_campaign_hours(self):
        add_campaign(self.conn, phones(1), {"hours_start": "10:00", "hours_end": "12:00"}, state="running")
        self.assertEqual(self.due(at(9, 30)), [])
        self.assertEqual(len(self.due(at(11, 0))), 1)

    def test_env_bounds_win_over_campaign_hours(self):
        add_campaign(self.conn, phones(1), state="running")   # 09:00-21:00 stored
        with mock.patch.object(campaigns, "HOURS_END", "18:00"):
            self.assertEqual(self.due(at(19, 0)), [])

    def test_retry_due_at_night_waits_for_the_window(self):
        add_campaign(self.conn, phones(1), state="running")
        self.set_contact("9000000001", status="retry_wait", attempts=1, next_attempt_at="2026-10-13 22:00:00")
        self.assertEqual(self.due(at(23, 0)), [])
        self.assertEqual(self.due(datetime(2026, 10, 14, 9, 0, tzinfo=IST))[0]["phone"], "9000000001")

    def test_time_zone_is_ist(self):
        add_campaign(self.conn, phones(1), state="running")
        utc_16_00 = datetime(2026, 10, 13, 16, 0, tzinfo=timezone.utc)    # 21:30 IST: outside
        utc_10_30 = datetime(2026, 10, 13, 10, 30, tzinfo=timezone.utc)   # 16:00 IST: inside
        self.assertEqual(self.due(utc_16_00), [])
        self.assertEqual(len(self.due(utc_10_30)), 1)


class TestDialResults(DispatchCase):
    def test_record_dial(self):
        campaign_id = add_campaign(self.conn, phones(1), state="running")
        self.conn.execute("UPDATE campaigns SET dial_errors=2 WHERE id=?", (campaign_id,))
        [picked] = self.due()
        campaigns.record_dial(self.conn, picked["id"], "req-1")
        self.assertEqual(self.contact("9000000001")["call_uuid"], "req-1")
        self.assertEqual(self.campaign(campaign_id)["dial_errors"], 0)

    def test_dial_error_does_not_count_an_attempt(self):
        add_campaign(self.conn, phones(2), state="running")
        self.set_contact("9000000002", status="retry_wait", attempts=1, next_attempt_at="2026-10-13 09:00:00")
        first, second = self.due()
        self.assertFalse(campaigns.dial_failed(self.conn, first["id"]))
        self.assertFalse(campaigns.dial_failed(self.conn, second["id"]))
        self.assertEqual((self.contact("9000000001")["status"], self.contact("9000000001")["attempts"]), ("pending", 0))
        self.assertEqual((self.contact("9000000002")["status"], self.contact("9000000002")["attempts"],
                          self.contact("9000000002")["next_attempt_at"]), ("retry_wait", 1, "2026-10-13 09:00:00"))

    def test_three_dial_errors_pause_the_campaign(self):
        campaign_id = add_campaign(self.conn, phones(3), {"concurrency": 3}, state="running")
        picked = self.due()
        results = [campaigns.dial_failed(self.conn, p["id"]) for p in picked]
        self.assertEqual(results, [False, False, True])
        self.assertEqual((self.campaign(campaign_id)["state"], self.campaign(campaign_id)["state_reason"]),
                         ("paused", "dial_errors"))
        self.assertEqual({self.contact(p)["status"] for p in phones(3)}, {"pending"})

    def test_release(self):
        add_campaign(self.conn, phones(1), state="running")
        [picked] = self.due()
        campaigns.release(self.conn, picked["id"])
        self.assertEqual((self.contact("9000000001")["status"], self.contact("9000000001")["attempts"]), ("pending", 0))


class TestHangupOutcome(unittest.TestCase):
    def test_answered(self):
        self.assertEqual(campaigns.hangup_outcome({"AnswerTime": "2026-10-13 10:00:05", "HangupCause": "NORMAL_CLEARING"}),
                         "answered")
        self.assertEqual(campaigns.hangup_outcome({"BillDuration": "12", "HangupCause": "USER_BUSY"}), "answered")

    def test_retry(self):
        for form in ({"HangupCause": "NO_ANSWER"}, {"HangupCause": "USER_BUSY", "BillDuration": "0", "AnswerTime": ""},
                     {"HangupCause": "Busy Line"}, {"HangupCause": "Ring Timeout Reached"},
                     {"HangupCause": "ORIGINATOR_CANCEL"}, {"CallStatus": "no-answer"}, {"CallStatus": "BUSY"},
                     {"CallStatus": "timeout"}):
            self.assertEqual(campaigns.hangup_outcome(form), "retry", form)

    def test_failed(self):
        for form in ({"HangupCause": "UNALLOCATED_NUMBER"}, {"HangupCause": "NORMAL_CLEARING"}, {},
                     {"BillDuration": "abc", "HangupCause": "CALL_REJECTED"}):
            self.assertEqual(campaigns.hangup_outcome(form), "failed", form)


class TestOnHangup(DispatchCase):
    def setUp(self):
        super().setUp()
        self.campaign_id = add_campaign(self.conn, phones(1), state="running")   # 2 retries, 120 min apart
        [picked] = self.due()
        campaigns.record_dial(self.conn, picked["id"], "req-1")

    def test_not_a_campaign_call(self):
        self.assertIsNone(campaigns.on_hangup(self.conn, "single-call", {"HangupCause": "NO_ANSWER"}, NOW))

    def test_answered_is_done(self):
        result = campaigns.on_hangup(self.conn, "req-1", {"BillDuration": "40", "HangupCause": "NORMAL_CLEARING"}, NOW)
        self.assertEqual((result["status"], result["cause"]), ("done", "NORMAL_CLEARING"))
        self.assertEqual(self.contact("9000000001")["finished_at"], "2026-10-13 10:00:00")

    def test_no_answer_waits_for_a_retry(self):
        campaigns.on_hangup(self.conn, "req-1", {"HangupCause": "NO_ANSWER"}, NOW)
        contact = self.contact("9000000001")
        self.assertEqual((contact["status"], contact["next_attempt_at"], contact["finished_at"]),
                         ("retry_wait", "2026-10-13 12:00:00", None))
        self.assertIsNone(campaigns.on_hangup(self.conn, "req-1", {"HangupCause": "NO_ANSWER"}, NOW))   # once only

    def test_retries_used_up(self):
        self.set_contact("9000000001", attempts=3)   # first dial + 2 retries
        self.assertEqual(campaigns.on_hangup(self.conn, "req-1", {"HangupCause": "USER_BUSY"}, NOW)["status"], "failed")

    def test_hard_failure_is_not_retried(self):
        self.assertEqual(campaigns.on_hangup(self.conn, "req-1", {"HangupCause": "UNALLOCATED_NUMBER"}, NOW)["status"],
                         "failed")

    def test_stopped_campaign_cancels_instead_of_retrying(self):
        campaigns.apply_action(self.conn, self.campaign_id, "stop", NOW)
        result = campaigns.on_hangup(self.conn, "req-1", {"HangupCause": "NO_ANSWER"}, NOW)
        self.assertEqual((result["status"], self.contact("9000000001")["last_cause"]), ("cancelled", "stopped"))

    def test_whole_life_of_a_contact(self):
        campaigns.on_hangup(self.conn, "req-1", {"HangupCause": "NO_ANSWER"}, NOW)
        self.assertEqual(self.due(NOW + timedelta(minutes=119)), [])
        [second] = self.due(NOW + timedelta(minutes=120))
        self.assertEqual(second["attempt"], 2)
        campaigns.record_dial(self.conn, second["id"], "req-2")
        campaigns.on_hangup(self.conn, "req-2", {"AnswerTime": "2026-10-13 12:00:09"}, NOW + timedelta(minutes=125))
        self.assertEqual(self.contact("9000000001")["status"], "done")
        [finished] = campaigns.finish_done(self.conn, NOW + timedelta(minutes=126))
        self.assertEqual((finished["id"], finished["counts"]["done"]), (self.campaign_id, 1))


class TestBackstopAndDone(DispatchCase):
    def test_backstop_ends_a_lost_call_without_retry(self):
        add_campaign(self.conn, phones(2), state="running")
        self.set_contact("9000000001", status="calling", attempts=1, dialed_at="2026-10-13 09:29:00")
        self.set_contact("9000000002", status="calling", attempts=1, dialed_at="2026-10-13 09:31:00")
        self.assertEqual(campaigns.expire_stuck(self.conn, NOW), 1)
        self.assertEqual((self.contact("9000000001")["status"], self.contact("9000000001")["last_cause"]),
                         ("done", "no_hangup_event"))
        self.assertEqual(self.contact("9000000002")["status"], "calling")

    def test_finish_done(self):
        running = add_campaign(self.conn, phones(1), state="running")
        paused = add_campaign(self.conn, phones(1, 9100000001), state="paused")
        stopped = add_campaign(self.conn, phones(1, 9200000001), state="stopped")
        self.set_contact("9000000001", status="failed")
        self.set_contact("9100000001", status="retry_wait")
        self.set_contact("9200000001", status="cancelled")
        self.assertEqual([f["id"] for f in campaigns.finish_done(self.conn, NOW)], [running])
        self.assertEqual(self.campaign(running)["state"], "done")
        self.assertEqual(self.campaign(running)["finished_at"], "2026-10-13 10:00:00")
        self.assertEqual((self.campaign(paused)["state"], self.campaign(stopped)["state"]), ("paused", "stopped"))
        self.assertEqual(campaigns.finish_done(self.conn, NOW), [])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to see them fail**

Run: `venv312/Scripts/python.exe -m unittest tests.test_campaign_dispatch -v`
Expected: errors, `AttributeError: module 'campaigns' has no attribute 'due_contacts'` (and the other new names).

- [ ] **Step 3: Append the dispatcher code to `campaigns.py`**

Append to `campaigns.py`:

```python
# --- dispatcher decisions (spec §2); app.py does the waiting and the Plivo calls ---

def _calling(conn, campaign_id=None):
    if campaign_id is None:
        return conn.execute("SELECT COUNT(*) FROM campaign_contacts WHERE status='calling'").fetchone()[0]
    return conn.execute("SELECT COUNT(*) FROM campaign_contacts WHERE status='calling' AND campaign_id=?",
                        (campaign_id,)).fetchone()[0]


def _inside(clock, start, end):
    """Inside the campaign's hours and the .env bounds; the end minute is not included."""
    return max(start, HOURS_START) <= clock < min(end, HOURS_END)


def due_contacts(conn, now):
    """Pick the contacts to dial now and mark them calling (attempts + 1) in the same transaction,
    so no later tick can pick them again. Campaigns take turns, least recently dialled first."""
    stamp, clock = _stamp(now), _clock(now)
    room = MAX_CONCURRENT - _calling(conn)
    slots = {}
    for campaign_id, concurrency, start, end in conn.execute(
            "SELECT id, concurrency, hours_start, hours_end FROM campaigns WHERE state='running' "
            "ORDER BY COALESCE(last_dial_at, ''), id").fetchall():
        if _inside(clock, start, end):
            slots[campaign_id] = concurrency - _calling(conn, campaign_id)
    dnc = {normalise_phone(r[0]) for r in conn.execute("SELECT phone FROM do_not_call")}
    picked = []
    while room > 0 and any(n > 0 for n in slots.values()):
        for campaign_id in slots:
            if room <= 0:
                break
            if slots[campaign_id] <= 0:
                continue
            found = conn.execute(
                "SELECT id, name, phone, attempts FROM campaign_contacts WHERE campaign_id=? AND "
                "(status='pending' OR (status='retry_wait' AND next_attempt_at <= ?)) ORDER BY id LIMIT 1",
                (campaign_id, stamp)).fetchone()
            if found is None:
                slots[campaign_id] = 0
                continue
            contact_id, name, phone, attempts = found
            if phone in dnc:
                conn.execute("UPDATE campaign_contacts SET status='cancelled', last_cause='dnc', finished_at=? "
                             "WHERE id=?", (stamp, contact_id))
                continue
            conn.execute("UPDATE campaign_contacts SET status='calling', attempts=attempts+1, dialed_at=?, "
                         "call_uuid=NULL WHERE id=?", (stamp, contact_id))
            conn.execute("UPDATE campaigns SET last_dial_at=? WHERE id=?", (stamp, campaign_id))
            slots[campaign_id] -= 1
            room -= 1
            picked.append({"id": contact_id, "campaign_id": campaign_id, "name": name, "phone": phone,
                           "attempt": attempts + 1})
    return picked


def record_dial(conn, contact_id, call_uuid):
    """Plivo accepted the call: keep its id (the hangup finds the contact by it)."""
    conn.execute("UPDATE campaign_contacts SET call_uuid=? WHERE id=?", (call_uuid, contact_id))
    conn.execute("UPDATE campaigns SET dial_errors=0 WHERE id=(SELECT campaign_id FROM campaign_contacts WHERE id=?)",
                 (contact_id,))


def release(conn, contact_id):
    """Undo a pick that was never dialled: back to pending (first attempt) or retry_wait, no attempt counted.
    SQLite evaluates the whole SET list against the old row, so CASE sees the old attempts."""
    conn.execute("UPDATE campaign_contacts SET attempts=attempts-1, dialed_at=NULL, "
                 "status=CASE WHEN attempts > 1 THEN 'retry_wait' ELSE 'pending' END "
                 "WHERE id=? AND status='calling'", (contact_id,))


def dial_failed(conn, contact_id):
    """Plivo refused the call. Returns True if this error paused the campaign (DIAL_ERRORS_TO_PAUSE in a row)."""
    release(conn, contact_id)
    campaign_id = conn.execute("SELECT campaign_id FROM campaign_contacts WHERE id=?", (contact_id,)).fetchone()[0]
    conn.execute("UPDATE campaigns SET dial_errors=COALESCE(dial_errors, 0)+1 WHERE id=?", (campaign_id,))
    errors = conn.execute("SELECT dial_errors FROM campaigns WHERE id=?", (campaign_id,)).fetchone()[0]
    if errors < DIAL_ERRORS_TO_PAUSE:
        return False
    conn.execute("UPDATE campaigns SET state='paused', state_reason='dial_errors' WHERE id=? AND state='running'",
                 (campaign_id,))
    return True


# Not answered and worth another try. Plivo has sent both the SIP-style names (app.py's hangup handler
# sees USER_BUSY / NORMAL_CLEARING) and readable ones; CallStatus is the fallback.
RETRY_CAUSES = frozenset({"NO_ANSWER", "USER_BUSY", "ORIGINATOR_CANCEL", "Busy Line", "No Answer",
                          "Ring Timeout Reached"})
RETRY_CALL_STATUSES = frozenset({"busy", "no-answer", "timeout"})


def _answered(form):
    try:
        billed = float(form.get("BillDuration") or 0)
    except ValueError:
        billed = 0
    return bool(str(form.get("AnswerTime") or "").strip()) or billed > 0


def hangup_outcome(form):
    if _answered(form):
        return "answered"
    if form.get("HangupCause") in RETRY_CAUSES or str(form.get("CallStatus") or "").lower() in RETRY_CALL_STATUSES:
        return "retry"
    return "failed"


def on_hangup(conn, call_uuid, form, now):
    """Settle the contact behind a Plivo hangup. None if it is not a live campaign call."""
    # ponytail: a hangup that beats record_dial (Plivo failing within ~1 s) finds no row; the
    # backstop ends that contact after CALL_TIMEOUT_MIN as done/no_hangup_event. Re-look-up after a
    # short wait if live logs show it happening.
    found = conn.execute(
        "SELECT c.id, c.campaign_id, c.attempts, k.max_retries, k.retry_gap_min, k.state "
        "FROM campaign_contacts c JOIN campaigns k ON k.id = c.campaign_id "
        "WHERE c.call_uuid=? AND c.status='calling'", (call_uuid,)).fetchone()
    if found is None:
        return None
    contact_id, campaign_id, attempts, max_retries, gap, state = found
    outcome = hangup_outcome(form)
    cause = str(form.get("HangupCause") or form.get("CallStatus") or "")[:100]
    next_at = None
    if outcome == "answered":
        status = "done"
    elif outcome == "retry" and attempts <= max_retries:
        if state == "stopped":
            status, cause = "cancelled", "stopped"
        else:
            status, next_at = "retry_wait", _stamp(now + timedelta(minutes=gap))
    else:
        status = "failed"
    conn.execute("UPDATE campaign_contacts SET status=?, next_attempt_at=?, last_cause=?, finished_at=? WHERE id=?",
                 (status, next_at, cause, None if status == "retry_wait" else _stamp(now), contact_id))
    return {"contact_id": contact_id, "campaign_id": campaign_id, "status": status, "cause": cause,
            "attempts": attempts}


def expire_stuck(conn, now):
    """Backstop: a contact still calling CALL_TIMEOUT_MIN after its dial lost its hangup. Done, never
    retried, so nobody is called twice by mistake. Returns how many."""
    cutoff = _stamp(now - timedelta(minutes=CALL_TIMEOUT_MIN))
    return conn.execute("UPDATE campaign_contacts SET status='done', last_cause='no_hangup_event', finished_at=? "
                        "WHERE status='calling' AND dialed_at < ?", (_stamp(now), cutoff)).rowcount


def finish_done(conn, now):
    """Running or paused campaigns with nothing left to call become done. Returns [{id, counts}]."""
    ids = [r[0] for r in conn.execute(
        "SELECT id FROM campaigns k WHERE state IN ('running', 'paused') AND NOT EXISTS "
        "(SELECT 1 FROM campaign_contacts c WHERE c.campaign_id = k.id "
        "AND c.status IN ('pending', 'calling', 'retry_wait')) ORDER BY id")]
    for campaign_id in ids:
        conn.execute("UPDATE campaigns SET state='done', finished_at=? WHERE id=?", (_stamp(now), campaign_id))
    return [{"id": campaign_id, "counts": _counts(conn, campaign_id)} for campaign_id in ids]
```

- [ ] **Step 4: Run the tests to see them pass**

Run: `venv312/Scripts/python.exe -m unittest tests.test_campaign_dispatch tests.test_campaign_upload tests.test_campaigns_store -v`
Expected: all OK.

- [ ] **Step 5: Run the full suite**

Run: `venv312/Scripts/python.exe -m unittest discover -s tests -t .`
Expected: no new failure.

- [ ] **Step 6: Commit (only if the operator asked for commits)**

```bash
git add campaigns.py tests/test_campaign_dispatch.py
git commit -m "Step 3 Task 3: campaign dispatcher decisions"
```

---

### Task 4: wire it into `app.py`

**Files:**
- Modify: `app.py` (imports ~line 57; `init_call_history_db` ~line 1055; `/plivo-hangup` ~line 3810; `/trigger-call` ~line 3840; `start_background_work` ~line 3885)
- Modify: `web_auth.py` (`ENDPOINT_ACCESS`, ~line 44)
- Modify: `tests/harness/appctl.py` (`DIAGNOSTIC_LOG_TAGS`, ~line 262)
- Modify: `tests/test_preservation_4_8_resumption_recording_stats.py` (re-pin)
- Test: `tests/test_campaign_wiring.py`

**Interfaces:**
- Consumes: everything `campaigns.py` produces; `call_store.run`; `app.spawn_background`; test bases `tests.test_access_control.AuthTestCase` / `run`, `tests.test_analysis_wiring.DbCase`; helpers `tests.test_campaign_upload.make_xlsx`, `tests.test_campaigns_store.BOUNDS` / `NOW` / `add_campaign`.
- Produces (app.py): `async place_call(name, phone) -> str`; `async campaign_tick(now=None)`; `async campaign_loop()`; `async settle_campaign_contact(call_uuid, form)`; globals `CAMPAIGN_TASK` (asyncio.Task or None), `_hangup_fields_logged` (bool); routes with endpoints `campaigns_page`, `list_campaigns_api`, `create_campaign_api`, `campaign_detail_api`, `campaign_action_api`, `campaign_settings_api`; helpers `json_body()`, `campaign_error(e)`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_campaign_wiring.py`:

```python
"""Step 3 wiring in app.py: routes, the tick, the hangup hook, startup (docs/spec/campaigns-design.md §2, §4, §5)."""

from __future__ import annotations

import asyncio
import base64
import json
import sqlite3
import types
import unittest
from datetime import datetime
from unittest import mock

from call_store import IST
from tests.harness.appctl import DIAGNOSTIC_LOG_TAGS, LogCapture, app
from tests.test_access_control import AuthTestCase, run
from tests.test_analysis_wiring import DbCase
from tests.test_campaign_upload import make_xlsx
from tests.test_campaigns_store import BOUNDS, NOW, add_campaign

web_auth = app.web_auth
FILE = make_xlsx([["Name", "Phone", "City"], ["Asha", "9876543210", "Kolkata"], ["Bad", "12345", "Howrah"]])


def b64(data):
    return base64.b64encode(data).decode()


class BoundsMixin:
    def patch_bounds(self):
        patcher = mock.patch.multiple(app.campaigns, **BOUNDS)
        patcher.start()
        self.addCleanup(patcher.stop)

    def db(self):
        conn = sqlite3.connect(app.CALL_HISTORY_DB_PATH)
        self.addCleanup(conn.close)
        return conn

    def contact(self, phone):
        found = self.db().execute("SELECT status, attempts, call_uuid, last_cause FROM campaign_contacts WHERE phone=?",
                                  (phone,)).fetchone()
        return dict(zip(("status", "attempts", "call_uuid", "last_cause"), found))


class CampaignApiCase(BoundsMixin, AuthTestCase):
    def setUp(self):
        super().setUp()
        self.patch_bounds()

    def call(self, role, method, path, **kwargs):
        async def go():
            client = await self.client(role)
            response = await getattr(client, method)(path, **kwargs)
            return response.status_code, await response.get_json()
        return run(go())

    def upload(self, role="operator", **overrides):
        body = {"name": "October list", "file_name": "list.xlsx", "file_b64": b64(FILE), **overrides}
        return self.call(role, "post", "/api/campaigns", json=body)


class TestAccess(CampaignApiCase):
    def test_pages_and_apis_need_a_login(self):
        self.assertEqual(self.get(None, "/campaigns")[:2], (302, "/login"))
        self.assertEqual(self.get(None, "/api/campaigns")[0], 401)
        self.assertEqual(self.get(None, "/api/campaigns/1")[0], 401)
        for role in ("operator", "admin"):
            self.assertEqual(self.get(role, "/campaigns")[0], 200, role)
            self.assertEqual(self.get(role, "/api/campaigns")[0], 200, role)

    def test_posts_need_a_login(self):
        self.assertEqual(self.call(None, "post", "/api/campaigns", json={})[0], 401)
        self.assertEqual(self.call(None, "post", "/api/campaigns/1/action", json={"action": "start"})[0], 401)
        self.assertEqual(self.call(None, "post", "/api/campaigns/1/settings", json={})[0], 401)

    def test_every_campaign_route_is_open_to_both_roles(self):
        for endpoint in ("campaigns_page", "list_campaigns_api", "create_campaign_api", "campaign_detail_api",
                         "campaign_action_api", "campaign_settings_api"):
            self.assertIn(web_auth.ENDPOINT_ACCESS[endpoint], (web_auth.OPERATOR_PAGE, web_auth.OPERATOR_API))


class TestUpload(CampaignApiCase):
    def test_upload_creates_a_draft(self):
        status, body = self.upload()
        self.assertEqual(status, 200, body)
        self.assertEqual((body["accepted"], body["extra_columns"], body["extras_dropped"]), (1, ["City"], 0))
        self.assertEqual(body["rejected"], [{"sheet_row": 3, "name": "Bad", "phone": "12345", "reason": "bad_number"}])
        state, created_by, file_name = self.db().execute("SELECT state, created_by, file_name FROM campaigns").fetchone()
        self.assertEqual((state, created_by, file_name), ("draft", "operator", "list.xlsx"))

    def test_admin_can_upload_too(self):
        self.assertEqual(self.upload("admin")[0], 200)

    def test_json_only(self):
        self.assertEqual(self.call("operator", "post", "/api/campaigns", form={"name": "x"})[0], 415)
        self.assertEqual(self.call("operator", "post", "/api/campaigns", json=["x"])[0], 400)

    def test_bad_inputs_are_400_and_store_nothing(self):
        for overrides in ({"file_b64": "not base64!!"}, {"file_b64": b64(b"name,phone")}, {"name": ""},
                          {"concurrency": 9}, {"hours_start": "07:00"}):
            status, body = self.upload(**overrides)
            self.assertEqual(status, 400, overrides)
            self.assertEqual(body["status"], "error")
        self.assertEqual(self.db().execute("SELECT COUNT(*) FROM campaigns").fetchone()[0], 0)

    def test_all_rejected_file_lists_the_rows(self):
        status, body = self.upload(file_b64=b64(make_xlsx([["Name", "Phone"], ["A", "123"]])))
        self.assertEqual((status, [r["reason"] for r in body["rejected"]]), (400, ["bad_number"]))


class TestControls(CampaignApiCase):
    def setUp(self):
        super().setUp()
        self.campaign_id = self.upload()[1]["id"]

    def action(self, action, campaign_id=None, role="operator"):
        return self.call(role, "post", f"/api/campaigns/{campaign_id or self.campaign_id}/action", json={"action": action})

    def test_moves(self):
        self.assertEqual(self.action("start"), (200, {"status": "success", "state": "running"}))
        self.assertEqual(self.action("start")[0], 409)
        self.assertEqual(self.action("pause", role="admin")[1]["state"], "paused")
        self.assertEqual(self.action("explode")[0], 400)
        self.assertEqual(self.action("start", campaign_id=999)[0], 404)
        self.assertEqual(self.call("operator", "post", f"/api/campaigns/{self.campaign_id}/action", form={})[0], 415)

    def test_settings(self):
        path = f"/api/campaigns/{self.campaign_id}/settings"
        good = {"concurrency": 3, "hours_start": "10:00", "hours_end": "18:00", "max_retries": 1, "retry_gap_min": 60}
        self.assertEqual(self.call("operator", "post", path, json=good)[0], 200)
        self.assertEqual(self.call("operator", "post", path, json={**good, "concurrency": 9})[0], 400)
        self.assertEqual(self.call("operator", "post", path, json={"concurrency": 3})[0], 400)   # all five required
        self.action("start")
        self.assertEqual(self.call("operator", "post", path, json=good)[0], 409)
        self.assertEqual(self.call("operator", "post", "/api/campaigns/999/settings", json=good)[0], 404)

    def test_detail_and_list_show_the_lead_but_never_a_cost(self):
        conn = self.db()
        with conn:
            conn.execute("UPDATE campaign_contacts SET status='done', call_uuid='u-1'")
            conn.execute("INSERT INTO calls (call_uuid, name, phone, cost, gemini_cost_usd) "
                         "VALUES ('u-1', 'Asha', '9876543210', '$0.14', 0.14)")
            conn.execute("INSERT INTO call_analysis (call_uuid, status, lead_status, campus_visit_day, summary) "
                         "VALUES ('u-1', 'ok', 'hot', 'Saturday', 'Wants B.Tech.')")
        for role in ("operator", "admin"):
            status, detail = self.call(role, "get", f"/api/campaigns/{self.campaign_id}")
            self.assertEqual(status, 200)
            self.assertEqual(detail["contacts"][0]["lead_status"], "hot")
            _, listing = self.call(role, "get", "/api/campaigns")
            self.assertEqual(listing["limits"]["max_concurrent"], 8)
            self.assertNotIn("cost", json.dumps([detail, listing]))
        self.assertEqual(self.call("operator", "get", f"/api/campaigns/{self.campaign_id}?status=bogus")[0], 400)
        self.assertEqual(self.call("operator", "get", f"/api/campaigns/{self.campaign_id}?page=x")[0], 200)
        self.assertEqual(self.call("operator", "get", "/api/campaigns/999")[0], 404)


class TickCase(BoundsMixin, DbCase):
    def setUp(self):
        super().setUp()
        self.patch_bounds()
        base_url = mock.patch.object(app, "PUBLIC_BASE_URL", "https://agent.example.com")
        base_url.start()
        self.addCleanup(base_url.stop)
        emit = mock.patch.object(app, "emit_call_event")
        self.emit = emit.start()
        self.addCleanup(emit.stop)

    def running(self, phones, **settings):
        conn = self.db()
        with conn:
            return add_campaign(conn, phones, settings, state="running")


class TestTick(TickCase):
    def test_tick_dials_and_stores_the_call_uuid(self):
        self.running(["9876543210"])
        made = types.SimpleNamespace(request_uuid="req-1")
        with mock.patch.object(app.plivo_client.calls, "create", return_value=made) as create, LogCapture() as log:
            run(app.campaign_tick(NOW))
        self.assertEqual(create.call_args.kwargs["to_"], "+919876543210")
        self.assertIn("/outbound-webhook?", create.call_args.kwargs["answer_url"])
        self.assertEqual(create.call_args.kwargs["hangup_url"], "https://agent.example.com/plivo-hangup")
        self.assertEqual(self.contact("9876543210"), {"status": "calling", "attempts": 1, "call_uuid": "req-1",
                                                      "last_cause": None})
        self.emit.assert_called_once_with("req-1", "call_queued", {"name": "Contact 0", "phone": "9876543210"})
        dial_lines = [line for line in log.lines if "[CAMPAIGN] dial" in line]
        self.assertEqual(len(dial_lines), 1, log.lines)
        self.assertIn("xxxxxx3210", dial_lines[0])
        self.assertFalse(any("9876543210" in line or "Contact 0" in line for line in log.lines), log.lines)

    def test_empty_name_goes_to_plivo_as_unknown(self):
        campaign_id = self.running(["9876543210"])
        conn = self.db()
        with conn:
            conn.execute("UPDATE campaign_contacts SET name='' WHERE campaign_id=?", (campaign_id,))
        with mock.patch.object(app.plivo_client.calls, "create", return_value=types.SimpleNamespace(request_uuid="r")) as create:
            run(app.campaign_tick(NOW))
        self.assertIn("user_name=Unknown", create.call_args.kwargs["answer_url"])

    def test_three_dial_errors_pause_and_spare_the_contacts(self):
        campaign_id = self.running(["9000000001", "9000000002", "9000000003", "9000000004"], concurrency=4)
        with mock.patch.object(app.plivo_client.calls, "create", side_effect=RuntimeError("plivo down")) as create, \
                LogCapture() as log:
            run(app.campaign_tick(NOW))
        self.assertEqual(create.call_count, 3)
        state, reason = self.db().execute("SELECT state, state_reason FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
        self.assertEqual((state, reason), ("paused", "dial_errors"))
        self.assertEqual({(self.contact(p)["status"], self.contact(p)["attempts"])
                          for p in ("9000000001", "9000000002", "9000000003", "9000000004")}, {("pending", 0)})
        self.assertFalse(any("plivo down" in line for line in log.lines))   # error text can hold a number

    def test_tick_outside_hours_dials_nothing(self):
        self.running(["9876543210"])
        with mock.patch.object(app.plivo_client.calls, "create") as create:
            run(app.campaign_tick(datetime(2026, 10, 13, 22, 0, tzinfo=IST)))
        create.assert_not_called()

    def test_tick_runs_the_backstop_and_finishes_campaigns(self):
        campaign_id = self.running(["9876543210"])
        conn = self.db()
        with conn:
            conn.execute("UPDATE campaign_contacts SET status='calling', attempts=1, dialed_at='2026-10-13 09:00:00'")
        with mock.patch.object(app.plivo_client.calls, "create") as create:
            run(app.campaign_tick(NOW))
        create.assert_not_called()
        self.assertEqual(self.contact("9876543210")["last_cause"], "no_hangup_event")
        self.assertEqual(self.db().execute("SELECT state FROM campaigns WHERE id=?", (campaign_id,)).fetchone()[0], "done")

    def test_loop_survives_a_failing_tick(self):
        calls = []

        async def failing_tick():
            calls.append(1)
            raise sqlite3.OperationalError("database is locked")

        async def go():
            with mock.patch.object(app, "campaign_tick", failing_tick), mock.patch.object(app.campaigns, "TICK_S", 0):
                try:
                    await asyncio.wait_for(app.campaign_loop(), 0.2)
                except asyncio.TimeoutError:
                    pass

        with LogCapture() as log:
            run(go())
        self.assertGreaterEqual(len(calls), 2)
        self.assertTrue(any("[CAMPAIGN] tick failed" in line for line in log.lines))


class TestHangupHook(TickCase):
    def setUp(self):
        super().setUp()
        for patcher in (mock.patch.object(web_auth, "PLIVO_SIGNATURE_CHECK", "off"),
                        mock.patch.object(app, "EVENT_FORWARD_URL", ""),
                        mock.patch.object(app, "_hangup_fields_logged", False),
                        mock.patch.object(app.plivo_cost, "schedule")):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.running(["9876543210"])
        conn = self.db()
        with conn:
            conn.execute("UPDATE campaign_contacts SET status='calling', attempts=1, call_uuid='req-1', "
                         "dialed_at='2026-10-13 09:59:00'")

    def post(self, form):
        async def go():
            status = (await app.app.test_client().post("/plivo-hangup", form=form)).status_code
            await asyncio.gather(*app._background_tasks)
            return status
        return run(go())

    def test_hangup_webhook_settles_the_contact(self):
        with LogCapture() as log:
            self.assertEqual(self.post({"CallUUID": "req-1", "HangupCause": "NO_ANSWER", "CallStatus": "no-answer",
                                        "BillDuration": "0", "AnswerTime": "", "To": "+919876543210"}), 200)
        self.assertEqual(self.contact("9876543210")["status"], "retry_wait")
        fields = [line for line in log.lines if "[CAMPAIGN] hangup fields" in line]
        self.assertEqual(len(fields), 1)
        self.assertIn("HangupCause", fields[0])
        self.assertFalse(any("9876543210" in line for line in log.lines if "[CAMPAIGN]" in line))

    def test_single_call_hangup_changes_nothing(self):
        self.post({"CallUUID": "other", "HangupCause": "NO_ANSWER"})
        self.assertEqual(self.contact("9876543210")["status"], "calling")

    def test_media_process_does_not_write(self):
        with mock.patch.object(app, "EVENT_FORWARD_URL", "http://127.0.0.1:8000/internal/call-event"):
            self.post({"CallUUID": "req-1", "HangupCause": "NO_ANSWER"})
        self.assertEqual(self.contact("9876543210")["status"], "calling")


class TestStartup(DbCase):
    def setUp(self):
        super().setUp()
        for patcher in (mock.patch.object(app, "CAMPAIGN_TASK", None),
                        mock.patch.object(app, "ANALYZER", None),
                        mock.patch.object(app.call_analysis, "ANALYSIS_ENABLED", False),
                        mock.patch.object(app.plivo_cost, "PLIVO_COST_ENABLED", False)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def started(self, forward, enabled):
        async def go():
            with mock.patch.object(app, "EVENT_FORWARD_URL", forward), \
                    mock.patch.object(app.campaigns, "CAMPAIGNS_ENABLED", enabled):
                await app.start_background_work()
                task = app.CAMPAIGN_TASK
                if task is not None:
                    task.cancel()
                return isinstance(task, asyncio.Task)
        return run(go())

    def test_main_process_starts_the_dispatcher(self):
        with LogCapture() as log:
            self.assertTrue(self.started("", True))
        self.assertTrue(any("[CAMPAIGN] dispatcher started" in line for line in log.lines))

    def test_switched_off_or_media_process_starts_nothing(self):
        self.assertFalse(self.started("", False))
        self.assertFalse(self.started("http://127.0.0.1:8000/internal/call-event", True))

    def test_main_process_uses_wal(self):
        with mock.patch.object(app, "EVENT_FORWARD_URL", ""):
            app.init_call_history_db()
        conn = sqlite3.connect(app.CALL_HISTORY_DB_PATH)
        try:
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            conn.close()
        self.assertTrue({"campaigns", "campaign_contacts", "do_not_call"} <= tables)

    def test_log_tag(self):
        self.assertIn("[CAMPAIGN]", DIAGNOSTIC_LOG_TAGS)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to see them fail**

Run: `venv312/Scripts/python.exe -m unittest tests.test_campaign_wiring -v`
Expected: many errors/failures: `/campaigns` 404s, `AttributeError: module 'app' has no attribute 'campaign_tick'` / `campaigns` / `CAMPAIGN_TASK`, `[CAMPAIGN]` missing from the tags.

- [ ] **Step 3: Log tag and access table**

In `tests/harness/appctl.py`, add one entry at the end of `DIAGNOSTIC_LOG_TAGS` (after `"[PLIVO-COST]", ...`):

```python
    "[CAMPAIGN]",     # step 3: campaign dispatcher, uploads and hangup results (campaigns.py)
```

In `web_auth.py`, add to `ENDPOINT_ACCESS` right after `"admin_page": ADMIN_PAGE,`:

```python
    "campaigns_page": OPERATOR_PAGE,
    "list_campaigns_api": OPERATOR_API,
    "create_campaign_api": OPERATOR_API,
    "campaign_detail_api": OPERATOR_API,
    "campaign_action_api": OPERATOR_API,
    "campaign_settings_api": OPERATOR_API,
```

- [ ] **Step 4: Import, tables and WAL**

In `app.py`, after the line `import plivo_cost` (~line 59), add:

```python
import campaigns
```

In `init_call_history_db`, change the body so it reads:

```python
def init_call_history_db():
    # timeout=30: four processes run the schema upgrade at once on first deploy.
    with sqlite3.connect(CALL_HISTORY_DB_PATH, timeout=30) as conn:
        if not EVENT_FORWARD_URL:
            conn.execute("PRAGMA journal_mode=WAL")   # main process; the mode stays in the file
        conn.execute("""
            CREATE TABLE IF NOT EXISTS calls (
```

(the `CREATE TABLE calls` block unchanged), and after `call_store.upgrade(conn)   # step 2 ...` add:

```python
        campaigns.upgrade(conn)    # step 3 campaign tables; every process, safe on every start
```

- [ ] **Step 5: `place_call` out of `/trigger-call`**

Replace the `trigger_call` function body from `encoded_name = ...` to the end of its `try` block so that, together with a new `place_call` right above the route, it reads exactly:

```python
async def place_call(name, phone):
    """Create one outbound Plivo call and emit call_queued; returns the call_uuid (Plivo's
    request_uuid). Raises on a Plivo error. Shared by /trigger-call and the campaign dispatcher."""
    encoded_name = urllib.parse.quote(name)
    encoded_phone = urllib.parse.quote(phone)

    answer_url = f"{PUBLIC_BASE_URL}/outbound-webhook?user_name={encoded_name}&phone_number={encoded_phone}"
    hangup_url = f"{PUBLIC_BASE_URL}/plivo-hangup"
    ring_url = f"{PUBLIC_BASE_URL}/plivo-ringing"

    call_made = await asyncio.to_thread(
        plivo_client.calls.create,
        from_=PLIVO_PHONE_NUMBER,
        to_="+91" + phone,
        answer_url=answer_url,
        answer_method='GET',
        ring_url=ring_url,
        ring_method='POST',
        hangup_url=hangup_url,
        hangup_method='POST'
    )
    logger.info(f"✅ Outbound call successfully queued: {call_made}")
    call_id = str(getattr(call_made, 'request_uuid', call_made))
    emit_call_event(call_id, "call_queued", {"name": name, "phone": phone})
    return call_id

@app.route("/trigger-call", methods=["POST"])
async def trigger_call():
    # The login session (enforce_access) decides who may call. JSON only: a cross-site form cannot
    # send it with our cookie, which blocks CSRF.
    if not request.is_json:
        return {"status": "error", "message": "JSON body required"}, 415
    data = await request.get_json(silent=True)
    if not isinstance(data, dict):
        return {"status": "error", "message": "JSON object required"}, 400
    
    target_user_name = data.get("user_name", "Unknown")
    target_phone_number = data.get("phone_number")
    
    if not target_phone_number:
        return {"status": "error", "message": "Phone number is required"}, 400

    try:
        logger.info(f"Initiating outbound call to {target_phone_number} via Dashboard...")
        call_id = await place_call(target_user_name, target_phone_number)
        return {"status": "success", "message": "Call queued", "call_uuid": call_id}, 200
    except Exception as e:
        logger.error(f"❌ Failed to initiate outbound call: {e}")
        emit_call_event("", "call_failed", {"name": target_user_name, "phone": target_phone_number, "error": str(e)})
        return {"status": "error", "message": str(e)}, 500
```

The route line stays byte-identical; the replies and log lines of `/trigger-call` are the same as before.

- [ ] **Step 6: Hangup hook**

In `plivo_hangup`, right after the `plivo_cost.schedule(...)` block (the `if call_uuid and plivo_cost.PLIVO_COST_ENABLED and not EVENT_FORWARD_URL:` two lines), add:

```python
    if call_uuid and not EVENT_FORWARD_URL:
        # A campaign contact behind this call is settled in the background; Plivo is not kept waiting.
        # Plain dict of first values: dict() on a werkzeug MultiDict can give lists.
        spawn_background(settle_campaign_contact(call_uuid, {key: data.get(key) for key in data}))
```

`spawn_background` and `settle_campaign_contact` are module functions; they resolve at call time, so the order of definitions in the file does not matter.

- [ ] **Step 7: Dispatcher, hangup settling and routes**

In `app.py`, right after the end of `trigger_call` (before `@app.before_serving`), add:

```python
# --- Step 3: bulk campaigns (docs/spec/campaigns-design.md). Main process only. ---

CAMPAIGN_TASK = None            # the dispatcher loop, started by start_background_work
_hangup_fields_logged = False   # the first campaign hangup logs Plivo's field names once (never values)


async def campaign_tick(now=None):
    """One dispatcher pass: backstop, finished campaigns, then dial what is due, spaced out."""
    now = now or datetime.now(ist_tz)
    expired = await call_store.run(CALL_HISTORY_DB_PATH, campaigns.expire_stuck, now)
    if expired:
        logger.warning(f"📣 [CAMPAIGN] backstop: {expired} contact(s) had no hangup within "
                       f"{campaigns.CALL_TIMEOUT_MIN} min, marked done")
    for finished in await call_store.run(CALL_HISTORY_DB_PATH, campaigns.finish_done, now):
        counts = " ".join(f"{k}={v}" for k, v in finished["counts"].items())
        logger.info(f"📣 [CAMPAIGN] campaign={finished['id']} done {counts}")
    picked = await call_store.run(CALL_HISTORY_DB_PATH, campaigns.due_contacts, now)
    paused = set()
    for i, contact in enumerate(picked):
        if contact["campaign_id"] in paused:
            await call_store.run(CALL_HISTORY_DB_PATH, campaigns.release, contact["id"])
            continue
        if i:
            await asyncio.sleep(campaigns.DIAL_GAP_S)
        try:
            call_uuid = await place_call(contact["name"] or "Unknown", contact["phone"])
        except Exception as e:   # type only: Plivo's error text can hold the number
            logger.error(f"❌ [CAMPAIGN] dial failed campaign={contact['campaign_id']} contact={contact['id']}: "
                         f"{type(e).__name__}")
            if await call_store.run(CALL_HISTORY_DB_PATH, campaigns.dial_failed, contact["id"]):
                paused.add(contact["campaign_id"])
                logger.warning(f"📣 [CAMPAIGN] campaign={contact['campaign_id']} paused reason=dial_errors")
            continue
        await call_store.run(CALL_HISTORY_DB_PATH, campaigns.record_dial, contact["id"], call_uuid)
        logger.info(f"📣 [CAMPAIGN] dial campaign={contact['campaign_id']} contact={contact['id']} "
                    f"attempt={contact['attempt']} phone={campaigns.masked(contact['phone'])} call={call_uuid[:8]}")


async def campaign_loop():
    while True:
        await asyncio.sleep(campaigns.TICK_S)   # sleep first: startup settles before the first dial
        try:
            await campaign_tick()
        except Exception as e:   # a locked database or anything else: skip this tick, never die
            logger.error(f"❌ [CAMPAIGN] tick failed: {type(e).__name__}: {e}")


async def settle_campaign_contact(call_uuid, form):
    global _hangup_fields_logged
    try:
        result = await call_store.run(CALL_HISTORY_DB_PATH, campaigns.on_hangup, call_uuid, form,
                                      datetime.now(ist_tz))
    except Exception as e:
        logger.error(f"❌ [CAMPAIGN] hangup not stored call={call_uuid[:8]}: {type(e).__name__}")
        return
    if result is None:
        return
    if not _hangup_fields_logged:
        _hangup_fields_logged = True
        logger.info(f"📣 [CAMPAIGN] hangup fields: {sorted(form)}")
    logger.info(f"📣 [CAMPAIGN] hangup campaign={result['campaign_id']} contact={result['contact_id']} "
                f"status={result['status']} cause={result['cause']} attempt={result['attempts']}")


async def json_body():
    """(data, None) for a JSON object body, else (None, error reply). JSON only: CSRF, as /trigger-call."""
    if not request.is_json:
        return None, ({"status": "error", "message": "JSON body required"}, 415)
    data = await request.get_json(silent=True)
    if not isinstance(data, dict):
        return None, ({"status": "error", "message": "JSON object required"}, 400)
    return data, None


def campaign_error(e):
    status = 404 if isinstance(e, campaigns.NotFound) else 409 if isinstance(e, campaigns.BadMove) else 400
    body = {"status": "error", "message": str(e)}
    if isinstance(e, campaigns.NothingToCall):
        body["rejected"] = e.rejected
    return body, status


@app.route("/campaigns", methods=["GET"])
async def campaigns_page():
    return await send_file("campaigns.html")


@app.route("/api/campaigns", methods=["GET"])
async def list_campaigns_api():
    rows = await call_store.run(CALL_HISTORY_DB_PATH, campaigns.list_campaigns)
    return {"campaigns": rows, "limits": campaigns.limits()}


@app.route("/api/campaigns", methods=["POST"])
async def create_campaign_api():
    data, error = await json_body()
    if error:
        return error
    try:
        name = campaigns.check_name(data.get("name"))
        settings = campaigns.check_settings(data)
        try:
            raw = base64.b64decode(str(data.get("file_b64") or ""), validate=True)
        except ValueError:
            raise campaigns.CampaignError("The file did not arrive intact (bad base64)")
        rows, extra_columns, extras_dropped = campaigns.parse_upload(raw)
        result = await call_store.run(CALL_HISTORY_DB_PATH, campaigns.create_from_upload, name, settings, rows,
                                      created_by=session.get("role"),
                                      file_name=str(data.get("file_name") or "")[:200], now=datetime.now(ist_tz))
    except campaigns.CampaignError as e:
        return campaign_error(e)
    logger.info(f"📣 [CAMPAIGN] created campaign={result['id']} accepted={result['accepted']} "
                f"rejected={len(result['rejected'])} by={session.get('role')}")
    return {"status": "success", **result, "extra_columns": extra_columns, "extras_dropped": extras_dropped}


@app.route("/api/campaigns/<int:campaign_id>", methods=["GET"])
async def campaign_detail_api(campaign_id):
    try:
        page = max(1, int(request.args.get("page", "1")))
    except ValueError:
        page = 1
    try:
        return await call_store.run(CALL_HISTORY_DB_PATH, campaigns.campaign_detail, campaign_id, page,
                                    request.args.get("status") or None)
    except campaigns.CampaignError as e:
        return campaign_error(e)


@app.route("/api/campaigns/<int:campaign_id>/action", methods=["POST"])
async def campaign_action_api(campaign_id):
    data, error = await json_body()
    if error:
        return error
    action = str(data.get("action") or "")
    try:
        state = await call_store.run(CALL_HISTORY_DB_PATH, campaigns.apply_action, campaign_id, action,
                                     datetime.now(ist_tz))
    except campaigns.CampaignError as e:
        return campaign_error(e)
    logger.info(f"📣 [CAMPAIGN] campaign={campaign_id} {action} -> {state} by={session.get('role')}")
    return {"status": "success", "state": state}


@app.route("/api/campaigns/<int:campaign_id>/settings", methods=["POST"])
async def campaign_settings_api(campaign_id):
    data, error = await json_body()
    if error:
        return error
    try:
        settings = campaigns.check_settings(data, require_all=True)
        await call_store.run(CALL_HISTORY_DB_PATH, campaigns.update_settings, campaign_id, settings)
    except campaigns.CampaignError as e:
        return campaign_error(e)
    logger.info(f"📣 [CAMPAIGN] campaign={campaign_id} settings changed by={session.get('role')}")
    return {"status": "success", "settings": settings}
```

`binascii.Error` (bad base64) is a subclass of `ValueError`, so `except ValueError` covers it.

- [ ] **Step 8: Start the dispatcher**

In `start_background_work`, change `global ANALYZER` to `global ANALYZER, CAMPAIGN_TASK`, and at the end of the function (after the Plivo catch-up loop) add:

```python
    if campaigns.CAMPAIGNS_ENABLED:
        CAMPAIGN_TASK = asyncio.get_running_loop().create_task(campaign_loop())
        logger.info(f"📣 [CAMPAIGN] dispatcher started max_concurrent={campaigns.MAX_CONCURRENT} "
                    f"hours={campaigns.HOURS_START}-{campaigns.HOURS_END} tick={campaigns.TICK_S}s")
```

Also extend the docstring's first line: `"""Main process only: the Haiku worker (with its catch-up of old transcripts), the Plivo costs that a restart may have missed (spec §2.5), and the campaign dispatcher (step 3)."""`.

- [ ] **Step 9: Run the new tests**

Run: `venv312/Scripts/python.exe -m unittest tests.test_campaign_wiring -v`
Expected: all OK.

- [ ] **Step 10: Re-pin the `GEMINI_MODEL` lines**

Run: `venv312/Scripts/python.exe -m unittest tests.test_preservation_4_8_resumption_recording_stats -v`
Expected: `test_model_identifier_is_recorded` FAILS with `AssertionError: Lists differ: [<new numbers>] != [107, 1890, 2416, 2426, 2857]`. All five move: `import campaigns` adds one line above all of them, and `init_call_history_db` (three new lines) sits above the last four; everything else this task adds is below them. Expect `[108, 1894, 2420, 2430, 2861]`; trust the failure message if it differs. In `tests/test_preservation_4_8_resumption_recording_stats.py`, replace the list in `self.assertEqual(references, [107, 1890, 2416, 2426, 2857])` with the new numbers from the failure message, and add one comment line above it after the last `# And again ...` line:

```python
        # And again for step 3 (import campaigns, the WAL line in init_call_history_db).
```

Run the test again. Expected: OK.

- [ ] **Step 11: Run the full suite**

Run: `venv312/Scripts/python.exe -m unittest discover -s tests -t .`
Expected: no new failure. In particular `TestDefaultDeny.test_access_table_matches_the_routes`, `TestTriggerCall` (test_operator_view) and the existing `TestStartup` / `TestWebhooks` (test_analysis_wiring) stay green. If a pinned log sequence in a `test_preservation_*` file now sees a `[CAMPAIGN]` line, that is expected to be filtered by the tag (Step 3); a failure there means a tag is missing, not that the pin is wrong.

- [ ] **Step 12: Commit (only if the operator asked for commits)**

```bash
git add app.py web_auth.py tests/harness/appctl.py tests/test_campaign_wiring.py tests/test_preservation_4_8_resumption_recording_stats.py
git commit -m "Step 3 Task 4: campaign dispatcher, hangup hook and routes in app.py"
```

---

### Task 5: `campaigns.html` and the calls-page link

**Files:**
- Create: `campaigns.html`
- Modify: `index.html` (nav, ~line 1489)
- Test: `tests/test_campaign_page.py`

**Interfaces:**
- Consumes: the Task 4 routes and their JSON (`GET /api/campaigns` -> `{campaigns, limits}`; `POST /api/campaigns` -> `{status, id, accepted, rejected, extra_columns, extras_dropped}` or `{status: "error", message, rejected?}`; `GET /api/campaigns/<id>?page=&status=` -> `{campaign, contacts, extra_columns, page, page_size, total}`; `POST .../action` `{action}`; `POST .../settings` (all five keys)); `GET /api/me` -> `{role}`.
- Produces: the page; element ids the test checks: `uploadForm`, `campaignRows`, `detail`, `contactRows`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_campaign_page.py`:

```python
"""campaigns.html and the calls-page link (docs/spec/campaigns-design.md §4)."""

from __future__ import annotations

import os
import re
import unittest

from tests.harness.appctl import APP_PY
from tests.test_access_control import AuthTestCase

ROOT = os.path.dirname(os.path.abspath(APP_PY))


def read(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as f:
        return f.read()


class TestCampaignPage(AuthTestCase):
    def test_page_is_served_to_both_roles(self):
        for role in ("operator", "admin"):
            status, _, body = self.get(role, "/campaigns")
            self.assertEqual(status, 200, role)
            for element in ('id="uploadForm"', 'id="campaignRows"', 'id="detail"', 'id="contactRows"'):
                self.assertIn(element, body)


class TestCampaignPageSource(unittest.TestCase):
    def setUp(self):
        self.html = read("campaigns.html")

    def test_page_never_builds_html_from_data(self):
        # Names and extra columns come from an uploaded Excel: text only, never markup.
        for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
            self.assertNotIn(sink, self.html)

    def test_page_shows_no_cost(self):
        self.assertNotIn("cost", self.html.lower())

    def test_page_uses_the_api_polls_and_goes_to_login(self):
        self.assertIn("/api/campaigns", self.html)
        self.assertIn("POLL_MS = 5000", self.html)
        self.assertIn('window.location.href = "/login"', self.html)

    def test_csv_export_defuses_formulas(self):
        self.assertRegex(self.html, re.escape("/^[=+\\-@]/"))

    def test_stop_asks_first(self):
        self.assertIn("confirm(", self.html)


class TestCallsPageLink(unittest.TestCase):
    def test_calls_page_links_to_campaigns(self):
        self.assertIn('href="/campaigns"', read("index.html"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to see them fail**

Run: `venv312/Scripts/python.exe -m unittest tests.test_campaign_page -v`
Expected: `FileNotFoundError` for `campaigns.html`, a 404 for `/campaigns` with no file behind it, and the link test failing.

- [ ] **Step 3: Write `campaigns.html`**

Create `campaigns.html`:

```html
<!DOCTYPE html>
<html lang="en">

<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Campaigns | AU Admission Assist</title>
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

        .grid { display: flex; flex-wrap: wrap; gap: 12px 18px; align-items: flex-end; }
        label { display: flex; flex-direction: column; gap: 4px; font-size: 0.8rem; font-weight: 600; }
        input { height: 36px; padding: 0 10px; border: 1px solid var(--border-color); border-radius: 8px; font: inherit; }
        input[type=number] { width: 90px; }
        select { height: 36px; border: 1px solid var(--border-color); border-radius: 8px; font: inherit; }

        .table-wrap { overflow-x: auto; }
        table { border-collapse: collapse; width: 100%; font-size: 0.85rem; }
        th, td { text-align: left; padding: 7px 9px; border-bottom: 1px solid var(--border-color); vertical-align: top; }
        th { color: var(--text-secondary); font-weight: 600; white-space: nowrap; }
        td.summary { min-width: 260px; }
        .status-calling { color: var(--accent); font-weight: 600; }
        .status-failed, .status-cancelled { color: var(--text-secondary); }
        .row { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin: 10px 0; }
    </style>
</head>

<body>
    <nav>
        <img src="/AU_Logo.png?v=2" alt="Adamas University">
        <div class="actions">
            <a class="nav-link" href="/">Calls</a>
            <a class="nav-link" href="/admin" id="adminLink" hidden>Admin</a>
            <form method="post" action="/logout"><button class="nav-link" type="submit">Logout</button></form>
        </div>
    </nav>

    <main>
        <h1>Campaigns</h1>

        <section>
            <h2>New campaign</h2>
            <p>Excel (.xlsx), first sheet. The first row needs a name column (Name / Student Name / Full Name) and a
                phone column (Phone / Mobile / Phone Number / Mobile Number / Contact). Other columns are kept and shown
                with each contact. Calling hours are IST.</p>
            <form id="uploadForm" class="grid">
                <label>Campaign name <input name="campaign_name" required maxlength="100"></label>
                <label>Excel file <input name="file" type="file" accept=".xlsx" required></label>
                <label>Calls at once <input name="concurrency" type="number" required></label>
                <label>From <input name="hours_start" type="time" required></label>
                <label>To <input name="hours_end" type="time" required></label>
                <label>Retries <input name="max_retries" type="number" required></label>
                <label>Minutes between tries <input name="retry_gap_min" type="number" required></label>
                <button class="btn primary" type="submit">Upload</button>
            </form>
            <p id="uploadStatus"></p>
            <div id="rejectedBox" hidden>
                <div class="row">
                    <strong>Rejected rows</strong>
                    <button class="btn" type="button" id="downloadRejected">Download as CSV</button>
                </div>
                <div class="table-wrap">
                    <table>
                        <thead><tr><th>Row</th><th>Name</th><th>Phone</th><th>Reason</th></tr></thead>
                        <tbody id="rejectedRows"></tbody>
                    </table>
                </div>
            </div>
        </section>

        <section>
            <h2>All campaigns</h2>
            <p id="noCampaigns" hidden>No campaigns yet.</p>
            <div class="table-wrap">
                <table>
                    <thead><tr><th>Name</th><th>State</th><th>Finished / total</th><th>On call now</th><th>Created</th><th></th></tr></thead>
                    <tbody id="campaignRows"></tbody>
                </table>
            </div>
        </section>

        <section id="detail" hidden>
            <h2 id="detailName"></h2>
            <p><strong id="detailState"></strong> · <span id="detailSettings"></span></p>
            <p id="detailCounts"></p>
            <div class="row">
                <button class="btn primary" type="button" id="startBtn">Start</button>
                <button class="btn" type="button" id="pauseBtn">Pause</button>
                <button class="btn primary" type="button" id="resumeBtn">Resume</button>
                <button class="btn danger" type="button" id="stopBtn">Stop</button>
                <span id="actionStatus" class="muted"></span>
            </div>
            <form id="settingsForm" class="grid" hidden>
                <label>Calls at once <input name="concurrency" type="number" required></label>
                <label>From <input name="hours_start" type="time" required></label>
                <label>To <input name="hours_end" type="time" required></label>
                <label>Retries <input name="max_retries" type="number" required></label>
                <label>Minutes between tries <input name="retry_gap_min" type="number" required></label>
                <button class="btn" type="submit">Save settings</button>
            </form>
            <div class="row">
                <label>Show
                    <select id="statusFilter">
                        <option value="">All</option>
                        <option value="pending">Waiting</option>
                        <option value="calling">On call</option>
                        <option value="retry_wait">Retry later</option>
                        <option value="done">Done</option>
                        <option value="failed">Not reached</option>
                        <option value="cancelled">Cancelled</option>
                    </select>
                </label>
                <button class="btn" type="button" id="prevPage">Previous</button>
                <span id="pageInfo" class="muted"></span>
                <button class="btn" type="button" id="nextPage">Next</button>
            </div>
            <div class="table-wrap">
                <table>
                    <thead id="contactHead"></thead>
                    <tbody id="contactRows"></tbody>
                </table>
            </div>
        </section>
    </main>

    <script>
        // Every value from the server is set with textContent: names and extra columns come from an
        // uploaded Excel and must never become markup.
        const POLL_MS = 5000;
        const STATUSES = ["pending", "calling", "retry_wait", "done", "failed", "cancelled"];
        const LABELS = { pending: "Waiting", calling: "On call", retry_wait: "Retry later", done: "Done",
                         failed: "Not reached", cancelled: "Cancelled" };
        const REASONS = { missing_number: "No number", bad_number: "Not a 10-digit Indian mobile number",
                          duplicate_in_file: "Duplicate in this file", on_do_not_call: "On the do-not-call list",
                          called_recently: "Called recently" };
        const ACTION_BUTTONS = { startBtn: ["start", ["draft"]], pauseBtn: ["pause", ["running"]],
                                 resumeBtn: ["resume", ["paused"]], stopBtn: ["stop", ["draft", "running", "paused"]] };
        const SETTING_KEYS = ["concurrency", "hours_start", "hours_end", "max_retries", "retry_gap_min"];

        const $ = (id) => document.getElementById(id);
        let limits = null, openId = null, page = 1, lastRejected = [];

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
            if (!response.ok) {
                const error = new Error(body.message || ("Request failed (" + response.status + ")"));
                error.body = body;
                throw error;
            }
            return body;
        }

        function post(path, data) {
            return api(path, { method: "POST", headers: { "Content-Type": "application/json" },
                               body: JSON.stringify(data) });
        }

        function readSettings(form) {
            const f = form.elements;
            return { concurrency: Number(f.concurrency.value), hours_start: f.hours_start.value,
                     hours_end: f.hours_end.value, max_retries: Number(f.max_retries.value),
                     retry_gap_min: Number(f.retry_gap_min.value) };
        }

        function fillSettings(form, values) {
            for (const key of SETTING_KEYS) form.elements[key].value = values[key];
        }

        function applyLimits(form) {
            const f = form.elements;
            f.concurrency.min = 1; f.concurrency.max = limits.max_concurrent;
            for (const key of ["hours_start", "hours_end"]) { f[key].min = limits.hours_start; f[key].max = limits.hours_end; }
            f.max_retries.min = 0; f.max_retries.max = limits.max_retries;
            f.retry_gap_min.min = limits.min_retry_gap_min;
        }

        function stateText(c) {
            if (!c.state_reason) return c.state;
            return c.state + (c.state_reason === "dial_errors" ? " (paused after repeated dial errors)" : " (" + c.state_reason + ")");
        }

        function fileToBase64(file) {
            return new Promise((resolve, reject) => {
                const reader = new FileReader();
                reader.onload = () => resolve(String(reader.result).split(",")[1] || "");
                reader.onerror = () => reject(reader.error);
                reader.readAsDataURL(file);
            });
        }

        function showRejected(rows) {
            lastRejected = rows || [];
            const body = $("rejectedRows");
            body.replaceChildren();
            for (const r of lastRejected) {
                const tr = el("tr");
                tr.append(el("td", r.sheet_row), el("td", r.name), el("td", r.phone), el("td", REASONS[r.reason] || r.reason));
                body.append(tr);
            }
            $("rejectedBox").hidden = lastRejected.length === 0;
        }

        function csvCell(value) {
            let text = String(value ?? "");
            if (/^[=+\-@]/.test(text)) text = "'" + text;   // a spreadsheet must not run a cell as a formula
            return '"' + text.replace(/"/g, '""') + '"';
        }

        $("downloadRejected").addEventListener("click", () => {
            const lines = [["Row", "Name", "Phone", "Reason"]].concat(
                lastRejected.map((r) => [r.sheet_row, r.name, r.phone, REASONS[r.reason] || r.reason]));
            const blob = new Blob([lines.map((line) => line.map(csvCell).join(",")).join("\r\n")], { type: "text/csv" });
            const link = el("a");
            link.href = URL.createObjectURL(blob);
            link.download = "rejected_rows.csv";
            link.click();
            URL.revokeObjectURL(link.href);
        });

        $("uploadForm").addEventListener("submit", async (event) => {
            event.preventDefault();
            const form = event.target, file = form.elements.file.files[0], status = $("uploadStatus");
            const button = form.querySelector("button[type=submit]");
            if (!file) { status.textContent = "Choose an .xlsx file."; return; }
            if (limits && file.size > limits.max_file_bytes) { status.textContent = "The file is larger than 2 MB."; return; }
            button.disabled = true;
            status.textContent = "Uploading...";
            try {
                const body = await post("/api/campaigns", Object.assign({
                    name: form.elements.campaign_name.value, file_name: file.name,
                    file_b64: await fileToBase64(file) }, readSettings(form)));
                status.textContent = "Saved as a draft: " + body.accepted + " contacts, " + body.rejected.length +
                    " rows rejected" + (body.extras_dropped ? ", " + body.extras_dropped + " extra columns dropped (20 kept)" : "") +
                    ". Check it below, then press Start.";
                showRejected(body.rejected);
                form.reset();
                fillSettings(form, limits.defaults);
                await loadList();
                openCampaign(body.id);
            } catch (error) {
                status.textContent = error.message;
                showRejected(error.body && error.body.rejected);
            } finally {
                button.disabled = false;
            }
        });

        async function loadList() {
            const body = await api("/api/campaigns");
            if (!limits) {
                limits = body.limits;
                for (const form of [$("uploadForm"), $("settingsForm")]) applyLimits(form);
                fillSettings($("uploadForm"), limits.defaults);
            }
            const rows = $("campaignRows");
            rows.replaceChildren();
            for (const c of body.campaigns) {
                const total = STATUSES.reduce((sum, s) => sum + c.counts[s], 0);
                const finished = c.counts.done + c.counts.failed + c.counts.cancelled;
                const tr = el("tr");
                tr.append(el("td", c.name), el("td", stateText(c)), el("td", finished + " / " + total),
                          el("td", c.counts.calling), el("td", c.created_at));
                const cell = el("td"), open = el("button", "Open", "btn");
                open.type = "button";
                open.addEventListener("click", () => openCampaign(c.id));
                cell.append(open);
                tr.append(cell);
                rows.append(tr);
            }
            $("noCampaigns").hidden = body.campaigns.length > 0;
        }

        function openCampaign(id) {
            openId = id;
            page = 1;
            $("detail").hidden = false;
            $("actionStatus").textContent = "";
            loadDetail().then(() => $("detail").scrollIntoView({ behavior: "smooth" })).catch(showDetailError);
        }

        function showDetailError(error) { $("actionStatus").textContent = error.message; }

        async function loadDetail() {
            if (openId === null) return;
            const filter = $("statusFilter").value;
            const body = await api("/api/campaigns/" + openId + "?page=" + page + (filter ? "&status=" + filter : ""));
            const c = body.campaign;
            $("detailName").textContent = c.name;
            $("detailState").textContent = stateText(c);
            $("detailSettings").textContent = c.concurrency + " at once, " + c.hours_start + "-" + c.hours_end +
                " IST, " + c.max_retries + " retries " + c.retry_gap_min + " min apart · file " + (c.file_name || "") +
                " · " + c.rejected_count + " rows rejected at upload";
            $("detailCounts").textContent = STATUSES.map((s) => LABELS[s] + " " + c.counts[s]).join(" · ");
            for (const [id, [, states]] of Object.entries(ACTION_BUTTONS)) $(id).hidden = !states.includes(c.state);
            const settingsForm = $("settingsForm");
            settingsForm.hidden = !["draft", "paused"].includes(c.state);
            if (!settingsForm.contains(document.activeElement)) fillSettings(settingsForm, c);   // not while typing

            const head = $("contactHead"), headRow = el("tr");
            ["Row", "Name", "Phone"].concat(body.extra_columns, ["Status", "Tries", "Last result", "Next try", "Lead",
                "Visit day", "Summary"]).forEach((title) => headRow.append(el("th", title)));
            head.replaceChildren(headRow);
            const rows = $("contactRows");
            rows.replaceChildren();
            for (const k of body.contacts) {
                const tr = el("tr");
                tr.append(el("td", k.sheet_row), el("td", k.name), el("td", k.phone));
                for (const column of body.extra_columns) tr.append(el("td", k.extra[column] ?? ""));
                tr.append(el("td", LABELS[k.status] || k.status, "status-" + k.status), el("td", k.attempts),
                          el("td", k.last_cause ?? ""), el("td", k.next_attempt_at ?? ""), el("td", k.lead_status ?? ""),
                          el("td", k.campus_visit_day ?? ""), el("td", k.summary ?? "", "summary"));
                rows.append(tr);
            }
            const pages = Math.max(1, Math.ceil(body.total / body.page_size));
            $("pageInfo").textContent = "Page " + body.page + " of " + pages + " (" + body.total + " contacts)";
            $("prevPage").disabled = page <= 1;
            $("nextPage").disabled = page >= pages;
        }

        for (const [id, [action]] of Object.entries(ACTION_BUTTONS)) {
            $(id).addEventListener("click", async () => {
                if (action === "stop" && !confirm("Stop this campaign? Contacts not called yet are cancelled. This cannot be undone.")) return;
                $("actionStatus").textContent = "";
                try {
                    await post("/api/campaigns/" + openId + "/action", { action: action });
                } catch (error) {
                    $("actionStatus").textContent = error.message;
                }
                await Promise.all([loadDetail(), loadList()]).catch(showDetailError);
            });
        }

        $("settingsForm").addEventListener("submit", async (event) => {
            event.preventDefault();
            try {
                await post("/api/campaigns/" + openId + "/settings", readSettings(event.target));
                $("actionStatus").textContent = "Settings saved.";
                document.activeElement.blur();
            } catch (error) {
                $("actionStatus").textContent = error.message;
            }
            await loadDetail().catch(showDetailError);
        });

        $("statusFilter").addEventListener("change", () => { page = 1; loadDetail().catch(showDetailError); });
        $("prevPage").addEventListener("click", () => { page = Math.max(1, page - 1); loadDetail().catch(showDetailError); });
        $("nextPage").addEventListener("click", () => { page += 1; loadDetail().catch(showDetailError); });

        fetch("/api/me").then((r) => (r.ok ? r.json() : null)).then((me) => {
            if (me && me.role === "admin") $("adminLink").hidden = false;
        }).catch(() => { });

        loadList().catch((error) => { $("uploadStatus").textContent = error.message; });
        setInterval(() => { if (openId !== null) loadDetail().catch(() => { }); }, POLL_MS);
        setInterval(() => { loadList().catch(() => { }); }, POLL_MS * 2);
    </script>
</body>

</html>
```

- [ ] **Step 4: Link from the calls page**

In `index.html`, in `<div class="nav-right">`, add one line right before `<a href="/admin" class="nav-link" id="adminLink" hidden>Admin</a>`:

```html
            <a href="/campaigns" class="nav-link">Campaigns</a>
```

- [ ] **Step 5: Run the tests to see them pass**

Run: `venv312/Scripts/python.exe -m unittest tests.test_campaign_page -v`
Expected: all OK.

- [ ] **Step 6: Run the full suite**

Run: `venv312/Scripts/python.exe -m unittest discover -s tests -t .`
Expected: no new failure.

- [ ] **Step 7: Commit (only if the operator asked for commits)**

```bash
git add campaigns.html index.html tests/test_campaign_page.py
git commit -m "Step 3 Task 5: campaigns page and calls-page link"
```

---

### Task 6: docs

**Files:**
- Modify: `CLAUDE.md`, `docs/spec/campaigns-design.md`, `docs/spec/admin-system-overview.md`

**Interfaces:** none (documentation only).

- [ ] **Step 1: Spec status**

In `docs/spec/campaigns-design.md`, replace the `Status:` line's first sentence with: `Status: design approved by the operator 2026-10-12 (all five sections, in chat); implemented (plan docs/spec/campaigns-plan.md), deviations in the report docs/handoff_step3_report.md.` (if the work ran on Windows, not EC2, say "deviations: see CLAUDE.md session log" instead).

- [ ] **Step 2: Overview**

In `docs/spec/admin-system-overview.md`, build order item 3: append `Implemented <date>; plan docs/spec/campaigns-plan.md.`

- [ ] **Step 3: CLAUDE.md**

Add a "Resume here" item above `0000.` named `00000.` with: step 3 state (built / deployed / live-verified), kill switch `CAMPAIGNS_ENABLED=0`, the rollback commit, the new suite numbers (run, failures, skips) pasted from the real run, and the new `GEMINI_MODEL` pins. Update the suite line in "Current state" the same way, and add a session-log entry (date, what was built, the test files, deviations).

- [ ] **Step 4: Full suite, final**

Run: `venv312/Scripts/python.exe -m unittest discover -s tests -t .`
Expected: no new failure; write the numbers into CLAUDE.md as above.

- [ ] **Step 5: Commit (only if the operator asked for commits)**

```bash
git add CLAUDE.md docs/spec/campaigns-design.md docs/spec/admin-system-overview.md
git commit -m "Step 3 Task 6: docs"
```
