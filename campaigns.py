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
import xml.etree.ElementTree as ET
import zipfile

from call_store import IST
from catalog.build_catalog import _column_index

_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


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
        raise CampaignError("List name must be 1-100 characters")
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
        raise NotFound("No such call list")
    return found[0]


def apply_action(conn, campaign_id, action, now):
    """start / pause / resume / stop (spec §2 state moves). Returns the new state."""
    if action not in ACTIONS:
        raise CampaignError("Unknown action")
    state = _state(conn, campaign_id)
    target = MOVES.get((state, action))
    if target is None:
        raise BadMove(f"Cannot {action} a {state} list")
    stamp = _stamp(now)
    # "AND state=?": the state read above is outside the write transaction; a request (or the
    # dispatcher) that moved the campaign in between makes this a 409, not a forbidden move.
    if action == "start":
        moved = conn.execute("UPDATE campaigns SET state='running', started_at=?, state_reason=NULL "
                             "WHERE id=? AND state=?", (stamp, campaign_id, state))
    elif action == "stop":
        moved = conn.execute("UPDATE campaigns SET state='stopped', finished_at=?, state_reason=NULL "
                             "WHERE id=? AND state=?", (stamp, campaign_id, state))
    else:
        moved = conn.execute("UPDATE campaigns SET state=?, state_reason=NULL, dial_errors=0 WHERE id=? AND state=?",
                             (target, campaign_id, state))
    if moved.rowcount != 1:
        raise BadMove(f"Cannot {action}: the list changed meanwhile")
    if action == "stop":
        # Live calls finish and get their hangup result; nothing new is dialled.
        conn.execute("UPDATE campaign_contacts SET status='cancelled', last_cause='stopped', finished_at=? "
                     "WHERE campaign_id=? AND status IN ('pending', 'retry_wait')", (stamp, campaign_id))
    return target


def update_settings(conn, campaign_id, settings):
    state = _state(conn, campaign_id)
    if state not in EDITABLE_STATES:
        raise BadMove(f"Settings can only change while the list is draft or paused, not {state}")
    moved = conn.execute("UPDATE campaigns SET concurrency=?, hours_start=?, hours_end=?, max_retries=?, "
                         "retry_gap_min=? WHERE id=? AND state=?",
                         (*(settings[k] for k in SETTING_KEYS), campaign_id, state))
    if moved.rowcount != 1:
        raise BadMove("Cannot change settings: the list changed meanwhile")


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
        raise NotFound("No such call list")
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
        # adjusted() < 15: "1E5000" would build a huge int (and raise past 4300 digits); no phone is that long.
        if value is not None and value.is_finite() and value.adjusted() < 15 and value == value.to_integral_value():
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


MAX_COLUMNS = 1024            # rows are padded to their rightmost cell: bound the width
MAX_ROW_ELEMENTS = 1_000_000  # blank <row/> elements the reader will walk through
MAX_SHARED_STRINGS = 1_000_000


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _stream(handle, on_end):
    """iterparse that hands each finished element to on_end(elem, stack) and then drops it from the
    tree, so memory stays flat whatever the file holds (a tiny zip can expand to millions of nodes)."""
    stack = []
    for event, elem in ET.iterparse(handle, events=("start", "end")):
        if event == "start":
            stack.append(elem)
            continue
        stack.pop()
        on_end(elem, stack)
        if stack:
            stack[-1].remove(elem)


def _shared_strings(z):
    if "xl/sharedStrings.xml" not in z.namelist():
        return []
    shared, parts = [], []

    def on_end(elem, stack):
        name = _local(elem.tag)
        if name == "t":
            parts.append(elem.text or "")
        elif name == "si":
            if len(shared) >= MAX_SHARED_STRINGS:
                raise CampaignError("The file holds too many text cells")
            shared.append("".join(parts)[:MAX_CELL_CHARS])
            parts.clear()

    with z.open("xl/sharedStrings.xml") as handle:
        _stream(handle, on_end)
    return shared


