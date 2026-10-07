# find_scholarships Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give Neha a fourth Gemini tool, `find_scholarships`, that picks the best Adamas scholarship for a caller on one program, prices it in rupees from that program's fees, and says which facts are worth asking next.

**Architecture:** `course_catalog.py` loads and validates the hand-written, operator-verified `catalog/scholarships.json` at import (fails loudly), and gains one handler plus its declaration in `TOOL_SPECS`. app.py builds declarations from `TOOL_SPECS` and routes on `TOOL_NAMES`, so it is untouched. `prompt_au.txt` gains a SCHOLARSHIPS section.

**Tech Stack:** Python 3.12 (`venv312`), stdlib only (`json`, `fractions`), stdlib `unittest`.

**Spec:** `docs/spec/course-catalog-tools-design.md` section 13 (approved 2026-10-06, with the planning corrections in 13.3/13.4/13.6).

## Global Constraints

- `requirements.txt` and `.env` are frozen: no new dependencies. Do not read `.env` or credential JSON.
- Tests are stdlib `unittest` only (no pytest). Suite: `venv312/Scripts/python.exe -m unittest discover -s tests -t .`
- Do not write test code through bash heredocs (escapes get mangled); use the Edit/Write tools.
- app.py is not modified (the `GEMINI_MODEL` line pins in the 4_8 test must keep holding). No new `call_state` key, so `tests/harness/appctl.py` is unchanged.
- Prompt edits go in `prompt_au.txt` only, never `prompt.txt`.
- `catalog/scholarships.json` is operator-verified data: do not change it.
- `source_text` and `note` never reach Gemini.
- Budgets: every `find_scholarships` response under 1800 characters, and under 1500 when no `situations` are given; all four declarations under 4200 (`response_chars`, compact JSON).
- Printing app/log lines on Windows needs `PYTHONIOENCODING=utf-8`.
- No commits: the operator commits when they ask. Where a task would commit, it stops after the suite run instead.
- Baseline before Task 1: 363 tests, 1 known failure (`test_reply_language_counts_as_the_choice`), 2 skipped, 1 expected failure.

## Review Focus

- Gemini passes a CGPA (8.2) as `qualifying_pct`: the tool cannot tell, so the prompt must say a CGPA is not a percentage (Task 4 test).
- Gemini guesses a situation the caller never confirmed (e.g. alumnus): the declaration says "Only those the caller confirmed" and the prompt says "never a guess" (Task 3 and Task 4 tests).
- A rank arrives as a float (`80000.0`): it must match exactly like the integer (Task 3 test).
- A fact sits in both `situations` and `ruled_out`: the stated fact wins (Task 3 test).
- An exam score that does not apply to the program (CAT for B.Tech): ignored silently, no error, still a best (Task 3 test).

---

### Task 1: Load and validate `catalog/scholarships.json`

**Files:**
- Modify: `course_catalog.py` (imports, module docstring, new block after `find_eligible_programs`, before `HANDLERS`)
- Test: `tests/test_find_scholarships.py` (create)

**Interfaces:**
- Consumes: `course_catalog._load(name)`, `RECORDS`, `LEVELS`.
- Produces:
  - constants `SCORE_INPUTS` (dict name to top value), `RANK_INPUTS` (list), `SITUATIONS` (list of 7), `FLAG_INPUTS` (dict input to situation list), `FACTS` (list of 15), `AWARD_FIELDS`, `SCHEME_KEYS`, `LINE_KEYS`, `SCHOLARSHIP_ITEMS_MAX = 3`
  - `_covers(programs: dict, record: dict) -> bool`
  - `check_scholarships(data: dict, records: list) -> None` (raises `ValueError("scholarships.json: <where>: <message>")`)
  - `SCHOLARSHIPS` (the loaded, checked dict)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_find_scholarships.py`:

```python
"""course_catalog.find_scholarships and the scholarships.json loader (spec section 13).

Loader tests mutate a copy of the real file. Formula and matching tests run on
fake schemes patched into SCHOLARSHIPS over a real record (B.Tech Civil), so
they do not depend on the policy text. Golden cases run on the real,
operator-verified catalog/scholarships.json.
"""
import copy
import unittest

import course_catalog as cc

REAL = cc._load("scholarships.json")


def scheme_of(data, sid):
    return next(s for s in data["schemes"] if s["id"] == sid)


