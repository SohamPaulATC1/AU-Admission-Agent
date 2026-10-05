# Course catalog tools for the Neha voice agent: design

Date: 2026-10-05. Status: approved by the operator 2026-10-05. Corrected the
same day after scratch validation (degree count, size budgets, id example,
resolution steps, seats_note, wiring test); the implementation plan is
`docs/spec/course-catalog-tools-plan.md`.

## 1. Goal

Neha (the Adamas University admission voice agent, Gemini Live) must answer
questions about every program the university offers for the 2027 session, not
only the SoET programs hard-coded in `prompt_au.txt`. Course data comes from two
Excel files in `AU Admission Data/`. The agent fetches only the data a question
needs, through function-calling tools, so that it stays fast on a live call and
cheap: on Gemini Live, the system prompt, tool declarations and every tool
response are re-billed on every subsequent turn of the session.

Success criteria:
- Neha can list programs (by school, level, degree, keyword, fee ceiling) and
  quote exact eligibility, duration, seats and fees for any catalog program.
- No fee or eligibility figure is spoken unless a tool returned it on that call.
- A typical tool response is under about 200 tokens (800 characters), the
  largest single-program response under about 350; the two tool declarations
  together are under about 450 tokens (1800 characters). Section 6.5.
- The operator can exercise each tool offline with arbitrary input and see the
  exact response Gemini would get.

Out of scope for this spec: scholarships (data exists, not yet provided; a
later tool), and wiring the new prompt into `prompt.txt` (the operator copies
`prompt_au.txt` over by hand).

## 2. Approach

Structured lookup, not vector RAG. The catalog is small (87 records), questions
are mostly about named programs, and the voice model already turns loose
phrasing ("computer wala course") into structured tool arguments. Vector search
would add an embedding call per lookup (about 150-400 ms), confuse near-identical
programs (several "Data Science" programs across degrees) and cannot enumerate
("list all B.Tech") or filter by fee. Instead: one JSON record per course, built
offline, held in memory, served by typed tools with deterministic name matching.
Lookups take under 1 ms.

Putting the whole catalog in the prompt was also rejected: about 17k tokens
re-billed every turn, roughly $0.25 per 20-turn call before audio.

## 3. Source data and join rules

Files:
- Program file: `Academic Program Notification_2026-2027_Corrigendum_FINAL_06082026 (4) (1).xlsx`,
  11 sheets, 87 rows. Columns: S.N., UID, School, Level, Department, Program,
  Program Duration (Years), Intake, Eligibility Criteria.
- Fee file: `National Course fee 2026.xlsx`, 86 rows. Columns: S.N.,
  UID, School Name, Department, Program., Duration, 1st..4th Sem Fees, 5th - 6th
  Sem Fees, 7th - 10th Sem Fees, Final Program Fees (Incl. FL, Alumni),
  Admission Fees (Incl. T-Shirt & Blazer), Total Fees.

"2026-2027" and "Fees 2026" both mean the 2027 session; records are labelled
`"session": "2027"`.

Verified fee arithmetic (holds on every fee row): the "5th - 6th" and
"7th - 10th" columns are per-semester amounts. For a program of D years
(2D semesters): sem1 + sem2 + sem3 + sem4 + 2 x sem5-6 + (2D - 6) x sem7-10 =
Final Program Fee, and Final Program Fee + Admission Fee = Total Fee. The build
asserts both.

Join is 1-to-1 on UID, plus an explicit allow-list of operator-confirmed special
cases. Any other duplicate UID, unmatched UID, blank UID or arithmetic mismatch
fails the build.