def _first_sheet_rows(z):
    """The non-empty rows of the workbook's first sheet, as lists of cell text, read as a stream.
    Same cell rules as catalog/build_catalog.read_sheets (shared strings, inline strings, raw <v>),
    but only one sheet, with the column, row and size limits checked while reading."""
    shared = _shared_strings(z)
    workbook = ET.fromstring(z.read("xl/workbook.xml"))
    rels = {r.get("Id"): r.get("Target") for r in ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))}
    sheets = workbook.find(f"{{{_MAIN_NS}}}sheets")
    first = next(iter(sheets)) if sheets is not None and len(sheets) else None
    if first is None:
        return []
    target = rels[first.get(f"{{{_REL_NS}}}id")].lstrip("/")
    if not target.startswith("xl/"):
        target = "xl/" + target
    rows, counts = [], {"row_elements": 0}
    cell = {}      # the cell being read: column, kind, value text, inline text parts
    cells = {}     # column -> text for the row being read

    def on_end(elem, stack):
        name = _local(elem.tag)
        if name == "v" and cell:
            cell["v"] = elem.text or ""
        elif name == "t" and cell:
            cell["parts"].append(elem.text or "")
        elif name == "c":
            if cell:
                kind, v = cell["kind"], cell.get("v")
                if kind == "s" and v is not None:
                    value = shared[int(v)]
                elif kind == "inlineStr":
                    value = "".join(cell["parts"])
                else:
                    value = v if v is not None else ""
                cells[cell["col"]] = value[:MAX_CELL_CHARS]
                cell.clear()
        elif name == "row":
            counts["row_elements"] += 1
            if counts["row_elements"] > MAX_ROW_ELEMENTS:
                raise CampaignError(f"More than {MAX_ROWS} rows")
            if any(str(text).strip() for text in cells.values()):
                if len(rows) > MAX_ROWS:   # header + MAX_ROWS data rows already read
                    raise CampaignError(f"More than {MAX_ROWS} rows")
                rows.append([cells.get(i, "") for i in range(max(cells) + 1)])
            cells.clear()

    def on_start_cells(handle):
        # A cell's column and type are attributes: read them at its start tag (iterparse gives both).
        stack = []
        for event, elem in ET.iterparse(handle, events=("start", "end")):
            if event == "start":
                if _local(elem.tag) == "c":
                    col = _column_index(elem.get("r"))
                    if col >= MAX_COLUMNS:
                        raise CampaignError(f"The sheet uses more than {MAX_COLUMNS} columns")
                    if col in cells:
                        raise CampaignError("The sheet has two cells at the same place")
                    cell.update(col=col, kind=elem.get("t"), parts=[])
                stack.append(elem)
                continue
            stack.pop()
            on_end(elem, stack)
            if stack:
                stack[-1].remove(elem)

    with z.open(target) as handle:
        on_start_cells(handle)
    return rows