class TestLoader(unittest.TestCase):
    def assertRejects(self, mutate, message, records=None):
        data = copy.deepcopy(REAL)
        mutate(data)
        with self.assertRaisesRegex(ValueError, message):
            cc.check_scholarships(data, cc.RECORDS if records is None else records)

    def test_real_file_loads_all_verified(self):
        cc.check_scholarships(REAL, cc.RECORDS)
        self.assertEqual(len(cc.SCHOLARSHIPS["schemes"]), 21)
        self.assertTrue(all(s["verified"] for s in cc.SCHOLARSHIPS["schemes"]))

    def test_unknown_scheme_key(self):
        self.assertRejects(lambda d: d["schemes"][0].update(tier=[]), "keys must be")

    def test_unknown_input(self):
        self.assertRejects(lambda d: scheme_of(d, "jee-main").update(input="neet_rank"), "unknown input")

    def test_unknown_kind(self):
        self.assertRejects(lambda d: scheme_of(d, "early-bird")["tiers"][0]["award"].update(kind="cashback"),
                           "unknown kind")

    def test_award_fields_must_match_the_kind(self):
        self.assertRejects(lambda d: scheme_of(d, "sibling")["tiers"][0]["award"].pop("cap_per_sem"),
                           "needs exactly")

    def test_fixed_inr_amount_shape(self):
        self.assertRejects(lambda d: scheme_of(d, "auat-mba")["tiers"][0]["award"].update(amount={"outside": 1}),
                           "amount must be")

    def test_score_tiers_use_min(self):
        def mutate(d):
            tier = scheme_of(d, "auat")["tiers"][0]
            tier["max"] = tier.pop("min")
        self.assertRejects(mutate, "tiers use min")

    def test_tiers_run_best_first(self):
        self.assertRejects(lambda d: scheme_of(d, "auat")["tiers"].reverse(), "best first")

    def test_only_the_last_tier_may_be_unbounded(self):
        self.assertRejects(lambda d: scheme_of(d, "auat")["tiers"][0].pop("min"), "only the last tier")

    def test_flag_input_needs_exactly_one_tier(self):
        def mutate(d):
            tiers = scheme_of(d, "alumni")["tiers"]
            tiers.append(copy.deepcopy(tiers[0]))
        self.assertRejects(mutate, "exactly one tier")

    def test_unknown_degree(self):
        self.assertRejects(lambda d: scheme_of(d, "jee-main")["programs"].update(degrees=["BTech"]),
                           "unknown degree")

    def test_programs_need_levels_or_degrees(self):
        self.assertRejects(lambda d: scheme_of(d, "early-bird").update(programs={"exclude_degrees": []}),
                           "programs needs levels or degrees")

    def test_scheme_must_cover_a_program(self):
        self.assertRejects(lambda d: scheme_of(d, "early-bird-pharmacy").update(
            programs={"degrees": ["B.Pharm"], "exclude_degrees": ["B.Pharm"]}), "covers no program")

    def test_covered_programs_need_a_third_semester(self):
        records = copy.deepcopy(cc.RECORDS)
        next(r for r in records if r["id"] == "btech-civil-engineering")["fees"]["per_semester"] = [1, 2]
        self.assertRejects(lambda d: None, "btech-civil-engineering has fewer than 3 semesters", records)

    def test_verified_must_be_a_boolean(self):
        self.assertRejects(lambda d: d["schemes"][0].update(verified="true"), "verified must be true or false")

    def test_line_verified_must_be_a_boolean(self):
        self.assertRejects(lambda d: d["general_conditions"][0].update(verified="yes"),
                           r"general_conditions\[0\]: verified must be true or false")

    def test_duplicate_id(self):
        self.assertRejects(lambda d: d["schemes"][1].update(id=d["schemes"][0]["id"]), "duplicate id")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `venv312/Scripts/python.exe -m unittest tests.test_find_scholarships -v`
Expected: every test ERRORs with `AttributeError: module 'course_catalog' has no attribute 'check_scholarships'` (or `SCHOLARSHIPS`).

- [ ] **Step 3: Implement the loader**

In `course_catalog.py`:

1. Module docstring: change the first paragraph's file list and the design pointer to:

```python
"""Course catalog lookup tools for the Neha voice agent (Gemini Live function calls).

Loads catalog/courses.json, catalog/aliases.json and catalog/scholarships.json
once at import and fails loudly if any is missing or malformed. app.py builds its
FunctionDeclarations from TOOL_SPECS and routes calls to handle_tool_call;
tests/tool_playground.py calls the same function offline. No SDK import and no
I/O after load.
Design: docs/spec/course-catalog-tools-design.md sections 6, 11 and 13.
"""
```

2. Imports: add `from fractions import Fraction` after `import re`.

3. Insert this block after `find_eligible_programs` (after its final `return`), before `HANDLERS = ...`:

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `venv312/Scripts/python.exe -m unittest tests.test_find_scholarships -v`
Expected: 17 tests, OK.

- [ ] **Step 5: Run the full suite**

Run: `venv312/Scripts/python.exe -m unittest discover -s tests -t .`
Expected: 380 run, only the known failure `test_reply_language_counts_as_the_choice`, 2 skipped, 1 expected failure. No commit (Global Constraints).

---

### Task 2: Rupee formulas and the spoken award

**Files:**
- Modify: `course_catalog.py` (append to the scholarship block, after `check_scholarships(SCHOLARSHIPS, RECORDS)`)
- Test: `tests/test_find_scholarships.py`

**Interfaces:**
- Consumes: `inr(amount)`, `BY_ID`.
- Produces:
  - `_share(amount, pct, per=100) -> int`: pct/per of amount, whole rupees, half up, exact.
  - `_value(award: dict, record: dict, internal: bool) -> dict | None`: `{"first", "third", "total"}`, or `{"every", "total"}` for `pct_every_sem_capped`, or `{"total": 0}` for `described`; `None` when a `fixed_inr` dict has no key for this caller.
  - `_describe(scheme: dict, award: dict, value: dict) -> dict`: the spoken `best` object (`name`, `award`, `first_semester`/`third_semester` or `every_semester_up_to`, `total`, `conditions` when non-empty), amounts as `inr()` strings.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_find_scholarships.py`, above `if __name__ == "__main__":`:

```python
CIVIL_RECORD = cc.BY_ID["btech-civil-engineering"]
# per_semester 55450, 55450, 52950, 52950, 52200 x4; admission_fee 43800.


