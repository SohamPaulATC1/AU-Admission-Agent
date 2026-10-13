# Step 4: the admin panel

Status: design approved by the operator 2026-10-13 (six scope answers, then four design sections, in
chat). Spec approved; plan `docs/spec/admin-panel-plan.md`. Built and deployed on EC2 2026-10-13
(report `docs/handoff_step4_report.md`). Shared decisions:
`docs/spec/admin-system-overview.md`. Steps 1 (logins), 2 (call analysis, full cost) and 3 (campaigns)
are built; this step reads their data and adds the admin-only views. Resume notes:
`docs/resume_step4.md`.

## Goal

The owner (admin login) sees what the calls cost and what they produced, in one place: totals and
cost per call and per hot lead, a filterable table of calls and leads, one call in full (transcript,
every analysis field, the four costs), a funnel and cost for each campaign, and the do-not-call list.
Every table exports to CSV. The operator login (the calling team) is unchanged: it never sees a cost
and has no new pages.

## Decisions (operator, 2026-10-13)

1. **Scope: five pages** (Overview, Calls & leads, Call detail, Campaign reports, Do-not-call), each
   table with a CSV export. No charts.
2. **Who:** only the admin login uses `/admin`. An operator gets 403 on every new API route and is
   sent to `/` from the page (existing `ADMIN_PAGE` behaviour). `web_auth` logic does not change.
3. **Export:** CSV only, UTF-8 with a BOM so Excel shows Bengali text. No `.xlsx`.
4. **Counting:** hot lead = `lead_status = 'hot'`; warm lead = `'warm'`; visits fixed = `campus_visit_agreed = 1`
   (separate count, it overlaps with hot); calls = every call in the range; connected = `connected_at`
   set; cost per call = total cost / calls; cost per hot lead = total cost of all calls in the range /
   hot count (shown as a dash when there is no hot lead). A call's cost is COALESCE(gemini_cost_usd,
   the old cost text) + STT + Haiku + Plivo, so an old call that the step 2 catch-up analysed still
   counts its Haiku and Plivo cost.
5. **Re-run analysis:** one call at a time from Call detail, plus "Re-run all failed" on the Calls
   table, at most 50 per click. A re-run overwrites the analysis fields and adds the run's cost to the
   call's Haiku cost (the totals then match the Bedrock bill).
6. **Date range:** default the last 7 days (IST); quick buttons Today, 7 days, 30 days, This month,
   All time; custom from/to; the range lives in the page URL.
7. **Build:** one `admin.html` with tabs (hash routing), JSON APIs under `/api/admin/`, SQL in a new
   `admin_data.py`. Not five pages, not server-rendered.

## Facts this builds on

- `calls` (created in app.py `init_call_history_db`, columns added by `call_store.upgrade`):
  `call_uuid, name, phone, status, fail_reason, cost` (old text, `$0.1404`), `end_reason, created_at,
  updated_at, duration_sec, connected_at, gemini_cost_usd, legs, stt_cost_usd, nlp_cost_usd,
  plivo_cost_usd, plivo_b_legs, plivo_cost_status, transcript_path, programs_looked_up`. Times are IST
  text, `YYYY-MM-DD HH:MM:SS`, so a date range is a string comparison.
- `call_analysis` (one row per call, joined on `call_uuid`): `status` (`pending`, `ok`, `failed`,
  `skipped`), `error`, `lead_status` (hot, warm, cold, not_interested, callback_requested,
  wrong_number, no_conversation), `is_prospective`, `interest_score`, `respondent`, academics,
  `primary_program`, `school_name`, `scholarship_*`, `campus_visit_agreed`, `campus_visit_day`,
  `callback_*`, `objections`, `language`, `summary`, `next_action`, `agent_issues`, `analyzed_at`,
  token counts. List fields are JSON text, yes/no fields 0/1.
- `campaigns`, `campaign_contacts` (joined to a call by `call_uuid`, which holds the contact's last
  attempt), `do_not_call (phone PRIMARY KEY, added_at, note)`. Campaign numbers are normalised to 10
  digits by `campaigns.normalise_phone`.
