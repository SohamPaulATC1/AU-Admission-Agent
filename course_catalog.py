"""Course catalog lookup tools for the Neha voice agent (Gemini Live function calls).

Loads catalog/courses.json, catalog/aliases.json and catalog/scholarships.json
once at import and fails loudly if any is missing or malformed. app.py builds its
FunctionDeclarations from TOOL_SPECS and routes calls to handle_tool_call;
tests/tool_playground.py calls the same function offline. No SDK import and no
I/O after load.
Design: docs/spec/course-catalog-tools-design.md sections 6, 11 and 13.
"""
import json
import os
import re
from fractions import Fraction

CATALOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "catalog")

LEVELS = ["UG", "PG", "Diploma"]
FIELDS = ["overview", "eligibility", "fees", "fee_breakdown", "seats"]
DEFAULT_FIELDS = ["overview", "eligibility", "fees"]
MAX_PROGRAMS = 3
LIST_NAMES_MAX = 20
MAX_CANDIDATES = 4
ELIGIBLE_NAMES_MAX = 10

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
SUBJECTS = CATALOG["subjects"]
UG_DEGREES = sorted({r["degree"] for r in RECORDS if r["level"] == "UG"})


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


def _keyword_filter(entries, keyword):
    words = tokens(keyword)
    if not words:
        return None, "keyword has no searchable words"
    return [e for e in entries if all(_token_hits(t, e["match_tokens"] | e["dept_tokens"]) for t in words)], None


def _by_degree(entries):
    counts = {}
    for entry in entries:
        counts[entry["record"]["degree"]] = counts.get(entry["record"]["degree"], 0) + 1
    return [{"degree": d, "count": n} for d, n in sorted(counts.items(), key=lambda dc: (-dc[1], dc[0]))]


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
        entries, problem = _keyword_filter(entries, args["keyword"])
        if problem:
            return _error(problem)
    if "max_total_fee" in args:
        cap = args["max_total_fee"]
        if isinstance(cap, bool) or not isinstance(cap, (int, float)):
            return _error("max_total_fee must be a number of rupees")
        entries = [e for e in entries if e["record"]["fees"]["total"] <= cap]
    if not entries:
        return {"count": 0}
    if len(entries) <= LIST_NAMES_MAX:
        return {"count": len(entries), "programs": [e["record"]["name"] for e in sorted(entries, key=_order)]}
    return {"count": len(entries), "by_degree": _by_degree(entries)}


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


def find_eligible_programs(args):
    """UG programs whose verified rule the caller's 10+2 marks meet (spec 11.4)."""
    args, problem = _check_args(args, ["aggregate_pct", "subjects", "degree", "keyword"])
    if problem:
        return _error(problem)
    pct = args.get("aggregate_pct")
    if isinstance(pct, bool) or not isinstance(pct, (int, float)) or not 0 <= pct <= 100:
        return _error("aggregate_pct is required: the 10+2 aggregate percentage, a number from 0 to 100")
    subjects = args.get("subjects")
    if subjects is None:
        return _error("subjects is required: the 10+2 subjects, may be an empty list")
    if isinstance(subjects, str):
        subjects = [subjects]
    try:
        subjects = list(subjects)
    except TypeError:
        return _error("subjects must be a list of subject names")
    have = set()
    for subject in subjects:
        choice, problem = _pick(subject, SUBJECTS, "subject")
        if problem:
            return _error(problem)
        have.add(choice)
    entries = [e for e in INDEX if e["record"]["level"] == "UG"]
    if "degree" in args:
        choice, problem = _pick(args["degree"], UG_DEGREES, "degree")
        if problem:
            return _error(problem)
        entries = [e for e in entries if e["record"]["degree"] == choice]
    if "keyword" in args:
        entries, problem = _keyword_filter(entries, args["keyword"])
        if problem:
            return _error(problem)
    matches, not_covered = [], 0
    for entry in entries:
        rule = entry["record"]["eligibility_rule"]
        if not rule["verified"] or rule["min_aggregate"] is None:
            not_covered += 1
        elif pct >= rule["min_aggregate"] and (rule["subject_options"] is None
                                               or any(set(o) <= have for o in rule["subject_options"])):
            matches.append(entry)
    if not matches:
        return {"count": 0, "not_covered": not_covered}
    by_degree = _by_degree(matches)
    if len(matches) > ELIGIBLE_NAMES_MAX and len(by_degree) > 1:
        return {"count": len(matches), "by_degree": by_degree, "not_covered": not_covered}
    # One degree left (e.g. degree=B.Tech, 18 matches): a single group gives the
    # agent nothing to narrow on, so name the first ten and count the rest.
    programs = []
    for entry in sorted(matches, key=_order)[:ELIGIBLE_NAMES_MAX]:
        item = {"name": entry["record"]["name"]}
        if entry["record"]["eligibility_rule"]["conditions"]:
            item["conditions"] = entry["record"]["eligibility_rule"]["conditions"]
        programs.append(item)
    response = {"count": len(matches), "programs": programs, "not_covered": not_covered}
    if len(matches) > ELIGIBLE_NAMES_MAX:
        response["more"] = len(matches) - ELIGIBLE_NAMES_MAX
    return response