| Case | Rule (operator decision 2026-10-05) |
|---|---|
| UID 133: B.Tech CSE (AI & ML), B.Tech AI & DS (Business Application), B.Tech AI & DS (FinTech) | All three take the single 133 fee row. Each keeps its own eligibility text. |
| UID 121: BBA, BBA (E-Commerce) | Two records, both with the BBA fee row. |
| UID 171: one program row "M.Pharm (Pharmacology) M.Pharm (Pharmaceutics)", two fee rows | Split into two records (Pharmaceutics, Pharmacology), shared eligibility, each with its own fee row. |
| UID 137: fee rows "with specialization in SAP" and "SAP add-on- Certification" | The program record joins the specialization row. The add-on row (1 year, 65,000, no admission fee) is attached as an `add_on` on that record. |
| Blank UID: M.Tech (VLSI & Embedded Systems) in the program file, PG Diploma in Nuclear Medicine Technology in the fee file | Excluded (incomplete data). Listed in the build report. |
| Name differs between files (about 20 UIDs, e.g. "Bachelor Of Medical Laboratory Science (BMLS)" vs "B.Sc Medical Laboratory Technology") | Program file name is canonical; fee file name becomes an alias. |

Result: 87 records (87 program rows, minus VLSI, plus the M.Pharm split).

Payment semantics (operator): the first-semester fee excludes the admission
fee; the one-time admission fee (43,800, includes T-shirt and blazer) is paid
together with the first-semester fee. So the amount due at admission is
first_semester + admission_fee, precomputed in the record so the model never
does arithmetic on a call.

## 4. Files

| Path | Kind | Purpose |
|---|---|---|
| `catalog/build_catalog.py` | offline script, stdlib only | Read both xlsx files (zip + XML; no openpyxl, requirements.txt is frozen), apply join rules, validate, write `courses.json`, print a report. Run by hand when the Excel changes. |
| `catalog/courses.json` | generated, committed | The only catalog file the app reads at runtime. |
| `catalog/aliases.json` | hand-edited, committed | Per-record aliases keyed by id ("cse", "ai ml", "mlt", "law", ...) and global short-form expansions (cse, ece, ee, ai, ml, ds, biotech, engg, ...). The build merges the aliases into `courses.json`; the expansions are read at runtime. Tunable from call logs. |
| `course_catalog.py` | runtime module, repo root | Loads the two JSON files once at import; exposes `TOOL_SPECS` (plain dicts) and `handle_tool_call(name, args) -> dict`. No Google SDK import, no I/O after load. |
| `tests/tool_playground.py` | manual CLI | Offline tool simulator (section 8). Not named `test_*`, so unittest discovery skips it. |
| `tests/test_catalog_build.py`, `tests/test_course_catalog.py`, `tests/test_catalog_tool_handler.py`, `tests/test_prompt_catalog.py` | unittest | Section 9. |
| `app.py`, `tests/harness/appctl.py` | modified | Section 7. |
| `prompt_au.txt` | rewritten | Section 10. `prompt.txt` is not touched. |

## 5. Stored record

```json
{
  "id": "btech-computer-science-and-engineering",
  "uid": "132",
  "name": "B.Tech (Computer Science and Engineering)",
  "aliases": ["cse", "computer science", "computer science and engineering"],
  "degree": "B.Tech",
  "level": "UG",
  "school": {"name": "School of Engineering and Technology", "code": "SoET"},
  "department": "Computer Science and Engineering",
  "duration_years": 4,
  "duration_note": null,
  "seats": 300,
  "eligibility": "Minimum 60% aggregate in 10+2 or equivalent ... (with min 45% marks in respective subject)",
  "fees": {
    "session": "2027",
    "first_semester": 108250,
    "admission_fee": 43800,
    "admission_fee_note": "one-time, paid with the first semester fee; includes T-shirt and blazer",
    "due_at_admission": 152050,
    "per_year": [216500, 211500, 210000, 210000],
    "per_semester": [108250, 108250, 105750, 105750, 105000, 105000, 105000, 105000],
    "program_fee": 848000,
    "total": 891800
  },
  "add_ons": []
}
```