- nginx sends everything except `/media-stream` to 8000, so every admin request reaches the process
  that owns the `Analyzer` and does all database writes.
- `web_auth.ENDPOINT_ACCESS` is default deny; a test keeps it equal to `app.url_map`. `ADMIN_API`
  gives an operator 403. The admin sees costs (`without_cost` is for the operator only).
- `call_store.run(db_path, fn, ...)` runs a sync function on a fresh connection in the thread pool.
  `app.json_body()` is the JSON-only guard for POSTs (CSRF), `campaign_error` shows the error reply
  shape: `{"status": "error", "message": ...}`.
- `Analyzer.enqueue(call_uuid, path)` dedupes queued calls; `analyze` falls back to the stored
  `transcript_path` when `path` is `None`, and serialises runs per call with a lock.

## 1. Data and queries: `admin_data.py` (new)

Plain sync functions taking an open connection (the `call_store.run` pattern). No SQL is built from
request text: values are bound parameters and filter values are checked against allow-lists first.

**Range.** `from` and `to` are IST dates. `calls.created_at >= 'from 00:00:00'` and `< 'to+1 day
00:00:00'`. No range given: last 7 days including today. `all=1`: no filter. The response echoes the
effective range.

**Overview** (one `calls LEFT JOIN call_analysis` pass):
- counts: calls, connected, hot, warm, visits fixed;
- cost: Gemini, STT, Haiku, Plivo sums, and total; cost per call; cost per hot lead;
- **calls before step 2** have `gemini_cost_usd IS NULL`; their `cost` text is parsed in SQL
  (`CAST(REPLACE(cost, '$', '') AS REAL)`) and reported as one separate line, "Earlier calls (no
  split)", which is included in the total. The four parts stay honest;
- notes: analyses `pending` and `failed` (counts), calls with `plivo_cost_status` pending or empty on
  an answered call.

**Calls & leads.** 50 rows per page, newest first. Row: time, name, phone, lead status, primary
program, school, visit day, interest score, total cost of the call, analysis status, `call_uuid`.
Filters: lead status, analysis status, visit yes/no, primary program, school, campaign id, and a
search on name or phone (substring, `LIKE` with escaped wildcards). The response carries the distinct
programs, schools and campaigns (id, name) so the page can fill its dropdowns. The same function
without the paging feeds the CSV. A call's cost is the sum of its four parts (null parts count as 0),
or the parsed `cost` text when `gemini_cost_usd` is null.

**Call detail.** The `calls` row, the whole `call_analysis` row (lists decoded), the four costs, the
campaign it belongs to (through `campaign_contacts.call_uuid`, absent for calls from the calls page),
and the transcript text. The transcript is read from `transcript_path` only if the resolved path is
inside the `TRANSCRIPTS/` directory; otherwise or if the file is missing it is `null`.

**Campaign reports.** One row per campaign created in the range: name, state, created, contacts,
dialed (`dialed_at` set), reached (its call has `connected_at`), hot, visits fixed, total cost (sum of
the four parts, or the parsed text, over the calls behind `campaign_contacts.call_uuid`) and cost per
hot lead. Limit: `call_uuid` holds only the contact's last attempt, so an earlier unanswered attempt
is not counted; those cost close to nothing.

**Do-not-call.** List (newest first, optional search), add and remove. A number is normalised with
`campaigns.normalise_phone`; one that does not normalise is rejected. Adding a number that is already
listed keeps the first row and updates its note. Removing an absent number is a 404.

**Index.** `CREATE INDEX IF NOT EXISTS calls_created_at ON calls (created_at)`, created by an
`upgrade(conn)` in `admin_data.py` that app.py calls beside the other two. Safe on every start in
every process.

## 2. API, CSV, access, re-run

All routes are new and have an `ADMIN_API` entry in `web_auth.ENDPOINT_ACCESS`. The page route
`/admin` already exists.

