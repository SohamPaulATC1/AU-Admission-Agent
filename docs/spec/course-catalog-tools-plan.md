# Course catalog tools Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give Neha two Gemini Live function tools, `list_programs` and `get_program_details`, that answer from a catalog of all 87 Adamas University programs for the 2027 session, plus an offline simulator and a prompt that uses them.

**Architecture:** An offline stdlib script (`catalog/build_catalog.py`) joins the two Excel files on UID, validates every fee row, and writes `catalog/courses.json`. A runtime module (`course_catalog.py`) loads that JSON and `catalog/aliases.json` once at import, resolves the caller's phrasing to records deterministically, and returns small projected responses. app.py declares the tools from `course_catalog.TOOL_SPECS` and routes calls to `course_catalog.handle_tool_call` in its existing tool-call branch.

**Tech Stack:** Python 3.12 (`venv312`), stdlib only (zipfile, xml.etree, json, re), `google.genai` `types.FunctionDeclaration` / `types.FunctionResponse` (already used by app.py), stdlib `unittest` with the existing `tests/harness`.

**Spec:** `docs/spec/course-catalog-tools-design.md` (approved 2026-10-05, corrected after scratch validation the same day).

## Global Constraints

- Stdlib only. `requirements.txt` and `.env` are frozen: no openpyxl, no new packages.
- Tests are stdlib `unittest` only (no pytest, no hypothesis).
- Run the suite with `venv312/Scripts/python.exe -m unittest discover -s tests -t .` from the repo root (Python 3.12; 3.14 has no audioop).
- Set `PYTHONIOENCODING=utf-8` when printing app log lines or catalog output (Windows console is cp1252).
- Don't write test code through bash heredocs (escapes get mangled). Use the Write/Edit tools.
- Prompt edits go in `prompt_au.txt`. Never touch `prompt.txt` (operator copies it over by hand).
- Do not read `.env` or credential JSON. Do not touch `aec.py` (hash pinned).
- Every new call_state key goes into the mirror in `tests/harness/appctl.py` (guarded by `test_harness_smoke`).
- Re-pin the GEMINI_MODEL line pins in `tests/test_preservation_4_8_resumption_recording_stats.py` whenever app.py's line count changes.
- app.py keeps its slim one-line comment style (operator decision 2026-10-05).
- Do not commit. The operator commits when asked. Never add `TEST_FILES/`, `.env`, credential JSON, `*.db`, recordings.
- `prompt_au.txt` may contain no braces other than `{user_name}` and `{phone_number}` (app.py fills it with `str.format`).
- Phase 1 only: no `find_eligible_programs`, no scholarship tool.

## Review Focus

1. Gemini sends a whole-number argument as a float (`max_total_fee=300000.0`): the filter must treat it as rupees, not reject it. Pinned in Task 2 `test_filters_combine`.
2. Gemini sends `programs` as one string instead of an array (`"cse"`): treat it as a one-item list. Pinned in Task 2 `test_mixed_statuses_and_string_programs`.
3. The SDK hands arrays over as its own sequence types, not `list`: `programs` / `fields` given as tuples must work. Pinned in Task 2 `test_mixed_statuses_and_string_programs`.
4. The model passes untranslated caller words ("computer wala") despite the prompt's English-arguments rule: return `not_found`, never a confident wrong program. Pinned in Task 2 `test_not_found`.
5. Gemini calls a tool name that is not declared (hallucinated or stale): it must get an error response, not silence, or the session waits forever. Pinned in Task 3 `TestUnknownTool`.

## File Map

| Path | Task | Responsibility |
|---|---|---|
| `catalog/__init__.py` | 1 | Makes `from catalog import build_catalog` importable under `-t .`. |
| `catalog/build_catalog.py` | 1 | Excel -> validated `courses.json`. Run by hand when the Excel changes. |
| `catalog/aliases.json` | 1 | Hand-edited aliases per record id and short-form expansions. |
| `catalog/courses.json` | 1 | Generated, committed. The only catalog data the app reads. |
| `tests/test_catalog_build.py` | 1 | Join rules, arithmetic, ids, degree set, staleness guard. |
| `course_catalog.py` | 2 | Runtime: load, resolve names, the two tools, `TOOL_SPECS`. |
| `tests/tool_playground.py` | 2 | Offline simulator CLI (not a test module). |
| `tests/test_course_catalog.py` | 2 | Resolution, responses, errors, size budgets, specs, playground parsing. |
| `app.py` | 3 | Import, declarations, tool branch, unknown-tool branch, counters, stats line. |
| `tests/harness/appctl.py` | 3 | call_state mirror and `[CATALOG]` diagnostic tag. |
| `tests/test_catalog_tool_handler.py` | 3 | The app.py wiring through the fake Gemini session. |
| `tests/test_preservation_4_4_tool_and_silence.py` | 3 | Intentional baseline update (unknown tool now answered). |
| `tests/test_preservation_4_8_resumption_recording_stats.py` | 3 | Re-pin GEMINI_MODEL lines. |
| `prompt_au.txt` | 4 | Rewritten prompt: catalog map and tool rules, no fee figures. |
| `tests/test_prompt_catalog.py` | 4 | Prompt formats, names the tools, has the map, no fee figures. |
| `CLAUDE.md` | 5 | Current state and session log. |

Suite count: 251 today; Tasks 1-4 add 18 + 33 + 9 + 5 = 65 tests, so 316 at the end (2 skipped, 1 expected failure, unchanged). The whole plan was dry-run on a scratch copy of the repo on 2026-10-05 with exactly these files and edits: 316 OK.

---

### Task 1: Catalog build script, aliases and generated courses.json

**Files:**
- Create: `catalog/__init__.py` (empty)
- Create: `catalog/build_catalog.py`
- Create: `catalog/aliases.json`
- Create (generated): `catalog/courses.json`
- Test: `tests/test_catalog_build.py`

**Interfaces:**
- Consumes: `AU Admission Data/Academic Program Notification_2026-2027_Corrigendum_FINAL_06082026 (4) (1).xlsx` and `AU Admission Data/National Course fee 2026.xlsx` (already in the repo).
- Produces:
  - `catalog/courses.json`: `{"session": "2027", "source": [two basenames], "excluded": [two names], "records": [87 records]}`. Record keys, in order: `id, uid, name, aliases, degree, level, school {name, code}, department, duration_years (int|null), duration_note (str|null), seats (int), eligibility (str), fees {session, first_semester, admission_fee, admission_fee_note, due_at_admission, per_year [int], per_semester [int], program_fee, total}, add_ons [{name, duration_years, fee}]`, plus `seats_note` on the two M.Pharm records. Amounts are int rupees.
  - `catalog/aliases.json`: `{"expansions": {short: phrase}, "records": {id: [alias, ...]}}`. Task 2 reads `expansions` at runtime; the build merges `records` into each record's `aliases`.
  - `build_catalog.build() -> dict`, `build_catalog.dumps(catalog) -> str`, `build_catalog.BuildError`, `slug(name)`, `degree_of(name)`, `amount(text, what)`, `fee_block(row, label) -> (years, fees)`, constants `PROGRAM_XLSX`, `FEE_XLSX`, `COURSES_JSON`, `EXCLUDED`.

- [ ] **Step 1: Write the failing test**

Create `catalog/__init__.py` as an empty file (needed so the test can import `catalog.build_catalog`). Then create `tests/test_catalog_build.py`:

