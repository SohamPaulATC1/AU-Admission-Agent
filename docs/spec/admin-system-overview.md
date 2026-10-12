# Admin system: overview and decisions

Status: agreed with the operator 2026-10-12. Each step below gets its own design doc, plan and
implementation; this file records the decisions they share.

## Goal

The agent's URL goes to another team in the organisation, who run the calls (single calls and bulk
contact lists). The operator (owner) gets an admin panel with call costs, transcripts, lead results
from a post-call NLP step, and campaign reports. Calls are judged afterwards by Claude Haiku 5.5 on
Bedrock (`bedrock_summary.py` already reaches it), most importantly whether the contact is a
prospective student.

## Roles and pages

Two shared logins, both from the environment (`.env`, and `/etc/au-app/common.env` on EC2):

| Role | Sees |
|---|---|
| operator (the calling team) | `/` calls page: single calls, bulk upload and campaigns (step 3), live status, history. No costs. |
| admin (the owner) | everything the operator sees, with costs, plus `/admin`. |

Plus `/login` and `/logout`. Everything else is closed by default (step 1).

## Storage: SQLite, not DynamoDB

- One writer: the main process (port 8000) already owns `call_history.db`; the media processes
  (8001-8003) forward their events to it. All new tables stay in this database and keep that rule.
- The admin panel is filters, sorts and totals ("hot leads in campaign X last week", "cost per hot
  lead"). That is SQL. DynamoDB has no ad-hoc queries (each filter needs a designed index or a scan)
  and adds IAM and per-request billing, for a volume of thousands of calls a month on one box.
- WAL mode. Storage code stays in one module, so a later move is contained.
- If the system ever outgrows one EC2 box, the move is Postgres on RDS plus S3 for transcripts, not
  DynamoDB. Not built now.
- Open, optional: a nightly copy of the database to S3 (an EBS failure loses it today).

## Build order

1. **Login and protection** (`docs/spec/admin-auth-design.md`): logins, default-deny routes,
   Plivo webhook signatures, signed `/media-stream` URLs. Before the URL is handed over. Live on EC2 2026-10-12 (commit f55e5c4, enforce).
2. **Data model and the post-call NLP step**, including every cost part.
3. **Bulk campaigns and the dispatcher**, on the calls page.
4. **Admin panel**: overview, calls and leads table, call detail with transcript, campaign reports,
   Excel/CSV export.

## Decisions for step 2 (to be designed there)

- **Post-call pipeline:** call ends, transcript written (in whichever process ran the call), a
  `transcript_ready` event goes to the main process, a Haiku worker there (bounded concurrency,
  retries) runs the analysis and writes the database. On startup the worker picks up calls that have
  a transcript but no analysis. Re-run from the admin panel.
- **Transcript cost leaves the transcript file** (operator, 2026-10-12). Today `call_transcript.py`
  ends the file with a `transcription_cost_usd` line (`cost_line`). Instead the job reports its
  tokens and cost with the `transcript_ready` event, the main process stores them, and the admin
  panel reads them from the database. The file keeps only the conversation and its header.
- **Cost per call, four parts:** Gemini Live (already computed in `log_call_stats`), transcript STT
  (`call_transcript.py`), Haiku NLP (Bedrock token usage), Plivo telephony (Plivo's call record,
  `total_amount`, after hangup; missing today and likely a large share). Admin sees each part, the
  total, and cost per lead.
- **Haiku output is structured** (tool use with a JSON schema), one database column per field.
  Draft fields:
  - `lead_status`: `hot` (visit day fixed or strong intent), `warm`, `cold`, `not_interested`,
    `callback_requested`, `wrong_number`, `no_conversation` (voicemail, hung up at the greeting,
    silence), `transferred`.
  - `is_prospective`: yes / no / unclear, with a one-line reason and a short quote as evidence.
  - Who answered (student / parent / other); applicant name if it differs from the contact.
  - Qualification, board, marks %, stream/subjects, entrance exams and scores.
  - Programs of interest, primary program, school (see below).
  - Scholarship discussed and the rupee amount quoted.
  - Campus visit agreed y/n and the day; callback requested y/n and the preferred time.
  - Objections (fee, distance, parent decides, other college, other).
  - Interest score 1-5, call language, a 2-3 line English summary, next action for the admission team.
  - Optional quality flags for prompt tuning: invented a fact, repeated a question, ignored a question.
  - Gemini's own `summary_of_whole_call` (from `endCall`) is extra input, not the source of truth: a
    caller who hangs up never triggers `endCall`.
- **School:** the catalog has 10 schools (not 12), and every program in `catalog/courses.json`
  carries `school: {name, code}`. So:
  1. Haiku picks `programs_of_interest` / `primary_program` from the catalog's exact program names
     (an enum in the schema, about 1k tokens), not free text.
  2. Code looks the school up from the record (exact name), Haiku never names the school.
  3. Programs Neha fetched with a `found` `get_program_details` on the call are recorded and used as
     a cross-check and fallback.
  4. Only a degree or a vague field ("B.Tech", "something in science"): school left blank with a
     note. Several programs: school of `primary_program`, all schools kept in a list.
  Loose names still go through `course_catalog.resolve()`.
- **Data residency, open:** `us.anthropic.claude-haiku-5-5` is a US cross-region inference profile,
  so transcripts are processed in the US. The organisation must accept that, or step 2 uses an
  APAC/India profile if one serves Haiku 5.5.
- **Dependencies:** `boto3` is not in `requirements.txt` (frozen per CLAUDE.md); step 2 needs it
  added with the operator's approval. Excel reading reuses the stdlib reader in
  `catalog/build_catalog.py` (no openpyxl).

## Decisions for step 3 (to be designed there)

- Contact queue stored in SQLite, so a restart resumes it.
- Dispatcher in the main process: at most N calls at once (Plivo caps at 10, target 5-8), calls
  started spaced out (Plivo's calls-per-second limit). A slot is freed by Plivo's hangup webhook
  (which always reaches the main process), with a timeout as a backstop for a lost event.
- Number validation (10-digit Indian), duplicates within a campaign and against recent calls, a
  do-not-call list, calling hours (for example 09:00-21:00 IST, TRAI), retries for no-answer and busy.
- Open: whether the operator sees lead results for their campaigns.