class TestRupees(unittest.TestCase):
    def value(self, award, internal=False):
        return cc._value(award, CIVIL_RECORD, internal)

    def test_share_rounds_half_up_exactly(self):
        self.assertEqual(cc._share(55450, 50, 200), 13863)
        self.assertEqual(cc._share(52950, 50, 200), 13238)
        self.assertEqual(cc._share(55450, 10, 200), 2773)
        self.assertEqual(cc._share(55450, 12.5, 200), 3466)
        self.assertEqual(cc._share(25001, 1, 2), 12501)

    def test_pct_split_takes_half_the_pct_off_semesters_one_and_three(self):
        self.assertEqual(self.value({"kind": "pct_split", "pct": 50}),
                         {"first": 13863, "third": 13238, "total": 27101})

    def test_pct_first_sem_is_a_share_of_semester_one_paid_in_halves(self):
        self.assertEqual(self.value({"kind": "pct_first_sem", "pct": 90}),
                         {"first": 24953, "third": 24952, "total": 49905})

    def test_fixed_inr_splits_in_halves(self):
        self.assertEqual(self.value({"kind": "fixed_inr", "amount": 15000}),
                         {"first": 7500, "third": 7500, "total": 15000})
        self.assertEqual(self.value({"kind": "fixed_inr", "amount": 25001}),
                         {"first": 12501, "third": 12500, "total": 25001})

    def test_fixed_inr_internal_and_external(self):
        both = {"kind": "fixed_inr", "amount": {"external": 30000, "internal": 40000}}
        self.assertEqual(self.value(both)["total"], 30000)
        self.assertEqual(self.value(both, internal=True)["total"], 40000)
        self.assertIsNone(self.value({"kind": "fixed_inr", "amount": {"internal": 25000}}))

    def test_pct_capped_total(self):
        self.assertEqual(self.value({"kind": "pct_capped_total", "pct": 20, "cap_total": 25000}),
                         {"first": 5420, "third": 5420, "total": 10840})
        self.assertEqual(self.value({"kind": "pct_capped_total", "pct": 80, "cap_total": 25000}),
                         {"first": 12500, "third": 12500, "total": 25000})

    def test_pct_every_sem_capped_sums_every_semester(self):
        self.assertEqual(self.value({"kind": "pct_every_sem_capped", "pct": 10, "cap_per_sem": 5000}),
                         {"every": 5000, "total": 40000})
        self.assertEqual(self.value({"kind": "pct_every_sem_capped", "pct": 10, "cap_per_sem": 6000}),
                         {"every": 5545, "total": 42560})

    def test_full_fee_is_semester_one_plus_admission_fee(self):
        self.assertEqual(self.value({"kind": "full_fee"}), {"first": 99250, "third": 0, "total": 99250})

    def test_described_has_no_figure(self):
        self.assertEqual(self.value({"kind": "described", "text": "half tuition"}), {"total": 0})


class TestDescribe(unittest.TestCase):
    def describe(self, award, conditions=()):
        scheme = {"name": "X", "conditions": list(conditions)}
        return cc._describe(scheme, award, cc._value(award, CIVIL_RECORD, False))

    def test_pct_split(self):
        self.assertEqual(self.describe({"kind": "pct_split", "pct": 50}), {
            "name": "X", "award": "50%", "first_semester": "13,863", "third_semester": "13,238",
            "total": "27,101"})

    def test_award_wording_per_kind(self):
        cases = [
            ({"kind": "pct_split", "pct": 12.5}, "12.5%"),
            ({"kind": "pct_first_sem", "pct": 90}, "90% of the first semester fee"),
            ({"kind": "fixed_inr", "amount": 15000}, "Rs 15,000"),
            ({"kind": "pct_capped_total", "pct": 20, "cap_total": 25000}, "20%, up to Rs 25,000"),
            ({"kind": "full_fee"}, "full first semester fee and admission fee"),
        ]
        for award, text in cases:
            with self.subTest(award["kind"]):
                self.assertEqual(self.describe(award)["award"], text)

    def test_every_semester_shape(self):
        self.assertEqual(self.describe({"kind": "pct_every_sem_capped", "pct": 10, "cap_per_sem": 5000}), {
            "name": "X", "award": "10% every semester, up to Rs 5,000", "every_semester_up_to": "5,000",
            "total": "40,000"})

    def test_conditions_only_when_present(self):
        self.assertEqual(self.describe({"kind": "fixed_inr", "amount": 15000}, ["pay on time"])["conditions"],
                         ["pay on time"])
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `venv312/Scripts/python.exe -m unittest tests.test_find_scholarships -v`
Expected: the 13 new tests ERROR with `AttributeError: module 'course_catalog' has no attribute '_share'` / `'_value'` / `'_describe'`; the 17 loader tests still pass.

- [ ] **Step 3: Implement the formulas**

Append to the scholarship block in `course_catalog.py`, right after `check_scholarships(SCHOLARSHIPS, RECORDS)`:

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `venv312/Scripts/python.exe -m unittest tests.test_find_scholarships -v`
Expected: 30 tests, OK.

