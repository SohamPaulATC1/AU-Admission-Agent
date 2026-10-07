# Course catalog tools for the Neha voice agent: design

Date: 2026-10-05. Status: approved by the operator 2026-10-05. Corrected the
same day after scratch validation (degree count, size budgets, id example,
resolution steps, seats_note, wiring test); the implementation plan is
`docs/spec/course-catalog-tools-plan.md`. Section 11 (phase 2,
`find_eligible_programs`) approved 2026-10-06; its plan is
`docs/spec/find-eligible-programs-plan.md`.

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
| `catalog/eligibility_rules.json` | hand-edited, committed | Phase 2: structured, operator-verified UG eligibility rules keyed by eligibility text (section 11.2). |
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
| All four catalog tool declarations (three were 2514) | 4161 | 4200 |
| Any `find_eligible_programs` response (all rules verified; worst: 60% PCM, keyword "computer science") | 1452 | 1500 |
| Any `find_scholarships` response, no `situations` (sweep of 87 programs x 5 fact sets) | 1255 | 1500 |
| Any `find_scholarships` response, all seven `situations` | 1529 | 1800 |

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
- It names both catalog tools (and, since phase 2, `find_eligible_programs`;
  section 11.6).
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
  and a senior counselor will follow up. Replaced by the phase 2 rule in
  section 11.5.
- Still routed to a counselor: scholarships (until the scholarship tool exists),
  hostel, class start dates, documents, application form links.
- No literal braces anywhere in the file: app.py fills placeholders with
  `str.format`.

## 11. Phase 2: `find_eligible_programs` (approved 2026-10-06)

Operator decision 2026-10-05 deferred this until live calls; reversed
2026-10-06: build it now as a safe fallback for "I got 58% with PCB, what can I
apply for?", which phase 1 could only route to a counselor. Decisions
(operator, 2026-10-06): UG only; rules hand-written and reviewed by the
operator; per-subject minimums and entrance exams returned as spoken
conditions, not filtered on.

### 11.1 Safety rules

- The tool only judges programs whose rule a person has verified. Unverified or
  unjudgeable programs are counted (`not_covered`), never named as matches.
- The tool never names programs the caller fails. It says nothing about
  ineligibility; the prompt never says "you are not eligible".
- Wording on the call is "you appear eligible for", plus "admissions confirms
  final eligibility".
- A changed eligibility text in the Excel invalidates its rule (rules are keyed
  by exact text), so a stale verified rule cannot survive a data change.
- Shipping with every rule unverified is safe: the tool then returns only
  `not_covered` counts.

### 11.2 Rules file: `catalog/eligibility_rules.json` (hand-written)

```json
{
 "subjects": ["Physics", "Chemistry", "Mathematics", "Biology", "Biotechnology",
              "Computer Science", "Computer Application", "Technical Vocational",
              "Statistics", "Economics", "Geography", "Psychology", "Agriculture",
              "Nutrition", "Home Science", "Human Development"],
 "rules": [
  {
   "text": "Minimum 60% aggregate in 10+2 or equivalent from any recognized board with PM + Chem/ Bio. Tech/ ...",
   "programs": ["B.Tech (Computer Science and Engineering)", "..."],
   "min_aggregate": 60,
   "subject_options": [["Physics", "Mathematics", "Chemistry"], ["Physics", "Mathematics", "Biology"], "..."],
   "conditions": ["at least 45% in each of those subjects"],
   "verified": false
  }
 ]
}
```

- One rule per distinct UG eligibility text (27 for the 53 UG records).
  `text` is the exact `eligibility` string of the records (after the build's
  `eligibility_text` normalisation) and is the match key.
- `programs`: names of every UG record with that text, in record order. Checked
  by the build so the reviewer always sees the right programs.
- `min_aggregate`: 10+2 aggregate percentage, or `null` when the rule does not
  turn on 10+2 marks (B.Ed needs a degree). A `null` rule is never matched.
- `subject_options`: list of subject sets; a caller matches when their subjects
  include every subject of at least one set. `null` = any stream. Every subject
  must be in `subjects`.