# Scholarships (spec section 13). catalog/scholarships.json is hand-written and
# operator-verified; only verified schemes are used, and source_text / note
# never leave this module.
SCORE_INPUTS = {"qualifying_pct": 100, "auat_pct": 100, "cat_pct": 100, "mat_pct": 100, "clat_score": 120}
RANK_INPUTS = ["aujet_rank", "jee_main_rank", "wbjee_rank"]
SITUATIONS = ["studied_at_adamas_university", "studied_at_adamas_school", "sibling_enrolled",
              "defence_ward", "employee_ward", "sports_achiever", "sports_ward"]
FLAG_INPUTS = {"alumnus": ["studied_at_adamas_university", "studied_at_adamas_school"],
               **{s: [s] for s in SITUATIONS[2:]}}
FACTS = [*SCORE_INPUTS, *RANK_INPUTS, *SITUATIONS]
AWARD_FIELDS = {"pct_split": {"pct"}, "pct_first_sem": {"pct"}, "fixed_inr": {"amount"},
                "pct_capped_total": {"pct", "cap_total"}, "pct_every_sem_capped": {"pct", "cap_per_sem"},
                "full_fee": set(), "described": {"text"}}
SCHEME_KEYS = {"id", "section", "name", "source_text", "programs", "input", "tiers", "conditions",
               "committee_decides", "verified", "note"}
LINE_KEYS = {"text", "source_text", "verified", "note"}
SCHOLARSHIP_ITEMS_MAX = 3
CGPA_MAX = 10  # a "percentage" this low is a CGPA; no scholarship tier starts below 60


