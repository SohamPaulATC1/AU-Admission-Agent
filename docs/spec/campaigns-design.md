# Step 3: bulk campaigns and the dispatcher

Status: design approved by the operator 2026-10-12 (all five sections, in chat); implemented (plan docs/spec/campaigns-plan.md), deviations in the report docs/handoff_step3_report.md. Correction found in the build (D14): app.py does put the answer URL's `user_name` into the Gemini prompt and greeting, so campaign dials send `Unknown` there. Shared decisions:
`docs/spec/admin-system-overview.md`. Step 1 (logins) and step 2 (call analysis, full call cost)
are live; step 4 (admin panel) adds campaign cost reports and the do-not-call UI on top of this.

## Goal

The calling team (operator login) uploads an Excel of contacts (name + phone, plus any extra
columns), sets how the campaign runs, and starts it. The main process (8000) calls the contacts
with at most N calls at once, inside calling hours, retries no-answer and busy, and resumes after a
restart. The campaign page shows each contact's live status and, once Haiku has analysed the call,
a short lead result. Nobody sees a cost on these pages.

## Decisions (operator, 2026-10-12)

1. **Concurrency:** a global cap `CAMPAIGN_MAX_CONCURRENT` in `.env` (8) over all campaign calls;
   each campaign has its own `concurrency` (default 5, operator picks 1 up to the cap). The cap
   leaves 2 of Plivo's 10 lines for single calls from the calls page.
2. **Lead results for the operator:** per contact, `lead_status`, `campus_visit_day` and `summary`
   from `call_analysis`. No costs, no transcripts, no other analysis fields (step 4, admin).
3. **Hours and retries per campaign**, inside hard bounds from `.env`: hours within
   `CAMPAIGN_HOURS_START`..`CAMPAIGN_HOURS_END` (09:00-21:00 IST, TRAI), retries 0..
   `CAMPAIGN_MAX_RETRIES` (3), retry gap at least `CAMPAIGN_MIN_RETRY_GAP_MIN` (30). New campaign
   defaults: 09:00-21:00, 2 retries, 120 min gap. The server enforces the bounds on save and the
   dispatcher checks them again on every dial.