def parse_upload(data):
    """The first sheet of an .xlsx upload: (rows, extra_headers, extras_dropped). Raises
    CampaignError (nothing stored) for a file it cannot use."""
    if len(data) > MAX_FILE_BYTES:
        raise CampaignError(f"File is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            if sum(info.file_size for info in z.infolist()) > MAX_UNPACKED_BYTES:
                raise CampaignError("File unpacks to more than 50 MB")
            rows = _first_sheet_rows(z)
    except CampaignError:
        raise
    except Exception as e:   # any damage in a file from outside is the same answer to the operator
        raise CampaignError("Not a readable .xlsx file") from e
    if not rows:
        raise CampaignError("The first sheet is empty")
    header = [str(c).strip()[:MAX_CELL_CHARS] for c in rows[0]]
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
    if not conn.in_transaction:
        # Python's sqlite3 only opens the transaction at the first write; take the write lock now so
        # a stop/pause from a request cannot commit between our read of a contact and its update.
        conn.execute("BEGIN IMMEDIATE")
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


def still_due(conn, campaign_id, now):
    """Just before each dial of a tick (dials are spaced out): still running and inside its hours?
    A Stop, Pause or the end of the calling window that lands mid-tick stops the remaining dials."""
    found = conn.execute("SELECT state, hours_start, hours_end FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
    return found is not None and found[0] == "running" and _inside(_clock(now), found[1], found[2])


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
    # Stopped meanwhile: stop only cancelled pending/retry_wait, so cancel this one too (else it stays pending).
    conn.execute("UPDATE campaign_contacts SET status='cancelled', last_cause='stopped' WHERE id=? "
                 "AND status IN ('pending', 'retry_wait') AND (SELECT state FROM campaigns k "
                 "WHERE k.id=campaign_contacts.campaign_id)='stopped'", (contact_id,))


def dial_failed(conn, contact_id):
    """Plivo refused the call. Returns True if this error paused the campaign (DIAL_ERRORS_TO_PAUSE in a row)."""
    release(conn, contact_id)
    campaign_id = conn.execute("SELECT campaign_id FROM campaign_contacts WHERE id=?", (contact_id,)).fetchone()[0]
    conn.execute("UPDATE campaigns SET dial_errors=COALESCE(dial_errors, 0)+1 WHERE id=?", (campaign_id,))
    errors = conn.execute("SELECT dial_errors FROM campaigns WHERE id=?", (campaign_id,)).fetchone()[0]
    if errors < DIAL_ERRORS_TO_PAUSE:
        return False
    # True only if this paused it (the operator may have paused or stopped it meanwhile).
    return conn.execute("UPDATE campaigns SET state='paused', state_reason='dial_errors' WHERE id=? AND state='running'",
                        (campaign_id,)).rowcount > 0


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
    retry_causes = {c.upper() for c in RETRY_CAUSES}   # read at call time: the set may be patched or extended
    if (str(form.get("HangupCause") or "").strip().upper() in retry_causes
            or str(form.get("CallStatus") or "").strip().lower() in RETRY_CALL_STATUSES):
        return "retry"
    return "failed"


def on_hangup(conn, call_uuid, form, now):
    """Settle the contact behind a Plivo hangup. None if it is not a live campaign call."""
    select = ("SELECT c.id, c.campaign_id, c.attempts, k.max_retries, k.retry_gap_min, k.state "
              "FROM campaign_contacts c JOIN campaigns k ON k.id = c.campaign_id ")
    found = conn.execute(select + "WHERE c.call_uuid=? AND c.status='calling'", (call_uuid,)).fetchone()
    if found is None:
        # A hangup that beats record_dial (a busy / bad number failing within a second) finds no
        # call_uuid yet: match the contact being dialled to that number instead of losing the result
        # to the 30-min backstop (which would wrongly mark it done).
        phone = normalise_phone(form.get("To"))
        if phone is None:
            return None
        # Only a pick of the last 2 minutes: a single call to the same number must not settle a contact
        # left in calling by a crash (that one is the backstop's).
        found = conn.execute(select + "WHERE c.call_uuid IS NULL AND c.status='calling' AND c.phone=? "
                             "AND c.dialed_at >= ? ORDER BY c.dialed_at DESC LIMIT 1",
                             (phone, _stamp(now - timedelta(minutes=2)))).fetchone()
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
    settled = conn.execute(
        "UPDATE campaign_contacts SET status=?, next_attempt_at=?, last_cause=?, finished_at=?, "
        "call_uuid=COALESCE(call_uuid, ?) WHERE id=? AND status='calling'",
        (status, next_at, cause, None if status == "retry_wait" else _stamp(now), call_uuid, contact_id))
    if settled.rowcount != 1:   # a second callback or the backstop got there first
        return None
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
