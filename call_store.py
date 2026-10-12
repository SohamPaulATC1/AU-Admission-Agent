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


def transcript_path(conn, call_uuid):
    """The last transcript file stored for the call (add_stt), or None."""
    found = conn.execute("SELECT transcript_path FROM calls WHERE call_uuid=?", (call_uuid,)).fetchone()
    return found[0] if found else None


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