- `conditions`: short English caveats the tool does not check, read out on the
  call (per-subject minimums, "CLAT or AUAT qualified", "English as a subject",
  "open-school 10+2 not accepted", "without Mathematics, a remedial Mathematics
  course in the first semester"). May be empty.
- `verified`: `false` in the first draft. The operator reviews each rule
  against its text and sets `true`. Must be a JSON boolean (a string `"true"`
  is a build error).
- `note` (optional): reviewer-only explanation of how the text was read; not
  copied into `courses.json`. Any other key is a build error (catches typos).
- One text shared by programs whose "respective subject" differs (B.Sc
  Chemistry and B.Sc Physics) gets one rule requiring the union (Physics and
  Chemistry): stricter is the safe side.
- English is not a subject in the vocabulary (nearly every caller has it); a
  rule that requires it says so in `conditions`.

### 11.3 Build

`build_catalog.py` reads the rules file and:
- Adds `eligibility_rule` (`min_aggregate`, `subject_options`, `conditions`,
  `verified`) to every UG record; PG and Diploma records get no such key.
- Adds the top-level `subjects` list to `courses.json`.
- Raises `BuildError` when a UG text has no rule, a rule's text matches no UG
  record, a rule's `programs` differ from the records sharing its text, two
  rules share a text, or a rule names a subject outside `subjects`.
- Prints `eligibility rules: <verified> verified / <total>`.

The freshness test (section 9) covers the rules file, since the build reads it.

### 11.4 Tool

`find_eligible_programs(aggregate_pct, subjects, degree?, keyword?)`:
- `aggregate_pct` (NUMBER, required): 10+2 aggregate, 0 to 100.
- `subjects` (ARRAY of the `subjects` enum, required, may be empty): the
  caller's 10+2 subjects. A single string is taken as a one-item list. The
  description tells the model "PCM = Physics, Chemistry, Mathematics; PCB =
  Physics, Chemistry, Biology".
- `degree` (UG degrees enum) and `keyword`: narrowing filters with
  `list_programs` semantics.

Logic, over UG records passing the filters:
- `not_covered`: rule unverified, or `min_aggregate` is `null`.
- Match: `aggregate_pct >= min_aggregate` and (`subject_options` is `null` or
  some set is a subset of `subjects`).
- Anything else is dropped silently.

Response (matches in `list_programs` order):
- Up to `ELIGIBLE_NAMES_MAX = 10` matches:
  `{"count": n, "programs": [{"name": ..., "conditions": [...]}], "not_covered": k}`
  (`conditions` omitted when empty). Lower than `LIST_NAMES_MAX` (20) because
  each entry carries conditions and the result is re-billed every turn.
- More, across several degrees: `{"count": n, "by_degree": [{"degree": ..., "count": ...}], "not_covered": k}`;
  the agent asks the caller to narrow, then calls again with `degree` or
  `keyword`.
- More, all one degree (e.g. `degree=B.Tech`, 18 matches; added 2026-10-07,
  a single group left nothing to narrow on and the model fell back to
  `list_programs`): the first 10 names as above plus `"more": n - 10`; the
  agent reads at most 5, says there are more, and narrows with `keyword`.
- None: `{"count": 0, "not_covered": k}`.

Errors (`{"error": ...}`, section 6.4): `aggregate_pct` missing, not a number,
a boolean, or outside 0-100; unknown subject (lists the allowed ones); unknown
degree; unknown argument; empty keyword. `result_status` returns
`eligible=<n>` (or `error`) for the `[CATALOG]` log line.

app.py needs no change: it builds declarations from `TOOL_SPECS` and routes on
`TOOL_NAMES`. The playground picks the tool up the same way.

Size budgets (added to 6.5 once measured): any `find_eligible_programs`
response under 1500 characters (10 B.Tech names each carrying the 45%
condition estimate about 1350); all three declarations under 2600.

### 11.5 Prompt

Section 10's open-ended eligibility rule is replaced by: for undergraduate
programs, ask the 10+2 aggregate and 10+2 subjects if not known, call
`find_eligible_programs`, say "based on what you shared, you appear eligible
for", read at most 5 names with their conditions, add that admissions confirms
final eligibility; never say "you are not eligible"; a CGPA or grade is
not a percentage (ask for the percentage); results not out yet: use the
expected percentage and say it depends on the final result; if `count` is 0 or
`not_covered` is above 0, say a senior admission counselor can check the other
programs. Postgraduate questions keep the phase 1 flow (ask which program, read
its `eligibility`). The tool-selection line gains the marks-based case.

### 11.6 Tests

- `test_catalog_build`: every UG text has a rule; each `BuildError` above;
  `subjects` carried into `courses.json`; PG/Diploma records have no rule.
- `test_find_eligible_programs` (new file; `test_course_catalog` only gains
  the third tool name and constant): matching against a fake rule set patched
  into the index (so tests do not depend on what is verified): at, above and below the
  minimum; subject-set coverage; `null` subjects; `null` minimum; unverified
  counts as `not_covered`; staging at 10; `degree`/`keyword` filters; every
  error; `result_status`; size budgets.
- Golden profiles on the real data, verified rules only ("58% PCB", "62% PCM",
  "55% commerce", "48% PCM", "70% arts"), each against a hand-written expected
  list. A profile skips while any rule it depends on is unverified.
- `test_catalog_tool_handler`: `LOCAL_GEMINI_TOOLS` order gains
  `find_eligible_programs` after `get_program_details`.
- `test_prompt_catalog`: names all three tools; the open-ended rule says
  "appear eligible" and "not eligible" appears only in the "never say" form.

### 11.7 Rollout

1. Build with every rule `verified: false`; suite green.
2. Operator reviews `catalog/eligibility_rules.json` and sets `verified: true`
   per rule; rebuild; golden profiles run.
3. Include the tool in the planned text-only Gemini test, then the live calls.

Later: a scholarship tool, now section 13.

## 12. Deployment

Copy to EC2 (file-copy sync, back up the old files first): `course_catalog.py`,
`catalog/` (all of it: `tests/test_catalog_build.py` imports the build script,
and skips itself when the Excel files are absent), `app.py`, the
new and changed tests, `tests/harness/appctl.py`. The operator copies
`prompt_au.txt` into `prompt.txt` when ready. The Excel files stay in the repo
as the source; EC2 needs only the JSON.

## 13. Phase 3: `find_scholarships` (approved 2026-10-06)

Source: `AU Scholarship Policy AY 2026-27 Updated V2 (2).pdf` (repo root). The
operator's decisions (2026-10-06):
- The PDF is the only source of truth. Its dates (Dec 2025 to 31 Mar 2026
  windows) are ignored.
- Callers do not know scheme names, so Neha brings up scholarships herself and
  uses them to push for admission.
- A deterministic code tool, not an LLM sub-agent. A Groq sub-agent was
  considered and rejected: it adds latency during the tool call, and an LLM can
  get the rupee figures wrong.

### 13.1 Safety rules

- Only schemes with `verified: true` are used. `source_text` and `note` never
  reach Gemini.
- Only one scholarship applies, the highest (policy notes 4 and 11). The code
  enforces this, not the model.
- Schemes with `committee_decides: true` (sports, merit-cum-means) are never
  `best`. They appear only in `may_also_apply`, and the prompt never promises
  them.
- Rupee figures come from the program's own fees in `courses.json`, computed in
  code. The model does no arithmetic.
- No income question: merit-cum-means is offered as "you may also apply" when
  marks are 60% or above.

### 13.2 Data file: `catalog/scholarships.json` (hand-written, operator-verified)

The operator verified all of it on 2026-10-06. Top level:
- `source`: the PDF title.
- `general_conditions` and `pitch_lines`: lists of
  `{"text", "source_text", "verified", "note"?}`. `text` is the spoken English
  line. `source_text` is the PDF wording for review.
- `schemes`: list of schemes.

Scheme keys (any other key is a load error):
- `id` (unique), `section` (PDF section number), `name` (spoken),
  `source_text`, `note` (optional, reviewer only).
- `programs`: `levels` (subset of `LEVELS`), `degrees`, `exclude_degrees`.
  Every degree named must exist in the catalog, and at least one of `levels` /
  `degrees` must be given. A record matches when its level is in `levels` (if
  given), its degree is in `degrees` (if given), and its degree is not in
  `exclude_degrees`.
- `input`: one of
  - score inputs, tiers use `min` (inclusive): `qualifying_pct`, `auat_pct`,
    `cat_pct`, `mat_pct`, `clat_score`.
  - rank inputs, tiers use `max` (inclusive): `aujet_rank`, `jee_main_rank`,
    `wbjee_rank`.
  - flag inputs, exactly one tier with no bound: `alumnus`,
    `sibling_enrolled`, `defence_ward`, `employee_ward`, `sports_achiever`,
    `sports_ward`.
  - `everyone`, exactly one tier with no bound.
- `tiers`: best first. Score bounds strictly falling, rank bounds strictly
  rising. Only the last tier may have no bound (it then matches any value, as
  in AUJET "Rest").
- `award.kind` and its fields:
  - `pct_split` (`pct`)
  - `pct_first_sem` (`pct`)
  - `fixed_inr` (`amount`: a positive integer, or
    `{"external"?: int, "internal"?: int}` with at least one key)
  - `pct_capped_total` (`pct`, `cap_total`)
  - `pct_every_sem_capped` (`pct`, `cap_per_sem`)
  - `full_fee`
  - `described` (`text`)
- `conditions`: spoken English strings, may be empty.
- `committee_decides`, `verified`: JSON booleans (a string `"true"` is a load
  error).

### 13.3 Loading

`course_catalog.py` loads the file at import with
`check_scholarships(data, records)`. On any violation of 13.2 it raises
`ValueError` naming the scheme id and field, so app.py fails at startup, before
any call is dialled. It also checks that every record a scheme covers has at
least 3 entries in `per_semester` (the split needs semester 3). There is no
merge into `courses.json` and no rebuild. The
file is policy-wide, and which programs a scheme covers follows from
`degree`/`level`.

### 13.4 Tool

```
find_scholarships(
  program,                                         STRING, required
  qualifying_pct, auat_pct, cat_pct, mat_pct,      NUMBER, 0 to 100
  clat_score,                                      NUMBER, 0 to 120
  aujet_rank, jee_main_rank, wbjee_rank,           INTEGER, 1 or more
  situations: [studied_at_adamas_university, studied_at_adamas_school,
               sibling_enrolled, defence_ward, employee_ward,
               sports_achiever, sports_ward],
  ruled_out:  [any score, rank or situation name])
```

- `program` is resolved with `resolve()` (6.3). If it is `not_found` or
  `ambiguous`, the response is `{"status", "candidates"?, "more"?}`, as in
  `get_program_details`.
- `qualifying_pct` means 10+2 marks for UG and graduation marks for PG.
- Flag mapping:
  - `alumnus` matches `studied_at_adamas_university` or
    `studied_at_adamas_school`.
  - The other flag inputs match the situation with the same name.
  - `fixed_inr` with an internal/external amount uses `internal` when
    `studied_at_adamas_university` is given, otherwise `external`.

Matching, over the verified schemes that cover the program:
- `everyone` always matches its tier. A score or rank matches the first tier
  it meets. A flag matches its tier when the situation is given.
- A tier whose `fixed_inr` amount has no usable key (only `internal`, and the
  caller has not said they studied at Adamas University) does not match.

Rupees. Let `s1` = `per_semester[0]`, `s3` = `per_semester[2]`, and
`r(x)` = round half up to whole rupees. Every program in the catalog has at
least 3 semesters, and the loader checks this.

| kind | first | third | total |
|---|---|---|---|
| `pct_split` | r(pct/200 × s1) | r(pct/200 × s3) | first + third |
| `pct_first_sem` | r(v/2), v = r(pct/100 × s1) | v − first | v |
| `fixed_inr` | r(a/2) | a − first | a |
| `pct_capped_total` | r(t/2), t = min(pct_split total, cap) | t − first | t |
| `pct_every_sem_capped` | per semester: min(r(pct/100 × fee), cap) | (every semester) | sum over `per_semester` |
| `full_fee` | s1 + admission fee | 0 | first |
| `described` | none | none | none |

Response, when the program is found:
- `program`: the program name.
- `best`: the matched non-committee scheme with the highest total (ties go to
  file order). It carries:
  - `name`
  - `award`, spoken short form: "50%", "Rs 30,000", "10% every semester, up
    to Rs 5,000"
  - `first_semester`, `third_semester` (or `every_semester_up_to` for
    `pct_every_sem_capped`)
  - `total`
  - `conditions` (left out when empty)
  - Amounts are `inr()` strings.
- `also_qualifies`: the other matched non-committee schemes, `{"name",
  "total"}`, highest first, at most 3.
- `may_also_apply`: matched committee schemes, `{"name"}` only. Their
  conditions stay with the counselor, which keeps the worst case in budget.
- `ask_about`: `{"fact", "up_to"}`, at most 3, highest `up_to` first.
  - A fact qualifies when it is not given, not in `ruled_out`, and the top
    tier of some verified non-committee scheme covering the program would
    beat `best`'s total (or there is no `best`).
  - For `alumnus` the fact is `studied_at_adamas_university`, or
    `studied_at_adamas_school` once the first is ruled out.
  - A `fixed_inr` tier that needs `internal` while it is unknown adds
    `studied_at_adamas_university`.
  - One entry per fact, with its highest value.
- `rules`: the verified `general_conditions` texts, and `pitch_lines`: the
  verified pitch texts. Both are sent only when `best` exists.
- When there is no `best` (M.Pharm), the response is `program` plus
  `ask_about` / `may_also_apply` if any.

Errors (`{"error"}`, 6.4):
- `program` is missing or empty.
- A score or rank is not a number, is a boolean, or is out of range.
- `qualifying_pct` of 10 or less: `qualifying_pct looks like a CGPA; ask for the
  percentage` (final review; no tier starts below 60).
- An unknown situation or `ruled_out` name.
- An unknown argument.

`result_status` returns `best=<name key>` (e.g. `best=jee-main-scholarship`), `best=none`, the resolution status, or
`error` for the `[CATALOG]` line. app.py is unchanged: it picks the tool up
from `TOOL_SPECS` / `TOOL_NAMES`, and so does the playground.

Budgets (measured while planning): every response under 1800 characters, and
under 1500 when no `situations` are given; all four declarations under 4200
(the new one alone is about 1650).

### 13.5 Prompt (`prompt_au.txt`)

- A new `## SCHOLARSHIPS` section:
  - Scholarships come only from `find_scholarships`.
  - **Neha raises it herself** (operator, 2026-10-06): callers usually do not
    know Adamas gives scholarships. Once the program and the marks are known,
    she brings it up once per call, right after sharing fees or eligibility;
    a fee worry triggers it right away; a decline means she moves on and does
    not raise it again; a direct question always gets the tool call. Without a
    program, ask the program first.
  - **Marks first** (operator, 2026-10-06): call-flow step 4 asks for the marks
    or percentage, and Neha has `qualifying_pct` before the first call, so
    `ask_about` does not lead with unreachable rank-exam tiers.
  - Pass every fact already known. Ask the `ask_about` facts one at a time in
    plain words, then call again. Facts the caller says no to go in
    `ruled_out`.
  - **Present `best`:** name, first-semester and third-semester amounts,
    total, conditions. Say only the highest applies. Mention `also_qualifies`
    only if asked. For `may_also_apply`: "you can apply, a committee decides".
    Never promise it.
  - Close with the `pitch_lines`: the first-250 cap and the Rs 60,000 seat
    booking.
  - No `best`: a senior admission counselor will share the options. Never say
    "no scholarship for you".
  - AUAT is the Adamas University Admission Test, conducted by the university;
    the AUAT scholarship is based on its score.
- Line 29: exams are never an eligibility demand, but Neha may ask about JEE,
  WBJEE, CAT, MAT, CLAT, AUAT or AUJET for scholarships.
- Scholarships are removed from NOT AVAILABLE. The "never invent scholarships"
  rule stays.

### 13.6 Tests (stdlib unittest)

- `tests/test_find_scholarships.py` (new):
  - **Loader rejects:** an unknown key, input or kind; a wrong bound key for
    the input; unsorted tiers; an unbounded tier that is not last; a flag
    input with more than one tier; an unknown degree; a non-boolean
    `verified`; a duplicate id.
  - **The real file loads:** 21 schemes, all verified.
  - **Matching on a fake scheme set patched in:** every award kind's formula
    and rounding; internal vs external; committee never `best`; `ruled_out`;
    the `ask_about` rule, order and cap; the alumni fact fallback; `rules` and
    `pitch_lines` only with a `best`.
  - **Golden cases on the real data:**
    - B.Tech Civil + JEE rank 80,000: best `jee-main`, total 27,101;
      `ask_about` is `aujet_rank` (55,450), `wbjee_rank` (43,360),
      `sibling_enrolled` (40,000).
    - B.Pharm with no facts: Early Bird at 10,000.
    - M.Pharm: no `best`.
    - MBA + CAT 85: 100% of the first-semester fee.
    - MBA + AUAT 87, ruled out AUJET/CAT/MAT: best `auat-mba` at the external
      Rs 30,000; `ask_about` is `studied_at_adamas_university` (alumni, 96,001)
      then `qualifying_pct` (40,000).
    - Sibling on a 4-year program: Rs 5,000 every semester, summed.
  - **Budgets:** a sweep of every program against a set of fact
    combinations: each response under 1800 characters, and under 1500 when
    no situations are given.
  - **Errors:** each error case above. `result_status`.
- `test_course_catalog`: declaration budget 4200.
- `test_catalog_tool_handler`: tool order gains `find_scholarships` last.
- `test_prompt_catalog`:
  - names all four tools
  - the scholarship section exists and says only one applies
  - "Scholarships" is gone from NOT AVAILABLE
  - the exam rule allows scholarship questions

### 13.7 Rollout

1. Build; suite green (the data is already verified).
2. Text-only Gemini test: run the scholarship flow next to the eligibility flow.
3. Live calls: watch `[CATALOG] find_scholarships` status and chars. Edit
   `catalog/scholarships.json` when the policy changes; no rebuild is needed,
   and the loader catches mistakes at startup.
4. Deployment (section 12): `catalog/scholarships.json` travels with the rest
   of `catalog/`.
