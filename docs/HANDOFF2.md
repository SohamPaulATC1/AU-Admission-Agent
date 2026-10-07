# HANDOFF 2: Course catalog tools for Neha (2026-10-05)

This covers the work done on 2026-10-05 on top of commit `a0ec061`. Nothing
here is committed yet; it is all working-tree changes on branch `dev`.

## 1. Baseline and rollback

- `a0ec061` ("Data Extraction 1") is the version the operator confirmed works
  correctly on EC2, with the Vertex AI backend (`GEMINI_BACKEND=vertex`) and
  aec1 (`AEC_IMPL=aec1`) as defaults. Both were committed earlier in `edb0bf1`.
- Everything below is additive on top of it. Commit it on its own, so one
  revert brings back the EC2 version exactly.
- Before copying to EC2, back up the files EC2 runs now: `app.py`,
  `prompt.txt`, `tests/harness/appctl.py`. Rolling back on EC2 is then a copy.
- `docs/handoff1_ec2_report.md` is out of date: it stops at the smoke-test
  step, but the server has since run correctly on EC2.

## 2. What was built

Neha used to have fees and eligibility for the SoET programs typed into the
prompt. She now looks up any of the 87 programs of the 2027 session through
two Gemini function calls, answered locally from a JSON catalog. Structured
lookup, no RAG, no network.