- [ ] **Step 5: Run the full suite**

Run: `venv312/Scripts/python.exe -m unittest discover -s tests -t .`
Expected: 393 run, only the known failure, 2 skipped, 1 expected failure. No commit.

---

### Task 3: The `find_scholarships` tool

**Files:**
- Modify: `course_catalog.py` (append to the scholarship block; `HANDLERS`; `TOOL_SPECS`; `result_status`)
- Modify: `tests/test_course_catalog.py:269` (declaration budget) and `:274-276` (tool names)
- Modify: `tests/test_catalog_tool_handler.py:48-49` (declaration order)
- Modify: `tests/tool_playground.py:10` (usage example)
- Test: `tests/test_find_scholarships.py`

**Interfaces:**
- Consumes: Task 1 constants and `_covers`, Task 2 `_value` / `_describe`, existing `_check_args`, `_pick`, `_error`, `resolve`, `inr`, `key`.
- Produces: `find_scholarships(args: dict) -> dict` registered as `"find_scholarships"` in `HANDLERS` / `TOOL_NAMES`, its declaration last in `TOOL_SPECS`, and `result_status("find_scholarships", response) -> str`.

- [ ] **Step 1: Write the failing tests**

1. In `tests/test_find_scholarships.py`, add `from unittest import mock` under `import unittest`, then add above `if __name__ == "__main__":`:

```python
TOOL = "find_scholarships"
CIVIL = "B.Tech (Civil Engineering)"
SPLIT50 = {"kind": "pct_split", "pct": 50}
MBA_TABLE = [{"min": 85, "award": {"kind": "fixed_inr", "amount": {"external": 30000, "internal": 40000}}},
             {"min": 80, "award": {"kind": "fixed_inr", "amount": {"internal": 25000}}}]


def find(**args):
    return cc.handle_tool_call(TOOL, args)


def scheme(sid, award=None, input_name="everyone", tiers=None, committee=False, verified=True, programs=None):
    return {"id": sid, "section": 1, "name": sid.title(), "source_text": "x",
            "programs": programs or {"levels": ["UG"]}, "input": input_name,
            "tiers": tiers if tiers is not None else [{"award": award}],
            "conditions": [], "committee_decides": committee, "verified": verified}


def policy(*schemes):
    return {"source": "test",
            "general_conditions": [{"text": "Only one applies.", "source_text": "x", "verified": True},
                                   {"text": "Unverified rule.", "source_text": "x", "verified": False}],
            "pitch_lines": [{"text": "Book soon.", "source_text": "x", "verified": True}],
            "schemes": list(schemes)}


def facts(response):
    return [a["fact"] for a in response.get("ask_about", [])]


class FakePolicyCase(unittest.TestCase):
    schemes = ()

    def setUp(self):
        patcher = mock.patch.object(cc, "SCHOLARSHIPS", policy(*self.schemes))
        patcher.start()
        self.addCleanup(patcher.stop)


class TestMatching(FakePolicyCase):
    # On Civil: Floor 15,000; Marks 27,101 (90+) or 5,421 (75+); Rank 55,450 (<=1000)
    # or 11,090; Alumni 27,101; Sibling 20,000; Sports is committee-only.
    schemes = (
        scheme("floor", {"kind": "fixed_inr", "amount": 15000}),
        scheme("marks", input_name="qualifying_pct",
               tiers=[{"min": 90, "award": SPLIT50}, {"min": 75, "award": {"kind": "pct_split", "pct": 10}}]),
        scheme("rank", input_name="jee_main_rank",
               tiers=[{"max": 1000, "award": {"kind": "pct_first_sem", "pct": 100}},
                      {"award": {"kind": "pct_first_sem", "pct": 20}}]),
        scheme("sports", {"kind": "full_fee"}, "sports_achiever", committee=True),
        scheme("alumni", SPLIT50, "alumnus"),
        scheme("sibling", {"kind": "fixed_inr", "amount": 20000}, "sibling_enrolled"),
        scheme("hidden", {"kind": "fixed_inr", "amount": 900000}, verified=False),
        scheme("pg-only", {"kind": "fixed_inr", "amount": 800000}, programs={"levels": ["PG"]}),
    )

    def test_highest_wins_and_the_rest_are_listed_highest_first(self):
        response = find(program=CIVIL, qualifying_pct=95, jee_main_rank=1000)
        self.assertEqual(response["best"]["name"], "Rank")
        self.assertEqual(response["also_qualifies"], [{"name": "Marks", "total": "27,101"},
                                                      {"name": "Floor", "total": "15,000"}])

    def test_tier_bounds_are_inclusive(self):
        self.assertEqual(find(program=CIVIL, qualifying_pct=90)["best"]["name"], "Marks")
        self.assertEqual(find(program=CIVIL, jee_main_rank=1000)["best"]["total"], "55,450")
        self.assertIn({"name": "Rank", "total": "11,090"}, find(program=CIVIL, jee_main_rank=1001)["also_qualifies"])

    def test_unverified_and_uncovered_schemes_never_appear(self):
        text = str(find(program=CIVIL, qualifying_pct=99, situations=cc.SITUATIONS))
        self.assertNotIn("Hidden", text)
        self.assertNotIn("Pg-Only", text)

    def test_committee_scheme_is_never_best(self):
        response = find(program=CIVIL, situations=["sports_achiever"])
        self.assertEqual(response["best"]["name"], "Floor")
        self.assertEqual(response["may_also_apply"], [{"name": "Sports"}])

    def test_ask_about_is_highest_first_and_capped_at_three(self):
        self.assertEqual(find(program=CIVIL)["ask_about"], [
            {"fact": "jee_main_rank", "up_to": "55,450"},
            {"fact": "qualifying_pct", "up_to": "27,101"},
            {"fact": "studied_at_adamas_university", "up_to": "27,101"}])

    def test_ask_about_only_facts_that_beat_best(self):
        self.assertNotIn("ask_about", find(program=CIVIL, qualifying_pct=95, jee_main_rank=1000))

    def test_given_and_ruled_out_facts_are_not_asked(self):
        self.assertNotIn("qualifying_pct", facts(find(program=CIVIL, qualifying_pct=80)))
        self.assertNotIn("jee_main_rank", facts(find(program=CIVIL, ruled_out=["jee_main_rank"])))

    def test_alumni_falls_back_to_the_school_question(self):
        response = find(program=CIVIL, ruled_out=["studied_at_adamas_university"])
        self.assertIn({"fact": "studied_at_adamas_school", "up_to": "27,101"}, response["ask_about"])

    def test_school_alumnus_matches_alumni(self):
        self.assertEqual(find(program=CIVIL, situations=["studied_at_adamas_school"])["best"]["name"], "Alumni")

    def test_rules_and_pitch_lines_are_the_verified_texts(self):
        response = find(program=CIVIL)
        self.assertEqual(response["rules"], ["Only one applies."])
        self.assertEqual(response["pitch_lines"], ["Book soon."])

    # Review focus.
    def test_float_rank_matches_like_an_integer(self):
        self.assertEqual(find(program=CIVIL, jee_main_rank=1000.0)["best"]["name"], "Rank")

    def test_stated_fact_wins_over_ruled_out(self):
        response = find(program=CIVIL, situations=["sibling_enrolled"], ruled_out=["sibling_enrolled"])
        self.assertEqual(response["best"]["name"], "Sibling")


class TestNoBest(FakePolicyCase):
    schemes = (scheme("pg-only", {"kind": "fixed_inr", "amount": 800000}, programs={"levels": ["PG"]}),)

    def test_no_best_means_no_rules_or_pitch(self):
        self.assertEqual(find(program=CIVIL), {"program": CIVIL})


class TestInternalStudents(FakePolicyCase):
    schemes = (scheme("table", input_name="auat_pct", tiers=MBA_TABLE),)

    def test_external_amount_by_default_and_asks_about_the_upgrade(self):
        response = find(program=CIVIL, auat_pct=90)
        self.assertEqual(response["best"]["total"], "30,000")
        self.assertEqual(response["ask_about"], [{"fact": "studied_at_adamas_university", "up_to": "40,000"}])

    def test_internal_amount_for_an_adamas_university_graduate(self):
        response = find(program=CIVIL, auat_pct=90, situations=["studied_at_adamas_university"])
        self.assertEqual(response["best"]["total"], "40,000")

    def test_internal_only_tier_is_asked_not_matched(self):
        self.assertEqual(find(program=CIVIL, auat_pct=82), {
            "program": CIVIL, "ask_about": [{"fact": "studied_at_adamas_university", "up_to": "25,000"}]})
        self.assertEqual(find(program=CIVIL, auat_pct=82, ruled_out=["studied_at_adamas_university"]),
                         {"program": CIVIL})

    def test_unknown_score_is_asked_at_its_best_value(self):
        self.assertEqual(find(program=CIVIL)["ask_about"], [{"fact": "auat_pct", "up_to": "40,000"}])


class TestErrors(unittest.TestCase):
    def test_errors(self):
        cases = [
            ({}, "program is required"),
            ({"program": "  "}, "program is required"),
            ({"program": CIVIL, "qualifying_pct": "ninety"}, "qualifying_pct must be a number from 0 to 100"),
            ({"program": CIVIL, "qualifying_pct": True}, "qualifying_pct must be a number"),
            ({"program": CIVIL, "qualifying_pct": 101}, "qualifying_pct must be a number"),
            ({"program": CIVIL, "clat_score": 121}, "clat_score must be a number from 0 to 120"),
            ({"program": CIVIL, "jee_main_rank": 0}, "jee_main_rank must be a rank, 1 or more"),
            ({"program": CIVIL, "situations": ["rich_uncle"]}, "unknown situation 'rich_uncle'"),
            ({"program": CIVIL, "ruled_out": ["neet_rank"]}, "unknown fact 'neet_rank'"),
            ({"program": CIVIL, "situations": 5}, "situations must be a list"),
            ({"program": CIVIL, "budget": 5}, "unknown argument 'budget'"),
        ]
        for args, message in cases:
            with self.subTest(args):
                self.assertIn(message, cc.handle_tool_call(TOOL, args)["error"])

    def test_single_situation_string_is_accepted(self):
        self.assertNotIn("error", find(program=CIVIL, situations="sibling_enrolled"))

    def test_unresolved_program(self):
        self.assertEqual(find(program="zzzz qqqq"), {"status": "not_found"})
        response = find(program="M.Pharm")
        self.assertEqual(response["status"], "ambiguous")
        self.assertEqual(len(response["candidates"]), 2)

    def test_result_status(self):
        self.assertEqual(cc.result_status(TOOL, {"error": "x"}), "error")
        self.assertEqual(cc.result_status(TOOL, {"status": "not_found"}), "not_found")
        self.assertEqual(cc.result_status(TOOL, {"program": "X"}), "best=none")
        self.assertEqual(cc.result_status(TOOL, {"program": "X", "best": {"name": "JEE Main Scholarship"}}),
                         "best=jee-main-scholarship")


class TestGolden(unittest.TestCase):
    """Real, operator-verified scholarships.json (spec 13.6)."""

    def test_btech_civil_with_jee_rank(self):
        response = find(program=CIVIL, jee_main_rank=80000)
        self.assertEqual(response["best"], {
            "name": "JEE Main Scholarship", "award": "50%", "first_semester": "13,863",
            "third_semester": "13,238", "total": "27,101", "conditions": ["JEE Main 2026 All India Rank"]})
        self.assertEqual(response["also_qualifies"], [{"name": "Early Bird Scholarship", "total": "15,000"}])
        self.assertEqual(response["ask_about"], [{"fact": "aujet_rank", "up_to": "55,450"},
                                                 {"fact": "wbjee_rank", "up_to": "43,360"},
                                                 {"fact": "sibling_enrolled", "up_to": "40,000"}])

    def test_bpharm_gets_the_pharmacy_early_bird(self):
        self.assertEqual(find(program="B.Pharm")["best"], {
            "name": "Early Bird Scholarship", "award": "Rs 10,000", "first_semester": "5,000",
            "third_semester": "5,000", "total": "10,000",
            "conditions": ["limited to the first 250 admissions", "fee paid within the payment deadline"]})

    def test_mpharm_has_no_scholarship(self):
        self.assertEqual(find(program="M.Pharm (Pharmaceutics)"), {"program": "M.Pharm (Pharmaceutics)"})

    def test_mba_with_cat(self):
        self.assertEqual(find(program="MBA", cat_pct=85)["best"], {
            "name": "CAT Scholarship for MBA", "award": "100% of the first semester fee",
            "first_semester": "96,625", "third_semester": "96,625", "total": "1,93,250",
            "conditions": ["valid CAT score"]})

    def test_mba_with_auat_asks_about_adamas_graduation(self):
        response = find(program="MBA", auat_pct=87, ruled_out=["aujet_rank", "cat_pct", "mat_pct"])
        self.assertEqual(response["best"]["name"], "AUAT Scholarship for MBA")
        self.assertEqual(response["best"]["total"], "30,000")
        self.assertEqual(response["ask_about"], [{"fact": "studied_at_adamas_university", "up_to": "96,001"},
                                                 {"fact": "qualifying_pct", "up_to": "40,000"}])

    def test_sibling_every_semester(self):
        self.assertEqual(find(program=CIVIL, situations=["sibling_enrolled"])["best"], {
            "name": "Sibling Scholarship", "award": "10% every semester, up to Rs 5,000",
            "every_semester_up_to": "5,000", "total": "40,000",
            "conditions": ["the brother or sister must be currently enrolled at Adamas University in a "
                           "program other than B.Pharm", "only one sibling can get it"]})

    def test_exam_that_does_not_apply_is_ignored(self):
        # Review focus: CAT means nothing for B.Tech; no error, Early Bird still best.
        self.assertEqual(find(program=CIVIL, cat_pct=99)["best"]["name"], "Early Bird Scholarship")


class TestBudgets(unittest.TestCase):
    COMBOS = [{}, {"qualifying_pct": 96}, {"qualifying_pct": 61, "auat_pct": 86},
              {"jee_main_rank": 1000, "wbjee_rank": 100, "aujet_rank": 5, "qualifying_pct": 96},
              {"cat_pct": 99, "mat_pct": 99, "clat_score": 95, "auat_pct": 90}]

    def test_every_response_fits(self):
        # Measured while planning: worst 1255 without situations, 1529 with all seven.
        for record in cc.RECORDS:
            for combo in self.COMBOS:
                for situations in ([], cc.SITUATIONS):
                    args = dict(combo, program=record["name"], situations=situations)
                    with self.subTest(record=record["id"], combo=combo, situations=bool(situations)):
                        self.assertLessEqual(cc.response_chars(find(**args)), 1800 if situations else 1500)


class TestDeclaration(unittest.TestCase):
    def test_declaration(self):
        spec = next(s for s in cc.TOOL_SPECS if s["name"] == TOOL)
        props = spec["parameters"]["properties"]
        self.assertEqual(spec["parameters"]["required"], ["program"])
        self.assertEqual(set(props), {"program", "situations", "ruled_out", *cc.SCORE_INPUTS, *cc.RANK_INPUTS})
        self.assertEqual(props["situations"]["items"]["enum"], cc.SITUATIONS)
        self.assertEqual(props["ruled_out"]["items"]["enum"], cc.FACTS)
        # Review focus: the model must not guess a situation.
        self.assertIn("confirmed", props["situations"]["description"])
```