```python
"""catalog/build_catalog.py against the real AU Excel files (spec sections 3, 5, 9).

Skipped when the Excel files are absent (EC2 carries only the JSON).
"""
import collections
import json
import os
import unittest

from catalog import build_catalog

HAVE_EXCEL = os.path.exists(build_catalog.PROGRAM_XLSX) and os.path.exists(build_catalog.FEE_XLSX)


@unittest.skipUnless(HAVE_EXCEL, "AU Admission Data Excel files not present")
class TestBuildFromExcel(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = build_catalog.build()
        cls.records = cls.catalog["records"]
        cls.by_name = {r["name"]: r for r in cls.records}

    def test_record_count_and_exclusions(self):
        self.assertEqual(len(self.records), 87)
        self.assertEqual(self.catalog["excluded"], sorted(build_catalog.EXCLUDED))
        for name in build_catalog.EXCLUDED:
            self.assertNotIn(name, self.by_name)

    def test_uid_133_records_share_fees_and_keep_their_own_eligibility(self):
        group = [r for r in self.records if r["uid"] == "133"]
        self.assertEqual(len(group), 3)
        self.assertEqual(len({json.dumps(r["fees"]) for r in group}), 1)
        self.assertEqual(len({r["eligibility"] for r in group}), 2)  # the two AI & DS rows share a text
        for r in group:  # a shared fee row's name is not an alias of the other records
            self.assertNotIn("B.Tech CSE (Artificial Intelligence & Machine Learning)", r["aliases"])

    def test_bba_e_commerce_takes_the_bba_fee_row(self):
        self.assertEqual(self.by_name["BBA (E-Commerce)"]["fees"], self.by_name["BBA"]["fees"])
        self.assertNotIn("BBA", self.by_name["BBA (E-Commerce)"]["aliases"])

    def test_mpharm_row_is_split_with_own_fees_and_shared_intake(self):
        a = self.by_name["M.Pharm (Pharmaceutics)"]
        b = self.by_name["M.Pharm (Pharmacology)"]
        self.assertEqual((a["uid"], b["uid"]), ("171", "171"))
        self.assertEqual(a["eligibility"], b["eligibility"])
        self.assertEqual(a["seats_note"], "intake shared with M.Pharm (Pharmacology)")
        self.assertEqual(b["seats_note"], "intake shared with M.Pharm (Pharmaceutics)")
        self.assertEqual(sum("seats_note" in r for r in self.records), 2)

    def test_sap_add_on_is_attached_to_the_sap_record_only(self):
        sap = self.by_name["B.Tech CSE with specialization in SAP"]
        self.assertEqual(sap["add_ons"], [{"name": "SAP add-on certification", "duration_years": 1, "fee": 65000}])
        self.assertEqual(sap["fees"]["total"], 881800)
        self.assertEqual(sum(bool(r["add_ons"]) for r in self.records), 1)

    def test_cse_record_matches_the_fee_sheet(self):
        fees = self.by_name["B.Tech (Computer Science and Engineering)"]["fees"]
        self.assertEqual(fees["per_semester"],
                         [108250, 108250, 105750, 105750, 105000, 105000, 105000, 105000])
        self.assertEqual(fees["per_year"], [216500, 211500, 210000, 210000])
        self.assertEqual((fees["program_fee"], fees["admission_fee"], fees["total"]), (848000, 43800, 891800))
        self.assertEqual(fees["due_at_admission"], 152050)

    def test_fee_arithmetic_holds_for_every_record(self):
        for r in self.records:
            fees = r["fees"]
            with self.subTest(r["name"]):
                self.assertEqual(sum(fees["per_semester"]), fees["program_fee"])
                self.assertEqual(sum(fees["per_year"]), fees["program_fee"])
                self.assertEqual(fees["program_fee"] + fees["admission_fee"], fees["total"])
                self.assertEqual(fees["due_at_admission"], fees["first_semester"] + fees["admission_fee"])
                self.assertEqual(fees["first_semester"], fees["per_semester"][0])
                self.assertEqual(fees["session"], "2027")
                if r["duration_years"] is not None:
                    self.assertEqual(len(fees["per_semester"]), 2 * r["duration_years"])

    def test_ids_are_unique_and_readable(self):
        ids = [r["id"] for r in self.records]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertIn("btech-computer-science-and-engineering", ids)
        self.assertIn("ba-english-language-and-literature", ids)  # source spells it "B. A (...)"
        self.assertIn("bba-llb-hons", ids)
        for i in ids:
            self.assertRegex(i, r"^[a-z0-9]+(-[a-z0-9]+)*$")

    def test_degree_set_is_pinned(self):
        self.assertEqual(sorted({r["degree"] for r in self.records}), [
            "B.A", "B.Com", "B.Ed", "B.Optom", "B.Pharm", "B.Sc", "B.Tech", "BA LL.B", "BBA",
            "BBA LL.B", "BCA", "BFND", "BMLS", "D.Pharm", "LL.M", "M.A", "M.Pharm", "M.Sc",
            "M.Tech", "MBA", "MCA", "Post-M.Sc Diploma",
        ])
        self.assertEqual(collections.Counter(r["degree"] for r in self.records)["B.Tech"], 18)

    def test_levels_schools_and_durations(self):
        self.assertEqual({r["level"] for r in self.records}, {"UG", "PG", "Diploma"})
        self.assertEqual(collections.Counter(r["school"]["code"] for r in self.records), {
            "SoBAS": 16, "SoB": 6, "SoE": 3, "SoET": 20, "SoHMS": 12,
            "SoLJ": 3, "SoLACS": 14, "SoLB": 8, "SoMC": 4, "SoSA": 1,
        })
        free_text = [r for r in self.records if r["duration_years"] is None]
        self.assertEqual([r["name"] for r in free_text], ["Post-M.Sc Diploma in Medical Physics"])
        self.assertTrue(free_text[0]["duration_note"].startswith("1st Year"))

    def test_eligibility_whitespace_is_tidied(self):
        for r in self.records:
            self.assertNotRegex(r["eligibility"], r"10\s+\+|\+\s+2|\s{2}")

    def test_fee_file_names_become_aliases(self):
        bmls = self.by_name["Bachelor Of Medical Laboratory Science (BMLS)"]
        self.assertIn("B.Sc Medical Laboratory Technology", bmls["aliases"])
        self.assertIn("BA. LLB (Hons)", self.by_name["B.A. LL.B (Hons)"]["aliases"])

    def test_committed_courses_json_is_fresh(self):
        with open(build_catalog.COURSES_JSON, encoding="utf-8") as f:
            committed = f.read()
        self.assertEqual(committed, build_catalog.dumps(self.catalog),
                         "catalog/courses.json is stale: run catalog/build_catalog.py")


class TestBuildHelpers(unittest.TestCase):
    def test_slug_joins_spaced_degree_letters(self):
        self.assertEqual(build_catalog.slug("B. A (English Language and Literature)"),
                         "ba-english-language-and-literature")
        self.assertEqual(build_catalog.slug("B.B.A. LL.B (Hons)"), "bba-llb-hons")
        self.assertEqual(build_catalog.slug("B. Sc. (Hons.) Agriculture"), "bsc-hons-agriculture")
        self.assertEqual(build_catalog.slug("B.Tech CSE (Artificial Intelligence & Machine Learning)"),
                         "btech-cse-artificial-intelligence-and-machine-learning")

    def test_degree_of_rejects_unknown_names(self):
        self.assertEqual(build_catalog.degree_of("M. Sc. Graphics and Animation"), "M.Sc")
        self.assertEqual(build_catalog.degree_of("B.B.A. LL.B (Hons)"), "BBA LL.B")
        with self.assertRaises(build_catalog.BuildError):
            build_catalog.degree_of("MBBS")

    def test_amount_parsing(self):
        self.assertEqual(build_catalog.amount("", "x"), 0)
        self.assertEqual(build_catalog.amount("108250.0", "x"), 108250)
        with self.assertRaises(build_catalog.BuildError):
            build_catalog.amount("100.5", "x")

    def _fee_row(self, **overrides):
        row = {
            "Duration": "2", "1st Sem Fees 2026": "100", "2nd Sem Fees 2026": "100",
            "3rd Sem Fees 2026": "90", "4th Sem Fees 2026": "90", "5th - 6th Sem Fees 2026": "0",
            "7th - 10th Sem Fees 2026": "0", "Final Program Fees 2026 (Incl. FL, Alumni )": "380",
            "Admission Fees 2026 ( Incl. T-Shirt & Blazer )": "50", "Total Fees 2026": "430",
        }
        row.update(overrides)
        return row

    def test_fee_block_on_a_consistent_row(self):
        years, fees = build_catalog.fee_block(self._fee_row(), "t")
        self.assertEqual(years, 2)
        self.assertEqual(fees["per_semester"], [100, 100, 90, 90])
        self.assertEqual(fees["per_year"], [200, 180])
        self.assertEqual(fees["due_at_admission"], 150)

    def test_fee_block_rejects_bad_arithmetic(self):
        for overrides in ({"Total Fees 2026": "431"},
                          {"Final Program Fees 2026 (Incl. FL, Alumni )": "381", "Total Fees 2026": "431"},
                          {"5th - 6th Sem Fees 2026": "10"}):
            with self.subTest(overrides), self.assertRaises(build_catalog.BuildError):
                build_catalog.fee_block(self._fee_row(**overrides), "t")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv312/Scripts/python.exe -m unittest tests.test_catalog_build -v`
Expected: ERROR, `ImportError: cannot import name 'build_catalog' from 'catalog'`.

- [ ] **Step 3: Write the aliases file**

Create `catalog/aliases.json` (one-space indent, UTF-8, trailing newline):

```json
{
 "expansions": {
  "cse": "computer science engineering",
  "cs": "computer science",
  "ece": "electronics communication engineering",
  "ee": "electrical engineering",
  "ai": "artificial intelligence",
  "ml": "machine learning",
  "ds": "data science",
  "biotech": "biotechnology",
  "mech": "mechanical",
  "maths": "mathematics",
  "stats": "statistics",
  "pharmacy": "pharmaceutical",
  "lab": "laboratory",
  "math": "mathematics",
  "engg": "engineering"
 },
 "records": {
  "btech-computer-science-and-engineering": [
   "cse",
   "computer science",
   "computer science and engineering"
  ],
  "btech-electronics-and-communication-engineering": [
   "ece"
  ],
  "btech-electrical-engineering": [
   "ee"
  ],
  "btech-cse-artificial-intelligence-and-machine-learning": [
   "aiml",
   "ai ml",
   "cse aiml",
   "cse ai ml"
  ],
  "btech-computer-science-and-engineering-and-business-systems": [
   "csbs",
   "cse business systems"
  ],
  "bba-e-commerce": [
   "ecommerce"
  ],
  "ba-llb-hons": [
   "ballb",
   "law"
  ],
  "bba-llb-hons": [
   "bballb",
   "law"
  ],
  "llm": [
   "master of laws",
   "law"
  ],
  "bachelor-of-medical-laboratory-science-bmls": [
   "mlt"
  ],
  "bba": [
   "bachelor of business administration"
  ],
  "mba": [
   "master of business administration"
  ],
  "bca": [
   "bachelor of computer applications"
  ],
  "mca": [
   "master of computer applications"
  ],
  "bcom": [
   "bachelor of commerce"
  ],
  "bed": [
   "bachelor of education"
  ],
  "bpharm": [
   "bachelor of pharmacy"
  ],
  "dpharm": [
   "diploma in pharmacy"
  ]
 }
}
```

Notes for the implementer: `"law"` is deliberately shared by the three SoLJ records so "law" stays ambiguous; `"llb"` is deliberately absent (token scoring makes it ambiguous between the two LL.B programs). Task 2's `test_alias_keys_do_not_collide_with_another_record` pins this.

- [ ] **Step 4: Write the build script**

Create `catalog/build_catalog.py`:

```python
"""Build catalog/courses.json from the two AU admission Excel files.

Run by hand whenever either Excel file changes, then commit the new JSON:

    venv312/Scripts/python.exe catalog/build_catalog.py

Stdlib only (zipfile + ElementTree): requirements.txt is frozen, so no openpyxl.
Anything the join rules below do not explain fails the build with BuildError.
Design: docs/spec/course-catalog-tools-design.md sections 3 and 5.
"""
import collections
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "AU Admission Data")
PROGRAM_XLSX = os.path.join(
    DATA_DIR, "Academic Program Notification_2026-2027_Corrigendum_FINAL_06082026 (4) (1).xlsx"
)
FEE_XLSX = os.path.join(DATA_DIR, "National Course fee 2026.xlsx")
CATALOG_DIR = os.path.join(ROOT, "catalog")
ALIASES_JSON = os.path.join(CATALOG_DIR, "aliases.json")
COURSES_JSON = os.path.join(CATALOG_DIR, "courses.json")

SESSION = "2027"
ADMISSION_FEE_NOTE = "one-time, paid with the first semester fee; includes T-shirt and blazer"

# Operator-confirmed join special cases (2026-10-05). An entry that no longer
# matches the data is a build error, so this list cannot go stale silently.
SHARED_FEE_UIDS = {"121", "133"}
SPLIT_PROGRAMS = {"171": ["M.Pharm (Pharmaceutics)", "M.Pharm (Pharmacology)"]}
ADD_ON_FEE_ROWS = {
    "137": ("B.Tech (Computer Science and Engineering) SAP add-on- Certification",
            "SAP add-on certification"),
}
EXCLUDED = {"M. Tech (VLSI & Embedded Systems)", "Post-Graduate Diploma in Nuclear Medicine Technology"}

LEVEL_MAP = {"UG": "UG", "PG": "PG", "DIPLOMA": "Diploma", "Post-M.Sc. Diploma": "Diploma"}

# Checked in order against the name lowercased with spaces and dots removed, so
# "B. Sc. (Hons.)" and "B.Sc." both read "bsc". Longer prefixes come first.
DEGREE_PREFIXES = [
    ("bachelorofmedicallaboratory", "BMLS"),
    ("bachelorofoptometry", "B.Optom"),
    ("bacheloroffoodnutrition", "BFND"),
    ("post-msc", "Post-M.Sc Diploma"),
    ("ballb", "BA LL.B"),
    ("bballb", "BBA LL.B"),
    ("llm", "LL.M"),
    ("btech", "B.Tech"),
    ("mtech", "M.Tech"),
    ("bca", "BCA"),
    ("mca", "MCA"),
    ("bsc", "B.Sc"),
    ("msc", "M.Sc"),
    ("bcom", "B.Com"),
    ("bba", "BBA"),
    ("mba", "MBA"),
    ("bpharm", "B.Pharm"),
    ("mpharm", "M.Pharm"),
    ("dpharm", "D.Pharm"),
    ("bed", "B.Ed"),
    ("ba", "B.A"),
    ("ma", "M.A"),
]

FEE_COLUMN_PREFIXES = {
    "s1": "1st Sem", "s2": "2nd Sem", "s3": "3rd Sem", "s4": "4th Sem",
    "s56": "5th - 6th", "s710": "7th - 10th",
    "final": "Final Program", "admission": "Admission Fees", "total": "Total Fees",
}

_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
_REL_ID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"


class BuildError(Exception):
    pass


def tidy(text):
    return " ".join(str(text).split())


def _column_index(ref):
    n = 0
    for ch in re.match(r"[A-Z]+", ref).group(0):
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def read_sheets(path):
    """Every sheet of an xlsx file as a list of rows (lists of cell strings)."""
    with zipfile.ZipFile(path) as z:
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", _NS):
                shared.append("".join(t.text or "" for t in si.iter("{%s}t" % _NS["m"])))
        workbook = ET.fromstring(z.read("xl/workbook.xml"))
        rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        targets = {r.get("Id"): r.get("Target") for r in rels}
        sheets = []
        for sheet in workbook.find("m:sheets", _NS):
            target = targets[sheet.get(_REL_ID)].lstrip("/")
            if not target.startswith("xl/"):
                target = "xl/" + target
            rows = []
            for row in ET.fromstring(z.read(target)).iter("{%s}row" % _NS["m"]):
                cells = {}
                for c in row.findall("m:c", _NS):
                    v = c.find("m:v", _NS)
                    kind = c.get("t")
                    if kind == "s" and v is not None:
                        value = shared[int(v.text)]
                    elif kind == "inlineStr":
                        value = "".join(x.text or "" for x in c.iter("{%s}t" % _NS["m"]))
                    else:
                        value = v.text if v is not None else ""
                    cells[_column_index(c.get("r"))] = value
                if cells:
                    rows.append([cells.get(i, "") for i in range(max(cells) + 1)])
            sheets.append(rows)
        return sheets


def table_rows(path):
    """Data rows of every sheet as dicts keyed by the sheet's "S.N." header row."""
    out = []
    for rows in read_sheets(path):
        header = None
        for row in rows:
            cells = [tidy(c) for c in row]
            if cells[0] == "S.N.":
                header = cells
                continue
            if header is None or not any(cells):
                continue
            cells += [""] * (len(header) - len(cells))
            out.append(dict(zip(header, cells)))
    return out


def find_column(row, prefix):
    matches = [key for key in row if key.startswith(prefix)]
    if len(matches) != 1:
        raise BuildError(f"expected one column starting {prefix!r}, found {matches}")
    return matches[0]


def amount(text, what):
    if text == "":
        return 0
    value = float(text.replace(",", ""))
    if value != int(value):
        raise BuildError(f"{what}: amount {text!r} is not a whole number of rupees")
    return int(value)


def fee_block(row, label):
    """Fee fields for one fee row; asserts the arithmetic in spec section 3."""
    cols = {key: find_column(row, prefix) for key, prefix in FEE_COLUMN_PREFIXES.items()}
    v = {key: amount(row[col], f"{label} {col}") for key, col in cols.items()}
    years = int(amount(row[find_column(row, "Duration")], f"{label} Duration"))
    sems = 2 * years
    firsts = [v["s1"], v["s2"], v["s3"], v["s4"]]
    per_semester = (firsts[:sems]
                    + [v["s56"]] * min(2, max(0, sems - 4))
                    + [v["s710"]] * max(0, sems - 6))
    unused = firsts[sems:] + ([v["s56"]] if sems <= 4 else []) + ([v["s710"]] if sems <= 6 else [])
    if any(unused):
        raise BuildError(f"{label}: fee columns beyond its {years}-year duration are not zero")
    if sum(per_semester) != v["final"]:
        raise BuildError(f"{label}: semesters sum to {sum(per_semester)}, Final Program Fees is {v['final']}")
    if v["final"] + v["admission"] != v["total"]:
        raise BuildError(f"{label}: final {v['final']} + admission {v['admission']} != total {v['total']}")
    return years, {
        "session": SESSION,
        "first_semester": per_semester[0],
        "admission_fee": v["admission"],
        "admission_fee_note": ADMISSION_FEE_NOTE,
        "due_at_admission": per_semester[0] + v["admission"],
        "per_year": [per_semester[i] + per_semester[i + 1] for i in range(0, sems, 2)],
        "per_semester": per_semester,
        "program_fee": v["final"],
        "total": v["total"],
    }


def degree_of(name):
    compact = name.lower().replace(" ", "").replace(".", "")
    for prefix, degree in DEGREE_PREFIXES:
        if compact.startswith(prefix):
            return degree
    raise BuildError(f"no degree pattern matches program {name!r}")


# Same degree-token joins as course_catalog.normalize, so "B. A", "B.A." and
# "BA" all slug to "ba".
_SLUG_JOINS = [
    (re.compile(r"\bb b a\b"), "bba"),
    (re.compile(r"\b([bmd]) (tech|sc|com|ed|pharm|optom|a)\b"), r"\1\2"),
    (re.compile(r"\bll ([bm])\b"), r"ll\1"),
]


def slug(name):
    text = re.sub(r"[^a-z0-9]+", " ", name.lower().replace("&", " and ")).strip()
    for pattern, replacement in _SLUG_JOINS:
        text = pattern.sub(replacement, text)
    return text.replace(" ", "-")


def school_of(text):
    m = re.match(r"^(.*?)\s*\((\w+)\)$", text)
    if not m:
        raise BuildError(f"school {text!r} has no (CODE) suffix")
    return {"name": m.group(1), "code": m.group(2)}


def eligibility_text(text):
    return re.sub(r"10\s*\+\s*2", "10+2", tidy(text))


def build():
    programs = table_rows(PROGRAM_XLSX)
    fees = table_rows(FEE_XLSX)
    with open(ALIASES_JSON, encoding="utf-8") as f:
        hand_aliases = json.load(f)["records"]

    excluded = []
    prog_by_uid = collections.OrderedDict()
    for row in programs:
        if row["Program"] in EXCLUDED:
            excluded.append(row["Program"])
            continue
        if not row["UID"]:
            raise BuildError(f"program {row['Program']!r} has a blank UID")
        prog_by_uid.setdefault(row["UID"], []).append(row)
    fee_by_uid = collections.defaultdict(list)
    for row in fees:
        if row["Program."] in EXCLUDED:
            excluded.append(row["Program."])
            continue
        if not row["UID"]:
            raise BuildError(f"fee row {row['Program.']!r} has a blank UID")
        fee_by_uid[row["UID"]].append(row)

    if set(excluded) != EXCLUDED:
        raise BuildError(f"EXCLUDED entries not found in the data: {sorted(EXCLUDED - set(excluded))}")
    for uid in sorted(set(fee_by_uid) - set(prog_by_uid)):
        raise BuildError(f"fee UID {uid} has no program row")
    for table in (SHARED_FEE_UIDS, SPLIT_PROGRAMS, ADD_ON_FEE_ROWS):
        for uid in table:
            if uid not in prog_by_uid:
                raise BuildError(f"special-case UID {uid} is not in the program file")

    records = []
    for uid, prog_rows in prog_by_uid.items():
        fee_rows = list(fee_by_uid.get(uid, []))
        if not fee_rows:
            raise BuildError(f"program UID {uid} has no fee row")
        add_ons = []
        if uid in ADD_ON_FEE_ROWS:
            source_name, add_on_name = ADD_ON_FEE_ROWS[uid]
            row = next((r for r in fee_rows if r["Program."] == source_name), None)
            if row is None:
                raise BuildError(f"add-on fee row {source_name!r} not found under UID {uid}")
            fee_rows.remove(row)
            years, block = fee_block(row, f"UID {uid} add-on")
            add_ons.append({"name": add_on_name, "duration_years": years, "fee": block["total"]})

        if uid in SPLIT_PROGRAMS:
            names = SPLIT_PROGRAMS[uid]
            by_name = {r["Program."]: r for r in fee_rows}
            if len(prog_rows) != 1 or sorted(by_name) != sorted(names):
                raise BuildError(f"UID {uid} no longer matches its SPLIT_PROGRAMS entry")
            pairs = [(prog_rows[0], name, by_name[name]) for name in names]
        elif len(fee_rows) == 1:
            if (len(prog_rows) > 1) != (uid in SHARED_FEE_UIDS):
                raise BuildError(f"UID {uid}: {len(prog_rows)} program rows, SHARED_FEE_UIDS disagrees")
            pairs = [(row, row["Program"], fee_rows[0]) for row in prog_rows]
        else:
            raise BuildError(f"UID {uid}: {len(prog_rows)} program rows and {len(fee_rows)} fee rows")

        for prog, name, fee_row in pairs:
            fee_years, fee = fee_block(fee_row, f"UID {uid} {name}")
            duration_text = prog["Program Duration (Years)"]
            if duration_text.isdigit():
                duration_years, duration_note = int(duration_text), None
                if duration_years != fee_years:
                    raise BuildError(f"{name}: duration {duration_years} vs fee-file duration {fee_years}")
            else:
                duration_years, duration_note = None, duration_text
            if prog["Level"] not in LEVEL_MAP:
                raise BuildError(f"{name}: unknown level {prog['Level']!r}")
            aliases = []
            fee_name = fee_row["Program."]
            if uid not in SHARED_FEE_UIDS and fee_name.lower() != name.lower():
                aliases.append(fee_name)
            record = {
                "id": slug(name),
                "uid": uid,
                "name": name,
                "aliases": aliases,
                "degree": degree_of(name),
                "level": LEVEL_MAP[prog["Level"]],
                "school": school_of(prog["School"]),
                "department": prog["Department"],
                "duration_years": duration_years,
                "duration_note": duration_note,
                "seats": int(prog["Intake"]),
                "eligibility": eligibility_text(prog["Eligibility Criteria"]),
                "fees": fee,
                "add_ons": add_ons,
            }
            if uid in SPLIT_PROGRAMS:
                others = [n for n in SPLIT_PROGRAMS[uid] if n != name]
                record["seats_note"] = "intake shared with " + ", ".join(others)
            records.append(record)

    ids = collections.Counter(r["id"] for r in records)
    clashes = sorted(i for i, n in ids.items() if n > 1)
    if clashes:
        raise BuildError(f"duplicate record ids: {clashes}")
    for record_id, extra in hand_aliases.items():
        if record_id not in ids:
            raise BuildError(f"aliases.json names unknown record id {record_id!r}")
    for record in records:
        for alias in hand_aliases.get(record["id"], []):
            if alias not in record["aliases"]:
                record["aliases"].append(alias)

    return {
        "session": SESSION,
        "source": [os.path.basename(PROGRAM_XLSX), os.path.basename(FEE_XLSX)],
        "excluded": sorted(excluded),
        "records": records,
    }


def dumps(catalog):
    return json.dumps(catalog, indent=1, ensure_ascii=False) + "\n"


def main():
    catalog = build()
    with open(COURSES_JSON, "w", encoding="utf-8", newline="\n") as f:
        f.write(dumps(catalog))
    records = catalog["records"]
    print(f"wrote {COURSES_JSON}: {len(records)} records")
    for code, count in sorted(collections.Counter(r["school"]["code"] for r in records).items()):
        print(f"  {code}: {count}")
    print("excluded (incomplete data):", ", ".join(catalog["excluded"]))
    print("records with aliases:", sum(1 for r in records if r["aliases"]))


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    main()
```