| Route | Purpose |
|---|---|
| `GET /api/admin/overview` | KPIs and costs for a range |
| `GET /api/admin/calls` | one page of the table, with filters and dropdown values |
| `GET /api/admin/calls.csv` | same filters, no paging |
| `GET /api/admin/calls/<call_uuid>` | call detail with transcript |
| `POST /api/admin/calls/<call_uuid>/rerun` | re-run one analysis |
| `POST /api/admin/rerun-failed` | re-run failed analyses |
| `GET /api/admin/campaigns` and `.csv` | campaign reports |
| `GET /api/admin/dnc` and `.csv` | do-not-call list |
| `POST /api/admin/dnc` | add `{phone, note}` |
| `POST /api/admin/dnc/remove` | remove `{phone}` |

The overview has no CSV (it is not a table). POSTs go through `json_body()` (JSON only).

**Validation.** `from`/`to` must be `YYYY-MM-DD` and `from <= to`; `page` an integer, clamped to
1..10^6 (a huge OFFSET overflows SQLite, as in the campaign route); filter values must be in the
allow-lists; anything else is 400 with `{"status": "error", "message": ...}`.

**CSV.** `text/csv; charset=utf-8`, a BOM first, `Content-Disposition: attachment` with the range in
the filename. Capped at 20,000 rows; when cut, the last line says so. Formula injection: any cell
starting with `=`, `+`, `-` or `@` gets a leading `'`, except a cell that is only an optional `+` and
digits (a phone number). Names and extra columns come from uploaded Excel files, so this matters.
The calls CSV is built in the thread pool with its query (8000 also carries live audio).

**Re-run.**
- `Analyzer` gets a public `is_busy(call_uuid)` (queued or running); today that state is private.
- Single: 404 for an unknown call; 503 when analysis is off (`ANALYSIS_ENABLED=0`, no analyzer); 409
  when `is_busy`; else `enqueue(call_uuid, None)` and 202. The call's `call_analysis` row is not
  touched until the run finishes.
- All failed: calls with analysis `status = 'failed'`, newest first, across all dates, at most 50,
  each through the same `is_busy` check. Skipped analyses are not retried (a missing transcript will
  not appear). The reply says how many were queued and how many failed calls remain.
- The success path is `Analyzer.analyze` unchanged: fields replaced, cost added.
- A call is re-run only when its transcript file exists (else 409 "No transcript to analyse");
  "Re-run all failed" skips such calls and reports them as no_transcript. Without the file the worker
  would save no_conversation over a good analysis. The queued path is the checked file, not `None`.
  (EC2 build: the file must also lie inside `TRANSCRIPTS/` and be non-empty, report D4.)

## 3. The page: `admin.html`

One file in the style of `campaigns.html` (same CSS variables and nav: Calls, Campaigns, Logout;
vanilla JS, no library). Every server value is set with `textContent`, never `innerHTML`: names, extra
columns and the transcript are caller or uploaded content.

Tabs by URL hash: `#overview`, `#calls`, `#call/<uuid>`, `#campaigns`, `#dnc`; an unknown hash is
Overview. The range bar (quick buttons, two native date inputs, Apply) shows on Overview, Calls and
Campaigns and writes into the hash (`#calls?from=2026-10-06&to=2026-10-13`). With no range the server
picks the last 7 days in IST, so no browser time-zone logic is needed.

- **Overview:** KPI cards (Calls, Connected, Hot, Warm, Visits fixed); a cost block (Total, then
  Gemini, STT, Haiku, Plivo, "Earlier calls (no split)"), cost per call, cost per hot lead; notes for
  failed or pending analyses (the failed note links to Calls filtered to failed) and pending Plivo costs.
- **Calls & leads:** filter row, the table above, Prev/Next, Export CSV (current filters), "Re-run all
  failed (n)" with a confirm. A row opens the detail.
