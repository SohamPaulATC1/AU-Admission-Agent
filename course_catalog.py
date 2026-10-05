"""Course catalog lookup tools for the Neha voice agent (Gemini Live function calls).

Loads catalog/courses.json and catalog/aliases.json once at import and fails
loudly if either is missing or malformed. app.py builds its FunctionDeclarations
from TOOL_SPECS and routes calls to handle_tool_call; tests/tool_playground.py
calls the same function offline. No SDK import and no I/O after load.
Design: docs/spec/course-catalog-tools-design.md section 6.
"""
import json
import os
import re

CATALOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "catalog")

LEVELS = ["UG", "PG", "Diploma"]
FIELDS = ["overview", "eligibility", "fees", "fee_breakdown", "seats"]
DEFAULT_FIELDS = ["overview", "eligibility", "fees"]
MAX_PROGRAMS = 3
LIST_NAMES_MAX = 20
MAX_CANDIDATES = 4

# Name resolution (spec 6.3). A record is found when its score reaches
# FOUND_SCORE and beats the runner-up by SCORE_MARGIN; otherwise every record
# within SCORE_MARGIN of the best (and at least CANDIDATE_SCORE) is a candidate.
FOUND_SCORE = 0.75
CANDIDATE_SCORE = 0.6
SCORE_MARGIN = 0.2
DEPARTMENT_WEIGHT = 0.5

STOPWORDS = {
    "a", "an", "and", "the", "in", "of", "with", "for", "hons", "specialization",
    "course", "courses", "program", "programme", "programs", "degree",
}
_JOINS = [
    (re.compile(r"\bb b a\b"), "bba"),
    (re.compile(r"\b([bmd]) (tech|sc|com|ed|pharm|optom|a)\b"), r"\1\2"),
    (re.compile(r"\bll ([bm])\b"), r"ll\1"),
]


def normalize(text):
    text = re.sub(r"[^a-z0-9]+", " ", str(text).lower().replace("&", " and ")).strip()
    for pattern, replacement in _JOINS:
        text = pattern.sub(replacement, text)
    return text


def tokens(text):
    return [t for t in normalize(text).split() if t not in STOPWORDS]


def key(text):
    return " ".join(tokens(text))


def inr(amount):
    """Indian digit grouping: 891800 -> "8,91,800"."""
    digits = str(int(amount))
    if len(digits) <= 3:
        return digits
    head, groups = digits[:-3], [digits[-3:]]
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    return ",".join([head] + groups)


def _load(name):
    with open(os.path.join(CATALOG_DIR, name), encoding="utf-8") as f:
        return json.load(f)


CATALOG = _load("courses.json")
RECORDS = CATALOG["records"]
_ALIASES = _load("aliases.json")
EXPANSIONS = {t: set(tokens(phrase)) for t, phrase in _ALIASES["expansions"].items()}

BY_ID = {r["id"]: r for r in RECORDS}
DEGREES = sorted({r["degree"] for r in RECORDS})
SCHOOLS = sorted({r["school"]["code"]: r["school"]["name"] for r in RECORDS}.items())
SCHOOL_CODES = [code for code, _ in SCHOOLS]
_DEGREE_KEYS = sorted(((tuple(tokens(d)), d) for d in DEGREES), key=lambda kd: -len(kd[0]))


def _index(record):
    name_tokens = tokens(record["name"])
    degree_tokens = set(tokens(record["degree"]))
    alias_tokens = [t for alias in record["aliases"] for t in tokens(alias)]
    return {
        "record": record,
        "name_key": " ".join(name_tokens),
        "rest_key": " ".join(t for t in name_tokens if t not in degree_tokens),
        "alias_keys": {key(alias) for alias in record["aliases"]},
        "match_tokens": set(name_tokens) | set(alias_tokens),
        "dept_tokens": set(tokens(record["department"])),
    }


INDEX = [_index(r) for r in RECORDS]


def _token_hits(token, words):
    return token in words or (token in EXPANSIONS and EXPANSIONS[token] <= words)


def _order(entry):
    record = entry["record"]
    return (record["school"]["code"], LEVELS.index(record["level"]), record["name"])