| Tool | Use |
|---|---|
| `list_programs` | What is offered. Filters (combine freely): `level` (UG, PG, Diploma), `degree`, `school` (code), `keyword` (English subject words), `max_total_fee` (rupees). No arguments: program counts per school. Up to 20 matches come back as names; more come back as counts per degree. |
| `get_program_details` | 1 to 3 programs by name (the caller's words in English are fine). `fields`: any of `overview`, `eligibility`, `fees`, `fee_breakdown`, `seats`; default `overview`, `eligibility`, `fees`. |

Each program in a `get_program_details` result has a `status`:
- `found`: the record, with only the requested fields.
- `ambiguous`: up to 4 candidate names, plus `more` if there are others.
- `not_found`: nothing matched.

Bad arguments (unknown degree, school, field, argument, wrong types) return
`{"error": "..."}` with the allowed values, so Gemini can correct itself.

Fees are returned as Indian-grouped strings ("1,08,250"). `due_at_admission`
is precomputed (first semester fee plus the one-time admission fee of 43,800,
which includes the T-shirt and blazer), so the model never does arithmetic on
a call.

## 3. Files

New:
- `AU Admission Data/` (came in with `a0ec061`): the two source Excel files,
  the program notification and the national course fee sheet.
- `catalog/build_catalog.py`: builds `catalog/courses.json` from the two Excel
  files. Stdlib only (zipfile + ElementTree), since `requirements.txt` is
  frozen. Run by hand when either Excel changes:
  `venv312/Scripts/python.exe catalog/build_catalog.py`.
  `test_committed_courses_json_is_fresh` fails until you do.
- `catalog/courses.json`: the built catalog, 87 records. Committed, so the app
  never reads Excel at runtime.
- `catalog/aliases.json`: hand-kept short forms ("cse", "ece", "mlt", "ai ml",
  "mechanical engg", ...). Tune it from live `not_found` and `ambiguous`
  queries, then rebuild.
- `catalog/__init__.py`.
- `course_catalog.py`: runtime module. Loads the JSON once at import and fails
  loudly if it is missing or malformed. Holds the name resolution, both tool
  handlers, `TOOL_SPECS` (the declarations, with enums built from the data),
  `handle_tool_call`, `response_chars`, `result_status`.
- `tests/tool_playground.py`: offline tool tester (section 6).
- `prompt_au.txt`: the new prompt (section 5).
- Tests: `tests/test_catalog_build.py` (18), `tests/test_course_catalog.py`
  (34), `tests/test_catalog_tool_handler.py` (13), `tests/test_prompt_catalog.py`
  (7).
- `docs/spec/course-catalog-tools-design.md` (design),
  `docs/spec/course-catalog-tools-plan.md` (implementation plan).

Modified:
- `app.py`:
  - `import course_catalog`; the catalog declarations are appended to
    `LOCAL_GEMINI_TOOLS` after `endCall` and `transferCall`.
  - Tool-call loop: a catalog branch that answers in the same turn and sets no
    terminal state. Each call logs
    `🔎 [CATALOG] tool=... status=... chars=... ms=...`.
  - An unknown tool name now gets `{"error": "unknown tool <name>"}`. Before,
    it got no response at all, which leaves Gemini waiting.
  - After a non-terminal tool reply (catalog or unknown tool), the model
    response deadline is re-armed, so a model that goes silent after a lookup
    still ends with "Gemini response timeout". `endCall` / `transferCall`
    keep their own terminal deadline, and speech during the lookup still
    clears it through the deferred activityStart.
  - call_state keys `catalog_calls` / `catalog_chars`, and a per-call stats
    line `🔎 [CATALOG] calls=N chars=N`.
  - The dead, commented-out MCP block is removed.
  - Audio, AEC, Vertex and Plivo code are untouched.
- `tests/harness/appctl.py`: mirror of the two call_state keys, and the
  `[CATALOG]` diagnostic tag.
- `tests/test_preservation_4_4_tool_and_silence.py`: intentional re-baseline,
  an unknown tool now gets a `tool_response` before the deferred
  activityStart.
- `tests/test_preservation_4_8_resumption_recording_stats.py`: `GEMINI_MODEL`
  pins re-pinned to `[61, 1612, 2129, 2139, 2552]`.
- `CLAUDE.md`: current state, resume list and session log.

## 4. Catalog data rules

The two Excel files are joined 1-to-1 on UID. Any duplicate, unmatched or
blank UID, or a fee row whose sums do not add up, fails the build, except for
these operator-approved cases:

| Case | Handling |
|---|---|
| UID 133: B.Tech CSE (AI & ML), B.Tech AI & DS (Business Application), B.Tech AI & DS (FinTech) | All three share the single 133 fee row; each keeps its own eligibility. |
| UID 121: BBA, BBA (E-Commerce) | Two records, both with the BBA fee row. |
| UID 171: one row for M.Pharm (Pharmacology) and M.Pharm (Pharmaceutics), two fee rows | Split into two records with shared eligibility, each with its own fees. |
| UID 137: "with specialization in SAP" and "SAP add-on Certification" fee rows | The program takes the specialization row; the add-on (1 year, 65,000) is attached as an `add_on`. |
| Blank UID: M.Tech (VLSI & Embedded Systems), PG Diploma in Nuclear Medicine Technology | Excluded (incomplete data), listed in the build report. |
| Name differs between the two files (about 20 UIDs) | The program file name is canonical; the fee file name becomes an alias. |

Per-school counts: SoBAS 16, SoB 6, SoE 3, SoET 20, SoHMS 12, SoLJ 3,
SoLACS 14, SoLB 8, SoMC 4, SoSA 1.

Name resolution (`course_catalog.resolve`), in order:
1. An exact id, name or alias, across all degrees.
2. If the query contains a degree ("btech", "M.Sc"), only that degree's
   programs are searched, and an exact name or alias match is tried there.
3. Otherwise each remaining word is scored: a hit in the program's name or
   aliases counts 1, a hit only in its department counts 0.5.
4. `found` needs a score of at least 0.75, a lead of at least 0.2 over the
   runner-up, and every word must hit the name or aliases. A department-only
   word can raise a score but never makes a `found` on its own:
   "mtech computer science" is `ambiguous` (there is no M.Tech CSE), not a
   confident wrong M.Tech Data Science.
5. Otherwise candidates within 0.2 of the best (and at least 0.6) are
   returned as `ambiguous`; nothing left is `not_found`.

## 5. Prompt (`PROMPT_FILES/prompt_au.txt`)

Correction 2026-10-07: app.py loads `PROMPT_FILES/prompt_au.txt` directly (since
`feb4b29`), so it is the live prompt. `PROMPT_FILES/prompt.txt` is the old Senco
content, unused and not edited by this work. Nothing is copied by hand.

- Rewritten for the tools: every hard-coded fee and eligibility line and the
  SoET-only scope are gone; the opening pitch is now "2027 admissions at
  Adamas University".
- Kept: the Neha persona, language and tone rules, the call flow (greeting
  and language, consent, student or parent, academic profile,
  correction-aware program interest), counselor handoff and closing with
  `transferCall` / `endCall`, the end-of-call summary contents, the
  guardrails.
- Tool rules: which tool for which question; reuse data already fetched and
  fetch only a missing field; a one-line filler before a lookup; English
  arguments; how to handle `ambiguous`, `not_found` and `error` (never read
  an error aloud, retry once, then counselor follow-up); at most 5 names read
  from a list; `due_at_admission` explained; amounts spoken Indian style in
  English words; no ids or field names read aloud; entrance exams only when
  the eligibility text names one.
- Open-ended eligibility ("I got 58% with PCB, what can I apply for?"): ask
  which program or field, fetch its eligibility and read it out. Never say
  "you are eligible" or "you are not eligible".
- Not available on the call (scholarships, hostel fees, exact start dates,
  documents, form links): noted, and a senior admission counselor follows up.
- Shortened because the prompt is re-billed on every turn: 10,835 to 6,782
  characters, about 1,000 fewer tokens per turn. Only duplicates were removed
  (the bot refusal, the no-invention rule, the not-available list and the
  entrance-exam rule were each stated two or three times; the goal section
  repeated the role). The degree list was dropped because the `list_programs`
  declaration already carries it as an enum.
- Braces: app.py fills the prompt with `str.format(user_name=...,
  phone_number=...)`, so the prompt may contain no other `{` or `}`
  (`test_formats_like_app_py_does` guards this).
- Capping the conversation history (`ContextWindowCompressionConfig`
  `trigger_tokens` / `target_tokens`) was considered and declined by the
  operator for now.

## 6. Trying the tools offline

`tests/tool_playground.py` calls the same `course_catalog.handle_tool_call` as
app.py, so its output is exactly what Gemini receives. No network, no cost.

```
venv312/Scripts/python.exe tests/tool_playground.py                       # interactive
venv312/Scripts/python.exe tests/tool_playground.py list_programs degree=B.Tech
venv312/Scripts/python.exe tests/tool_playground.py list_programs max_total_fee=300000 level=UG
venv312/Scripts/python.exe tests/tool_playground.py get_program_details programs=cse,ece fields=fees
venv312/Scripts/python.exe tests/tool_playground.py get_program_details programs="data science"
venv312/Scripts/python.exe tests/tool_playground.py --declarations
```

Every reply ends with its size in characters and approximate tokens. Commas
split array arguments, so for a program name that contains a comma pass raw
JSON: `get_program_details "{\"programs\": [\"...\"]}"`.

## 7. Tests

Full suite: `PYTHONIOENCODING=utf-8 venv312/Scripts/python.exe -m unittest discover -s tests -t .`
Result: 323 OK, 2 skipped, 1 expected failure (251 before this work).

The new tests cover: the build and every join rule; name resolution (aliases,
degree narrowing, ambiguous and not-found cases, the department-only rule);
both tools' filters, fields and errors; response size budgets (any list
response under 1,000 characters, one program with default fields under
1,400, the declarations under 1,800); the app.py wiring (responses, counters,
log lines, call keeps running, deferred activityStart, deadline re-arm); and
the prompt (formats cleanly, school map matches the data, no fee figures,
reuse rule, error rule, eligibility rule).