- [ ] **Step 5: Generate courses.json**

Run: `PYTHONIOENCODING=utf-8 venv312/Scripts/python.exe catalog/build_catalog.py`
Expected output: per-school counts `SoB 6, SoBAS 16, SoE 3, SoET 20, SoHMS 12, SoLACS 14, SoLB 8, SoLJ 3, SoMC 4, SoSA 1` (87 records), the two exclusions (`M. Tech (VLSI & Embedded Systems)`, `Post-Graduate Diploma in Nuclear Medicine Technology`), and the alias count. `catalog/courses.json` is written.

If the build raises `BuildError`, stop and report the message: it means the Excel differs from what the join rules were validated against. Do not loosen a check to make it pass.

- [ ] **Step 6: Run test to verify it passes**

Run: `venv312/Scripts/python.exe -m unittest tests.test_catalog_build -v`
Expected: 18 tests OK.

- [ ] **Step 7: Commit**

Do not commit (project rule: the operator commits when asked). Leave the files staged-ready: `catalog/__init__.py`, `catalog/build_catalog.py`, `catalog/aliases.json`, `catalog/courses.json`, `tests/test_catalog_build.py`.

---

### Task 2: Runtime module and offline simulator

**Files:**
- Create: `course_catalog.py` (repo root)
- Create: `tests/tool_playground.py`
- Test: `tests/test_course_catalog.py`

**Interfaces:**
- Consumes: `catalog/courses.json` and `catalog/aliases.json` from Task 1 (formats above).
- Produces (Task 3 relies on exactly these):
  - `course_catalog.TOOL_SPECS: list[dict]` — two plain dicts, `name` `list_programs` then `get_program_details`, each valid as `types.FunctionDeclaration(**spec)`.
  - `course_catalog.TOOL_NAMES: frozenset[str]` — `{"list_programs", "get_program_details"}`.
  - `course_catalog.handle_tool_call(name: str, args: dict | None) -> dict` — never raises on bad arguments; returns `{"error": "..."}` instead. Unknown name -> `{"error": "unknown tool <name>"}`.
  - `course_catalog.response_chars(response) -> int` — compact JSON length (`separators=(",", ":")`, `ensure_ascii=False`).
  - `course_catalog.result_status(name: str, response: dict) -> str` — `"error"`, `"list"`, or comma-joined per-program statuses (`"found,ambiguous"`).
  - Also used by tests: `SCHOOLS` (sorted `(code, name)` pairs), `DEGREES` (sorted list), `SCHOOL_CODES`, `LEVELS`, `FIELDS`, `resolve(query) -> (status, record|None, candidates, more)`, `normalize`, `inr`.
  - `tests/tool_playground.parse_args(tool, items) -> dict` (raises `ValueError` on a malformed pair).

- [ ] **Step 1: Write the failing test**

Create `tests/test_course_catalog.py`:

```python
"""course_catalog: name resolution, tool responses, errors and size budgets (spec section 6).

Runs on the committed catalog/*.json only, so it also runs on EC2.
"""
import collections
import json
import unittest

import course_catalog as cc
from tests import tool_playground

CSE = "B.Tech (Computer Science and Engineering)"


def details(*programs, fields=None):
    args = {"programs": list(programs)}
    if fields is not None:
        args["fields"] = fields
    return cc.handle_tool_call("get_program_details", args)


def one(query, fields=None):
    return details(query, fields=fields)["results"][0]


class TestHelpers(unittest.TestCase):
    def test_normalize_joins_split_degree_tokens(self):
        self.assertEqual(cc.normalize("B. Tech (CSE)"), "btech cse")
        self.assertEqual(cc.normalize("M. Sc."), "msc")
        self.assertEqual(cc.normalize("B.B.A. LL.B (Hons)"), "bba llb hons")
        self.assertEqual(cc.normalize("AI & ML"), "ai and ml")

    def test_inr_uses_indian_grouping(self):
        for amount, text in ((0, "0"), (999, "999"), (43800, "43,800"), (108250, "1,08,250"),
                             (891800, "8,91,800"), (12345678, "1,23,45,678")):
            self.assertEqual(cc.inr(amount), text)

    def test_school_counts(self):
        self.assertEqual(collections.Counter(r["school"]["code"] for r in cc.RECORDS), {
            "SoBAS": 16, "SoB": 6, "SoE": 3, "SoET": 20, "SoHMS": 12,
            "SoLJ": 3, "SoLACS": 14, "SoLB": 8, "SoMC": 4, "SoSA": 1,
        })

    def test_alias_keys_do_not_collide_with_another_record(self):
        owners = collections.defaultdict(set)
        for entry in cc.INDEX:
            for k in entry["alias_keys"] | {entry["name_key"]}:
                owners[k].add(entry["record"]["id"])
        shared = {k: ids for k, ids in owners.items() if len(ids) > 1}
        # Deliberately shared: "law" names the three SoLJ programs.
        self.assertEqual(shared, {"law": {"ba-llb-hons", "bba-llb-hons", "llm"}})

    def test_every_expansion_matches_some_record(self):
        for short, words in cc.EXPANSIONS.items():
            with self.subTest(short):
                self.assertTrue(any(words <= e["match_tokens"] | e["dept_tokens"] for e in cc.INDEX))


class TestResolution(unittest.TestCase):
    def assertFound(self, query, name):
        status, record, _, _ = cc.resolve(query)
        self.assertEqual((status, record and record["name"]), ("found", name), query)

    def test_aliases_and_short_forms(self):
        self.assertFound("cse", CSE)
        self.assertFound("CSE", CSE)
        self.assertFound("computer science engineering", CSE)
        self.assertFound("b tech cse", CSE)
        self.assertFound("btech ai ml", "B.Tech CSE (Artificial Intelligence & Machine Learning)")
        self.assertFound("b.tech ai & ml", "B.Tech CSE (Artificial Intelligence & Machine Learning)")
        self.assertFound("ece", "B.Tech (Electronics and Communication Engineering)")
        self.assertFound("mechanical engg", "B.Tech (Mechanical Engineering)")
        self.assertFound("mlt", "Bachelor Of Medical Laboratory Science (BMLS)")

    def test_canonical_names_ids_and_fee_file_names(self):
        for record in cc.RECORDS:
            with self.subTest(record["id"]):
                self.assertFound(record["name"], record["name"])
                self.assertFound(record["id"], record["name"])
                for alias in record["aliases"]:
                    if alias != "law":
                        self.assertFound(alias, record["name"])

    def test_degree_word_narrows_the_pool(self):
        self.assertFound("MBA", "MBA")
        self.assertFound("bsc psychology", "B.Sc (Psychology)")
        self.assertFound("ma english", "M.A (English Language and Literature)")
        self.assertFound("m tech data science", "M.Tech (Data Science and Digital Transformation)")
        self.assertFound("btech biotech", "B.Tech (Biotechnology)")

    def test_single_clear_subject(self):
        self.assertFound("civil", "B.Tech (Civil Engineering)")
        self.assertFound("optometry", "Bachelor of Optometry")
        self.assertFound("medical physics", "Post-M.Sc Diploma in Medical Physics")

    def test_ambiguous(self):
        status, _, candidates, more = cc.resolve("data science")
        self.assertEqual(status, "ambiguous")
        self.assertEqual(candidates, [
            "B.Tech CSE (Data Science)",
            "B.Sc (Applied Statistics and Data Science)",
            "M.Sc. (Applied Statistics and Data Science)",
            "M.Tech (Data Science and Digital Transformation)",
        ])
        self.assertEqual(more, 2)
        self.assertEqual(cc.resolve("law")[2], ["LL.M", "B.A. LL.B (Hons)", "B.B.A. LL.B (Hons)"])
        self.assertEqual(cc.resolve("llb")[2], ["B.A. LL.B (Hons)", "B.B.A. LL.B (Hons)"])
        self.assertEqual(cc.resolve("psychology")[0], "ambiguous")
        self.assertEqual(len(cc.resolve("psychology")[2]), 4)

    def test_not_found(self):
        # "computer wala": untranslated caller words must not land on a wrong program.
        for query in ("mbbs", "nursing", "information technology", "", "   ", "course", "it",
                      "computer wala"):
            self.assertEqual(cc.resolve(query)[0], "not_found", repr(query))

    def test_constants_are_pinned(self):
        self.assertEqual((cc.FOUND_SCORE, cc.CANDIDATE_SCORE, cc.SCORE_MARGIN, cc.DEPARTMENT_WEIGHT),
                         (0.75, 0.6, 0.2, 0.5))
        self.assertEqual((cc.MAX_PROGRAMS, cc.LIST_NAMES_MAX, cc.MAX_CANDIDATES), (3, 20, 4))


class TestListPrograms(unittest.TestCase):
    def call(self, **args):
        return cc.handle_tool_call("list_programs", args)

    def test_no_arguments_groups_by_school(self):
        response = self.call()
        self.assertEqual(list(response), ["by_school"])
        self.assertEqual(len(response["by_school"]), 10)
        self.assertIn({"school": "SoET - School of Engineering and Technology", "count": 20},
                      response["by_school"])
        self.assertEqual(sum(s["count"] for s in response["by_school"]), 87)
        self.assertEqual(cc.handle_tool_call("list_programs", None), response)

    def test_small_result_lists_names(self):
        response = self.call(degree="B.Tech")
        self.assertEqual(response["count"], 18)
        self.assertEqual(len(response["programs"]), 18)
        self.assertEqual(response["programs"][-2:], ["B.Tech (Biotechnology)", "B.Tech (Food Technology)"])
        self.assertEqual(self.call(degree="btech"), response)
        self.assertEqual(self.call(school="soet")["count"], 20)

    def test_large_result_groups_by_degree(self):
        response = self.call(level="UG")
        self.assertEqual(response["count"], 53)
        self.assertNotIn("programs", response)
        self.assertEqual(response["by_degree"][:2], [{"degree": "B.Tech", "count": 18},
                                                     {"degree": "B.Sc", "count": 15}])

    def test_filters_combine(self):
        self.assertEqual(self.call(level="PG", degree="B.Tech"), {"count": 0})
        self.assertEqual(self.call(max_total_fee=300000.0, level="UG"), {
            "count": 3, "programs": ["B.A (Education)", "B.Ed", "B. A (Bengali Language and Literature)"]})
        response = self.call(keyword="data science")
        self.assertEqual(response["count"], 6)

    def test_max_total_fee_is_inclusive(self):
        cse_total = next(r for r in cc.RECORDS if r["name"] == CSE)["fees"]["total"]
        names = self.call(degree="B.Tech", max_total_fee=cse_total)["programs"]
        self.assertIn(CSE, names)
        self.assertNotIn(CSE, self.call(degree="B.Tech", max_total_fee=cse_total - 1)["programs"])

    def test_errors(self):
        for args, fragment in (({"degree": "MBBS"}, "unknown degree"),
                               ({"level": "PhD"}, "unknown level"),
                               ({"school": "SoX"}, "unknown school"),
                               ({"foo": 1}, "unknown argument 'foo'"),
                               ({"max_total_fee": True}, "max_total_fee"),
                               ({"max_total_fee": "5 lakh"}, "max_total_fee"),
                               ({"keyword": "course"}, "keyword")):
            with self.subTest(args):
                self.assertIn(fragment, cc.handle_tool_call("list_programs", args)["error"])
        self.assertEqual(cc.handle_tool_call("list_programs", {"level": None}), self.call())


class TestProgramDetails(unittest.TestCase):
    def test_default_fields(self):
        entry = one("cse")
        self.assertEqual(list(entry), ["query", "status", "name", "school", "department", "level",
                                       "duration", "eligibility", "fees"])
        self.assertEqual(entry["school"], "SoET - School of Engineering and Technology")
        self.assertEqual(entry["duration"], "4 years")
        self.assertEqual(entry["fees"], {
            "session": "2027",
            "first_semester": "1,08,250",
            "admission_fee": "43,800",
            "admission_fee_note": "one-time, paid with the first semester fee; includes T-shirt and blazer",
            "due_at_admission": "1,52,050",
            "total": "8,91,800",
        })

    def test_only_requested_fields(self):
        self.assertEqual(list(one("cse", ["eligibility"])), ["query", "status", "name", "eligibility"])
        self.assertEqual(one("cse", ["seats"])["seats"], 300)
        breakdown = one("cse", ["fee_breakdown"])["fee_breakdown"]
        self.assertEqual(breakdown["per_year"], ["2,16,500", "2,11,500", "2,10,000", "2,10,000"])
        self.assertEqual(len(breakdown["per_semester"]), 8)
        self.assertEqual(one("cse", [])["fees"]["total"], "8,91,800")  # empty list means default

    def test_add_ons_only_with_fees(self):
        sap = one("sap", ["fees"])
        self.assertEqual(sap["fees"]["add_ons"],
                         [{"name": "SAP add-on certification", "duration": "1 year", "fee": "65,000"}])
        self.assertNotIn("add_ons", one("cse", ["fees"])["fees"])
        self.assertNotIn("add_ons", json.dumps(one("sap", ["overview", "seats"])))

    def test_seats_note_and_duration_note(self):
        entry = one("m pharm pharmacology", ["seats"])
        self.assertEqual(entry["seats_note"], "intake shared with M.Pharm (Pharmaceutics)")
        self.assertTrue(one("medical physics", ["overview"])["duration"].startswith("1st Year"))

    def test_mixed_statuses_and_string_programs(self):
        response = details("cse", "data science", "mbbs", fields=["fees"])
        self.assertEqual([r["status"] for r in response["results"]], ["found", "ambiguous", "not_found"])
        self.assertEqual(response["results"][2], {"query": "mbbs", "status": "not_found"})
        self.assertNotIn("more", one("law"))
        self.assertEqual(cc.handle_tool_call("get_program_details", {"programs": "cse"}),
                         details("cse"))
        # The SDK may hand over arrays as its own sequence types, not lists.
        self.assertEqual(cc.handle_tool_call("get_program_details",
                                             {"programs": ("cse",), "fields": ("fees",)}),
                         details("cse", fields=["fees"]))
        self.assertEqual(cc.result_status("get_program_details", response), "found,ambiguous,not_found")

    def test_errors(self):
        for args, fragment in (({}, "programs is required"),
                               ({"programs": []}, "1 to 3"),
                               ({"programs": ["a", "b", "c", "d"]}, "1 to 3"),
                               ({"programs": [""]}, "non-empty"),
                               ({"programs": [5]}, "non-empty"),
                               ({"programs": ["cse"], "fields": ["price"]}, "unknown field 'price'"),
                               ({"programs": ["cse"], "extra": 1}, "unknown argument 'extra'")):
            with self.subTest(args):
                response = cc.handle_tool_call("get_program_details", args)
                self.assertIn(fragment, response["error"])
                self.assertEqual(cc.result_status("get_program_details", response), "error")
        self.assertEqual(cc.handle_tool_call("bookSeat", {}), {"error": "unknown tool bookSeat"})


class TestSizeBudgets(unittest.TestCase):
    """Spec 6.5, in characters of compact JSON (about 4 characters per token)."""

    def test_every_list_response_fits(self):
        calls = [{}] + [{"degree": d} for d in cc.DEGREES] + [{"school": s} for s in cc.SCHOOL_CODES] \
            + [{"level": lv} for lv in cc.LEVELS]
        for args in calls:
            with self.subTest(args):
                self.assertLessEqual(cc.response_chars(cc.handle_tool_call("list_programs", args)), 1000)

    def test_one_program_default_fields(self):
        self.assertLessEqual(cc.response_chars(details("cse")), 800)
        for record in cc.RECORDS:
            with self.subTest(record["id"]):
                self.assertLessEqual(cc.response_chars(details(record["name"])), 1400)

    def test_three_programs_fees_only(self):
        self.assertLessEqual(cc.response_chars(details("cse", "ece", "civil", fields=["fees"])), 1000)

    def test_declarations(self):
        self.assertLessEqual(cc.response_chars(cc.TOOL_SPECS), 1800)


class TestToolSpecs(unittest.TestCase):
    def test_names_and_enums_come_from_the_data(self):
        self.assertEqual(cc.TOOL_NAMES, {"list_programs", "get_program_details"})
        self.assertEqual([s["name"] for s in cc.TOOL_SPECS], ["list_programs", "get_program_details"])
        props = cc.TOOL_SPECS[0]["parameters"]["properties"]
        self.assertEqual(props["degree"]["enum"], cc.DEGREES)
        self.assertEqual(len(props["degree"]["enum"]), 22)
        self.assertEqual(props["school"]["enum"], sorted(cc.SCHOOL_CODES))
        self.assertEqual(props["level"]["enum"], ["UG", "PG", "Diploma"])
        details_spec = cc.TOOL_SPECS[1]["parameters"]
        self.assertEqual(details_spec["required"], ["programs"])
        self.assertEqual(details_spec["properties"]["fields"]["items"]["enum"], cc.FIELDS)

    def test_specs_build_sdk_declarations(self):
        from google.genai import types
        for spec in cc.TOOL_SPECS:
            declaration = types.FunctionDeclaration(**spec)
            self.assertEqual(declaration.name, spec["name"])


class TestPlayground(unittest.TestCase):
    def test_key_value_pairs(self):
        self.assertEqual(tool_playground.parse_args("get_program_details", ["programs=cse,ece", "fields=fees"]),
                         {"programs": ["cse", "ece"], "fields": ["fees"]})
        self.assertEqual(tool_playground.parse_args("list_programs", ["max_total_fee=300000", "degree=B.Tech"]),
                         {"max_total_fee": 300000, "degree": "B.Tech"})

    def test_raw_json(self):
        self.assertEqual(tool_playground.parse_args("get_program_details", ['{"programs": ["ai ml"]}']),
                         {"programs": ["ai ml"]})

    def test_malformed_pair(self):
        with self.assertRaises(ValueError):
            tool_playground.parse_args("list_programs", ["degree"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv312/Scripts/python.exe -m unittest tests.test_course_catalog -v`
Expected: ERROR, `ModuleNotFoundError: No module named 'course_catalog'`.

- [ ] **Step 3: Write the runtime module**

Create `course_catalog.py` in the repo root:

```python
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
    if best >= FOUND_SCORE and best - second >= SCORE_MARGIN:
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
```

- [ ] **Step 4: Run test to verify the playground import is the only failure left**

Run: `venv312/Scripts/python.exe -m unittest tests.test_course_catalog -v`
Expected: ERROR, `ImportError: cannot import name 'tool_playground' from 'tests'`.

- [ ] **Step 5: Write the simulator**

Create `tests/tool_playground.py`:

```python
"""Offline simulator for the course catalog tools (spec section 8).

Calls the same course_catalog.handle_tool_call as app.py, so what it prints is
exactly what Gemini receives. Not named test_*, so unittest discovery skips it.

    venv312/Scripts/python.exe tests/tool_playground.py                         # interactive
    venv312/Scripts/python.exe tests/tool_playground.py list_programs degree=B.Tech
    venv312/Scripts/python.exe tests/tool_playground.py get_program_details programs=cse,ece fields=fees
    venv312/Scripts/python.exe tests/tool_playground.py get_program_details "{\"programs\": [\"ai ml\"]}"
    venv312/Scripts/python.exe tests/tool_playground.py --declarations
"""
import json
import os
import shlex
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import course_catalog  # noqa: E402

SPECS = {spec["name"]: spec for spec in course_catalog.TOOL_SPECS}


def parse_args(tool, items):
    """key=value pairs (commas split array args) or a single raw JSON object."""
    if len(items) == 1 and items[0].lstrip().startswith("{"):
        return json.loads(items[0])
    properties = SPECS[tool]["parameters"]["properties"] if tool in SPECS else {}
    args = {}
    for item in items:
        name, sep, value = item.partition("=")
        if not sep:
            raise ValueError(f"expected key=value, got {item!r}")
        kind = properties.get(name, {}).get("type")
        if kind == "ARRAY":
            args[name] = [v.strip() for v in value.split(",") if v.strip()]
        elif kind in ("INTEGER", "NUMBER"):
            try:
                args[name] = int(value)
            except ValueError:
                args[name] = value
        else:
            args[name] = value
    return args


def show(tool, args):
    response = course_catalog.handle_tool_call(tool, args)
    print(json.dumps(response, indent=2, ensure_ascii=False))
    chars = course_catalog.response_chars(response)
    print(f"-- {chars} chars, about {chars // 4} tokens")


def show_declarations():
    print(json.dumps(course_catalog.TOOL_SPECS, indent=2, ensure_ascii=False))
    chars = course_catalog.response_chars(course_catalog.TOOL_SPECS)
    print(f"-- {chars} chars, about {chars // 4} tokens")


def interactive():
    names = sorted(SPECS)
    print("Tools: " + ", ".join(f"{i + 1}) {n}" for i, n in enumerate(names)) + ". Empty line exits.")
    while True:
        try:
            choice = input("tool> ").strip()
            if not choice:
                return
            tool = names[int(choice) - 1] if choice.isdigit() and 0 < int(choice) <= len(names) else choice
            line = input("args> ").strip()
        except EOFError:
            return
        try:
            args = parse_args(tool, [line] if line.startswith("{") else shlex.split(line))
        except ValueError as e:
            print(f"!! {e}")
            continue
        show(tool, args)


def main(argv):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if not argv:
        interactive()
    elif argv[0] == "--declarations":
        show_declarations()
    else:
        show(argv[0], parse_args(argv[0], argv[1:]))


if __name__ == "__main__":
    main(sys.argv[1:])
```