def resolve(query):
    """Map the caller's phrasing to one record: (status, record or None, candidates, more)."""
    text = str(query).strip()
    if text.lower() in BY_ID:
        return "found", BY_ID[text.lower()], [], 0
    words = tokens(text)
    if not words:
        return "not_found", None, [], 0
    full_key = " ".join(words)
    # Exact name or alias first, across every degree: "B.Sc Medical Laboratory
    # Technology" is an alias of the BMLS record.
    exact = [e for e in INDEX if full_key == e["name_key"] or full_key in e["alias_keys"]]
    pool, rest = INDEX, words
    if not exact:
        for degree_tokens, degree in _DEGREE_KEYS:
            n = len(degree_tokens)
            at = next((i for i in range(len(words) - n + 1) if tuple(words[i:i + n]) == degree_tokens), None)
            if at is not None:
                rest = words[:at] + words[at + n:]
                pool = [e for e in INDEX if e["record"]["degree"] == degree]
                rest_key = " ".join(rest)
                exact = [e for e in pool if rest_key == e["rest_key"] or rest_key in e["alias_keys"]]
                break
    if len(exact) == 1:
        return "found", exact[0]["record"], [], 0
    if exact:
        exact.sort(key=lambda e: (len(e["record"]["name"]), e["record"]["name"]))
        names = [e["record"]["name"] for e in exact]
        return "ambiguous", None, names[:MAX_CANDIDATES], max(0, len(names) - MAX_CANDIDATES)

    scored = []
    for entry in pool:
        if rest:
            hits = 0.0
            for token in rest:
                if _token_hits(token, entry["match_tokens"]):
                    hits += 1.0
                elif _token_hits(token, entry["dept_tokens"]):
                    hits += DEPARTMENT_WEIGHT
            score = hits / len(rest)
        else:
            score = 1.0
        if score > 0:
            scored.append((score, entry))
    scored.sort(key=lambda se: (-se[0], len(se[1]["record"]["name"]), se[1]["record"]["name"]))
    if not scored:
        return "not_found", None, [], 0
    best = scored[0][0]
    second = scored[1][0] if len(scored) > 1 else 0.0
    # A department-only word can lift the score, never make a found on its own:
    # "mtech computer science" must not land on M.Tech (Data Science ...).
    named = all(_token_hits(t, scored[0][1]["match_tokens"]) for t in rest)
    if best >= FOUND_SCORE and best - second >= SCORE_MARGIN and named:
        return "found", scored[0][1]["record"], [], 0
    names = [e["record"]["name"] for s, e in scored if s >= max(CANDIDATE_SCORE, best - SCORE_MARGIN)]
    if not names:
        return "not_found", None, [], 0
    return "ambiguous", None, names[:MAX_CANDIDATES], max(0, len(names) - MAX_CANDIDATES)


def _error(message):
    return {"error": message}


def _check_args(args, allowed):
    if args is None:
        return {}, None
    if not isinstance(args, dict):
        return None, "arguments must be an object"
    unknown = sorted(set(args) - set(allowed))
    if unknown:
        return None, f"unknown argument {unknown[0]!r}; allowed: {', '.join(allowed)}"
    return {k: v for k, v in args.items() if v is not None}, None


def _pick(value, choices, what):
    """Case-insensitive enum match; degrees also match on their normalised key."""
    text = str(value).strip()
    for choice in choices:
        if text.lower() == choice.lower() or (what == "degree" and key(text) == key(choice)):
            return choice, None
    return None, f"unknown {what} {text!r}; use one of: {', '.join(choices)}"


def list_programs(args):
    allowed = ["level", "degree", "school", "keyword", "max_total_fee"]
    args, problem = _check_args(args, allowed)
    if problem:
        return _error(problem)
    entries = INDEX
    if not args:
        counts = {}
        for entry in entries:
            school = entry["record"]["school"]
            label = f"{school['code']} - {school['name']}"
            counts[label] = counts.get(label, 0) + 1
        return {"by_school": [{"school": s, "count": n} for s, n in sorted(counts.items())]}
    for what, choices, field in (("level", LEVELS, "level"), ("degree", DEGREES, "degree"),
                                 ("school", SCHOOL_CODES, None)):
        if what in args:
            choice, problem = _pick(args[what], choices, what)
            if problem:
                return _error(problem)
            if field:
                entries = [e for e in entries if e["record"][field] == choice]
            else:
                entries = [e for e in entries if e["record"]["school"]["code"] == choice]
    if "keyword" in args:
        words = tokens(args["keyword"])
        if not words:
            return _error("keyword has no searchable words")
        entries = [e for e in entries
                   if all(_token_hits(t, e["match_tokens"] | e["dept_tokens"]) for t in words)]
    if "max_total_fee" in args:
        cap = args["max_total_fee"]
        if isinstance(cap, bool) or not isinstance(cap, (int, float)):
            return _error("max_total_fee must be a number of rupees")
        entries = [e for e in entries if e["record"]["fees"]["total"] <= cap]
    if not entries:
        return {"count": 0}
    if len(entries) <= LIST_NAMES_MAX:
        return {"count": len(entries), "programs": [e["record"]["name"] for e in sorted(entries, key=_order)]}
    counts = {}
    for entry in entries:
        counts[entry["record"]["degree"]] = counts.get(entry["record"]["degree"], 0) + 1
    by_degree = sorted(counts.items(), key=lambda dc: (-dc[1], dc[0]))
    return {"count": len(entries), "by_degree": [{"degree": d, "count": n} for d, n in by_degree]}


def _years(n):
    return "1 year" if n == 1 else f"{n} years"