## 8. Before and during live calls

Not yet done, in this order:
1. Commit this work on its own (operator decides when).
2. (Retired: no copy step. app.py reads `PROMPT_FILE`, default `PROMPT_FILES/prompt_au_v2.txt` since
   2026-10-07; `PROMPT_FILES/prompt_au.txt` is the rollback.)
3. Back up EC2's current files (section 1), then copy over: `course_catalog.py`,
   all of `catalog/` (the build test imports `build_catalog.py`), `app.py`,
   the `PROMPT_FILES/` folder (delete the old root-level `prompt.txt` and
   `prompt_au.txt` on EC2), `tests/harness/appctl.py`, and the new and changed tests.
   Run the suite on EC2.
4. Optional first step from Windows: a text-only Gemini session with
   `prompt_au.txt` and the real declarations, typed questions, no Plivo, to
   check tool choice and answers before a phone call. The script is written in
   `TEST_FILES/text_chat.py`. It uses AI Studio (via GOOGLE_API_KEY from .env)
   and defaults to `gemini-3.8-flash`.
   Run: `venv312/Scripts/python.exe TEST_FILES/text_chat.py`
5. 3 to 5 live calls on EC2. Ask about a fee, an eligibility, a list, a
   follow-up needing a field not fetched yet, a program that does not exist
   ("MBBS"), an ambiguous one ("data science"), the 58% PCB question, a
   counselor request, and one question in Bengali. Include a speakerphone call
   and one call longer than 10 minutes.

What to watch in the logs:
- `🔎 [CATALOG] tool=... status=...` per lookup, and `[CATALOG] calls= chars=`
  in the call stats.
- `[MODEL] ... backend=vertex`, `[AEC]` and `[AEC-GUARD]`, as before.
- In transcripts: right tool picked, no figure that a tool did not return, no
  field names or errors read aloud, a thorough end-of-call summary.

Rollback if a live call regresses: copy back the backed-up EC2 files. For the
backend or the canceller alone, `GEMINI_BACKEND=studio` or `AEC_IMPL=aec`.

## 9. Known minor items (not fixed)

- The tool-call `except` log line still says "Error executing MCP tool".
- `tests/tool_playground.py` splits `key=value` arrays on commas (raw JSON
  works around it).