- **Call detail:** header (name, phone, time, duration, end reason, campaign); the four costs; summary,
  next action and agent issues first, then the other analysis fields as label/value; the transcript in
  a `<pre>` or "Transcript not available"; a Re-run analysis button. After a click: "Queued", poll
  every 3 s for up to 60 s until `analyzed_at` changes, then redraw; 409 and 503 show their reason.
- **Campaigns:** funnel and cost table, Export CSV.
- **Do-not-call:** search, add form (number, note), table with Remove (confirm), Export CSV.
- A 401 sends the browser to `/login` (as `index.html`); a 403 or other failure shows the message in
  the view.

## 4. Leftovers, safety, testing, rollout

**Leftovers from steps 1-3 that this step closes:**
- Old calls without a split: the "Earlier calls" line (section 1).
- Do-not-call UI, per-campaign cost, re-run analysis: above.
- **Phone numbers in logs:** `app.py` prints a full number at three places (`/outbound-webhook` "answered
  by" with the name, `Client connected to Quart WebSocket. Caller:`, `Initiating outbound call to`).
  All three use `campaigns.masked(...)` (`xxxxxx1234`); the name is dropped from the first.
- **Not in this step:** the optional nightly S3 copy of `call_history.db` (an EC2 cron job using the
  SQLite backup API, not app code). Deferred until the operator asks.

**Safety.** Bound parameters only; allow-listed filters; the transcript path must resolve inside
`TRANSCRIPTS/`; POSTs JSON-only; CSV cells escaped; admin-only through `ENDPOINT_ACCESS`. No kill
switch: the views are read-only and the only writes are DNC add/remove and re-run. Rollback is a
revert of the commit; the only schema change is the index.

**Known limits.** A call's cost can be partly missing (`plivo_cost_status` pending); the totals say so
instead of guessing. `calls.phone` keeps whatever format the call was placed with, so a long number
may show in scientific form in Excel until the column is widened.

**Tests** (stdlib `unittest`; about 80-100 tests in three new files):
- `tests/test_admin_data.py`: range edges (inclusive of `to`, all-time, default 7 days); KPIs and the
  cost fallback for old calls; hot/warm/visit counts; filters and the `LIKE` escaping; the campaign
  cost sum; DNC add (normalised, duplicate, invalid) and remove; the transcript path guard (a
  `..\` path, an absolute path outside, a missing file).
- `tests/test_admin_api.py`: operator 403 and no login 401 on every route; 400 validation; 415 for a
  non-JSON POST; CSV (BOM, header row, escaping, the 20,000 cap); re-run with a fake analyzer (404, 409,
  503, 202, the 50 cap, the "remaining" count, skipped not retried).
- `tests/test_admin_page.py`: static checks of `admin.html`: no `innerHTML`; every `/api/admin/` path it
  calls is in `ENDPOINT_ACCESS` as `ADMIN_API`; the five hashes exist.
- Existing: `test_access_control` keeps `ENDPOINT_ACCESS` equal to `app.url_map`; the 4_8
  `GEMINI_MODEL` pins are re-pinned after the app.py edits (new routes, masked log lines, the
  `admin_data.upgrade` call); tests that pin the three log lines are updated. No new `call_state`
  key, no `requirements.txt` change.

**Rollout.** Prerequisite: commit and push the uncommitted prompt v4 work first (step 4 edits app.py
too, so the plan needs a clean baseline). Then plan (`docs/spec/admin-panel-plan.md`), then a handoff
`docs/handoff_step4.md` for the EC2 agent (subagents on Sonnet 5.5, a report in
`docs/handoff_step4_report.md`); the operator checks the page on EC2, not locally. Deploy needs a
`git pull` and an idle restart; nothing in `.env` changes.

## Open points for the plan (not for the operator)

- Find the tests that pin the three log lines before changing them.
- Choose the exact cost SQL expression once and reuse it for the table, the overview and the campaign
  report (one helper, so the three views cannot disagree).
- The placement of `admin_data.upgrade` among the other two `upgrade` calls in `init_call_history_db`.