def _project(record, fields):
    out = {}
    if "overview" in fields:
        school = record["school"]
        out["school"] = f"{school['code']} - {school['name']}"
        out["department"] = record["department"]
        out["level"] = record["level"]
        out["duration"] = (_years(record["duration_years"]) if record["duration_years"] is not None
                           else record["duration_note"])
    if "eligibility" in fields:
        out["eligibility"] = record["eligibility"]
    fees = record["fees"]
    if "fees" in fields:
        out["fees"] = {
            "session": fees["session"],
            "first_semester": inr(fees["first_semester"]),
            "admission_fee": inr(fees["admission_fee"]),
            "admission_fee_note": fees["admission_fee_note"],
            "due_at_admission": inr(fees["due_at_admission"]),
            "total": inr(fees["total"]),
        }
        if record["add_ons"]:
            out["fees"]["add_ons"] = [
                {"name": a["name"], "duration": _years(a["duration_years"]), "fee": inr(a["fee"])}
                for a in record["add_ons"]
            ]
    if "fee_breakdown" in fields:
        out["fee_breakdown"] = {
            "per_year": [inr(x) for x in fees["per_year"]],
            "per_semester": [inr(x) for x in fees["per_semester"]],
            "program_fee": inr(fees["program_fee"]),
            "admission_fee": inr(fees["admission_fee"]),
            "total": inr(fees["total"]),
        }
    if "seats" in fields:
        out["seats"] = record["seats"]
        if "seats_note" in record:
            out["seats_note"] = record["seats_note"]
    return out


def get_program_details(args):
    args, problem = _check_args(args, ["programs", "fields"])
    if problem:
        return _error(problem)
    programs = args.get("programs")
    if isinstance(programs, str):
        programs = [programs]
    if programs is None:
        return _error("programs is required: 1 to 3 program names")
    try:
        programs = list(programs)
    except TypeError:
        return _error("programs must be a list of program names")
    if not 1 <= len(programs) <= MAX_PROGRAMS:
        return _error(f"programs must list 1 to {MAX_PROGRAMS} names; got {len(programs)}")
    if not all(isinstance(p, str) and p.strip() for p in programs):
        return _error("each program must be a non-empty name")
    fields = args.get("fields", DEFAULT_FIELDS)
    if isinstance(fields, str):
        fields = [fields]
    try:
        fields = [str(f).strip().lower() for f in fields]
    except TypeError:
        return _error("fields must be a list")
    unknown = [f for f in fields if f not in FIELDS]
    if unknown:
        return _error(f"unknown field {unknown[0]!r}; use any of: {', '.join(FIELDS)}")
    if not fields:
        fields = DEFAULT_FIELDS
    results = []
    for query in programs:
        status, record, candidates, more = resolve(query)
        entry = {"query": query, "status": status}
        if record is not None:
            entry["name"] = record["name"]
            entry.update(_project(record, fields))
        if candidates:
            entry["candidates"] = candidates
            if more:
                entry["more"] = more
        results.append(entry)
    return {"results": results}


HANDLERS = {"list_programs": list_programs, "get_program_details": get_program_details}
TOOL_NAMES = frozenset(HANDLERS)

TOOL_SPECS = [
    {
        "name": "list_programs",
        "description": ("List Adamas University programs for the 2027 session. No arguments: "
                        "program counts per school. Filters combine. Returns names, or counts "
                        "per degree when more than 20 match."),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "level": {"type": "STRING", "enum": LEVELS},
                "degree": {"type": "STRING", "enum": DEGREES},
                "school": {"type": "STRING", "enum": SCHOOL_CODES},
                "keyword": {"type": "STRING", "description": "Subject words in English, e.g. 'data science'."},
                "max_total_fee": {"type": "INTEGER", "description": "Highest total fee in rupees."},
            },
        },
    },
    {
        "name": "get_program_details",
        "description": ("Details for 1 to 3 programs: overview, eligibility, fees (first payment "
                        "and total), fee_breakdown (per year and semester), seats. Ask only for "
                        "the fields you need."),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "programs": {
                    "type": "ARRAY",
                    "items": {"type": "STRING"},
                    "description": "Program names as listed, or the caller's words in English.",
                },
                "fields": {
                    "type": "ARRAY",
                    "items": {"type": "STRING", "enum": FIELDS},
                    "description": "Default: overview, eligibility, fees.",
                },
            },
            "required": ["programs"],
        },
    },
]


def handle_tool_call(name, args):
    handler = HANDLERS.get(name)
    if handler is None:
        return _error(f"unknown tool {name}")
    return handler(args)


def response_chars(response):
    return len(json.dumps(response, ensure_ascii=False, separators=(",", ":")))


def result_status(name, response):
    """Short status for the [CATALOG] log line: error, list, or per-program statuses."""
    if "error" in response:
        return "error"
    if name == "list_programs":
        return "list"
    return ",".join(r["status"] for r in response["results"])
