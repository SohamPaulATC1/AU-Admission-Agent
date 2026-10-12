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
