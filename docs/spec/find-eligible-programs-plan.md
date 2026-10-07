# find_eligible_programs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A third Gemini tool, `find_eligible_programs`, that answers "I got 58% with PCB, what can I apply for?" for UG programs from operator-verified structured rules, never naming a program the caller fails.

**Architecture:** A hand-written `catalog/eligibility_rules.json` (one rule per distinct UG eligibility text, keyed by exact text) is merged by `catalog/build_catalog.py` into `catalog/courses.json`. `course_catalog.py` gains the tool function, its `TOOL_SPECS` entry and status; app.py picks it up unchanged through `TOOL_SPECS` / `TOOL_NAMES`. `prompt_au.txt` replaces its phase 1 open-ended eligibility rule.

**Tech Stack:** Python 3.12 (venv312), stdlib only (json, unittest, unittest.mock). No new dependencies.

**Spec:** `docs/spec/course-catalog-tools-design.md` section 11 (11.1-11.7). Sections 6.4 (errors), 6.5 (budgets) and 9 (tests) also apply.

## Global Constraints

- Tests: stdlib unittest only (no pytest/hypothesis). Run the suite with `venv312/Scripts/python.exe -m unittest discover -s tests -t .`. Single module: `venv312/Scripts/python.exe -m unittest tests.test_find_eligible_programs -v`.
- Never write test code through bash heredocs; use the Edit/Write tools.
- Printing app or catalog output on Windows needs `PYTHONIOENCODING=utf-8`.
- `requirements.txt` and `.env` are frozen. Do not read `.env` or credential JSON.
- app.py is NOT modified by this plan (so the 4_8 GEMINI_MODEL pins and call_state mirror are untouched). If a step seems to need an app.py change, stop and ask.
- Do not commit. The operator commits when asked. Never add `TEST_FILES/`, `.env`, credential JSON, `*.db`, recordings.
- `prompt_au.txt` may contain no braces other than `{user_name}` and `{phone_number}` and no 5+ digit numbers (`test_no_fee_figures`).
- Safety (spec 11.1): only `verified: true` rules are judged; unverified or `min_aggregate: null` rules are counted as `not_covered`; failing programs are never named; spoken wording is "you appear eligible for".
- `ELIGIBLE_NAMES_MAX = 10`. Budgets: any `find_eligible_programs` response <= 1500 chars; all three declarations <= 2600 chars.
- Subject vocabulary (exact, in this order): Physics, Chemistry, Mathematics, Biology, Biotechnology, Computer Science, Computer Application, Technical Vocational, Statistics, Economics, Geography, Psychology, Agriculture, Nutrition, Home Science, Human Development.
- Baseline before this plan: 325 tests run, 1 failure (`test_reply_language_counts_as_the_choice`, caused by the operator's `db6fe2b` prompt rewording, not by this plan), 2 skipped, 1 expected failure. Leave that test alone unless the operator says otherwise; "suite green" below means "no new failures beyond that one".

## Review Focus

- A reviewer types `"verified": "true"` (a string) or `"min_agregate"` in the rules file: the build must fail, not treat the string as truthy or silently ignore the key. Pinned in Task 1 (`test_verified_must_be_a_boolean`, `test_unknown_or_missing_keys`).
- Gemini sends `aggregate_pct` as a float (`58.5`) or a string (`"58"`, `"58%"`): float accepted, string rejected with an error the model can fix. Pinned in Task 2 (`test_float_percentage`, `test_errors`).
- Caller gives a CGPA ("8.2") or results are not out yet: the tool cannot tell 8.2 from 8.2%, so the prompt must ask for the percentage, and use the expected percentage with a caveat. Pinned in Task 3 (`test_open_eligibility_rule`).
- Caller names subjects outside the vocabulary (Accountancy, History) and Gemini passes them: the tool errors (model retries without them), and the declaration tells the model to leave such subjects out. Pinned in Task 2 (`test_errors`, `test_declaration_tells_model_to_drop_unlisted_subjects`).
- Narrowing with a PG degree ("MBA") or a filter that leaves nothing: error listing the UG degrees, or `{"count": 0, "not_covered": 0}`. Pinned in Task 2 (`test_errors`, `test_filters_narrow_the_pool`).

---

### Task 1: Rules file and build merge

**Files:**
- Create (throwaway, gitignored): `TEST_FILES/_draft_eligibility_rules.py`
- Create: `catalog/eligibility_rules.json` (generated once by the draft script, then hand-edited only)
- Modify: `catalog/build_catalog.py` (constants near line 26; new function after `eligibility_text` at line 224; `build()` return at lines 337-342; `main()` at 349-358)
- Regenerate: `catalog/courses.json`
- Test: `tests/test_catalog_build.py`

**Interfaces:**
- Consumes: nothing new.
- Produces:
  - `build_catalog.ELIGIBILITY_JSON` (path), `build_catalog.RULE_KEYS` (set), `build_catalog.attach_eligibility_rules(records: list[dict], rules_file: dict) -> list[str]` (mutates UG records, returns the subject vocabulary).
  - `courses.json` top-level key order: `session`, `source`, `excluded`, `subjects`, `records`.
  - Every UG record gains `"eligibility_rule": {"min_aggregate": int|None, "subject_options": list[list[str]]|None, "conditions": list[str], "verified": bool}` (keys in that order). PG/Diploma records have no `eligibility_rule` key.

- [ ] **Step 1: Write the draft generator (throwaway)**

Create `TEST_FILES/_draft_eligibility_rules.py`. It takes texts and program lists straight from the data (so they are exact) and fills rule fields from the hand-written table below, keyed by the first program sharing each text.

```python
"""One-off: draft catalog/eligibility_rules.json from courses.json (spec 11.2).

Texts and program lists come from the data; rule fields come from RULES below.
Run once; afterwards the JSON is edited by hand only. Refuses to overwrite.
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COURSES = os.path.join(ROOT, "catalog", "courses.json")
OUT = os.path.join(ROOT, "catalog", "eligibility_rules.json")

P, C, M, B = "Physics", "Chemistry", "Mathematics", "Biology"
BT, CS, CA, TV = "Biotechnology", "Computer Science", "Computer Application", "Technical Vocational"
ST, EC, GE, PS, AG = "Statistics", "Economics", "Geography", "Psychology", "Agriculture"
PCM, PCB = [P, C, M], [P, C, B]
SUBJECTS = [P, C, M, B, BT, CS, CA, TV, ST, EC, GE, PS, AG, "Nutrition", "Home Science", "Human Development"]
S45 = "at least 45% in Physics, Mathematics and the third subject"
EACH45 = "at least 45% in each of those subjects"
PM_ANY = [[P, M, x] for x in (C, BT, B, TV, CS, CA)]

# first program sharing the text -> (min_aggregate, subject_options, conditions, note or None)
RULES = {
    "B.Sc (Chemistry)": (50, [[P, C]], [],
        "Shared by B.Sc Chemistry and B.Sc Physics; 'respective subject(s)' differs per program, "
        "so both Physics and Chemistry are required (stricter is the safe side)."),
    "B.Sc (Environmental Science and Sustainability)": (50, None, ["10+2 with science and humanities subjects"],
        "Subjects are vague; any stream matches and the requirement is read out as a condition."),
    "B.Sc (Forensic Science)": (50, [PCM, PCB], [], None),
    "B.Sc (Geography)": (50, [[GE], PCM, PCB], [], "'Respective subject(s)' read as Geography."),
    "B.Sc (Geoinformatics)": (50, [PCM, PCB,
                                   [M, ST, EC], [M, ST, CS], [M, EC, CS], [ST, EC, CS],
                                   [M, ST, CA], [M, EC, CA], [ST, EC, CA], [GE]], [],
        "Science: PCM or PCB. Eco-Science: any three of Mathematics, Statistics, Economics, "
        "Computer Science/Application. Humanities: Geography."),
    "B.Sc (Applied Statistics and Data Science)": (50, [[M], [ST]], [], None),
    "B.Sc (Mathematics and Computing)": (50, [[M]], [], "'Respective subject(s)' read as Mathematics."),
    "B.Com": (55, None, [], None),
    "BBA": (60, None, [], None),
    "B.A (Education)": (50, None, [], None),
    "B.Ed": (None, None, ["a graduate or postgraduate degree with at least 50%"],
        "Needs a degree, not 10+2 marks: never matched, counted as not_covered."),
    "B.Tech (Civil Engineering)": (55, PM_ANY, [S45],
        "'with min 45% marks' read as 45% in Physics, Mathematics and the third subject."),
    "B.Tech (Computer Science and Engineering & Business Systems)": (60, PM_ANY, [S45],
        "Same as the B.Tech CSE text except for a trailing comma."),
    "B.Tech (Computer Science and Engineering)": (60, PM_ANY, [S45], None),
    "B.Tech in Artificial Intelligence & Data Science (Business Application)": (
        50, [[P, M, x] for x in (C, BT, B, TV)], [S45, "at least 40% in each individual subject"], None),
    "B.Tech (Biomedical Engineering)": (55, [PCB, PCM], [EACH45], None),
    "B.A. LL.B (Hons)": (60, None, ["CLAT or AUAT qualified"], None),
    "B.Sc (Economics)": (50, None, ["without Mathematics in class 11 and 12, a non-credit remedial "
                                    "Mathematics course in the first semester must be passed"], None),
    "B.Sc (Biochemistry)": (50, [[C, B]], [], "'Respective subjects' read as Chemistry and Biology."),
    "B.Sc (Biotechnology)": (55, [[C, B], [C, BT]], [],
        "'Respective subjects' read as Chemistry with Biology or Biotechnology."),
    "B.Tech (Biotechnology)": (60, [[P, C, x] for x in (M, BT, B, TV)], [EACH45], None),
    "B.Pharm": (60, [PCM, PCB], ["English as a subject in 10+2",
                                 "10+2 from open schooling such as NIOS is not accepted"], None),
    "Bachelor Of Medical Laboratory Science (BMLS)": (50, [PCB], [], None),
    "Bachelor of Optometry": (50, [PCB, PCM], [], None),
    "Bachelor Of Food Nutrition and Dietetics": (50, [[C, B], ["Nutrition"], ["Home Science"], ["Human Development"]], [],
        "'Science group with Chemistry, Biology, Nutrition, Human development, Home science, etc' read as "
        "Chemistry with Biology, or any one of Nutrition, Home Science, Human Development."),
    "B.Sc (Psychology)": (50, [PCM, PCB, [PS]], [], "'Science background' read as PCM or PCB."),
    "B. Sc. (Hons.) Agriculture": (50, [PCB, PCM, [P, C, AG], [C, M, B], [C, M, AG], [C, B, AG],
                                        [P, M, B], [P, M, AG], [P, B, AG], [M, B, AG]], [],
        "The ten combinations listed in the text."),
}


def main():
    if os.path.exists(OUT):
        sys.exit(f"{OUT} exists; edit it by hand instead")
    with open(COURSES, encoding="utf-8") as f:
        records = json.load(f)["records"]
    groups = {}
    for r in records:
        if r["level"] == "UG":
            groups.setdefault(r["eligibility"], []).append(r["name"])
    if sorted(p[0] for p in groups.values()) != sorted(RULES):
        sys.exit(f"RULES keys do not match the data: {sorted(set(RULES) ^ {p[0] for p in groups.values()})}")
    d = lambda v: json.dumps(v, ensure_ascii=False)
    out = ['{', ' "subjects": ' + d(SUBJECTS) + ',', ' "rules": [']
    for i, (text, programs) in enumerate(groups.items()):
        minimum, options, conditions, note = RULES[programs[0]]
        fields = [("text", text), ("programs", programs), ("min_aggregate", minimum),
                  ("subject_options", options), ("conditions", conditions), ("verified", False)]
        if note:
            fields.append(("note", note))
        body = ",\n".join(f'   "{k}": {d(v)}' for k, v in fields)
        out.append("  {\n" + body + "\n  }" + ("," if i < len(groups) - 1 else ""))
    out += [' ]', '}']
    with open(OUT, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(out) + "\n")
    print(f"wrote {OUT}: {len(groups)} rules")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run it**

Run: `PYTHONIOENCODING=utf-8 venv312/Scripts/python.exe TEST_FILES/_draft_eligibility_rules.py`
Expected: `wrote ...catalog/eligibility_rules.json: 27 rules`. Then `venv312/Scripts/python.exe -c "import json; d=json.load(open('catalog/eligibility_rules.json',encoding='utf-8')); print(len(d['rules']), sum(r['verified'] for r in d['rules']))"` prints `27 0`.

- [ ] **Step 3: Write the failing build tests**

In `tests/test_catalog_build.py`, add `import copy` to the imports, then add inside `TestBuildFromExcel` (after `test_fee_file_names_become_aliases`):

```python
    def test_every_ug_record_has_an_eligibility_rule(self):
        for r in self.records:
            with self.subTest(r["name"]):
                if r["level"] == "UG":
                    self.assertEqual(list(r["eligibility_rule"]),
                                     ["min_aggregate", "subject_options", "conditions", "verified"])
                else:
                    self.assertNotIn("eligibility_rule", r)
        self.assertEqual(self.catalog["subjects"], [
            "Physics", "Chemistry", "Mathematics", "Biology", "Biotechnology", "Computer Science",
            "Computer Application", "Technical Vocational", "Statistics", "Economics", "Geography",
            "Psychology", "Agriculture", "Nutrition", "Home Science", "Human Development"])
        self.assertEqual(list(self.catalog), ["session", "source", "excluded", "subjects", "records"])

    def test_shared_respective_subject_text_requires_both_subjects(self):
        for name in ("B.Sc (Physics)", "B.Sc (Chemistry)"):
            self.assertEqual(self.by_name[name]["eligibility_rule"]["subject_options"], [["Physics", "Chemistry"]])

    def test_bed_needs_a_degree_so_is_never_matched(self):
        self.assertIsNone(self.by_name["B.Ed"]["eligibility_rule"]["min_aggregate"])
```

And add a new class after `TestBuildHelpers`:

```python
class TestAttachEligibilityRules(unittest.TestCase):
    RECORDS = [
        {"name": "A", "level": "UG", "eligibility": "t1"},
        {"name": "B", "level": "UG", "eligibility": "t1"},
        {"name": "C", "level": "UG", "eligibility": "t2"},
        {"name": "D", "level": "PG", "eligibility": "t3"},
    ]

    def rule(self, text, programs, **overrides):
        rule = {"text": text, "programs": programs, "min_aggregate": 50, "subject_options": None,
                "conditions": [], "verified": False}
        rule.update(overrides)
        return rule

    def attach(self, rules, subjects=("Physics", "Chemistry")):
        records = copy.deepcopy(self.RECORDS)
        returned = build_catalog.attach_eligibility_rules(records, {"subjects": list(subjects), "rules": rules})
        return records, returned

    def good_rules(self):
        return [self.rule("t1", ["A", "B"], subject_options=[["Physics", "Chemistry"]], verified=True,
                          note="reviewer only"),
                self.rule("t2", ["C"], min_aggregate=None, conditions=["needs a degree"])]

    def test_rules_attach_to_ug_records_only(self):
        records, subjects = self.attach(self.good_rules())
        self.assertEqual(subjects, ["Physics", "Chemistry"])
        self.assertEqual(records[0]["eligibility_rule"], {
            "min_aggregate": 50, "subject_options": [["Physics", "Chemistry"]], "conditions": [], "verified": True})
        self.assertEqual(records[1]["eligibility_rule"], records[0]["eligibility_rule"])
        self.assertIsNone(records[2]["eligibility_rule"]["min_aggregate"])
        self.assertNotIn("eligibility_rule", records[3])
        self.assertNotIn("note", records[0]["eligibility_rule"])

    def assertBuildError(self, rules, fragment):
        with self.assertRaises(build_catalog.BuildError) as caught:
            self.attach(rules)
        self.assertIn(fragment, str(caught.exception))

    def test_ug_text_without_a_rule(self):
        self.assertBuildError(self.good_rules()[:1], "no eligibility rule")

    def test_rule_matching_no_ug_record(self):
        self.assertBuildError(self.good_rules() + [self.rule("t3", ["D"])], "matches no UG record")

    def test_programs_must_match_the_records_sharing_the_text(self):
        rules = self.good_rules()
        rules[0]["programs"] = ["A"]
        self.assertBuildError(rules, "lists ['A']")

    def test_duplicate_text(self):
        self.assertBuildError(self.good_rules() + [self.rule("t1", ["A", "B"])], "two rules share")

    def test_unknown_subject(self):
        rules = self.good_rules()
        rules[0]["subject_options"] = [["Physics", "Accountancy"]]
        self.assertBuildError(rules, "unknown subject 'Accountancy'")

    def test_verified_must_be_a_boolean(self):
        rules = self.good_rules()
        rules[0]["verified"] = "true"
        self.assertBuildError(rules, "verified must be true or false")

    def test_unknown_or_missing_keys(self):
        rules = self.good_rules()
        rules[0]["min_agregate"] = 50
        self.assertBuildError(rules, "unexpected keys ['min_agregate']")
        rules = self.good_rules()
        del rules[1]["conditions"]
        self.assertBuildError(rules, "missing keys ['conditions']")

    def test_field_types(self):
        for field, value, fragment in (("min_aggregate", 150, "min_aggregate"),
                                       ("min_aggregate", True, "min_aggregate"),
                                       ("subject_options", [], "subject_options"),
                                       ("subject_options", [[]], "subject_options"),
                                       ("conditions", "CLAT", "conditions"),
                                       ("conditions", [""], "conditions")):
            rules = self.good_rules()
            rules[0][field] = value
            with self.subTest((field, value)):
                self.assertBuildError(rules, fragment)
```

- [ ] **Step 4: Run to verify they fail**

Run: `venv312/Scripts/python.exe -m unittest tests.test_catalog_build -v`
Expected: the new tests ERROR with `AttributeError: module 'catalog.build_catalog' has no attribute 'attach_eligibility_rules'` (TestAttachEligibilityRules) and `KeyError: 'eligibility_rule'` / `'subjects'` (TestBuildFromExcel). Existing tests pass.

- [ ] **Step 5: Implement the merge in `catalog/build_catalog.py`**

After the `COURSES_JSON = ...` line (line 27) add:

```python
ELIGIBILITY_JSON = os.path.join(CATALOG_DIR, "eligibility_rules.json")
```

After `ADMISSION_FEE_NOTE = ...` (line 30) add:

```python
# Phase 2 rule keys (spec 11.2). "note" is reviewer-only and not copied.
RULE_KEYS = {"text", "programs", "min_aggregate", "subject_options", "conditions", "verified", "note"}
RULE_FIELDS = ["min_aggregate", "subject_options", "conditions", "verified"]
```

After `def eligibility_text(...)` (line 224-225) add:

```python
def _check_rule(rule, known):
    label = f"eligibility rule {str(rule.get('text', '?'))[:60]!r}"
    extra = sorted(set(rule) - RULE_KEYS)
    missing = sorted(RULE_KEYS - {"note"} - set(rule))
    if extra:
        raise BuildError(f"{label}: unexpected keys {extra}")
    if missing:
        raise BuildError(f"{label}: missing keys {missing}")
    if not isinstance(rule["verified"], bool):
        raise BuildError(f"{label}: verified must be true or false")
    m = rule["min_aggregate"]
    if m is not None and (isinstance(m, bool) or not isinstance(m, (int, float)) or not 0 <= m <= 100):
        raise BuildError(f"{label}: min_aggregate must be a percentage or null")
    options = rule["subject_options"]
    if options is not None and not (isinstance(options, list) and options
                                    and all(isinstance(o, list) and o for o in options)):
        raise BuildError(f"{label}: subject_options must be null or a non-empty list of non-empty lists")
    for option in options or []:
        for subject in option:
            if subject not in known:
                raise BuildError(f"{label}: unknown subject {subject!r}")
    if not (isinstance(rule["conditions"], list) and all(isinstance(c, str) and c for c in rule["conditions"])):
        raise BuildError(f"{label}: conditions must be a list of non-empty strings")


def attach_eligibility_rules(records, rules_file):
    """Give every UG record its rule (matched on exact eligibility text); return the subjects."""
    subjects = rules_file["subjects"]
    by_text = {}
    for rule in rules_file["rules"]:
        _check_rule(rule, set(subjects))
        if rule["text"] in by_text:
            raise BuildError(f"two rules share the text {rule['text'][:60]!r}")
        by_text[rule["text"]] = rule
    ug = [r for r in records if r["level"] == "UG"]
    for text in dict.fromkeys(r["eligibility"] for r in ug):
        rule = by_text.get(text)
        if rule is None:
            raise BuildError(f"no eligibility rule for UG text {text[:60]!r}")
        names = [r["name"] for r in ug if r["eligibility"] == text]
        if rule["programs"] != names:
            raise BuildError(f"eligibility rule {text[:60]!r} lists {rule['programs']}; "
                             f"the UG records with that text are {names}")
    for text in by_text:
        if not any(r["eligibility"] == text for r in ug):
            raise BuildError(f"eligibility rule matches no UG record: {text[:60]!r}")
    for r in ug:
        r["eligibility_rule"] = {k: by_text[r["eligibility"]][k] for k in RULE_FIELDS}
    return subjects
```

In `build()`, replace the final `return {...}` (lines 337-342) with:

```python
    with open(ELIGIBILITY_JSON, encoding="utf-8") as f:
        subjects = attach_eligibility_rules(records, json.load(f))

    return {
        "session": SESSION,
        "source": [os.path.basename(PROGRAM_XLSX), os.path.basename(FEE_XLSX)],
        "excluded": sorted(excluded),
        "subjects": subjects,
        "records": records,
    }
```

In `main()`, after the `print("records with aliases:", ...)` line add:

```python
    rules = [r["eligibility_rule"] for r in records if "eligibility_rule" in r]
    print(f"eligibility rules: {sum(r['verified'] for r in rules)} verified / {len(rules)} UG records")
```

Also extend the module docstring's "Design:" line to `Design: docs/spec/course-catalog-tools-design.md sections 3, 5 and 11.`

- [ ] **Step 6: Rebuild `courses.json`**

Run: `PYTHONIOENCODING=utf-8 venv312/Scripts/python.exe catalog/build_catalog.py`
Expected: the usual report plus `eligibility rules: 0 verified / 53 UG records`.

- [ ] **Step 7: Run the build tests**

Run: `venv312/Scripts/python.exe -m unittest tests.test_catalog_build -v`
Expected: all pass, including `test_committed_courses_json_is_fresh`.

- [ ] **Step 8: Run the full suite**

Run: `venv312/Scripts/python.exe -m unittest discover -s tests -t .`
Expected: only the known baseline failure. (`course_catalog` ignores the new keys so far.)

- [ ] **Step 9: No commit** (operator commits when asked).

---

### Task 2: The tool in `course_catalog.py`

**Files:**
- Modify: `course_catalog.py` (constants at lines 16-20; derived values at lines 78-82; `list_programs` at 195-237; `result_status` at 385-391; `HANDLERS` / `TOOL_SPECS` at 329-371)
- Modify: `tests/tool_playground.py:36-40` (float parsing) and its docstring
- Create: `tests/test_find_eligible_programs.py`
- Modify: `tests/test_course_catalog.py` (`test_constants_are_pinned` line 128, `TestToolSpecs` lines 273-275, `test_declarations` line 269)
- Modify: `tests/test_catalog_tool_handler.py:48`

**Interfaces:**
- Consumes: `courses.json` `subjects` and per-UG-record `eligibility_rule` from Task 1.
- Produces:
  - `course_catalog.ELIGIBLE_NAMES_MAX = 10`, `course_catalog.SUBJECTS: list[str]`, `course_catalog.UG_DEGREES: list[str]` (13 entries, sorted).
  - `course_catalog.find_eligible_programs(args: dict|None) -> dict` with responses `{"count": n, "programs": [{"name": str, "conditions"?: list[str]}], "not_covered": k}`, `{"count": n, "by_degree": [{"degree": str, "count": int}], "not_covered": k}`, `{"count": 0, "not_covered": k}`, or `{"error": str}`.
  - `course_catalog.result_status("find_eligible_programs", response) -> "eligible=<n>" | "error"`.
  - Private helpers `_keyword_filter(entries, keyword) -> (entries|None, problem|None)` and `_by_degree(entries) -> list[dict]`, also used by `list_programs`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_find_eligible_programs.py`:

```python
"""course_catalog.find_eligible_programs (spec section 11.4).

Matching tests run on a fake rule set patched into the index, so they do not
depend on which real rules the operator has verified. The golden profiles run
on the real data and skip until every UG rule is verified.
"""
import unittest
from unittest import mock

import course_catalog as cc
from tests import tool_playground

PCM = ["Physics", "Chemistry", "Mathematics"]
PCB = ["Physics", "Chemistry", "Biology"]
TOOL = "find_eligible_programs"


def rule(minimum=50, options=None, conditions=(), verified=True):
    return {"min_aggregate": minimum, "subject_options": options, "conditions": list(conditions),
            "verified": verified}


def record(name, degree, eligibility_rule, school="SoX"):
    return {"id": name.lower().replace(" ", "-"), "name": name, "aliases": [], "degree": degree,
            "level": "UG", "school": {"code": school, "name": "X"}, "department": "Dept",
            "eligibility_rule": eligibility_rule}


FAKES = [
    record("B.Tech (Alpha Engineering)", "B.Tech", rule(60, [["Physics", "Mathematics", "Chemistry"],
                                                              ["Physics", "Mathematics", "Biology"]],
                                                          ["at least 45% in each of those subjects"])),
    record("B.A (Beta Studies)", "B.A", rule(50)),
    record("B.Sc (Gamma Science)", "B.Sc", rule(55, [["Biology"]])),
    record("B.Ed", "B.Ed", rule(None, conditions=["a degree"])),
    record("BBA (Delta)", "BBA", rule(40, verified=False)),
]


def find(**args):
    return cc.handle_tool_call(TOOL, args)


def names(response):
    return [p["name"] for p in response["programs"]]


class FakeIndexCase(unittest.TestCase):
    records = FAKES

    def setUp(self):
        patcher = mock.patch.object(cc, "INDEX", [cc._index(r) for r in self.records])
        patcher.start()
        self.addCleanup(patcher.stop)


class TestMatching(FakeIndexCase):
    def test_minimum_is_inclusive(self):
        self.assertIn("B.Tech (Alpha Engineering)", names(find(aggregate_pct=60, subjects=PCM)))
        self.assertNotIn("B.Tech (Alpha Engineering)", names(find(aggregate_pct=59.9, subjects=PCM)))

    def test_one_subject_set_must_be_fully_covered(self):
        self.assertNotIn("B.Tech (Alpha Engineering)", names(find(aggregate_pct=70, subjects=PCB)))
        self.assertIn("B.Tech (Alpha Engineering)",
                      names(find(aggregate_pct=70, subjects=["physics", "MATHEMATICS", "Biology"])))

    def test_any_stream_rule_matches_no_subjects(self):
        self.assertEqual(find(aggregate_pct=52, subjects=[]), {
            "count": 1, "programs": [{"name": "B.A (Beta Studies)"}], "not_covered": 2})

    def test_unverified_and_null_minimum_are_not_covered_never_named(self):
        response = find(aggregate_pct=99, subjects=PCB)
        self.assertEqual(response["not_covered"], 2)
        self.assertNotIn("B.Ed", names(response))
        self.assertNotIn("BBA (Delta)", names(response))

    def test_conditions_only_when_present(self):
        response = find(aggregate_pct=70, subjects=["Physics", "Mathematics", "Chemistry", "Biology"])
        self.assertEqual(response["programs"], [
            {"name": "B.A (Beta Studies)"},
            {"name": "B.Sc (Gamma Science)"},
            {"name": "B.Tech (Alpha Engineering)", "conditions": ["at least 45% in each of those subjects"]},
        ])

    def test_nothing_matches(self):
        self.assertEqual(find(aggregate_pct=30, subjects=PCM), {"count": 0, "not_covered": 2})

    def test_filters_narrow_the_pool(self):
        self.assertEqual(find(aggregate_pct=70, subjects=PCB, degree="B.Sc"), {
            "count": 1, "programs": [{"name": "B.Sc (Gamma Science)"}], "not_covered": 0})
        self.assertEqual(find(aggregate_pct=70, subjects=PCB, degree="bsc")["count"], 1)
        self.assertEqual(find(aggregate_pct=70, subjects=PCB, keyword="gamma")["count"], 1)
        self.assertEqual(find(aggregate_pct=70, subjects=PCB, degree="B.Com"), {"count": 0, "not_covered": 0})

    def test_float_percentage_and_sdk_sequences(self):
        self.assertEqual(find(aggregate_pct=55.5, subjects=("Biology",))["count"], 2)
        self.assertEqual(find(aggregate_pct=55, subjects="Biology"), find(aggregate_pct=55, subjects=["Biology"]))

    def test_status(self):
        self.assertEqual(cc.result_status(TOOL, find(aggregate_pct=52, subjects=[])), "eligible=1")
        self.assertEqual(cc.result_status(TOOL, find(subjects=[])), "error")

    def test_errors(self):
        for args, fragment in (({"subjects": []}, "aggregate_pct is required"),
                               ({"aggregate_pct": "58", "subjects": []}, "aggregate_pct"),
                               ({"aggregate_pct": "58%", "subjects": []}, "aggregate_pct"),
                               ({"aggregate_pct": True, "subjects": []}, "aggregate_pct"),
                               ({"aggregate_pct": 101, "subjects": []}, "aggregate_pct"),
                               ({"aggregate_pct": -1, "subjects": []}, "aggregate_pct"),
                               ({"aggregate_pct": 60}, "subjects is required"),
                               ({"aggregate_pct": 60, "subjects": 5}, "subjects must be a list"),
                               ({"aggregate_pct": 60, "subjects": ["Accountancy"]}, "unknown subject 'Accountancy'"),
                               ({"aggregate_pct": 60, "subjects": [], "degree": "MBA"}, "unknown degree 'MBA'"),
                               ({"aggregate_pct": 60, "subjects": [], "keyword": "course"}, "keyword"),
                               ({"aggregate_pct": 60, "subjects": [], "level": "UG"}, "unknown argument 'level'")):
            with self.subTest(args):
                self.assertIn(fragment, cc.handle_tool_call(TOOL, args)["error"])
        self.assertIn("B.Tech", find(aggregate_pct=60, subjects=[], degree="MBA")["error"])
        self.assertIn("error", cc.handle_tool_call(TOOL, None))


class TestStaging(FakeIndexCase):
    records = [record(f"B.A (Subject {chr(65 + i)})", "B.A", rule(50)) for i in range(10)] \
        + [record("B.Sc (Extra)", "B.Sc", rule(50))]

    def test_more_than_ten_groups_by_degree(self):
        self.assertEqual(find(aggregate_pct=60, subjects=[]), {
            "count": 11, "by_degree": [{"degree": "B.A", "count": 10}, {"degree": "B.Sc", "count": 1}],
            "not_covered": 0})

    def test_ten_lists_names(self):
        response = find(aggregate_pct=60, subjects=[], degree="B.A")
        self.assertEqual(response["count"], 10)
        self.assertEqual(len(response["programs"]), 10)


class TestRealData(unittest.TestCase):
    def test_not_covered_counts_unjudgeable_ug_records(self):
        ug = [r for r in cc.RECORDS if r["level"] == "UG"]
        expected = sum(1 for r in ug if not r["eligibility_rule"]["verified"]
                       or r["eligibility_rule"]["min_aggregate"] is None)
        self.assertEqual(find(aggregate_pct=100, subjects=cc.SUBJECTS)["not_covered"], expected)

    def test_vocabulary_and_degrees(self):
        self.assertEqual(len(cc.SUBJECTS), 16)
        self.assertEqual(cc.UG_DEGREES, ["B.A", "B.Com", "B.Ed", "B.Optom", "B.Pharm", "B.Sc", "B.Tech",
                                         "BA LL.B", "BBA", "BBA LL.B", "BCA", "BFND", "BMLS"])

    def test_declaration(self):
        spec = next(s for s in cc.TOOL_SPECS if s["name"] == TOOL)
        props = spec["parameters"]["properties"]
        self.assertEqual(spec["parameters"]["required"], ["aggregate_pct", "subjects"])
        self.assertEqual(props["subjects"]["items"]["enum"], cc.SUBJECTS)
        self.assertEqual(props["degree"]["enum"], cc.UG_DEGREES)
        self.assertEqual(props["aggregate_pct"]["type"], "NUMBER")

    def test_declaration_tells_model_to_drop_unlisted_subjects(self):
        spec = next(s for s in cc.TOOL_SPECS if s["name"] == TOOL)
        self.assertIn("leave out", spec["parameters"]["properties"]["subjects"]["description"])
        self.assertIn("not CGPA", spec["parameters"]["properties"]["aggregate_pct"]["description"])
        self.assertIn("PCB = Physics, Chemistry, Biology", spec["description"])

    def test_every_response_fits_the_budget(self):
        profiles = [[], PCM, PCB, PCM + ["Biology"], ["Economics"], ["Geography"], cc.SUBJECTS]
        for pct in (50, 60, 75, 100):
            for subjects in profiles:
                for degree in [None] + cc.UG_DEGREES:
                    args = {"aggregate_pct": pct, "subjects": subjects}
                    if degree:
                        args["degree"] = degree
                    with self.subTest(args):
                        self.assertLessEqual(cc.response_chars(cc.handle_tool_call(TOOL, args)), 1500)

    def test_playground_parses_a_float_percentage(self):
        self.assertEqual(tool_playground.parse_args(TOOL, ["aggregate_pct=58.5", "subjects=Physics,Chemistry,Biology"]),
                         {"aggregate_pct": 58.5, "subjects": PCB})


ALL_VERIFIED = all(r["eligibility_rule"]["verified"] for r in cc.RECORDS if r["level"] == "UG")


@unittest.skipUnless(ALL_VERIFIED, "eligibility_rules.json not fully verified by the operator yet")
class TestGoldenProfiles(unittest.TestCase):
    """Expected results derived by hand from the eligibility texts (spec 11.6)."""

    def test_58_pcb(self):
        self.assertEqual(find(aggregate_pct=58, subjects=PCB), {
            "count": 27, "by_degree": [
                {"degree": "B.Sc", "count": 13}, {"degree": "B.A", "count": 9},
                {"degree": "B.Com", "count": 1}, {"degree": "B.Optom", "count": 1},
                {"degree": "B.Tech", "count": 1}, {"degree": "BFND", "count": 1}, {"degree": "BMLS", "count": 1}],
            "not_covered": 1})

    def test_58_pcb_btech(self):
        self.assertEqual(find(aggregate_pct=58, subjects=PCB, degree="B.Tech"), {
            "count": 1, "programs": [{"name": "B.Tech (Biomedical Engineering)",
                                      "conditions": ["at least 45% in each of those subjects"]}],
            "not_covered": 0})

    def test_62_pcm(self):
        response = find(aggregate_pct=62, subjects=PCM)
        self.assertEqual(response["count"], 47)
        self.assertIn({"degree": "B.Tech", "count": 18}, response["by_degree"])

    def test_55_commerce(self):
        self.assertEqual(find(aggregate_pct=55, subjects=["Economics"]), {
            "count": 13, "by_degree": [{"degree": "B.A", "count": 9}, {"degree": "B.Sc", "count": 3},
                                       {"degree": "B.Com", "count": 1}],
            "not_covered": 1})

    def test_48_pcm_matches_nothing(self):
        self.assertEqual(find(aggregate_pct=48, subjects=PCM), {"count": 0, "not_covered": 1})

    def test_70_arts_with_geography_bsc(self):
        response = find(aggregate_pct=70, subjects=["Geography"], degree="B.Sc")
        self.assertCountEqual(names(response), [
            "B.Sc (Environmental Science and Sustainability)", "B.Sc (Economics)", "B.Sc (Geography)",
            "B.Sc (Geoinformatics)", "B.Sc. Graphics, Animation and Media Technology"])
        conditions = {p["name"]: p.get("conditions") for p in response["programs"]}
        self.assertEqual(conditions["B.Sc (Environmental Science and Sustainability)"],
                         ["10+2 with science and humanities subjects"])


if __name__ == "__main__":
    unittest.main()
```

Golden-count derivation (so a mismatch is investigated, not copied over): 58% PCB matches the 10 programs of the plain 50% text (9 B.A plus B.Sc Graphics), B.Com (55), B.Sc Environmental Science (any stream), B.Sc Economics (any stream), B.Sc Chemistry and Physics (P+C), Forensic, Geography, Geoinformatics (PCB), Biomedical (55, PCB), Biochemistry and Microbiology (C+B), B.Sc Biotechnology (55, C+B), BMLS, Optometry, BFND (C+B), B.Sc Psychology (PCB), Agriculture (PCB) = 27, of which B.Sc 13. 62% PCM: 18 any-stream (10 + B.Com + BBA, BBA E-Commerce, BCA + Env Sci + Economics + the two LL.B) plus 29 subject-matched (B.Sc Chemistry/Physics 2, Forensic, Geography, Geoinformatics, Applied Statistics, Mathematics and Computing, all 18 B.Tech, B.Pharm, Optometry, B.Sc Psychology, Agriculture) = 47. `not_covered` is 1 (B.Ed) in the unfiltered pool.

Update `tests/test_course_catalog.py`:
- line 128: `self.assertEqual((cc.MAX_PROGRAMS, cc.LIST_NAMES_MAX, cc.MAX_CANDIDATES, cc.ELIGIBLE_NAMES_MAX), (3, 20, 4, 10))`
- lines 274-275:
```python
        self.assertEqual(cc.TOOL_NAMES, {"list_programs", "get_program_details", "find_eligible_programs"})
        self.assertEqual([s["name"] for s in cc.TOOL_SPECS],
                         ["list_programs", "get_program_details", "find_eligible_programs"])
```
- `test_declarations` (line 269): budget `1800` -> `2600`.

Update `tests/test_catalog_tool_handler.py:48`:
```python
        self.assertEqual(names, ["endCall", "transferCall", "list_programs", "get_program_details",
                                 "find_eligible_programs"])
```

- [ ] **Step 2: Run to verify they fail**

Run: `venv312/Scripts/python.exe -m unittest tests.test_find_eligible_programs tests.test_course_catalog tests.test_catalog_tool_handler -v`
Expected: `test_find_eligible_programs` fails to import usefully (`AttributeError: ... 'SUBJECTS'` at `ALL_VERIFIED`/tests, or `unknown tool find_eligible_programs`); the three updated assertions in the other two modules FAIL.

- [ ] **Step 3: Implement in `course_catalog.py`**

Constants (after `MAX_CANDIDATES = 4`, line 20):

```python
ELIGIBLE_NAMES_MAX = 10
```

Derived values (after `SCHOOL_CODES = ...`, line 81):

```python
SUBJECTS = CATALOG["subjects"]
UG_DEGREES = sorted({r["degree"] for r in RECORDS if r["level"] == "UG"})
```

Replace the keyword and grouping code in `list_programs` with two helpers. Add above `def list_programs`:

```python
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
```

In `list_programs`, replace

```python
    if "keyword" in args:
        words = tokens(args["keyword"])
        if not words:
            return _error("keyword has no searchable words")
        entries = [e for e in entries
                   if all(_token_hits(t, e["match_tokens"] | e["dept_tokens"]) for t in words)]
```
with
```python
    if "keyword" in args:
        entries, problem = _keyword_filter(entries, args["keyword"])
        if problem:
            return _error(problem)
```
and replace its last five lines (`counts = {}` ... `return {"count": len(entries), "by_degree": ...}`) with
```python
    return {"count": len(entries), "by_degree": _by_degree(entries)}
```

Add after `get_program_details`:

```python
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
    if len(matches) > ELIGIBLE_NAMES_MAX:
        return {"count": len(matches), "by_degree": _by_degree(matches), "not_covered": not_covered}
    programs = []
    for entry in sorted(matches, key=_order):
        item = {"name": entry["record"]["name"]}
        if entry["record"]["eligibility_rule"]["conditions"]:
            item["conditions"] = entry["record"]["eligibility_rule"]["conditions"]
        programs.append(item)
    return {"count": len(matches), "programs": programs, "not_covered": not_covered}
```

Replace `HANDLERS` with:

```python
HANDLERS = {"list_programs": list_programs, "get_program_details": get_program_details,
            "find_eligible_programs": find_eligible_programs}
```

Append to `TOOL_SPECS` (after the `get_program_details` entry):

```python
    {
        "name": "find_eligible_programs",
        "description": ("Undergraduate programs the caller appears eligible for from 10+2 marks. "
                        "PCM = Physics, Chemistry, Mathematics; PCB = Physics, Chemistry, Biology. "
                        "Returns names with conditions to read out, or counts per degree when more "
                        "than 10 match; not_covered counts programs it cannot judge."),
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
```

In `result_status`, before `return ",".join(...)`:

```python
    if name == "find_eligible_programs":
        return f"eligible={response['count']}"
```

Update the module docstring's "Design:" line to `Design: docs/spec/course-catalog-tools-design.md sections 6 and 11.`

- [ ] **Step 4: Playground float parsing**

In `tests/tool_playground.py`, replace

```python
        elif kind in ("INTEGER", "NUMBER"):
            try:
                args[name] = int(value)
            except ValueError:
                args[name] = value
```
with
```python
        elif kind in ("INTEGER", "NUMBER"):
            for convert in (int, float):
                try:
                    args[name] = convert(value)
                    break
                except ValueError:
                    args[name] = value
```

and add to the docstring usage block, after the `get_program_details` JSON example:

```
    venv312/Scripts/python.exe tests/tool_playground.py find_eligible_programs aggregate_pct=58 subjects=Physics,Chemistry,Biology
```

- [ ] **Step 5: Run the tests**

Run: `venv312/Scripts/python.exe -m unittest tests.test_find_eligible_programs tests.test_course_catalog tests.test_catalog_tool_handler -v`
Expected: all pass; `TestGoldenProfiles` skipped ("not fully verified"). If `test_every_response_fits_the_budget` fails, print the largest response with the playground and report the measured size to the operator instead of raising the budget.

- [ ] **Step 6: Exercise the playground**

Run: `PYTHONIOENCODING=utf-8 venv312/Scripts/python.exe tests/tool_playground.py find_eligible_programs aggregate_pct=58 subjects=Physics,Chemistry,Biology`
Expected (all rules unverified): `{"count": 0, "not_covered": 53}`. Then `... --declarations` and note the total chars (under 2600).

- [ ] **Step 7: Full suite**

Run: `venv312/Scripts/python.exe -m unittest discover -s tests -t .`
Expected: only the known baseline failure.

- [ ] **Step 8: No commit.**

---

### Task 3: Prompt

**Files:**
- Modify: `prompt_au.txt:22` and `prompt_au.txt:30`
- Test: `tests/test_prompt_catalog.py` (lines 24-27 and 62-63)

**Interfaces:**
- Consumes: tool name `find_eligible_programs`, response keys `count`, `not_covered`, `by_degree` (Task 2).
- Produces: nothing code-facing.

- [ ] **Step 1: Write the failing tests**

In `tests/test_prompt_catalog.py`, replace `test_names_the_phase_1_tools_only` with:

```python
    def test_names_all_tools(self):
        for name in ("list_programs", "get_program_details", "find_eligible_programs", "transferCall", "endCall"):
            self.assertIn(name, self.text)
```

and replace `test_open_eligibility_rule` with:

```python
    def test_open_eligibility_rule(self):
        # Spec 11.1 and 11.5: "appear eligible", never a negative verdict.
        self.assertIn("you appear eligible for", self.text)
        self.assertIn('Never say "you are not eligible"', self.text)
        self.assertEqual(self.text.count("not eligible"), 1)
        self.assertNotIn('Do NOT say "you are eligible"', self.text)
        self.assertIn("admissions team confirms final eligibility", self.text)
        # Review focus: the tool cannot tell a CGPA from a percentage.
        self.assertIn("CGPA", self.text)
        self.assertIn("expected percentage", self.text)
        self.assertIn("`not_covered`", self.text)
```

- [ ] **Step 2: Run to verify they fail**

Run: `venv312/Scripts/python.exe -m unittest tests.test_prompt_catalog -v`
Expected: `test_names_all_tools` and `test_open_eligibility_rule` FAIL (plus the known baseline failure).

- [ ] **Step 3: Edit `prompt_au.txt`**

Line 22: append one sentence at the end of the line (after `...only \`eligibility\` for an eligibility question).`):

```
 Marks-based "what can I apply for": `find_eligible_programs`.
```

Line 30: replace the whole line with:

```
- Open-ended eligibility for undergraduate programs ("I got 58% with PCB, what can I apply for?"): if not known yet, ask the 10+2 aggregate percentage and the 10+2 subjects, then call `find_eligible_programs` (subjects only from its list; leave out others). If they give a CGPA or grade, ask for the percentage; if results are not out yet, use the expected percentage and say it depends on the final result. Say "based on what you shared, you appear eligible for", read at most 5 names with their conditions, and add that the admissions team confirms final eligibility. Never say "you are not eligible". If `count` is 0 or `not_covered` is above 0, say a senior admission counselor can check the other programs. For postgraduate programs there is no check across programs: ask which program interests them, fetch its `eligibility` and read it out.
```

- [ ] **Step 4: Run the prompt tests**

Run: `venv312/Scripts/python.exe -m unittest tests.test_prompt_catalog -v`
Expected: all pass except the known baseline failure; `test_formats_like_app_py_does` and `test_no_fee_figures` still pass.

- [ ] **Step 5: No commit.**

---

### Task 4: Docs, measurements, final suite

**Files:**
- Modify: `docs/spec/course-catalog-tools-design.md` §6.5 table
- Modify: `CLAUDE.md` ("Current state" suite count, "Resume here" item 0, new session-log entry)
- Modify: `docs/HANDOFF2.md` (the phase 2 line, if it says "designed, not built")
- Modify memory: `C:\Users\com\.claude\projects\C--Users-com-local-AU-Admission-Assist\memory\course-catalog-tools.md`

**Interfaces:** none.

- [ ] **Step 1: Measure**

Run:
```
PYTHONIOENCODING=utf-8 venv312/Scripts/python.exe -c "import course_catalog as c; print(c.response_chars(c.TOOL_SPECS)); print(max(c.response_chars(c.handle_tool_call('find_eligible_programs', {'aggregate_pct': p, 'subjects': s, **({'degree': d} if d else {})})) for p in (50,60,75,100) for s in ([], ['Physics','Chemistry','Mathematics'], ['Physics','Chemistry','Biology'], c.SUBJECTS) for d in [None]+c.UG_DEGREES))"
```
Note both numbers. The second is meaningful only after rules are verified; with all unverified it is tiny. Record it as "unverified" in the table and re-measure after the operator's review.

- [ ] **Step 2: Spec §6.5**

In the table, replace the `| Both catalog tool declarations | 1397 | 1800 |` row with:

```
| All three catalog tool declarations | <measured> | 2600 |
| Any `find_eligible_programs` response | <measured, or "re-measure after review"> | 1500 |
```

- [ ] **Step 3: CLAUDE.md**

- "Current state" suite line: the new count from the final run (Step 5).
- "Resume here" item 0: add a sub-bullet: `find_eligible_programs` built (spec §11, plan `docs/spec/find-eligible-programs-plan.md`); all 27 rules in `catalog/eligibility_rules.json` are `verified: false` until the operator reviews them (then rebuild with `catalog/build_catalog.py`; the golden profiles in `tests/test_find_eligible_programs.py` then run). Remove "Phase 2 ... designed, not built".
- Session log: a short 2026-10-06 entry (operator reversed the deferral; UG only, operator-verified rules keyed by text, conditions returned not filtered, app.py untouched; known baseline failure `test_reply_language_counts_as_the_choice` from `db6fe2b`).

- [ ] **Step 4: Memory**

Update `course-catalog-tools.md` (phase 2 built 2026-10-06, rules unverified pending operator review, golden tests skip until then) and its `MEMORY.md` line.

- [ ] **Step 5: Final suite**

Run: `venv312/Scripts/python.exe -m unittest discover -s tests -t .`
Expected: baseline 325 plus the new tests, the one known baseline failure, skips = 2 + the golden class.

- [ ] **Step 6: No commit.** Report to the operator: rules file ready for review (`catalog/eligibility_rules.json`; read each `text` against its rule and `note`, set `verified: true`, rebuild, run the suite so the golden profiles run).
