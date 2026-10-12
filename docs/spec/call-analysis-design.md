# Step 2: call analysis, data model and full call cost

Status: design approved by the operator 2026-10-12 (all four parts). Implemented and deployed on EC2 2026-10-12 (plan `docs/spec/call-analysis-plan.md`, report `docs/handoff_step2_report.md`); live call pending. Shared decisions:
`docs/spec/admin-system-overview.md`. Step 1 (logins) is live; step 4 (admin panel) reads what this
step stores.

## Goal

After every call, one structured record in `call_history.db`: the lead result from Claude Haiku 5.5
(is this a prospective student, lead status, academics, program and school, scholarship, visit,
callback, objections, summary) and every cost part of the call (Gemini Live, transcript STT, Haiku,
Plivo telephony). Nothing in the call path waits for it.

## Decisions (operator, 2026-10-12)

- Haiku runs in the US: model `us.anthropic.claude-haiku-5-5` (US cross-region inference profile on
  Amazon Bedrock), the id `bedrock_summary.py` already uses.
- Client: the official Anthropic Python SDK's Bedrock client, `anthropic[bedrock]` added to
  `requirements.txt` (lifts the freeze for this package). `AsyncAnthropicBedrock` (the InvokeModel /
  `bedrock-runtime` path, which serves Haiku 5.5), AWS keys from the environment
  (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_DEFAULT_REGION`).
- The analysis runs in a worker in the main process (port 8000), the only database writer.
- Old transcripts already in `TRANSCRIPTS/` are analysed once on first start.
- All fields below are kept; `interest_score` is 1-10. Columns can be filtered later for the
  calling team.
- The transcript cost line leaves the transcript file and goes into the database.

## Facts this design relies on

- Plivo's `request_uuid` (returned by `calls.create`, used for the `call_queued` row) equals the
  `CallUUID` of the stream for our single-destination outbound calls (3 of 3 in the local log), so
  one `call_uuid` keys a call everywhere.
- Plivo's hangup callback does not reliably carry a cost (its docs disagree). The call record
  (`plivo_client.calls.get(call_uuid)`, field `total_amount`) does. A transfer's human leg is billed
  as its own call; its id arrives at `/dial-action` as `DialBLegUUID` (empty if not answered). The
  currency is not stated (expected USD, the account currency); check against the first bill.
- Haiku 5.5: $0.10 / $0.50 per million input / output tokens (prompts up to 100K) at Anthropic's
  rates; Bedrock's US regional route adds 10%. Structured outputs (`output_config.format`) work on
  Bedrock for Haiku 5.5. Thinking is on by default (`effort` default `medium`); a safety decline
  (`stop_reason: "refusal"`) has no server-side fallback on Bedrock. Usage tokens are in every
  response.
- A transferred call that returns runs `handle_media_stream` twice with the same `call_uuid`: two
  `call_ended` events, two transcript jobs (the file gets a second segment). Today the second
  `call_ended` overwrites the first leg's `cost` in the database.

## 1. Database (`call_history.db`, written only by 8000)

### `calls`: new columns

Added at startup with `ALTER TABLE ... ADD COLUMN` when missing (existing rows untouched, idempotent):

| Column | Type | Filled by |
|---|---|---|
| `gemini_cost_usd` | REAL | sum over legs of `call_ended.cost_usd` |
| `legs` | INTEGER | count of `call_ended` events |
| `stt_cost_usd` | REAL | sum over `transcript_ready.stt_cost_usd` |
| `nlp_cost_usd` | REAL | sum over Haiku runs for the call |
| `plivo_cost_usd` | REAL | A leg + human legs from Plivo call records |
| `plivo_b_legs` | TEXT | JSON list of `DialBLegUUID`s seen at `/dial-action` |
| `plivo_cost_status` | TEXT | `pending`, `ok`, `failed` |
| `transcript_path` | TEXT | last `transcript_ready.path` |
| `programs_looked_up` | TEXT | JSON list of catalog names Neha fetched with `found` (union over legs) |

The existing `cost` text (shown on the calls page for admin) becomes `$` + the summed Gemini cost, so
a transfer no longer loses its first leg.

### `call_analysis`: new table, one row per `call_uuid`

| Column | Type | Notes |
|---|---|---|
| `call_uuid` | TEXT PRIMARY KEY | |
| `status` | TEXT | `pending`, `ok`, `failed`, `skipped` |
| `error` | TEXT | reason when `failed` / `skipped` (never transcript text) |
| `attempts` | INTEGER | |
| `lead_status` | TEXT | `hot`, `warm`, `cold`, `not_interested`, `callback_requested`, `wrong_number`, `no_conversation` |
| `is_prospective` | TEXT | `yes`, `no`, `unclear` |
| `prospective_reason` | TEXT | one line |
| `prospective_evidence` | TEXT | short quote from the call |
| `interest_score` | INTEGER | 1-10 |
| `respondent` | TEXT | `student`, `parent_guardian`, `other`, `unknown` |
| `applicant_name` | TEXT | only if different from the contact name |
| `qualification`, `board` | TEXT | |
| `marks_pct` | REAL | |
| `subjects` | TEXT | JSON list |
| `entrance_exams` | TEXT | JSON list of `{"exam", "result"}` |
| `programs_of_interest` | TEXT | JSON list of exact catalog names |
| `primary_program` | TEXT | exact catalog name |
| `school_code`, `school_name` | TEXT | from the catalog record (code, not Haiku) |
| `scholarship_discussed` | INTEGER | 0/1 |
| `scholarship_name` | TEXT | |
| `scholarship_amount_inr` | INTEGER | |
| `campus_visit_agreed` | INTEGER | 0/1 |
| `campus_visit_day` | TEXT | |
| `callback_requested` | INTEGER | 0/1 |
| `callback_time` | TEXT | |
| `objections` | TEXT | JSON list from `fee`, `distance`, `parent_decides`, `other_college`, `not_now`, `other` |
| `language` | TEXT | `en`, `bn`, `hi` |
| `summary` | TEXT | 2-3 lines, English |
| `next_action` | TEXT | one line for the admission team |
| `agent_issues` | TEXT | JSON list from `invented_fact`, `repeated_question`, `ignored_question`, `wrong_language` |
| `transferred` | INTEGER | 0/1, from code (`legs > 1` or a B leg) |
| `model` | TEXT | |
| `input_tokens`, `output_tokens` | INTEGER | last run |
| `analyzed_at` | TEXT | IST, `YYYY-MM-DD HH:MM:SS` like the rest of the table |
| `raw_json` | TEXT | Haiku's reply as returned, so a field can be re-derived without a new call |

Code: new module `call_store.py` (schema upgrade, the new writes, the reads step 4 will need). The
existing `init_call_history_db` / `persist_call_event_sync` / `/api/call-history` stay in app.py;
`persist_call_event_sync`'s `call_ended` branch calls into `call_store` for the sums.

## 2. Pipeline

1. **Call end (any process).**
   - `log_call_stats` adds `cost_usd` (float) and `programs_looked_up` (list) to the `call_ended`
     event data. `programs_looked_up` comes from a new `call_state["catalog_found"]` (names from
     `get_program_details` results with `status == "found"`); mirrored in `tests/harness/appctl.py`.
   - `call_transcript.write_transcript` writes the file without the cost line (`render_file` no
     longer appends `cost_line`) and returns a result dict: `path`, `stt_status`, `audio_in`,
     `text_in`, `output` tokens, `stt_cost_usd`, `language`, `user_turns`.
   - `call_transcript.start_job(..., on_done=callback)` calls `on_done(result)` after the file is
     written; app.py passes a callback that emits `transcript_ready` (through `emit_call_event`, so it
     is forwarded to 8000 from a media process). The callback never raises into the job.
   - A call with no transcript turns (no job today) emits `transcript_ready` with `path: null`,
     `user_turns: 0` from app.py, so it still gets an analysis row.
2. **8000 receives `transcript_ready`.** `call_store` adds the STT cost and sets `transcript_path`,
   then the analysis is queued. `user_turns == 0` or a file with no `User` lines: row set to
   `status=ok`, `lead_status=no_conversation`, everything else empty, no Haiku call, cost 0. A second
   `transcript_ready` for the same call (transfer return) re-queues it; the new run reads the whole
   file and replaces the row; its cost is added to `nlp_cost_usd`.
3. **Worker** (`call_analysis.py`, started in 8000 only, i.e. when `EVENT_FORWARD_URL` is empty):
   - An `asyncio.Queue` of `call_uuid`s, at most `ANALYSIS_CONCURRENCY` (2) requests at once; a call
     already queued is not queued twice.
   - SDK retries (`max_retries=4`) cover 429 / 5xx / connection errors. On a final error: up to 3
     attempts of our own, 30 s and 120 s apart; then `status=failed`, `error` set.
   - Database writes go through `run_in_executor`, as the existing persistence does.
4. **Plivo cost (8000).**
   - `/plivo-hangup` (every hangup, not only failures) schedules `fetch_plivo_cost(call_uuid)` after
     60 s; retries at +3 min and +10 min while `total_amount` is missing. Sets `plivo_cost_status`.
   - `/dial-action` appends a non-empty `DialBLegUUID` to `plivo_b_legs`.
   - The A leg's and every B leg's `total_amount` are summed into `plivo_cost_usd`. The Plivo REST call
     runs in `asyncio.to_thread` (the SDK is synchronous).
   - The raw call record of the first fetched call is logged once at INFO (no phone numbers) to
     confirm the field names and currency live.
5. **Startup (8000 only).**
   - Every `TRANSCRIPTS/*.txt` whose `call_uuid` has no `call_analysis` row with `status` `ok` or
     `failed` is queued (the one-time catch-up of old calls, then a restart-safety net). For a file
     whose last line is an old `transcription_cost_usd:` line and whose `calls.stt_cost_usd` is empty,
     that cost is read once into `stt_cost_usd`. The analysis ignores that line.
   - Every call ended in the last 24 h with `plivo_cost_status` empty or `pending` gets a Plivo fetch.
6. **Kill switch.** `ANALYSIS_ENABLED=0`: no Haiku calls and no worker (rows stay `pending`);
   transcripts, costs and Plivo fetches carry on. `PLIVO_COST_ENABLED=0` stops the Plivo fetches.

## 3. The Haiku request (`call_analysis.py`)

- Client: `anthropic.AsyncAnthropicBedrock()` created once in the worker (region and keys from the
  environment). `import anthropic` happens inside the worker start, so the media processes never load
  it.
- Request: `messages.create(model=ANALYSIS_MODEL, max_tokens=4000, output_config={"effort": "low",
  "format": {"type": "json_schema", "schema": SCHEMA}}, system=SYSTEM_PROMPT, messages=[user])`.
  No sampling parameters (non-default values are a 400 on Haiku 5.5), no prefill.
- System prompt (fixed text, in English): the analyst role; what each `lead_status` and
  `is_prospective` value means (`hot` = a campus visit day fixed or a clear intent to apply; `warm` =
  interested, no commitment; `cold` = little interest; `callback_requested` = asked to be called back
  and nothing more decided; `wrong_number` = not the applicant's family or unrelated);
  `interest_score` 1-10; use only what was said on the call, leave a field empty (null) when it was
  not stated, never guess; programs only from the given list; English output whatever the call
  language; `agent_issues` only for clear cases.
- User message: the contact name (from `calls.name`), the call length, Gemini's end-of-call summary
  if any (`calls.end_reason`), the 87 catalog program names, then the transcript file (header and
  turns).
- Schema: every field in the table above except the code-filled ones (`school_*`, `transferred`,
  `status`, cost and token columns). Choice fields are `enum`s; `programs_of_interest` items and
  `primary_program` are `enum`s of the exact catalog names (`course_catalog` records); unknown values
  are `null`. `additionalProperties: false`, all fields `required`. The real-call spike (section 4)
  confirms Bedrock accepts this schema (nullable fields, an 87-value enum); if it rejects a construct,
  the smallest change that keeps the field (for example `""` instead of `null`) is used and recorded.
- After the reply:
  - `stop_reason == "refusal"`: `failed`, `error="refusal: <category>"`.
  - `stop_reason == "max_tokens"`: `failed`, `error="max_tokens"`.
  - Otherwise parse the first text block as JSON; on a parse error, `failed`.
  - School: `primary_program` -> its catalog record -> `school.code`, `school.name`. If
    `primary_program` is null, the first of `programs_looked_up` gives the school (logged as
    `school_from=lookup` on the `[ANALYSIS]` line; `primary_program` itself stays null); if neither,
    the school stays empty.
  - Cost: `input_tokens * NLP_PRICE_INPUT + output_tokens * NLP_PRICE_OUTPUT` per million.
- Logging: one line per run, `🧠 [ANALYSIS] status=<s> lead=<lead_status> prospective=<v> tokens=<in>/<out>
  cost=$<x> latency=<s>s attempts=<n> school_from=<haiku|lookup|none>` (tag added to `DIAGNOSTIC_LOG_TAGS`). Never transcript text,
  names or phone numbers. `💰 [PLIVO-COST] status=<s> total=$<x> legs=<n>` for the Plivo fetch.

### Configuration

| Variable | Default | Notes |
|---|---|---|
| `ANALYSIS_ENABLED` | `1` | kill switch |
| `ANALYSIS_MODEL` | `us.anthropic.claude-haiku-5-5` | |
| `ANALYSIS_CONCURRENCY` | `2` | |
| `NLP_PRICE_INPUT`, `NLP_PRICE_OUTPUT` | `0.11`, `0.55` | USD per million tokens (Anthropic price + 10% US route); check the first AWS bill |
| `PLIVO_COST_ENABLED` | `1` | |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_DEFAULT_REGION` | (none) | read by the SDK; in `/etc/au-app/common.env` on EC2. Missing keys: the worker logs one error at start and marks runs `failed` (`error="aws credentials"`); calls are unaffected |

## 4. Testing and rollout

### Offline (stdlib unittest; fake Bedrock client, fake Plivo client, temp database)

- Schema upgrade: adds the columns to an existing `calls` table, idempotent, rows untouched;
  `call_analysis` created.
- Events: `call_ended` twice sums `gemini_cost_usd` and `legs` and keeps the `cost` text in step;
  `transcript_ready` sums STT cost; `programs_looked_up` union.
- Transcript: the file has no cost line; `write_transcript` returns the result dict; `on_done` is
  called once, and a raising callback does not break the job; the no-turns path emits
  `transcript_ready`.
- Worker: `no_conversation` without a Haiku call; a good reply fills every column and the school;
  null `primary_program` falls back to `programs_looked_up`; refusal, max_tokens and bad JSON become
  `failed`; retry then success; retry exhaustion; concurrency limit; no duplicate queueing; a second
  `transcript_ready` replaces the row and adds the cost.
- Startup: old files queued once, old cost line read once, finished rows skipped.
- Plivo: A leg + B legs summed; missing `total_amount` retried; final failure sets `failed`;
  `/dial-action` stores `DialBLegUUID`.
- Media processes never import `anthropic` (checked by source and by `sys.modules` after importing
  app with `EVENT_FORWARD_URL` set).
- Existing suite stays green; new `call_state` key mirrored; 4_8 pins re-pinned.

### Real-call spike (before deploy, run on EC2 by the operator)

`TEST_FILES/_analysis_spike.py` (gitignored): one Haiku call on an existing transcript with the real
system prompt and schema; prints the parsed result, stop reason and tokens. About $0.001. Confirms the
model id, AWS permissions, the schema on Bedrock and the token counts. Any schema change it forces
goes back into the code before deploy.

### Deploy

1. `venv/bin/pip install "anthropic[bedrock]"` on EC2 (once).
2. AWS keys and `AWS_DEFAULT_REGION` in `/etc/au-app/common.env`.
3. `git pull`, restart `au-app@800{0,1,2,3}` when no call is live.
4. Startup log: the worker started (8000 only) and how many old transcripts it queued.
5. One live call: the `call_analysis` row, the four costs on the `calls` row, the `[ANALYSIS]` and
   `[PLIVO-COST]` lines. Compare the Plivo amount and currency with the Plivo console once.

## Out of scope

- The admin panel and any read API (step 4). Until then, rows are read with a one-line `sqlite3` /
  Python query on EC2.
- Showing lead results to the operator (decided in step 3 or 4).
- Bulk campaigns (step 3).
- Re-run from the admin panel (step 4 uses this step's queue).