2. In `tests/test_course_catalog.py`, replace

```python
        self.assertLessEqual(cc.response_chars(cc.TOOL_SPECS), 2600)
```

with

```python
        # Spec 13.4: measured 4161 with find_scholarships.
        self.assertLessEqual(cc.response_chars(cc.TOOL_SPECS), 4200)
```

and replace

```python
        self.assertEqual(cc.TOOL_NAMES, {"list_programs", "get_program_details", "find_eligible_programs"})
        self.assertEqual([s["name"] for s in cc.TOOL_SPECS],
                         ["list_programs", "get_program_details", "find_eligible_programs"])
```

with

```python
        self.assertEqual(cc.TOOL_NAMES, {"list_programs", "get_program_details", "find_eligible_programs",
                                         "find_scholarships"})
        self.assertEqual([s["name"] for s in cc.TOOL_SPECS],
                         ["list_programs", "get_program_details", "find_eligible_programs", "find_scholarships"])
```

3. In `tests/test_catalog_tool_handler.py`, replace

```python
        self.assertEqual(names, ["endCall", "transferCall", "list_programs", "get_program_details",
                                 "find_eligible_programs"])
```

with

```python
        self.assertEqual(names, ["endCall", "transferCall", "list_programs", "get_program_details",
                                 "find_eligible_programs", "find_scholarships"])
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `venv312/Scripts/python.exe -m unittest tests.test_find_scholarships tests.test_course_catalog tests.test_catalog_tool_handler -v`
Expected: the new matching/golden/error tests FAIL with `{'error': 'unknown tool find_scholarships'}` lookups (KeyError on `"best"` / assertion errors), `TestDeclaration` raises `StopIteration`, and the two name/order tests fail on the missing fourth tool. Task 1 and 2 tests still pass.

- [ ] **Step 3: Implement the tool**

1. Append to the scholarship block in `course_catalog.py`, after `_describe`:

```python
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
```

2. Replace

```python
HANDLERS = {"list_programs": list_programs, "get_program_details": get_program_details,
            "find_eligible_programs": find_eligible_programs}
