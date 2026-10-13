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
    if end == date.max:   # the query adds a day to `to`: 9999-12-31 would overflow (a 500)
        raise AdminError("to is out of range")
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


def _inside(path, root):
    """The real path of `path` if it lies inside `root` (after resolving links and ..), else None."""
    if not path:
        return None
    try:
        base = os.path.realpath(root)
        target = os.path.realpath(path)
        if os.path.commonpath([base, target]) != base:   # ValueError on another drive
            return None
    except (OSError, ValueError):
        return None
    return target


def read_transcript(path, root):
    """The transcript text, or None when there is none, it is missing, or it is outside `root`."""
    target = _inside(path, root)
    if target is None:
        return None
    try:
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
    for name in (*call_store.BOOL_FIELDS, "transferred"):   # transferred is 0/1 from code, not Haiku
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
        # "earlier" only for a call that has the old cost text; an unanswered new call has neither.
        f"CASE WHEN c.gemini_cost_usd IS NULL AND c.cost IS NOT NULL THEN {EARLIER_SQL} END, {COST_SQL} "
        "FROM calls c WHERE c.call_uuid = ?", (call_uuid,)).fetchone()
    names = ("gemini", "stt", "haiku", "plivo", "earlier", "total")
    cost = {name: (None if value is None else _money(value)) for name, value in zip(names, parts)}
    cost["plivo_status"] = call.get("plivo_cost_status")
    campaign = conn.execute("SELECT cp.id, cp.name FROM campaign_contacts k JOIN campaigns cp ON cp.id = k.campaign_id "
                            "WHERE k.call_uuid = ? LIMIT 1", (call_uuid,)).fetchone()
    return {"call": call, "cost": cost, "analysis": _decoded_analysis(conn, call_uuid),
            "campaign": {"id": campaign[0], "name": campaign[1]} if campaign else None,
            "transcript": read_transcript(path, transcript_root)}


def failed_analyses(conn, root=None):
    """(call_uuid, transcript path or None) for every failed analysis, newest first, across all dates.
    A skipped one is not retried (its transcript is missing)."""
    rows = conn.execute(
        "SELECT a.call_uuid, c.transcript_path FROM call_analysis a LEFT JOIN calls c ON c.call_uuid = a.call_uuid "
        "WHERE a.status = 'failed' ORDER BY c.created_at DESC, a.call_uuid").fetchall()
    return [(call_uuid, _usable_transcript(path, root)) for call_uuid, path in rows]


def _usable_transcript(path, root=None):
    """`path` if it is a file inside the transcript folder, else None. The re-run sends the file to
    Bedrock, so a stored path that leaves TRANSCRIPTS/ is never used (same guard as read_transcript)."""
    if root is None:
        import call_transcript   # read at call time: tests point TRANSCRIPT_DIR at a temp folder
        root = call_transcript.TRANSCRIPT_DIR
    # An empty file is no transcript either: the worker would save no_conversation over the analysis.
    return path if _inside(path, root) and os.path.isfile(path) and os.path.getsize(path) > 0 else None


def transcript_file(conn, call_uuid, root=None):
    """The call's stored transcript path if the file is there, else None. A re-run needs it: with no
    transcript, Analyzer.analyze reads empty text and saves no_conversation over a good analysis."""
    return _usable_transcript(call_store.transcript_path(conn, call_uuid), root)


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
    if note is not None and not isinstance(note, str):
        raise AdminError("note must be text")
    note = (note or "").strip()
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
CAMPAIGNS_CSV_HEADERS = ("ID", "Call list", "State", "Created", "Contacts", "Dialed", "Reached", "Hot", "Visits",
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