def _bad(where, message):
    return ValueError(f"scholarships.json: {where}: {message}")


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_rupees(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _covers(programs, record):
    return (("levels" not in programs or record["level"] in programs["levels"])
            and ("degrees" not in programs or record["degree"] in programs["degrees"])
            and record["degree"] not in programs.get("exclude_degrees", []))


def _check_award(where, award):
    kind = award.get("kind") if isinstance(award, dict) else None
    if kind not in AWARD_FIELDS:
        raise _bad(where, f"unknown kind {kind!r}; use one of: {', '.join(AWARD_FIELDS)}")
    if set(award) - {"kind"} != AWARD_FIELDS[kind]:
        raise _bad(where, f"{kind} needs exactly: {', '.join(sorted(AWARD_FIELDS[kind])) or 'no other field'}")
    for field in ("pct", "cap_total", "cap_per_sem"):
        if field in award and not (_is_number(award[field]) and award[field] > 0):
            raise _bad(where, f"{field} must be a positive number")
    if kind == "fixed_inr":
        amount = award["amount"]
        ok = (all(_is_rupees(v) for v in amount.values()) and amount and set(amount) <= {"external", "internal"}
              if isinstance(amount, dict) else _is_rupees(amount))
        if not ok:
            raise _bad(where, "amount must be whole rupees, or {external, internal} of whole rupees")
    if kind == "described" and not (isinstance(award["text"], str) and award["text"].strip()):
        raise _bad(where, "described needs a text")


def _check_tiers(sid, name, tiers):
    if name in SCORE_INPUTS:
        bound = "min"
    elif name in RANK_INPUTS:
        bound = "max"
    elif name in FLAG_INPUTS or name == "everyone":
        bound = None
    else:
        raise _bad(sid, f"unknown input {name!r}")
    if not isinstance(tiers, list) or not tiers:
        raise _bad(sid, "tiers must be a non-empty list")
    if bound is None and (len(tiers) != 1 or set(tiers[0]) != {"award"}):
        raise _bad(sid, f"{name} needs exactly one tier with no bound")
    previous = None
    for i, tier in enumerate(tiers):
        where = f"{sid} tier {i}"
        if not isinstance(tier, dict) or "award" not in tier or not set(tier) <= {"award", "min", "max"}:
            raise _bad(where, "a tier is {award} plus min or max")
        if bound is not None:
            if ({"min", "max"} - {bound}) & set(tier):
                raise _bad(where, f"{name} tiers use {bound}")
            if bound not in tier:
                if i != len(tiers) - 1:
                    raise _bad(where, "only the last tier may have no bound")
            else:
                value = tier[bound]
                if not _is_number(value):
                    raise _bad(where, f"{bound} must be a number")
                if previous is not None and (value >= previous if bound == "min" else value <= previous):
                    raise _bad(where, "tiers must run best first")
                previous = value
        _check_award(where, tier["award"])


def check_scholarships(data, records):
    """Raise ValueError on anything spec 13.2 does not allow."""
    if not isinstance(data, dict) or set(data) != {"source", "general_conditions", "pitch_lines", "schemes"}:
        raise _bad("top level", "keys must be source, general_conditions, pitch_lines, schemes")
    for group in ("general_conditions", "pitch_lines"):
        for i, line in enumerate(data[group]):
            where = f"{group}[{i}]"
            if not isinstance(line, dict) or not {"text", "source_text", "verified"} <= set(line) <= LINE_KEYS:
                raise _bad(where, "keys must be text, source_text, verified, optional note")
            if not (isinstance(line["text"], str) and line["text"].strip()):
                raise _bad(where, "text must be a non-empty string")
            if not isinstance(line["verified"], bool):
                raise _bad(where, "verified must be true or false")
    degrees = {r["degree"] for r in records}
    seen = set()
    for scheme in data["schemes"]:
        sid = scheme.get("id", "?") if isinstance(scheme, dict) else "?"
        if not isinstance(scheme, dict) or not SCHEME_KEYS - {"note"} <= set(scheme) <= SCHEME_KEYS:
            raise _bad(sid, f"keys must be {', '.join(sorted(SCHEME_KEYS - {'note'}))}, optional note")
        if sid in seen:
            raise _bad(sid, "duplicate id")
        seen.add(sid)
        for flag in ("committee_decides", "verified"):
            if not isinstance(scheme[flag], bool):
                raise _bad(sid, f"{flag} must be true or false")
        if not (isinstance(scheme["name"], str) and scheme["name"].strip()):
            raise _bad(sid, "name must be a non-empty string")
        if not (isinstance(scheme["conditions"], list)
                and all(isinstance(c, str) and c.strip() for c in scheme["conditions"])):
            raise _bad(sid, "conditions must be a list of strings")
        programs = scheme["programs"]
        if (not isinstance(programs, dict) or not set(programs) <= {"levels", "degrees", "exclude_degrees"}
                or not {"levels", "degrees"} & set(programs)):
            raise _bad(sid, "programs needs levels or degrees, optional exclude_degrees")
        if not set(programs.get("levels", [])) <= set(LEVELS):
            raise _bad(sid, f"unknown level; use: {', '.join(LEVELS)}")
        for degree in programs.get("degrees", []) + programs.get("exclude_degrees", []):
            if degree not in degrees:
                raise _bad(sid, f"unknown degree {degree!r}")
        covered = [r for r in records if _covers(programs, r)]
        if not covered:
            raise _bad(sid, "covers no program")
        short = [r["id"] for r in covered if len(r["fees"]["per_semester"]) < 3]
        if short:
            raise _bad(sid, f"{short[0]} has fewer than 3 semesters")
        _check_tiers(sid, scheme["input"], scheme["tiers"])


SCHOLARSHIPS = _load("scholarships.json")
check_scholarships(SCHOLARSHIPS, RECORDS)


def _share(amount, pct, per=100):
    """pct/per of amount in whole rupees, rounded half up (Fraction: no float drift)."""
    return int(Fraction(amount) * Fraction(str(pct)) / per + Fraction(1, 2))


def _value(award, record, internal):
    """Rupees for one award on one program (spec 13.4 table), or None when it cannot apply."""
    fees = record["fees"]
    s1, s3 = fees["per_semester"][0], fees["per_semester"][2]
    kind = award["kind"]
    if kind == "pct_split":
        first, third = _share(s1, award["pct"], 200), _share(s3, award["pct"], 200)
        return {"first": first, "third": third, "total": first + third}
    if kind == "pct_every_sem_capped":
        each = [min(_share(fee, award["pct"]), award["cap_per_sem"]) for fee in fees["per_semester"]]
        return {"every": max(each), "total": sum(each)}
    if kind == "full_fee":
        total = s1 + fees["admission_fee"]
        return {"first": total, "third": 0, "total": total}
    if kind == "described":
        return {"total": 0}
    if kind == "pct_first_sem":
        total = _share(s1, award["pct"])
    elif kind == "pct_capped_total":
        total = min(_share(s1, award["pct"], 200) + _share(s3, award["pct"], 200), award["cap_total"])
    else:
        total = award["amount"]
        if isinstance(total, dict):
            total = total.get("internal" if internal else "external")
            if total is None:
                return None
    first = _share(total, 1, 2)
    return {"first": first, "third": total - first, "total": total}


def _pct(pct):
    return f"{pct:g}%"


def _award_text(award, value):
    kind = award["kind"]
    if kind == "fixed_inr":
        return f"Rs {inr(value['total'])}"
    if kind == "pct_first_sem":
        return f"{_pct(award['pct'])} of the first semester fee"
    if kind == "pct_capped_total":
        return f"{_pct(award['pct'])}, up to Rs {inr(award['cap_total'])}"
    if kind == "pct_every_sem_capped":
        return f"{_pct(award['pct'])} every semester, up to Rs {inr(award['cap_per_sem'])}"
    if kind == "full_fee":
        return "full first semester fee and admission fee"
    if kind == "described":
        return award["text"]
    return _pct(award["pct"])


def _describe(scheme, award, value):
    out = {"name": scheme["name"], "award": _award_text(award, value)}
    if "every" in value:
        out["every_semester_up_to"] = inr(value["every"])
    elif "first" in value:
        out["first_semester"] = inr(value["first"])
        out["third_semester"] = inr(value["third"])
    out["total"] = inr(value["total"])
    if scheme["conditions"]:
        out["conditions"] = scheme["conditions"]
    return out


def _matched_tier(scheme, facts):
    """First tier the caller meets, or None (input not given, or no tier met)."""
    name = scheme["input"]
    if name == "everyone":
        return scheme["tiers"][0]
    if name in FLAG_INPUTS:
        return scheme["tiers"][0] if any(s in facts for s in FLAG_INPUTS[name]) else None
    value = facts.get(name)
    if value is None:
        return None
    return next((t for t in scheme["tiers"]
                 if not ("min" in t and value < t["min"]) and not ("max" in t and value > t["max"])), None)


def _open_fact(scheme, facts, ruled_out):
    """The fact worth asking for this scheme's input, or None when it is known or ruled out."""
    name = scheme["input"]
    options = [] if name == "everyone" else FLAG_INPUTS.get(name, [name])
    if any(o in facts for o in options):
        return None
    return next((o for o in options if o not in ruled_out), None)


def find_scholarships(args):
    """Best scholarship for the caller on one program, with rupees from its fees (spec 13.4)."""
    args, problem = _check_args(args, ["program", *SCORE_INPUTS, *RANK_INPUTS, "situations", "ruled_out"])
    if problem:
        return _error(problem)
    program = args.get("program")
    if not (isinstance(program, str) and program.strip()):
        return _error("program is required: the program name")
    facts = {}
    for name, top in SCORE_INPUTS.items():
        if name in args:
            if not (_is_number(args[name]) and 0 <= args[name] <= top):
                return _error(f"{name} must be a number from 0 to {top}")
            if name == "qualifying_pct" and args[name] <= CGPA_MAX:
                return _error("qualifying_pct looks like a CGPA; ask for the percentage")
            facts[name] = args[name]
    for name in RANK_INPUTS:
        if name in args:
            if not (_is_number(args[name]) and args[name] >= 1):
                return _error(f"{name} must be a rank, 1 or more")
            facts[name] = args[name]
    picked = {}
    for what, choices, label in (("situations", SITUATIONS, "situation"), ("ruled_out", FACTS, "fact")):
        items = args.get(what, [])
        if isinstance(items, str):
            items = [items]
        if not isinstance(items, (list, tuple)):
            return _error(f"{what} must be a list")
        picked[what] = set()
        for item in items:
            choice, problem = _pick(item, choices, label)
            if problem:
                return _error(problem)
            picked[what].add(choice)
    facts.update(dict.fromkeys(picked["situations"], True))
    ruled_out = picked["ruled_out"] - set(facts)

    status, record, candidates, more = resolve(program)
    if record is None:
        out = {"status": status}
        if candidates:
            out["candidates"] = candidates
            if more:
                out["more"] = more
        return out
    internal = "studied_at_adamas_university" in facts
    internal_open = not internal and "studied_at_adamas_university" not in ruled_out
    matched, committee, potentials = [], [], {}

    def offer(fact, value):
        if value is not None and value["total"] > potentials.get(fact, 0):
            potentials[fact] = value["total"]

    for scheme in SCHOLARSHIPS["schemes"]:
        if not scheme["verified"] or not _covers(scheme["programs"], record):
            continue
        tier = _matched_tier(scheme, facts)
        if scheme["committee_decides"]:
            if tier is not None:
                committee.append(scheme["name"])
            continue
        if tier is not None:
            value = _value(tier["award"], record, internal)
            if value is not None:
                matched.append((scheme, tier["award"], value))
            if internal_open:
                upgrade = _value(tier["award"], record, True)
                if upgrade is not None and (value is None or upgrade["total"] > value["total"]):
                    offer("studied_at_adamas_university", upgrade)
        fact = _open_fact(scheme, facts, ruled_out)
        if fact is not None:
            offer(fact, _value(scheme["tiers"][0]["award"], record, internal or internal_open))

    matched.sort(key=lambda m: -m[2]["total"])  # stable: ties keep file order
    out = {"program": record["name"]}
    best_total = matched[0][2]["total"] if matched else 0
    if matched:
        out["best"] = _describe(*matched[0])
        if len(matched) > 1:
            out["also_qualifies"] = [{"name": s["name"], "total": inr(v["total"])}
                                     for s, _, v in matched[1:1 + SCHOLARSHIP_ITEMS_MAX]]
    if committee:
        out["may_also_apply"] = [{"name": n} for n in committee]
    asks = sorted(((f, v) for f, v in potentials.items() if v > best_total), key=lambda fv: -fv[1])
    if asks:
        out["ask_about"] = [{"fact": f, "up_to": inr(v)} for f, v in asks[:SCHOLARSHIP_ITEMS_MAX]]
    if matched:
        out["rules"] = [line["text"] for line in SCHOLARSHIPS["general_conditions"] if line["verified"]]
        out["pitch_lines"] = [line["text"] for line in SCHOLARSHIPS["pitch_lines"] if line["verified"]]
    return out


HANDLERS = {"list_programs": list_programs, "get_program_details": get_program_details,
            "find_eligible_programs": find_eligible_programs, "find_scholarships": find_scholarships}
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
    {
        "name": "find_eligible_programs",
        "description": ("Undergraduate programs the caller appears eligible for from 10+2 marks. "
                        "PCM = Physics, Chemistry, Mathematics; PCB = Physics, Chemistry, Biology. "
                        "Returns names with conditions to read out, or counts per degree when more "
                        "than 10 match (one degree: 10 names and more); not_covered counts programs "
                        "it cannot judge."),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "aggregate_pct": {"type": "NUMBER", "description": "10+2 aggregate percentage, not CGPA."},
                "subjects": {
                    "type": "ARRAY",
                    "items": {"type": "STRING", "enum": SUBJECTS},
                    "description": "10+2 subjects from this list; leave out others. May be empty.",
                },
                "degree": {"type": "STRING", "enum": UG_DEGREES},
                "keyword": {"type": "STRING", "description": "Subject words in English, e.g. 'computer science'."},
            },
            "required": ["aggregate_pct", "subjects"],
        },
    },
    {
        "name": "find_scholarships",
        "description": ("Best scholarship for the caller on one program, in rupees from its fees. Only the "
                        "highest one applies. Pass every fact known; ask_about lists facts worth asking "
                        "next; facts the caller says no to go in ruled_out."),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "program": {"type": "STRING", "description": "Program name as listed, or the caller's words in English."},
                "qualifying_pct": {"type": "NUMBER", "description": "10+2 percentage for UG, graduation percentage for PG."},
                "auat_pct": {"type": "NUMBER", "description": "Adamas University Admission Test score, percent."},
                "cat_pct": {"type": "NUMBER", "description": "CAT percentile."},
                "mat_pct": {"type": "NUMBER", "description": "MAT, CMAT or ATMA score, percent."},
                "clat_score": {"type": "NUMBER", "description": "CLAT marks."},
                "aujet_rank": {"type": "INTEGER", "description": "Adamas University Joint Entrance Test rank."},
                "jee_main_rank": {"type": "INTEGER", "description": "JEE Main All India Rank."},
                "wbjee_rank": {"type": "INTEGER"},
                "situations": {"type": "ARRAY", "items": {"type": "STRING", "enum": SITUATIONS},
                               "description": "Only those the caller confirmed."},
                "ruled_out": {"type": "ARRAY", "items": {"type": "STRING", "enum": FACTS},
                              "description": "Facts the caller said no to or does not have."},
            },
            "required": ["program"],
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
    if name == "find_eligible_programs":
        return f"eligible={response['count']}"
    if name == "find_scholarships":
        if "status" in response:
            return response["status"]
        return "best=" + (key(response["best"]["name"]).replace(" ", "-") if "best" in response else "none")
    return ",".join(r["status"] for r in response["results"])