```

with

```python
HANDLERS = {"list_programs": list_programs, "get_program_details": get_program_details,
            "find_eligible_programs": find_eligible_programs, "find_scholarships": find_scholarships}
```

3. In `TOOL_SPECS`, after the `find_eligible_programs` entry's closing `},` and before the list's closing `]`, add:

```python
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
```

4. In `result_status`, after the `find_eligible_programs` branch, add:

```python
    if name == "find_scholarships":
        if "status" in response:
            return response["status"]
        return "best=" + (key(response["best"]["name"]).replace(" ", "-") if "best" in response else "none")
```

5. In `tests/tool_playground.py`, after the usage line that ends `subjects=Physics,Chemistry,Biology` (line 10), add:

```
    venv312/Scripts/python.exe tests/tool_playground.py find_scholarships program=MBA cat_pct=85 situations=sibling_enrolled
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `venv312/Scripts/python.exe -m unittest tests.test_find_scholarships tests.test_course_catalog tests.test_catalog_tool_handler -v`
Expected: all OK (`test_find_scholarships`: 60 tests).

- [ ] **Step 5: Try it in the playground**

Run: `PYTHONIOENCODING=utf-8 venv312/Scripts/python.exe tests/tool_playground.py find_scholarships "program=B.Tech (Civil Engineering)" jee_main_rank=80000`
Expected: best `JEE Main Scholarship`, total `27,101`, and a `-- 1128 chars` line.

- [ ] **Step 6: Run the full suite**

Run: `venv312/Scripts/python.exe -m unittest discover -s tests -t .`
Expected: 423 run, only the known failure, 2 skipped, 1 expected failure (app.py untouched, so the 4_8 pins hold). No commit.

---

### Task 4: Prompt rules and docs

**Files:**
- Modify: `prompt_au.txt:22` (tool selection), `:29` (entrance exams), new `## SCHOLARSHIPS` section after `## CATALOG TOOLS (CRITICAL)`, `:33` (NOT AVAILABLE)
- Modify: `tests/test_prompt_catalog.py`
- Modify: `docs/spec/course-catalog-tools-design.md` section 6.5 (budgets)
- Modify: `CLAUDE.md` (current state, Resume here, session log)

**Interfaces:**
- Consumes: the tool name and response keys from Task 3 (`best`, `also_qualifies`, `may_also_apply`, `ask_about`, `rules`, `pitch_lines`, `ruled_out`).
- Produces: prompt text the tests below pin.

- [ ] **Step 1: Write the failing tests**

In `tests/test_prompt_catalog.py`:

1. Replace

```python
        for name in ("list_programs", "get_program_details", "find_eligible_programs", "transferCall", "endCall"):
```

with

```python
        for name in ("list_programs", "get_program_details", "find_eligible_programs", "find_scholarships",
                     "transferCall", "endCall"):
```

2. Add above `if __name__ == "__main__":`:

```python
    def section(self, heading):
        return self.text.split(heading, 1)[1].split("\n## ", 1)[0]

    def test_scholarship_section(self):
        # Spec 13.5; CGPA and "never a guess" are review-focus items: the tool
        # cannot tell a CGPA from a percentage or a guessed situation from a fact.
        section = self.section("## SCHOLARSHIPS")
        for phrase in ("`find_scholarships`", "only one scholarship applies", "`ask_about`", "`ruled_out`",
                       "never promise it", "`pitch_lines`", "CGPA", "never a guess",
                       "Adamas University Admission Test"):
            with self.subTest(phrase):
                self.assertIn(phrase, section)

    def test_scholarships_no_longer_routed_to_a_counselor(self):
        self.assertNotIn("Scholarship", self.section("## NOT AVAILABLE"))

    def test_exam_rule_allows_scholarship_questions(self):
        # The old "never ask for any other exam" line would stop Neha asking for
        # the JEE rank that ask_about suggests.
        self.assertIn("for a scholarship is fine", self.text)
        self.assertIn("no WBJEE/JEE demand for B.Tech", self.text)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `venv312/Scripts/python.exe -m unittest tests.test_prompt_catalog -v`
Expected: `test_names_all_tools`, `test_scholarship_section` (IndexError, no such heading), `test_scholarships_no_longer_routed_to_a_counselor` and `test_exam_rule_allows_scholarship_questions` fail; `test_reply_language_counts_as_the_choice` still fails as before.

- [ ] **Step 3: Edit `prompt_au.txt`**

1. Line 22: append to the end of the line (after `` `find_eligible_programs`. ``):

```
 Scholarships, discounts or a worry about fees: `find_scholarships` (see SCHOLARSHIPS).
```

2. Replace line 29

```
- Entrance exams: mention one only if the returned eligibility text names it. Never ask for or insist on any other exam (no WBJEE/JEE demand for B.Tech).
```

with

```
- Entrance exams: as an eligibility requirement, mention one only if the returned eligibility text names it, and never insist on any other (no WBJEE/JEE demand for B.Tech). Asking about JEE, WBJEE, CAT, MAT, CLAT, AUAT or AUJET results for a scholarship is fine.
```

3. Insert this section after the `## CATALOG TOOLS (CRITICAL)` section (after line 30) and before `## NOT AVAILABLE`, with a blank line on each side:

```
## SCHOLARSHIPS
- Scholarships come only from `find_scholarships`: never quote a scholarship, amount or condition it did not return on this call.
- Once the program is known, call it when the caller asks about scholarships or discounts, worries about fees, or right after you quote a fee (offer it: "There are scholarships that can bring this down. May I ask a couple of quick questions?"). If the program is not known yet, ask which program first.
- Pass every fact the caller has stated on this call, never a guess. A CGPA is not a percentage: ask for the percentage. Then ask the `ask_about` facts one at a time in plain words ("Did you appear for JEE Main? What was your rank?", "Did you study at Adamas University or an Adamas school?") and call again with the answer. Put facts the caller says no to, or does not have, in `ruled_out`.
- Present `best`: its name, the first and third semester amounts (or the amount every semester) and the total, with its conditions, and say only one scholarship applies, the highest one. Mention `also_qualifies` only if asked; use `rules` when asked how it is paid or kept. For `may_also_apply`, say they can also apply and a committee decides; never promise it.
- Then use `pitch_lines` to encourage them to book their seat soon.
- No `best`: say a senior admission counselor will share the options; never say they get no scholarship.
- AUAT is the Adamas University Admission Test, conducted by Adamas University itself; admission and the AUAT scholarship are based on its score. AUJET is the Adamas University Joint Entrance Test.
```

4. In `## NOT AVAILABLE`, replace `- Scholarships, hostel fees, exact class start dates,` with `- Hostel fees, exact class start dates,`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `venv312/Scripts/python.exe -m unittest tests.test_prompt_catalog -v`
Expected: all pass except the known `test_reply_language_counts_as_the_choice`. `test_no_fee_figures` and `test_formats_like_app_py_does` still pass (no amounts, no braces in the new text).

- [ ] **Step 5: Record budgets and state in the docs**

1. `docs/spec/course-catalog-tools-design.md` section 6.5: add these two lines at the end of its list:

```
- `find_scholarships`: under 1800 characters, under 1500 with no `situations`
  (measured worst 1529 and 1255).
- All four declarations: under 4200 (measured 4161).
```

2. `CLAUDE.md`:
   - "Current state" suite line: update the counts to the Step 6 result.
   - "Resume here" item 0: add a bullet: "**`find_scholarships` built 2026-10-06** (spec §13, plan `docs/spec/find-scholarships-plan.md`): best scholarship per program with rupees from its fees, `ask_about` hints, `ruled_out`; data `catalog/scholarships.json` (21 schemes, operator-verified, loaded and checked at import, no rebuild). Prompt `## SCHOLARSHIPS` added to `prompt_au.txt` (includes the AUAT explanation). app.py untouched. Next: text-only tool test, then live calls; EC2 needs `catalog/scholarships.json` too."
   - Session log: add a "2026-10-06: `find_scholarships` (phase 3)" entry summarising the above, the Groq sub-agent rejection, and the measured budgets.

- [ ] **Step 6: Run the full suite**

Run: `venv312/Scripts/python.exe -m unittest discover -s tests -t .`
Expected: 426 run, only the known failure `test_reply_language_counts_as_the_choice`, 2 skipped, 1 expected failure. No commit: report to the operator and wait.