Field rules:
- `id`: unique readable slug, because UID is not unique. Stable across rebuilds:
  derived from the canonical name, with split degree letters joined ("B. A
  (English ...)" -> `ba-english-language-and-literature`). Two records with the
  same id fail the build.
- `degree`: derived from the canonical name by an ordered pattern table that
  tolerates the source's spacing ("B. Sc. (Hons.)", "M. A.", "M. Tech"). The 22
  values, checked against the program file:
  - UG: B.Tech, BCA, B.Sc, B.A, B.Com, BBA, B.Ed, BA LL.B, BBA LL.B, B.Pharm,
    BMLS, B.Optom, BFND.
  - PG: M.Tech, MCA, M.Sc, M.A, MBA, LL.M, M.Pharm.
  - Diploma: D.Pharm and Post-M.Sc Diploma.

  BMLS, B.Optom and BFND are the labels for "Bachelor Of Medical Laboratory
  Science (BMLS)", "Bachelor of Optometry" and "Bachelor Of Food Nutrition and
  Dietetics". These labels are my assumption; the full name stays an alias. A
  name matching no pattern fails the build. A test pins the set, and the set
  becomes the `degree` enum in the tool declaration.
- `level`: `UG`, `PG` or `Diploma` (program file values `DIPLOMA` and
  `Post-M.Sc. Diploma` both map to `Diploma`).
- `school.code`: the short code from the program file's school name
  (SoBAS, SoB, SoE, SoET, SoHMS, SoLJ, SoLACS, SoLB, SoMC, SoSA).
- `duration_years`: integer, or `null` with the original wording in
  `duration_note` when the source is free text (Post-M.Sc Diploma in Medical
  Physics).
- `eligibility`: source text with whitespace tidied ("10 +2" -> "10+2"); wording
  otherwise unchanged.
- Amounts are stored as integers (rupees); formatting happens at response time.
- `add_ons`: list of `{name, duration_years, fee}`; only UID 137 has one.
- `seats_note`: present only on the two M.Pharm records ("intake shared with
  M.Pharm (Pharmacology)"), because the source gives one intake for both.

## 6. Tools

### 6.1 `list_programs`

All arguments optional.

| Arg | Type | Notes |
|---|---|---|
| `level` | enum UG / PG / Diploma | |
| `degree` | enum (section 5 set) | |
| `school` | enum of the 10 school codes | |
| `keyword` | string | Matched against name, aliases and department after normalisation. |
| `max_total_fee` | integer, rupees | Compared with `fees.total`. |

Response shape scales with the result set, so a broad question never returns
87 names:
- No arguments: `{"by_school": [{"school": "SoET - School of Engineering and Technology", "count": 20}, ...]}`.
- 1-20 matches: `{"count": N, "programs": ["B.Tech (Civil Engineering)", ...]}`;
  names only (the details tool accepts exact names), sorted by school, then
  level, then name.
- More than 20 matches: `{"count": N, "by_degree": [{"degree": "B.Tech", "count": 18}, ...]}`.
- No matches: `{"count": 0}`.

### 6.2 `get_program_details`

| Arg | Type | Notes |
|---|---|---|
| `programs` | array of 1-3 strings, required | Exact name, id, or the caller's phrasing. |
| `fields` | array, subset of overview / eligibility / fees / fee_breakdown / seats | Default: overview, eligibility, fees. |

Per-field projections:
- `overview`: school (code and name), department, level, duration as text
  ("4 years", or the duration note).
- `eligibility`: the eligibility text.
- `fees`: session, first_semester, admission_fee, admission_fee_note,
  due_at_admission, total; plus `add_ons` when the record has any.
- `fee_breakdown`: per_year, per_semester, program_fee, admission_fee, total.
- `seats`: intake, plus `seats_note` when the record has one.

All amounts in responses are Indian-grouped strings ("1,08,250").

Response: `{"results": [...]}`, one entry per requested program:
- `{"query": "cse", "status": "found", "name": "...", <requested fields>}`
- `{"query": "data science", "status": "ambiguous", "candidates": [up to 4 names], "more": <int, omitted when 0>}`
- `{"query": "mbbs", "status": "not_found"}`

### 6.3 Name resolution (deterministic)

1. Normalise: lowercase; "&" -> "and"; strip punctuation; collapse whitespace;
   join split degree tokens ("b tech" -> "btech", "m sc" -> "msc"); drop
   stopwords (and, of, hons, course, program, ...).
2. The query equals an id -> `found`.
3. Exact match of the normalised query on a canonical name or alias, across all
   records -> `found` (several -> `ambiguous`). This runs before the degree
   filter, so the fee-file name "B.Sc Medical Laboratory Technology" finds the
   BMLS record.
4. Otherwise detect a degree in the query (longest match first). It restricts
   the pool to that degree, and the rest of the query is matched exactly
   against each pooled record's name-without-degree and aliases ("ma english").
5. Otherwise score each pooled record: per query token, 1.0 if it is in the
   record's name or alias words (a short form counts when all its expansion
   words are), 0.5 (`DEPARTMENT_WEIGHT`) if only in the department; divided by
   the token count. A degree with nothing else ("MBA") scores 1.0.
6. `found` when the best score is at least 0.75 (`FOUND_SCORE`) and beats the
   runner-up by 0.2 (`SCORE_MARGIN`), and every query token is in its name or
   alias words (added after final review, 2026-10-05: a department-only word
   must not make a found, so "mtech computer science" does not land on M.Tech
   (Data Science ...)). Otherwise `ambiguous` with every record
   within 0.2 of the best and at least 0.6 (`CANDIDATE_SCORE`), top 4 plus
   `more`; none -> `not_found`. All four constants are pinned by tests.

No embeddings and no randomness; the same query always yields the same result.

### 6.4 Errors

- Invalid arguments (unknown enum value, empty or more than 3 `programs`,
  unknown field) -> `{"error": "<plain-English message>"}` so the model can
  retry.
- Unknown tool name -> handled in app.py (section 7).

### 6.5 Size budgets (characters of compact JSON, about 4 per token)

Measured on the 2027 data, budgets set with headroom:

| Response | Measured | Budget |
|---|---|---|
| Any `list_programs` response (no args, every degree, school, level) | 834 max | 1000 |
| CSE, default fields | 682 | 800 |
| Any single program, default fields (B.Pharm, longest eligibility) | 1217 | 1400 |
| cse + ece + civil, `fees` only | 924 | 1000 |
| Both catalog tool declarations | 1397 | 1800 |

## 7. app.py integration

- `import course_catalog`; `LOCAL_GEMINI_TOOLS` gains `list_programs` and
  `get_program_details`, built as `types.FunctionDeclaration(**spec)` from
  `course_catalog.TOOL_SPECS`.
- Tool-call handler (around app.py:2676): new branch for catalog tool names
  calls `course_catalog.handle_tool_call` and appends a `types.FunctionResponse`
  with the result. It does not touch `pending_end_call`, `closing_audio_phase`
  or any other terminal-action state. Logs
  `🔎 [CATALOG] tool=<name> status=<found|ambiguous|not_found|list|error> chars=<n> ms=<t>`.
- New `else` branch: an unknown tool name returns
  `FunctionResponse(response={"error": "unknown tool <name>"})`. Today an unknown
  tool gets no response at all and the session stalls.
- New call_state keys `catalog_calls` and `catalog_chars`, reported on the call
  stats; both mirrored in `tests/harness/appctl.py` (guarded by
  test_harness_smoke). `[CATALOG]` is added to `DIAGNOSTIC_LOG_TAGS`.
- Re-pin the GEMINI_MODEL line references in
  `tests/test_preservation_4_8_resumption_recording_stats.py`.
- Intentional baseline update in `tests/test_preservation_4_4_tool_and_silence.py`:
  its unknown-tool scenario now sees a `tool_response` before the deferred
  `activityStart` and audio. The dead commented-out MCP `else` block is removed.
- `tool_call_in_progress` already blocks activity-open during a tool call; the
  sub-millisecond lookup keeps that window negligible.

## 8. Offline simulator: `tests/tool_playground.py`

Calls the same `course_catalog.handle_tool_call` as app.py, so offline output
equals what Gemini receives.

```
venv312/Scripts/python.exe tests/tool_playground.py                         # interactive loop
venv312/Scripts/python.exe tests/tool_playground.py list_programs degree=B.Tech
venv312/Scripts/python.exe tests/tool_playground.py get_program_details programs=cse,ece fields=fees
venv312/Scripts/python.exe tests/tool_playground.py get_program_details '{"programs": ["ai ml"]}'
venv312/Scripts/python.exe tests/tool_playground.py --declarations            # the declarations Gemini sees
```

- Arguments as `key=value` pairs (comma-separated values become arrays for
  array-typed args) or one raw JSON object.
- Prints the response as indented JSON, then its size in characters and
  estimated tokens (characters / 4).
- Interactive mode: choose a tool, type arguments, repeat; empty line exits.
- Prints UTF-8 explicitly (Windows console is cp1252).

## 9. Tests (stdlib unittest only)

`tests/test_catalog_build.py`
- Building from the real Excel yields 87 records; VLSI and Nuclear Medicine are
  absent and reported.
- Each special case: the three 133 records share fees and keep distinct
  eligibility; BBA (E-Commerce) has BBA's fees; M.Pharm split into two; SAP
  add-on attached with fee 65,000.
- Fee arithmetic and `due_at_admission` for every record.
- Unique ids; the degree set is pinned.
- The committed `catalog/courses.json` equals a fresh build (staleness guard).

`tests/test_course_catalog.py`
- Per-school counts after exclusions: SoBAS 16, SoB 6, SoE 3, SoET 20, SoHMS 12
  (M.Pharm split), SoLJ 3, SoLACS 14, SoLB 8, SoMC 4, SoSA 1.
- Resolution: "cse" -> B.Tech (Computer Science and Engineering); "data science"
  -> ambiguous; "mbbs" -> not_found; exact canonical names and ids resolve;
  fee-file names (aliases) resolve.
- `list_programs` staging: no args -> by_school; degree=B.Tech -> 18 names;
  level=UG -> by_degree; impossible filter -> count 0; max_total_fee filtering.
- Field projection: only requested fields are present; the default set; amounts
  Indian-grouped; add_ons only with fees.
- Error responses for each invalid-argument case.
- Size budgets from 6.5.

`tests/test_catalog_tool_handler.py`
- `LOCAL_GEMINI_TOOLS` declares endCall, transferCall, list_programs,
  get_program_details, in that order.
- A catalog call through `stream_gemini_to_plivo` sends one tool_response equal
  to `handle_tool_call(...)`, bumps both counters, logs the `[CATALOG]` line,
  and leaves every terminal flag unset.
- Speech during a lookup still opens the deferred activityStart afterwards.
- An unknown tool gets `{"error": "unknown tool <name>"}`.
- The call-stats `[CATALOG]` totals line.

`tests/test_prompt_catalog.py`
- `prompt_au.txt` formats with `user_name` and `phone_number` and raises no
  KeyError (no stray braces).
- It names both catalog tools, and does not name `find_eligible_programs`
  (phase 2).
- It contains no hard-coded fee figures (no 5+ digit amounts or lakh-grouped
  numbers), so the prompt cannot drift from the data again.

- Every school code with its full name appears in the map.

Plus the existing suite stays green (251 tests today; 323 with these).

## 10. Prompt (`prompt_au.txt`)

Kept from the current `prompt_au.txt`: the Neha persona, language and tone
rules, the call flow (greeting, consent, student or parent, academic profile,
correction-aware program interest), human handoff and closing with the existing
`transferCall` and `endCall` rules, the end-of-call summary requirements, the
guardrails including no WBJEE/JEE for B.Tech.

Removed: every hard-coded fee and eligibility line, and the "this call covers
only SoET" scope.

Added:
- Opening pitch generalised to "2027 admissions at Adamas University".
- Catalog map: the 10 school codes with full names, on one line. The degree
  types are not repeated: the `list_programs` degree enum already carries them,
  and the prompt is re-billed every turn (shortened 2026-10-05, about 10.8k to
  about 6.8k characters, no rule dropped).
- Tool rules:
  - Never state a fee, eligibility, duration or seat count unless a tool
    returned it on this call.
  - Pick the tool by question: lists -> `list_programs`; a specific program ->
    `get_program_details` with only the fields needed.
  - Reuse: if the data was already fetched on this call, answer from it; if a
    needed field was not fetched yet, fetch just that field.
  - Say a one-line filler in the caller's language before a lookup.
  - `ambiguous`: read the candidates (at most 4) and ask which one. `not_found`:
    say it is noted and a senior counselor will follow up.
  - Lists: read at most 5 names, then offer to narrow down. A grouped result
    (by school or degree): tell the caller the groups and ask which.
  - First payment: quote `due_at_admission` and explain it is the first-semester
    fee plus the one-time admission fee.
  - Speak amounts in English in Indian style ("one lakh eight thousand two
    hundred fifty rupees"). Never read ids or field names aloud.
  - Tool arguments always in English.
  - `error`: never read it aloud; retry once with fixed arguments, then say it
    is noted and a senior counselor will follow up.
- Entrance exams: mention only those that appear in the returned eligibility
  text.
- Open-ended eligibility questions ("I got 58% with PCB, what can I apply
  for?"): there is no cross-catalog eligibility check in phase 1. Ask which
  program or field interests the caller, fetch that program's `eligibility`,
  and read it out without saying "you are eligible" or "you are not
  eligible". If the caller wants a check across all programs, say it is noted
  and a senior counselor will follow up. This rule is replaced when
  `find_eligible_programs` ships (section 11).
- Still routed to a counselor: scholarships (until the scholarship tool exists),
  hostel, class start dates, documents, application form links.
- No literal braces anywhere in the file: app.py fills placeholders with
  `str.format`.

## 11. Phase 2 (designed, built after phase 1 is tried on live calls)

Operator decision 2026-10-05: not in phase 1. Wait until live calls show how
often callers ask open-ended eligibility questions; until then, the phase 1
prompt rule in section 10 handles them.

`find_eligible_programs(qualification, aggregate_pct, subjects?, entrance_exams?, level?, degree?)`
answers "I got 58% with PCB, what can I apply for?".

- Data: a structured `eligibility_rules` block per record (qualifying exam, minimum
  aggregate %, required subject combinations, minimum subject %, entrance exams),
  extracted by the build script from the eligibility text plus a hand-written
  `catalog/eligibility_overrides.json`. Every record carries a `verified` flag;
  the tool only returns programs whose rules have been checked by a person.
- Response: same staging as `list_programs` (names, or grouped counts).
  Wording is always "you appear eligible for"; the tool never declares a
  caller ineligible, and the prompt sends borderline cases to a counselor.
- Phase 1 needs no change for this: it is an additive field and a third tool spec.

Later: a scholarship tool once that data is provided, following the same
pattern (build from source, project only requested fields).

## 12. Deployment

Copy to EC2 (file-copy sync, back up the old files first): `course_catalog.py`,
`catalog/` (all of it: `tests/test_catalog_build.py` imports the build script,
and skips itself when the Excel files are absent), `app.py`, the
new and changed tests, `tests/harness/appctl.py`. The operator copies
`prompt_au.txt` into `prompt.txt` when ready. The Excel files stay in the repo
as the source; EC2 needs only the JSON.