4. **Excel:** name and phone columns found by header name; every other column is kept per contact
   (`extra`) and shown on the campaign page. Extras never reach Gemini (the prompt does not get the
   contact's name either; Neha addresses the caller neutrally).
5. **Controls:** campaign states `draft`, `running`, `paused`, `stopped`, `done`; pause / resume /
   stop; a new `/campaigns` page; a global do-not-call list (table + dispatcher check now, admin UI
   in step 4).
6. **Dispatcher:** one asyncio polling loop in 8000 (not event-driven, not a separate service).

## Facts this design relies on

- `/trigger-call` (app.py) creates the Plivo call inline with `answer_url=/outbound-webhook?
  user_name=..&phone_number=..`, `ring_url=/plivo-ringing`, `hangup_url=/plivo-hangup`, then emits
  `call_queued`. Plivo's `request_uuid` equals the later `CallUUID`, so one id joins the contact,
  the `calls` row and the `call_analysis` row.
- `/plivo-hangup` fires for every call and always reaches 8000 (nginx sends everything except
  `/media-stream` there). It carries `CallUUID` and `HangupCause`; it emits `call_failed` only for a
  cause other than `NORMAL_CLEARING`. `USER_BUSY` covers both busy and a manual reject (the comment
  in the handler: Indian carriers send 180 then 486 for a switched-off phone too).
- `/outbound-webhook` reads `user_name` with default `Unknown`; the name is logged and passed to the
  media stream but not put into the prompt.
- `catalog/build_catalog.read_sheets(path)` reads every sheet of an .xlsx with `zipfile` +
  `ElementTree` (stdlib). `zipfile.ZipFile` takes a file-like object, so an upload can be read from
  `io.BytesIO` without writing a file. A number cell comes back as its raw `<v>` text (for example
  `9876543210`, or `9.87654321E9` from some writers).
- `call_store.run(db_path, fn, ...)` runs a sync DB function on a fresh connection in the thread
  pool; `app.spawn_background(coro)` starts fire-and-forget work. Only 8000 writes the database.
- `web_auth.ENDPOINT_ACCESS` is default deny, and a test keeps it equal to `app.url_map`.
- To check at plan time, against Plivo docs and a live hangup log: the hangup callback's answered
  signal (`AnswerTime` set or `BillDuration` > 0) and the cause for a ring timeout
  (`NO_ANSWER` expected).

## 1. Data model

New module `campaigns.py` (storage + dispatcher logic, sync functions taking a `sqlite3`
connection, the caller commits, same style as `call_store.py`). `app.init_call_history_db` calls
`campaigns.upgrade(conn)` right after `call_store.upgrade(conn)` (not from inside `call_store`:
`campaigns` imports `call_store`, so the other way round would be a circular import), so every
process at import has the same schema. Only 8000 writes them.

`campaigns`

| column | type | notes |
|---|---|---|
| id | INTEGER PRIMARY KEY | |
| name | TEXT | operator's label |
| state | TEXT | `draft` `running` `paused` `stopped` `done` |
| state_reason | TEXT | for an automatic move, e.g. `dial_errors` |
| created_by | TEXT | role (`operator` / `admin`); logins are shared |
| created_at | TEXT | IST, `call_store.now_ist()` |
| started_at, finished_at | TEXT | |
| concurrency | INTEGER | 1..`CAMPAIGN_MAX_CONCURRENT` |
| hours_start, hours_end | TEXT | `HH:MM` IST |
| max_retries | INTEGER | 0..`CAMPAIGN_MAX_RETRIES` |
| retry_gap_min | INTEGER | >= `CAMPAIGN_MIN_RETRY_GAP_MIN` |
| file_name | TEXT | uploaded file name |
| rejected_count | INTEGER | rows rejected at upload |
| last_dial_at | TEXT | IST; the round-robin order |
| dial_errors | INTEGER | Plivo dial errors in a row; reset by a good dial |

`campaign_contacts`

| column | type | notes |
|---|---|---|
| id | INTEGER PRIMARY KEY | |
| campaign_id | INTEGER | |
| sheet_row | INTEGER | data row position in the sheet (header = row 1; blank rows are not counted), for the operator |
| name | TEXT | may be `""` |
| phone | TEXT | 10 digits |
| extra | TEXT | JSON object, header name as written to cell text |
| status | TEXT | `pending` `calling` `retry_wait` `done` `failed` `cancelled` |
| attempts | INTEGER | dials that reached Plivo |
| next_attempt_at | TEXT | IST; set for `retry_wait` |
| call_uuid | TEXT | latest attempt |
| last_cause | TEXT | Plivo `HangupCause`, or `dnc`, `no_hangup_event`, `stopped` |
| dialed_at, finished_at | TEXT | IST |

Indexes: unique `(campaign_id, phone)`; `(status, campaign_id)`; `(call_uuid)`.

`do_not_call`: `phone TEXT PRIMARY KEY`, `added_at TEXT`, `note TEXT`.

The lead result is a join `campaign_contacts.call_uuid = call_analysis.call_uuid`; nothing is
copied.

**WAL:** 8000 sets `PRAGMA journal_mode=WAL` once at startup (the dispatcher adds steady writes next
to the Analyzer; open item 6 of step 2).

## 2. Dispatcher and call lifecycle

**Shared dial.** The Plivo `calls.create` call and the `call_queued` emit move out of
`/trigger-call` into one `place_call(name, phone)` (returns the `call_uuid`, raises on a Plivo
error). `/trigger-call` keeps its request handling, responses and logs exactly as today. An empty
name goes to Plivo as `Unknown`, the same default `/trigger-call` and `/outbound-webhook` use.

**Loop.** One asyncio task in 8000, started in `start_background_work` (skipped when
`EVENT_FORWARD_URL` is set, and when `CAMPAIGNS_ENABLED=0`). Every `CAMPAIGN_TICK_S` (2 s):

1. Count all contacts in `calling`. At or above `CAMPAIGN_MAX_CONCURRENT`: no dial this tick.
2. For every `running` campaign whose current IST time is inside its hours (and inside the `.env`
   bounds) and whose own `calling` count is below its `concurrency`, pick the next due contact:
   `pending`, or `retry_wait` with `next_attempt_at` passed; lowest `id` first. Campaigns take
   turns (round-robin by the campaign that dialled least recently), so one big list cannot starve
   another.
3. A due contact on `do_not_call` becomes `cancelled`, cause `dnc`, and the next one is taken.
4. In the same transaction that picked it: status `calling`, `dialed_at` now, `attempts + 1`. Then
   `place_call`, then store `call_uuid`. Wait `CAMPAIGN_DIAL_GAP_S` (1 s) before the next dial
   (Plivo's calls-per-second limit).

The pick logic is a sync function `due_contacts(conn, now, settings)` returning the contacts to dial
this tick, so it is testable without asyncio or Plivo.

**Hangup.** `/plivo-hangup` looks the contact up by `CallUUID` (no row: a single call, nothing
changes). The DB write runs in the background (`spawn_background`), so the webhook reply does not
wait on the database.

| hangup | contact becomes |
|---|---|
| answered (`AnswerTime` set or `BillDuration` > 0) | `done` |
| not answered and `HangupCause` in `NO_ANSWER`, `USER_BUSY`, `ORIGINATOR_CANCEL`, `Busy Line`, `No Answer`, `Ring Timeout Reached`, or `CallStatus` in `busy`, `no-answer`, `timeout` | `retry_wait` at now + `retry_gap_min` if `attempts <= max_retries`, else `failed` (`max_retries=2`: at most 3 dials) |
| any other cause, not answered (invalid / unallocated number, network error) | `failed`, no retry |
| a retry outcome in a `stopped` campaign | `cancelled`, cause `stopped` |

The first campaign hangup after a start logs the callback's field names (never values), so the
answered signal and the cause names can be checked against a live call (rollout step 3).

`last_cause` and `finished_at` are stored. A `retry_wait` whose time falls outside the hours waits
for the next window (the loop simply does not pick it until then).

**Edge cases:**
- **Dial error** (`place_call` raises): the contact goes back to where it was (`pending` for a
  first attempt, else `retry_wait` with its due time kept), `attempts - 1` (no attempt counted).
  Contacts already picked in the same tick for a campaign that just auto-paused go back the same
  way. After 3 dial errors in a row in one campaign it moves to `paused`,
  `state_reason=dial_errors`, so a Plivo outage does not spin.
- **Backstop:** a contact in `calling` more than `CAMPAIGN_CALL_TIMEOUT_MIN` (30) after `dialed_at`
  becomes `done`, cause `no_hangup_event`. Not retried: nobody is called twice by mistake.
- **Crash between the DB write and Plivo:** the contact sits in `calling` and the backstop ends it
  (same rule, no duplicate dial).
- **Restart:** `calling` rows keep their slots until their hangup arrives (Plivo still posts it to
  8000) or the backstop fires; `pending` and `retry_wait` resume on the first tick.
- **Done:** a `running` or `paused` campaign with no `pending`, `calling` or `retry_wait` contact
  moves to `done`, `finished_at` set.

**State moves** (anything else is a 409):

| from | action | to |
|---|---|---|
| `draft` | start | `running` |
| `running` | pause | `paused` |
| `paused` | resume | `running` (clears `state_reason`) |
| `draft` / `running` / `paused` | stop | `stopped`: `pending` and `retry_wait` contacts become `cancelled`, cause `stopped`; live calls finish and get their hangup result |

Pause stops new dials; live calls finish.

## 3. Upload and validation

**Flow.** On `/campaigns` the operator enters a name and the settings and picks an `.xlsx`.
`POST /api/campaigns` (JSON: `name`, settings, `file_name`, `file_b64`) parses it and stores a
`draft`. The reply has the counts and every rejected row with its reason. Start is a separate
action. Nothing dials at upload.

**Limits** (400 with one clear message, nothing stored): not an .xlsx (bad zip, no workbook), file
over 2 MB, unpacked size over 50 MB (sum of the zip entries' `file_size`, checked before parsing),
more than 5,000 data rows, no name or phone column.

**Parsing.** First sheet only, via `read_sheets` (reused from `catalog/build_catalog.py`; the plan
decides import vs a small shared module). The header row is the first non-empty row. Headers are
matched lowercased with spaces collapsed:
- name: `name`, `student name`, `full name`
- phone: `phone`, `mobile`, `phone number`, `mobile number`, `contact`
- extras: every other column with a non-empty header, at most 20 (more: the first 20, the rest
  dropped, noted in the reply); cells cut at 200 chars. Header names kept as written.

**Phone normalisation.** Take the cell text; a numeric cell in E notation or with `.0` is turned
back into its integer digits. Strip spaces, `-`, `(`, `)`, `.`, and a leading `+`. Then: 12 digits
starting `91`, drop `91`; 11 digits starting `0`, drop `0`. Valid: `^[6-9]\d{9}$`.

**Row rejections** (rejected rows are not stored; the reply lists them with row number, name,
phone as written, reason; the page offers them as a CSV made in the browser):
- `missing_number`
- `bad_number`
- `duplicate_in_file` (the first copy is kept)
- `on_do_not_call`
- `called_recently`: the number is in `calls` or in another campaign's `campaign_contacts` within
  `CAMPAIGN_RECENT_DAYS` (30)

A missing name is allowed (`""`). A file whose every row is rejected is still a 400 (nothing to
call).

**Settings check** (server side, on upload and on settings edit): concurrency 1..
`CAMPAIGN_MAX_CONCURRENT`; `hours_start` < `hours_end`, both within the `.env` bounds; retries 0..
`CAMPAIGN_MAX_RETRIES`; gap >= `CAMPAIGN_MIN_RETRY_GAP_MIN`; name 1-100 chars. Settings can be
edited in `draft` and `paused` only.

## 4. Routes, page, access

Every route gets an entry in `web_auth.ENDPOINT_ACCESS`; all are open to both roles.

| route | access | does |
|---|---|---|
| `GET /campaigns` | `OPERATOR_PAGE` | `campaigns.html` |
| `GET /api/campaigns` | `OPERATOR_API` | list: id, name, state, `state_reason`, settings, created/started/finished, counts per contact status |
| `POST /api/campaigns` | `OPERATOR_API` | upload (section 3) |
| `GET /api/campaigns/<id>` | `OPERATOR_API` | the campaign plus contacts, 100 per page, optional status filter; each contact: row number, name, phone, extras, status, attempts, last cause, next attempt, and `lead_status`, `campus_visit_day`, `summary` from `call_analysis` |
| `POST /api/campaigns/<id>/action` | `OPERATOR_API` | JSON `{"action": "start"\|"pause"\|"resume"\|"stop"}`; 409 for a move section 2 does not allow |
| `POST /api/campaigns/<id>/settings` | `OPERATOR_API` | edit settings (`draft` / `paused`) |

JSON bodies only (as `/trigger-call`): a cross-site form cannot send one with our cookie (CSRF).
Unknown id: 404.

**Page.** `campaigns.html`, vanilla JS in the style of `index.html`: upload form with the settings
(defaults filled, bounds from `GET /api/campaigns`), the upload result with rejected rows and the
CSV download, the campaign list, and a detail view (counts bar, the controls the state allows, the
contact table with the extra columns as columns). The detail view polls `GET /api/campaigns/<id>`
every 5 s while open; no new SSE events. A 401 redirects to `/login`. The calls page gets a
"Campaigns" link; the campaign page links back. Campaign calls still appear in the calls page feed
through the existing events.

**Costs:** campaign APIs return no cost column for either role (no `without_cost` branch needed).
Per-campaign cost reports are step 4.

**Do-not-call:** no route in step 3. Until step 4 an admin adds numbers with `sqlite3` on EC2.

## 5. Errors, logging, testing, rollout

**Errors.** Each tick runs in try/except: any error (a locked database included) is logged and the
tick skipped; the loop never dies and the server keeps serving. The pick-and-mark is one
transaction, so two ticks cannot dial the same contact.

**Logging** (`📣 [CAMPAIGN]`, added to `DIAGNOSTIC_LOG_TAGS`): created (counts), start, pause,
resume, stop, done (counts per status); each dial (campaign id, contact id, attempt, `call_uuid`);
each hangup result (status, cause); backstop; auto-pause. Phone numbers masked to the last 4 digits.

**Settings** (`.env`; all have defaults, none required):

| key | default |
|---|---|
| `CAMPAIGNS_ENABLED` | `1` |
| `CAMPAIGN_MAX_CONCURRENT` | `8` |
| `CAMPAIGN_DEFAULT_CONCURRENCY` | `5` |
| `CAMPAIGN_HOURS_START` / `CAMPAIGN_HOURS_END` | `09:00` / `21:00` |
| `CAMPAIGN_MAX_RETRIES` | `3` |
| `CAMPAIGN_MIN_RETRY_GAP_MIN` | `30` |
| `CAMPAIGN_RECENT_DAYS` | `30` |
| `CAMPAIGN_TICK_S` | `2` |
| `CAMPAIGN_DIAL_GAP_S` | `1` |
| `CAMPAIGN_CALL_TIMEOUT_MIN` | `30` |

**Tests** (stdlib unittest, in-memory SQLite, fake clock, fake `place_call`; no Plivo, no network):
- dispatcher: global cap, per-campaign cap, hours (and `.env` bounds), round-robin, retry timing
  and count, `retry_wait` outside hours, DNC, backstop, pause / resume / stop, auto-pause after 3
  dial errors, dial error does not count an attempt, restart resume, campaign done, no double pick;
- hangup: answered / retry / failed mapping, retries used up, single call without a contact row
  changes nothing;
- upload: header variants, phone normalisation (prefixes, E notation, `.0`), each rejection
  reason, extras limits, row / size / unpacked-size limits, all-rejected file; test .xlsx files
  built in the test with `zipfile`;
- routes: both roles reach every campaign route, logged-out gets 401 / redirect, 409 on a bad
  move, settings bounds, no cost key in any campaign reply; the existing `ENDPOINT_ACCESS` =
  `url_map` test covers the new routes;
- regression: `/trigger-call` unchanged after `place_call` is extracted (existing tests); 4_8
  `GEMINI_MODEL` pins re-pinned. No new `call_state` key is planned (any that appears is mirrored in
  `tests/harness/appctl.py`).

**Rollout** (EC2, handoff file like step 2, restart when no call is live):
1. Deploy; `.env` keys only where the defaults do not fit.
2. Campaign A: 3 internal numbers, concurrency 2. Check dials are spaced, at most 2 live, hangups
   mark contacts, lead results appear after analysis, the operator login shows no cost.
3. Campaign B: one number left unanswered, 1 retry, 30 min gap: `retry_wait`, then `failed`.
4. Pause and resume mid-campaign; stop one.
5. A small real list.

Kill switch `CAMPAIGNS_ENABLED=0` (no dispatcher; pages load, nothing dials). Rollback: the commit
before step 3.

## Out of scope (step 4 or later)

Do-not-call management UI; per-campaign cost and lead reports; Excel / CSV export; adding contacts
to an existing campaign; editing or deleting contacts; scheduling a campaign for a later date;
per-user logins.