- [ ] **Step 6: Run test to verify it passes**

Run: `venv312/Scripts/python.exe -m unittest tests.test_course_catalog -v`
Expected: 33 tests OK.

- [ ] **Step 7: Smoke the simulator by hand**

Run: `venv312/Scripts/python.exe tests/tool_playground.py get_program_details programs=cse fields=fees`
Expected: the CSE fees block (`"due_at_admission": "1,52,050"`, `"total": "8,91,800"`), then `-- N chars, about N/4 tokens`.

Run: `venv312/Scripts/python.exe tests/tool_playground.py --declarations`
Expected: the two specs, then a size line under 1800 chars.

- [ ] **Step 8: Commit**

Do not commit. Files ready: `course_catalog.py`, `tests/tool_playground.py`, `tests/test_course_catalog.py`.

---

### Task 3: Wire the tools into app.py

**Files:**
- Modify: `app.py` (import at line 20; `LOCAL_GEMINI_TOOLS` at line 402; `log_call_stats` after line 1628; call_state literal after line 1868; tool branch lines 2742-2767)
- Modify: `tests/harness/appctl.py` (call_state mirror after line 180; `DIAGNOSTIC_LOG_TAGS` after line 265)
- Modify: `tests/test_preservation_4_4_tool_and_silence.py` (line 115 and the module docstring)
- Modify: `tests/test_preservation_4_8_resumption_recording_stats.py:315-318`
- Test: `tests/test_catalog_tool_handler.py`

**Interfaces:**
- Consumes: `course_catalog.TOOL_SPECS`, `TOOL_NAMES`, `handle_tool_call`, `response_chars`, `result_status` (Task 2).
- Produces: call_state keys `catalog_calls: int`, `catalog_chars: int`; log lines `🔎 [CATALOG] tool=<name> status=<status> chars=<n> ms=<t>` per call and `🔎 [CATALOG] calls=<n> chars=<n>` in the call stats; `app.LOCAL_GEMINI_TOOLS[0]["function_declarations"]` names `[endCall, transferCall, list_programs, get_program_details]`.

Background for the implementer: the harness (`tests/harness/fakes.py`) drives `stream_gemini_to_plivo` with a `FakeSession`; `resp_tool_call(name, args, call_id)` builds one Gemini response holding one function call; `session.sent` records `("tool_response", {"function_responses": [...]})`; `FakeClock` freezes `time.perf_counter`, so the `ms=` field logs `0.0`. `LogCapture.existing_lines` drops lines carrying a `DIAGNOSTIC_LOG_TAGS` tag, which is why `[CATALOG]` must be added there: the golden log records in the 4_x tests stay unchanged.

Today, a tool name that matches neither `endCall` nor `transferCall` falls through to a commented-out MCP block, so **no tool response is sent and Gemini waits forever**. This task removes that dead block and answers every call.

- [ ] **Step 1: Write the failing test**

Create `tests/test_catalog_tool_handler.py`:

```python
"""Course catalog tools wired into app.py's tool-call branch (spec section 7).

A catalog call is answered in the same turn and leaves the call running: no
terminal flags, no VAD reset. An unknown tool name now gets an error response
instead of no response at all.
"""

from __future__ import annotations

import asyncio
import unittest

import course_catalog
from tests.harness.appctl import LogCapture, app, live_call_state
from tests.harness.fakes import (
    FakeClock,
    FakePlivoClient,
    FakePlivoWS,
    FakeSession,
    resp_tool_call,
    run_gemini_output,
)


def run_tool_calls(*calls, **state):
    async def scenario():
        clock = FakeClock()
        ws = FakePlivoWS(clock)
        client = FakePlivoClient()
        call_state = live_call_state(with_denoiser=False, **state)
        session = FakeSession([resp_tool_call(name, args, call_id=f"call-{i}")
                               for i, (name, args) in enumerate(calls, 1)])
        with clock.install(), LogCapture() as log:
            await run_gemini_output(session, ws, call_state, client)
        return call_state, session, log

    return asyncio.run(scenario())


def responses(session):
    return [fr for kind, payload in session.sent if kind == "tool_response"
            for fr in payload["function_responses"]]


class TestCatalogDeclarations(unittest.TestCase):
    def test_catalog_tools_are_declared_after_the_call_control_tools(self):
        names = [d.name for d in app.LOCAL_GEMINI_TOOLS[0]["function_declarations"]]
        self.assertEqual(names, ["endCall", "transferCall", "list_programs", "get_program_details"])


class TestCatalogToolCall(unittest.TestCase):
    ARGS = {"programs": ["cse"], "fields": ["fees"]}

    def setUp(self):
        self.call_state, self.session, self.log = run_tool_calls(("get_program_details", self.ARGS))

    def test_response_is_the_catalog_result(self):
        self.assertEqual(self.session.sent_kinds, ["tool_response"])
        [fr] = responses(self.session)
        self.assertEqual((fr.id, fr.name), ("call-1", "get_program_details"))
        self.assertEqual(fr.response, course_catalog.handle_tool_call("get_program_details", self.ARGS))

    def test_counters_and_log_line(self):
        expected = course_catalog.handle_tool_call("get_program_details", self.ARGS)
        chars = course_catalog.response_chars(expected)
        self.assertEqual(self.call_state["catalog_calls"], 1)
        self.assertEqual(self.call_state["catalog_chars"], chars)
        self.assertEqual(self.log.matching("[CATALOG]"),
                         [f"🔎 [CATALOG] tool=get_program_details status=found chars={chars} ms=0.0"])

    def test_call_keeps_running(self):
        for flag in ("pending_end_call", "pending_transfer_call", "closing_audio_phase",
                     "terminate_session", "tool_call_in_progress"):
            self.assertFalse(self.call_state[flag], flag)
        self.assertFalse(any("[CATALOG]" in line for line in self.log.existing_lines))

    def test_bad_arguments_are_answered_with_the_error(self):
        call_state, session, log = run_tool_calls(("list_programs", {"degree": "MBBS"}))
        [fr] = responses(session)
        self.assertIn("unknown degree 'MBBS'", fr.response["error"])
        self.assertEqual(call_state["catalog_calls"], 1)
        self.assertIn("status=error", log.matching("[CATALOG]")[0])

    def test_each_call_is_answered_and_counted(self):
        call_state, session, _ = run_tool_calls(("list_programs", {}),
                                                ("get_program_details", {"programs": ["mba"]}))
        self.assertEqual(session.sent_kinds, ["tool_response", "tool_response"])
        self.assertEqual([fr.id for fr in responses(session)], ["call-1", "call-2"])
        self.assertEqual(call_state["catalog_calls"], 2)

    def test_speech_during_lookup_opens_the_deferred_activity(self):
        call_state, session, log = run_tool_calls(("list_programs", {"degree": "MBA"}),
                                                  is_speaking=True)
        self.assertEqual(session.sent_kinds[:2], ["tool_response", "activityStart"])
        self.assertTrue(call_state["user_activity_open"])
        self.assertTrue(log.contains("deferred activityStart"))


class TestUnknownTool(unittest.TestCase):
    def test_unknown_tool_gets_an_error_response(self):
        call_state, session, log = run_tool_calls(("bookSeat", {"x": 1}))
        [fr] = responses(session)
        self.assertEqual(fr.response, {"error": "unknown tool bookSeat"})
        self.assertEqual(call_state["catalog_calls"], 0)
        self.assertEqual(log.matching("Unknown tool"), ["[⚠️ Unknown tool requested by Gemini: bookSeat]"])


class TestCatalogStatsLine(unittest.TestCase):
    def test_stats_line_reports_the_totals(self):
        call_state = live_call_state(with_denoiser=False, with_aec=False,
                                     catalog_calls=2, catalog_chars=1500)
        with LogCapture() as log:
            app.log_call_stats(call_state)
        self.assertEqual(log.matching("[CATALOG]"), ["🔎 [CATALOG] calls=2 chars=1500"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONIOENCODING=utf-8 venv312/Scripts/python.exe -m unittest tests.test_catalog_tool_handler -v`
Expected: FAIL/ERROR in every class: the declarations list is `['endCall', 'transferCall']`, a catalog or unknown call sends no tool_response (so `[fr] = responses(...)` raises `ValueError`), and there is no `[CATALOG]` stats line.

- [ ] **Step 3: Import the module**

In `app.py`, replace:

```python
import bargein
from pyrnnoise import RNNoise
```

with:

```python
import bargein
import course_catalog
from pyrnnoise import RNNoise
```

- [ ] **Step 4: Declare the tools**

In `app.py`, replace (line 402):

```python
LOCAL_GEMINI_TOOLS = [{"function_declarations": [end_call_tool, transfer_call_tool]}]
```

with:

```python
# Course catalog lookups (course_catalog.py, docs/spec/course-catalog-tools-design.md).
catalog_tools = [types.FunctionDeclaration(**spec) for spec in course_catalog.TOOL_SPECS]

LOCAL_GEMINI_TOOLS = [{"function_declarations": [end_call_tool, transfer_call_tool, *catalog_tools]}]
```

- [ ] **Step 5: Add the call_state counters**

In `app.py`'s call_state literal (around line 1867), replace:

```python
        "aec_guard_frames_checked": 0,
        "aec_guard_fallback_frames": 0,
```

with:

```python
        "aec_guard_frames_checked": 0,
        "aec_guard_fallback_frames": 0,

        # Course catalog tool calls this call, and the response characters sent.
        "catalog_calls": 0,
        "catalog_chars": 0,
```

In `tests/harness/appctl.py` (around line 179), replace:

```python
        "aec_guard_frames_checked": 0,
        "aec_guard_fallback_frames": 0,
```

with:

```python
        "aec_guard_frames_checked": 0,
        "aec_guard_fallback_frames": 0,

        # --- course catalog tools: calls and response characters this call.
        "catalog_calls": 0,
        "catalog_chars": 0,
```

- [ ] **Step 6: Tag the log lines as diagnostic**

In `tests/harness/appctl.py` `DIAGNOSTIC_LOG_TAGS`, replace:

```python
    "[AEC]",          # path B trial: which canceller ran (AEC_IMPL) and aec1 stats
```

with:

```python
    "[AEC]",          # path B trial: which canceller ran (AEC_IMPL) and aec1 stats
    "[CATALOG]",      # course catalog tool calls and the per-call totals
```

- [ ] **Step 7: Add the call-stats line**

In `app.py` `log_call_stats`, replace (line 1628):

```python
    logger.info(f"🎛️ [AEC] impl={AEC_IMPL}{aec_detail}")
```

with:

```python
    logger.info(f"🎛️ [AEC] impl={AEC_IMPL}{aec_detail}")
    logger.info(f"🔎 [CATALOG] calls={call_state.get('catalog_calls', 0)} chars={call_state.get('catalog_chars', 0)}")
```

- [ ] **Step 8: Add the catalog and unknown-tool branches**

In `app.py`'s tool-call loop, the `if call.name == "endCall": ... elif call.name == "transferCall": ...` chain is followed by a commented-out MCP `# else:` block and then `except Exception as e:`. Replace that commented block (keep the `except` line) — old:

```python
                                # else:
                                #     # Execute against Adamas Tech server
                                #     mcp_result = await mcp_session.call_tool(
                                #         call.name, args_dict
                                #     )
                                #     result_text = "\n".join(
                                #         [
                                #             c.text
                                #             for c in mcp_result.content
                                #             if c.type == "text"
                                #         ]
                                #     )
                                #     logger.info("[✅ Tool executed successfully. Returning data to Gemini...]")
                                #     logger.info(f"Raw Data from MCP: {result_text}")

                                #     function_responses_to_send.append(
                                #         types.FunctionResponse(
                                #             id=call.id,
                                #             name=call.name,
                                #             response={
                                #                 "result": result_text,
                                #                 # schedule
                                #                 # "scheduling": "WHEN_IDLE"
                                #             },
                                #         )
                                #     )
                            except Exception as e:
```

new (the `elif` lines up with `elif call.name == "transferCall":`, 32 spaces):

```python
                                elif call.name in course_catalog.TOOL_NAMES:
                                    # Local lookup, no terminal state: the call carries on after the response.
                                    started = time.perf_counter()
                                    result = course_catalog.handle_tool_call(call.name, args_dict)
                                    elapsed_ms = (time.perf_counter() - started) * 1000
                                    chars = course_catalog.response_chars(result)
                                    call_state["catalog_calls"] += 1
                                    call_state["catalog_chars"] += chars
                                    logger.info(
                                        f"🔎 [CATALOG] tool={call.name} "
                                        f"status={course_catalog.result_status(call.name, result)} "
                                        f"chars={chars} ms={elapsed_ms:.1f}"
                                    )
                                    function_responses_to_send.append(
                                        types.FunctionResponse(id=call.id, name=call.name, response=result)
                                    )
                                else:
                                    # Always answer: an unanswered function call leaves Gemini waiting.
                                    logger.warning(f"[⚠️ Unknown tool requested by Gemini: {call.name}]")
                                    function_responses_to_send.append(
                                        types.FunctionResponse(
                                            id=call.id,
                                            name=call.name,
                                            response={"error": f"unknown tool {call.name}"},
                                        )
                                    )
                            except Exception as e:
```

- [ ] **Step 9: Run the new test to verify it passes**

Run: `PYTHONIOENCODING=utf-8 venv312/Scripts/python.exe -m unittest tests.test_catalog_tool_handler -v`
Expected: 9 tests OK.

- [ ] **Step 10: Run the preservation tests to see the two expected breaks**

Run: `PYTHONIOENCODING=utf-8 venv312/Scripts/python.exe -m unittest tests.test_preservation_4_4_tool_and_silence tests.test_preservation_4_8_resumption_recording_stats tests.test_harness_smoke`
Expected: exactly two failures:
- `test_ordering_is_activity_start_then_one_prepended_blob`: `['tool_response', 'activityStart', 'audio'] != ['activityStart', 'audio']` (the unknown tool is now answered).
- `test_model_identifier_is_recorded`: `[61, 1612, 2129, 2139, 2552] != [60, 1608, 2120, 2130, 2543]`.

`test_harness_smoke` must pass (it checks the call_state mirror). Any other failure is a real regression: stop and investigate.

- [ ] **Step 11: Re-baseline 4_4 (intentional)**

In `tests/test_preservation_4_4_tool_and_silence.py`, replace:

```python
    def test_ordering_is_activity_start_then_one_prepended_blob(self):
        self.assertEqual(self.session.sent_kinds, ["activityStart", "audio"])
```

with:

```python
    def test_ordering_is_activity_start_then_one_prepended_blob(self):
        # INTENTIONAL BASELINE UPDATE — course catalog tools: an unknown tool
        # name now gets an error tool_response (it used to get none, leaving
        # Gemini waiting). The deferred activityStart and the one prepended
        # blob still follow, in the same order.
        self.assertEqual(self.session.sent_kinds, ["tool_response", "activityStart", "audio"])
```

Then, in the same file's module docstring, replace:

```python
The only ways in are (a) a tool name matching neither branch -- the commented-out
MCP ``else`` at app.py 1560-1585 -- or (b) the inbound task setting
```

with:

```python
The only ways in are (a) a tool name matching neither branch -- now the
unknown-tool ``else``, which answers with an error tool_response first, and also
any course catalog tool, which leaves ``is_speaking`` alone -- or (b) the inbound
task setting
```

- [ ] **Step 12: Re-pin 4_8**

Run: `venv312/Scripts/python.exe -c "print([i + 1 for i, l in enumerate(open('app.py', encoding='utf-8').read().splitlines()) if 'GEMINI_MODEL' in l])"`
Expected: `[61, 1612, 2129, 2139, 2552]`. If it differs, use the printed list below instead (only the numbers change; still five sites).

In `tests/test_preservation_4_8_resumption_recording_stats.py`, replace:

```python
        # (no logic change) and made PRICE_*/GEMINI_MODEL/PORT env-overridable.
        self.assertEqual(references, [60, 1608, 2120, 2130, 2543])
```

with:

```python
        # (no logic change) and made PRICE_*/GEMINI_MODEL/PORT env-overridable.
        # And again for the course catalog tools (import course_catalog, the
        # catalog declarations, the [CATALOG] stats line, the call_state
        # counters and the catalog / unknown-tool branches).
        self.assertEqual(references, [61, 1612, 2129, 2139, 2552])
```

- [ ] **Step 13: Run the full suite**

Run: `PYTHONIOENCODING=utf-8 venv312/Scripts/python.exe -m unittest discover -s tests -t .`
Expected: `Ran 311 tests`, `OK (skipped=2, expected failures=1)` (251 existing + 18 + 33 + 9). Prompt tests come in Task 4.

- [ ] **Step 14: Commit**

Do not commit. Files ready: `app.py`, `tests/harness/appctl.py`, `tests/test_catalog_tool_handler.py`, `tests/test_preservation_4_4_tool_and_silence.py`, `tests/test_preservation_4_8_resumption_recording_stats.py`.

---

### Task 4: Rewrite prompt_au.txt for the catalog tools

**Files:**
- Modify (full rewrite): `prompt_au.txt`
- Test: `tests/test_prompt_catalog.py`

**Interfaces:**
- Consumes: `course_catalog.SCHOOLS`, `course_catalog.DEGREES` (Task 2). The tool names `list_programs`, `get_program_details`, the result statuses `found` / `ambiguous` / `not_found`, and the `due_at_admission` field (Task 2 responses).
- Produces: `prompt_au.txt` with exactly two placeholders, `{user_name}` and `{phone_number}`. `prompt.txt` is NOT touched.

- [ ] **Step 1: Write the failing test**

Create `tests/test_prompt_catalog.py`:

```python
"""prompt_au.txt carries the catalog map and tool rules, and no fee figures (spec section 10)."""

import os
import re
import unittest

import course_catalog

PROMPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "prompt_au.txt")


class TestPromptCatalog(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(PROMPT, encoding="utf-8") as f:
            cls.text = f.read()

    def test_formats_like_app_py_does(self):
        # app.py fills these two with str.format; any other brace would raise.
        filled = self.text.format(user_name="Asha", phone_number="9800000000")
        self.assertIn("Asha", filled)
        self.assertIn("9800000000", filled)

    def test_names_the_phase_1_tools_only(self):
        for name in ("list_programs", "get_program_details", "transferCall", "endCall"):
            self.assertIn(name, self.text)
        self.assertNotIn("find_eligible_programs", self.text)

    def test_school_map_matches_the_catalog(self):
        for code, name in course_catalog.SCHOOLS:
            self.assertIn(f"{code} - {name}", self.text)
        for degree in course_catalog.DEGREES:
            self.assertIn(degree, self.text)

    def test_no_fee_figures(self):
        self.assertIsNone(re.search(r"\d{1,2},\d{2},\d{3}|\d{2},\d{3}|\d{5,}", self.text))

    def test_open_eligibility_rule(self):
        self.assertIn('Do NOT say "you are eligible"', self.text)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv312/Scripts/python.exe -m unittest tests.test_prompt_catalog -v`
Expected: failures against the current `prompt_au.txt` (it hard-codes SoET fees, so `test_no_fee_figures` fails, and it names neither catalog tool).

- [ ] **Step 3: Rewrite the prompt**

Replace the whole of `prompt_au.txt` with:

```text
## ROLE & OBJECTIVE
- You act like **Neha**, a polite, professional, warm outbound call voice agent for the admissions team of **Adamas University, Kolkata** — the first NAAC A-accredited private university from West Bengal.
- You are making an outbound admission-assistance call to a prospective applicant named {user_name} on their phone number {phone_number}.
- Your job is to inform them about Adamas University programs for the **2027 session**, understand their interest, and qualify them as an admission lead.
- Do NOT hallucinate or invent any information. If you do not have a detail, say a senior admission counselor will follow up.
- If asked about your AI model, politely refuse to answer and say you are Neha from Adamas University.

## LANGUAGE & TONE
- Tone: Young female from Kolkata (IN) — polite, friendly, respectful, warm, crisp, and concise. Speak at a natural pace.
- Dynamic Language Matching: Seamlessly adapt to the user's language preference.
- Conversational Mix (CRITICAL): Never use pure, highly formal Bengali or Hindi.
  - If the user speaks Hindi, reply in natural "Hinglish" (approx 60% conversational Hindi + 40% English vocabulary like "course", "admission", "eligibility", "fees", "campus", "program").
  - If the user speaks Bengali, reply in natural "Benglish" (approx 60% conversational Bengali + 40% English vocabulary).
  - If the user speaks English, reply in clear, simple English.
- Always speak all numbers, percentages, and fees in English only.
- When reading a phone number, read each digit individually with a brief pause between digits.
- You explicitly let the user know early in the call that you are comfortable switching languages to suit their preference.

## YOUR GOAL IS TO
- Warmly inform the applicant about Adamas University programs, eligibility, and fees for the 2027 session.
- Understand who you are speaking with (the student, or a parent/guardian) and gather their basic academic profile and program interest.
- Answer their questions accurately, using the catalog tools for every program detail.
- Qualify the lead, then either close the call politely or hand off to a senior admission counselor if requested or if the query is beyond what you have.

## WHAT THE UNIVERSITY OFFERS (CATALOG MAP)
- University: Adamas University, Kolkata, West Bengal. The first NAAC A-accredited private university from West Bengal. Admissions for the 2027 session are open.
- Schools (code - name):
    - SoB - School of Business
    - SoBAS - School of Basic and Applied Sciences
    - SoE - School of Education
    - SoET - School of Engineering and Technology
    - SoHMS - School of Health and Medical Sciences
    - SoLACS - School of Liberal Arts and Culture Studies
    - SoLB - School of Life Science and Biotechnology
    - SoLJ - School of Law and Justice
    - SoMC - School of Media and Communication
    - SoSA - School of Smart Agriculture
- Levels: UG, PG, Diploma.
- Degree types: B.Tech, BCA, B.Sc, B.A, B.Com, BBA, B.Ed, BA LL.B, BBA LL.B, B.Pharm, BMLS, B.Optom, BFND, M.Tech, MCA, M.Sc, M.A, MBA, LL.M, M.Pharm, D.Pharm, Post-M.Sc Diploma.
- This map only tells you what exists. Every fee, eligibility, duration and seat figure comes from the tools.

## CATALOG TOOLS (CRITICAL)
- Never state a fee, eligibility, duration or seat count unless a tool returned it on this call.
- Pick the tool by the question:
    - Questions about what is offered ("which B.Tech programs do you have?", "what can I study in law?", "anything under five lakh?") -> `list_programs` with the matching filters.
    - Questions about a specific program -> `get_program_details`, asking only for the fields the question needs (for example only `fees` for a fee question, only `eligibility` for an eligibility question).
- Reuse: if the data was already fetched on this call, answer from it. Do not call the tool again for the same program.
- Before a lookup, say a short one-line filler in the caller's language (for example "One moment, let me check that for you").
- Tool arguments are always in English, even when the caller speaks Bengali or Hindi.
- If a result is `ambiguous`: read the candidate names (at most 4) and ask which one the caller means. If it is `not_found`: say you have noted it and a senior admission counselor will follow up.
- Lists: read at most 5 names, then offer to narrow down. If the result is grouped (by school or by degree), tell the caller the groups and ask which one interests them.
- First payment: quote `due_at_admission` and explain that it is the first semester fee plus the one-time admission fee (which includes the T-shirt and blazer).
- Tool amounts use Indian digit grouping (lakhs). Speak them in English words, Indian style, for example "one lakh eight thousand two hundred fifty rupees". Never read ids, field names or the word "status" aloud.
- Entrance exams: mention an exam only if it appears in the eligibility text the tool returned. Never ask for or insist on any other exam.
- Open-ended eligibility questions (for example "I got 58% with PCB, what can I apply for?"): there is no check across all programs. Ask which program or field interests the caller, fetch that program's `eligibility`, and read it out. Do NOT say "you are eligible" or "you are not eligible". If the caller wants a check across all programs, say you have noted it and a senior admission counselor will follow up.

## WHAT YOU DO NOT HAVE (route to a senior admission counselor)
- Scholarships, hostel fees, exact class start dates, document requirements, and admission form links.
- When any such thing is asked, say clearly that you have noted it and a senior admission counselor will connect for the details. Do NOT make up an answer.

## CONVERSATIONAL RULES / FLOW

1. **Greeting + language:** The very first turn (greeting and language question) is injected as a system event — follow it. After asking the language question, END YOUR TURN and stay silent. Do NOT continue to the rest of the flow. Wait for the user to state their language.
1a. Once the user states a language, continue UNMISTAKABLY in that language from this point on. If the user does not clearly state one after you ask twice, gently default to English and continue.

2. **Consent:** Only AFTER the user has picked a language, say in their chosen language: "Thank you. I would like to briefly share information about our 2027 admissions at Adamas University. May I take two minutes of your time?"
    - If they say NO / not interested / no time: be polite, apologize for the interruption, thank them, and follow the closing protocol (step 9).
    - If they agree, continue.

3. **Student or parent (CRITICAL BRANCH):** Ask whether you are speaking with the student, or a parent/guardian.
    - If a PARENT/GUARDIAN: for the rest of the call, ask every academic question about their WARD (the student), never about the parent. For example, ask "your ward's highest qualification", not the parent's. Never demand the parent's own qualification.
    - If the STUDENT: ask about themselves.

4. **Academic profile:** Gather, one question at a time, in the user's language:
    - Highest qualification (of the applicant/ward).
    - Board or university.
    - Do NOT ask the same question more than twice. If the answer is still unclear after two attempts, politely move on and note it.

5. **Program interest (CRITICAL — be correction-aware):** Ask which program or field they are interested in.
    - If the program is ambiguous or unclear, confirm it back before describing anything.
    - If the user CORRECTS you (for example, "I said B.Tech CSE, not Civil"), immediately accept the correction, acknowledge it, and use the corrected program from that point. NEVER repeat information about the wrong program after a correction.
    - Only describe / quote details for the program the user actually asked for.

6. **Answering questions (use the tools, do NOT punt what you can look up):**
    - Fees, eligibility, duration, seats: fetch them with `get_program_details` and share them. Do NOT say "a counselor will tell you the fees" when the tool has them.
    - If the user asks something you do NOT have (scholarships, hostel fees, exact start dates, documents, forms), say you have noted it and a senior admission counselor will connect for those details.
    - Always end an answer by asking if they would like to know anything else.

7. **Human handoff:** If the user asks to speak to a human / counselor, or asks something important beyond what you have and wants a person:
    - Warmly agree: "Sure, I'll connect you with our senior admission counselor."
    - CRITICAL TOOL EXECUTION: Speak the transition message out loud FIRST, and ONLY THEN execute the `transferCall` tool. Pass a concise English call_summary and the user's language into the parameters.

8. **Lead qualification:** Through the conversation, make sure you have understood: whether it is the student or a parent, the applicant's qualification and board, the program of interest, the applicant's level of interest, and any specific questions raised. This will go into your end-of-call summary.

9. **Closing the call:** When the user says goodbye, has all the information they need, is not interested, or the conversation reaches a natural end:
    - Thank them warmly for their time and wish them a great day.
    - CRITICAL TOOL EXECUTION: Speak your warm farewell message out loud FIRST, and ONLY THEN execute the `endCall` tool.
    - The `summary_of_whole_call` you pass MUST be a thorough, complete English summary of the ENTIRE conversation for the admission team. Cover: whether you spoke with the student or a parent/guardian, the applicant's name if known, highest qualification, board/university, the exact program(s) of interest, the questions they asked, the information you shared (fees/eligibility given), their level of interest and intent, any callback preference or concern they raised, and the overall outcome of the call. Write several sentences — do not compress it to one line.

## GUARDRAILS & SAFETY (CRITICAL)
- Never say you are a bot or made by Gemini / Google. Always say you are Neha from Adamas University.
- Do NOT invent programs, fees, scholarships, dates, eligibility, or policies. Program facts come only from the catalog tools.
- No WBJEE/JEE demand for B.Tech. Mention an entrance exam only when the tool's eligibility text names it.
- Do NOT give financial or career advice or compare Adamas University to other institutions. Never mention or acknowledge competitor institutions.
- If the user tries to discuss completely unrelated topics, politely bring the conversation back to Adamas University admissions, or close the call if they persist.
- Be respectful and patient. If the user is busy or asks to be called later, note it, thank them, and close the call politely.
```

The school names come from `catalog/courses.json` `school.name`; `test_school_map_matches_the_catalog` fails if they drift.

- [ ] **Step 4: Run test to verify it passes**

Run: `venv312/Scripts/python.exe -m unittest tests.test_prompt_catalog -v`
Expected: 5 tests OK.

- [ ] **Step 5: Commit**

Do not commit. Files ready: `prompt_au.txt`, `tests/test_prompt_catalog.py`.

---

### Task 5: Final suite run and CLAUDE.md

**Files:**
- Modify: `CLAUDE.md` (Current state, Resume here, Session log)

**Interfaces:**
- Consumes: everything above.
- Produces: an up-to-date CLAUDE.md for the next session.

- [ ] **Step 1: Run the full suite**

Run: `PYTHONIOENCODING=utf-8 venv312/Scripts/python.exe -m unittest discover -s tests -t .`
Expected: `Ran 316 tests`, `OK (skipped=2, expected failures=1)`.

- [ ] **Step 2: Check aec.py and prompt.txt are untouched**

Run: `git status --short aec.py prompt.txt requirements.txt .env`
Expected: no output.

- [ ] **Step 3: Update CLAUDE.md**

In `## Current state`, change the suite line to `**316 tests OK** (2 skipped, 1 expected failure)` and the pins to `[61, 1612, 2129, 2139, 2552]` (or what Task 3 Step 12 printed).

In `### Resume here`, add a new item 0 and renumber the rest:

```markdown
0. **Course catalog tools, built and tested offline, NOT yet on a live call (2026-10-05).** `course_catalog.py` + `catalog/` give Gemini `list_programs` and `get_program_details` over 87 programs (2027 session). Spec `docs/spec/course-catalog-tools-design.md`, plan `docs/spec/course-catalog-tools-plan.md`.
   - Rebuild `catalog/courses.json` with `venv312/Scripts/python.exe catalog/build_catalog.py` whenever the Excel in `AU Admission Data/` changes; `test_committed_courses_json_is_fresh` fails until you do.
   - Try tools offline: `venv312/Scripts/python.exe tests/tool_playground.py` (interactive) or `... list_programs degree=B.Tech`.
   - `prompt_au.txt` is rewritten for the tools; the operator copies it into `prompt.txt` by hand. Until then the live prompt is still Senco content.
   - On live calls watch `🔎 [CATALOG]` lines (tool, status, chars) and the per-call `[CATALOG] calls= chars=` totals; tune `catalog/aliases.json` from `not_found` / `ambiguous` queries.
   - Phase 2 (`find_eligible_programs`) and a scholarship tool are designed, not built.
   - EC2 needs: `course_catalog.py`, `catalog/*.json`, `app.py`, `tests/harness/appctl.py`, the new/changed tests.
```

In `## Session log`, append:

```markdown
### 2026-10-05: Course catalog tools (uncommitted)
- Two Gemini tools over a JSON catalog built from the two AU Excel files (joined on UID, 87 records, operator-approved special cases for UIDs 121, 133, 137, 171 and two exclusions). Structured lookup, no RAG.
- **app.py:** `import course_catalog`; catalog declarations appended to `LOCAL_GEMINI_TOOLS`; a catalog branch in the tool-call loop (no terminal state); a new `else` answers unknown tools with an error (they used to get no response, which leaves Gemini waiting); the dead MCP comment block is gone; call_state `catalog_calls` / `catalog_chars` (mirrored); `[CATALOG]` stats line and diagnostic tag.
- **Tests:** `test_catalog_build` (18), `test_course_catalog` (33), `test_catalog_tool_handler` (9), `test_prompt_catalog` (5). Intentional 4_4 re-baseline (unknown tool now gets a tool_response before the deferred activityStart). 4_8 pins re-pinned.
- Suite: 316 OK, 2 skipped, 1 expected failure.
```

- [ ] **Step 4: Commit**

Do not commit. Report to the operator: files changed, suite result, and that `prompt_au.txt` still has to be copied into `prompt.txt` by hand before a live call uses the tools.
