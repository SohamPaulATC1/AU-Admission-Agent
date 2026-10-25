# Step 3 report (EC2): bulk campaigns + dispatcher

Written 2026-10-12 by the EC2 agent, for the Windows agent and the operator. No PII: campaigns and
contacts are named by id only, and no name, number or Excel content appears here.

**Status: built, reviewed and deployed on EC2 since 2026-10-12 22:42 IST. The dispatcher runs in
8000 only, and WAL is on. Campaigns A and B and the controls check (handoff §8 steps 4-8) have not
run yet: they need calling hours (09:00-21:00 IST) and the operator's test numbers.** Nothing is
committed (branch `dev`, HEAD `8dcc5b8` = `origin/dev`, which already contains `2448772`).

## 1. Suite

| Run | Result |
|---|---|
| Baseline (EC2, `8dcc5b8`, before any change) | Ran 808, FAILED (failures=4, skipped=4, expected failures=1) |
| Final | Ran 924, FAILED (failures=4, skipped=4, expected failures=1) |

The 4 failures are the same in both runs:
- the 3 known ones in `tests/test_prompt_catalog.py`;
- `tests.test_gemini_backend.TestVertexEndpoint.test_defaults`, which fails on EC2 only.

`GEMINI_MODEL` pins are now `[108, 1899, 2425, 2435, 2866]`. The plan predicted
`[108, 1894, 2420, 2430, 2861]`; the +5 comes from deviations D11/D12 in app.py.

New tests: 116 in total (100 from the plan, 16 added for the deviations).

| File | Tests |
|---|---|
| `test_campaigns_store.py` | 21 |
| `test_campaign_upload.py` | 24 |
| `test_campaign_dispatch.py` | 37 |
| `test_campaign_wiring.py` | 27 |
| `test_campaign_page.py` | 7 |

## 2. Execution mode

The work was subagent-driven. Each task had a fresh implementer and a fresh reviewer, and one final
whole-change review covered everything. **My harness cannot choose the subagent model, so they did
not necessarily run on Sonnet 5.5** (same as step 2). I applied the review fixes inline and reran
the tests after each one.

## 3. Tasks

| Task | Status | Notes |
|---|---|---|
| 1 `campaigns.py` tables, settings, state moves, reads | done | D1 |
| 2 upload parsing and screening | done | D2, D3, D13 |
| 3 dispatcher decisions | done | D4-D8 |
| 4 app.py wiring | done | D9-D12, D14, D15 |
| 5 `campaigns.html` + calls-page link | done | D16 |
| 6 docs | done | spec status, overview, CLAUDE.md |

Plan flaw, no code change: Task 4 Step 9 expects all OK, but `test_pages_and_apis_need_a_login`
needs `campaigns.html`, which only Task 5 creates. The Windows dry run applied every task at once,
so it missed this. Without the file, `GET /campaigns` returns 500, not 404 as Task 5 says. Both
pass after Task 5.

### Deviations

| # | File | Change | Why | Cost if wrong |
|---|---|---|---|---|
| D1 | `campaigns.apply_action`, `update_settings` | writes carry `AND state=?` (the state just read); `BadMove` (409) when no row moved | the state SELECT runs outside the write transaction, so two requests could produce a forbidden move (start after stop) | none |
| D2 | `campaigns.normalise_phone` | E notation converted only when finite and `adjusted() < 15` | a cell `1E5000` gave a 500, and `1E999999999` would build a huge int | none |
| D3 | `campaigns.parse_upload` | new streaming reader of the **first sheet only** (`_first_sheet_rows`, `ET.iterparse` with elements dropped as read); column index > 1024 refused, at most 5,001 non-empty rows built, at most 1M row elements, cells and shared strings cut to 200 chars; header text cut to 200 | `read_sheets` pads each row out to its rightmost cell and parses every sheet whole: a 3 KB zip with 20,000 rows referencing column `AMJ` used about 180 MB, and at the 50 MB unpacked limit it would be about 13 GB on 8000, which also serves live media. A regex pre-check was bypassable (single quotes, `&#65;`, a sheet outside `xl/worksheets/`) | a sheet with a cell beyond column 1024 is refused. The cell rules are the same as `read_sheets` (shared strings, inline strings, raw `<v>`), and `catalog/build_catalog.py` is unchanged |
| D4 | `campaigns.due_contacts` | `BEGIN IMMEDIATE` when not already in a transaction | atomic pick: a stop from a request cannot commit between reading a contact and marking it `calling` | the write lock is held for milliseconds every 2 s |
| D5 | `campaigns.release` | a contact released in a stopped campaign becomes `cancelled`/`stopped` | it stayed `pending` forever | none |
| D6 | `campaigns.dial_failed` | returns True only if it actually paused the campaign | avoids a false "paused reason=dial_errors" log after an operator pause | none |
| D7 | `campaigns.hangup_outcome` | `HangupCause` matched case-insensitively, against `RETRY_CAUSES` read at call time | `no_answer` / `BUSY LINE ` would have been marked `failed` | none |
| D8 | `campaigns.on_hangup` | if no `calling` row has the `call_uuid` yet (the hangup beat `record_dial`), match the `calling` contact with `call_uuid IS NULL`, phone = `normalise_phone(form["To"])` and dialled in the last 2 min; the settle UPDATE is guarded `AND status='calling'`, stores the `call_uuid`, and returns None if nothing moved | a fast busy or invalid-number hangup was lost: the contact held a slot for 30 min, then the backstop marked it **done**. The guard also makes repeat callbacks idempotent | relies on Plivo's `To` holding the number (not yet checked live; the hangup field-names line will show it) |
| D9 | app.py `create_campaign_api` | `parse_upload` runs in `asyncio.to_thread` | a 5,000-row file takes about 0.7 s to parse, on 8000's event loop | none |
| D10 | app.py `campaign_tick` | on any exception, picks that were not dialled are released (`BaseException`, then re-raise); a pick whose `place_call` succeeded but whose `record_dial` failed stays `calling` (D8 settles it) | a mid-tick DB error left contacts in `calling`, and 30 min later the backstop marked them done although nobody was called | none |
| D11 | app.py `campaign_tick` + `campaigns.still_due` | before every dial after the first in a tick, re-check that the campaign is running and inside hours (live clock); if not, release the contact and skip it | up to 8 dials take 10 s or more: a Stop, a Pause or 21:00 (TRAI) pressed meanwhile did not stop the rest | one small query per dial |
| D12 | app.py `init_call_history_db` | the WAL switch is wrapped in try/except and warns if the mode isn't `wal` | a busy database on first deploy must not stop 8000 from starting | none |
| D13 | `.gitignore` | `*.db-wal`, `*.db-shm` | WAL side files hold call data | none |
| D14 | app.py `place_call(name, phone, answer_name=None)`; the dispatcher passes `answer_name="Unknown"` | the campaign answer URL carries `user_name=Unknown`; the `call_queued` event still has the Excel name for the calls page | **the handoff and spec say "the contact's name never reaches the Gemini prompt; that is already how app.py works". That is wrong:** `/outbound-webhook` passes `user_name` to the media stream, which puts it into the system prompt (`{user_name}` in `prompt_au_v3.txt` line 4, "You are calling {user_name}") and into the greeting ("Greet {user_name}"). Decision 4 is kept for campaigns: Neha greets campaign callers neutrally. It also closes a prompt-injection path from a 200-char Excel cell. `/trigger-call` single calls are unchanged (they still pass the typed name) | if the operator wants campaign callers greeted by name, revert this one line. That would need a spec change |
| D15 | app.py `campaign_detail_api` | page capped at 10^6 | a huge `?page=` overflowed SQLite's OFFSET (500) | none |
| D16 | `campaigns.html` CSV export | the formula guard also covers a leading tab or CR (`/^[=+\-@\t\r]/`); test updated to the stricter pattern | CSV formula injection | none |

Reviewer notes left open (minor):
- `release` in a stopped campaign leaves `finished_at` empty.
- A bad `CAMPAIGN_*` value in `.env` (not an integer, or bad hours) stops **every** process at import, the voice agent included. This fail-fast is the plan's own design. Check `.env` before any restart after editing those keys.
- `/outbound-webhook` (pre-existing) logs the full number and name of every answered call. Campaigns multiply that; worth masking in step 4.
- The tick takes the DB write lock every 2 s even with no running campaign (milliseconds).
- `call_history.db` is in WAL now. Back it up with the SQLite backup API (as done below) or `sqlite3 call_history.db ".backup <file>"`, not with `cp` alone.

## 4. Deploy

- DB backup: `call_history.db.bak-20261012-1711` (SQLite backup API; the file name is in server UTC).
  Note: any test that imports `app` runs `init_call_history_db` on the live `call_history.db` (the
  same pre-existing harness behaviour as in step 2). So the campaign tables and WAL mode were
  already in place before the restart. No row data changed.
- No `CAMPAIGN_*` key is in `.env` or `common.env` (names checked), so all defaults apply.
- Restart 2026-10-12 22:42 IST (the operator had cleared restarts; 0 calls in progress, the last
  call update was 20:54 IST). All 4 services are active.

Startup lines seen:
- `🔐 [AUTH] logins configured, plivo checks=enforce` four times.
- 8000 only:
  - `🧠 [ANALYSIS] worker started model=us.anthropic.claude-haiku-5-5 concurrency=2`
  - `📣 [CAMPAIGN] dispatcher started max_concurrent=8 hours=09:00-21:00 tick=2.0s`
- The media processes logged neither line. No `tick failed`, no traceback.
- `PRAGMA journal_mode` gives `('wal',)`.
- Logged out: `/campaigns` returns 302 (to `/login`) and `/api/campaigns` returns 401.

## 5. Live lists

### List A ("Admission 2027"): done, 2026-10-13 14:37-14:40 IST

The operator uploaded 5 of the team's own numbers, with one extra column (City), concurrency 5,
default hours and retries, then pressed Start. For this test the operator set
`CAMPAIGN_RECENT_DAYS=0` in `.env`: these numbers had been called during the week and were rejected
as "called recently". Remove that line after the tests.

- **Dials:** 5, spaced about 1.1-1.3 s apart (14:37:32 to 14:37:37). Never more than 5 at once.
  Numbers were masked in every line.
- **Answered:** all 5, 6-11 s after the dial.
- **Hangups:** all 5 `status=done cause=NORMAL_CLEARING attempt=1`. The list moved to
  `done ... done=5 failed=0 cancelled=0` at 14:40:24.
- **Hangup field names** (the first hangup, names only): `ALegRequestUUID, ALegUUID, AnswerTime,
  BillDuration, BillRate, CallStatus, CallUUID, ComplianceApplicationId, ComplianceStatus, Direction,
  Duration, EndTime, Event, From, HangupCause, HangupCauseCode, HangupCauseName, HangupSource,
  ParentAuthID, RequestUUID, STIRAttestation, STIRVerification, SessionStart, StartTime, To,
  TotalCost`. `To` is there, so the early-hangup number fallback (D8) has what it needs. Plivo also
  sends `TotalCost` in the hangup; it isn't used (the cost comes from the call record a minute later).
- **Analyses:** all 5 `status=ok` within 4-8 s of hangup, one at 59 s.
  - Leads: callback_requested, cold, no_conversation, not_interested, wrong_number.
  - 7.5-9.3k input tokens and $0.0004-0.0012 each.
  - The prompt cache worked: the first analysis wrote the cache (`cache_write`), the next four read
    it, at about half the cost.
- **Plivo costs:** all 5 `ok`, $0.00475-$0.01425.
- **Lead results on the page:** shown. The operator's own check was that it worked.

**Defect found (open, not fixed): one lost `call_ended`.** For call `f7eef765` the media process
logged `⚠ [EVENT-FWD] call_ended ... not delivered: ReadError('')` when it forwarded the event to
8000. The forwarder does not retry, so that call's `calls` row is stuck at `connected`:
- the calls page shows it as live;
- it has no duration and no Gemini cost (about $0.10 missing from the totals);
- the analysis, the transcript cost, the Plivo cost and the contact's `done` all arrived, because
  they travel separately.

**Fixed 2026-10-13, at the operator's go-ahead (live since 15:33 IST):**
- **Retry:** a forwarded event that fails with a connection error (`httpx.TransportError`: ReadError,
  ConnectError, RemoteProtocolError and similar) is sent again after 0.5 s and then 2 s. After that it
  gives up with the old `not delivered` warning. Each retry logs `🔁 [EVENT-FWD] <type> for <call>
  retry <n>`.
- **No retry on a reply:** an HTTP answer from 8000 that is not 200 is a refusal, not a loss, so it
  is not retried.
- **No double-counting:** a connection can drop after 8000 has already handled the event, and a
  second `call_ended` would add the call's Gemini cost and leg again. So each forwarded event now
  carries a random `event_id`:
  - `/internal/call-event` remembers the last 5,000 ids and answers a copy with
    `{"status": "ok", "duplicate": true}` without applying it;
  - events without an id are still applied, so a media process that hasn't restarted yet keeps working.
- **Code:** `app.py`: `forward_call_event` (the id), `_deliver_event` (the retry), `_already_seen`
  plus `internal_call_event` (the duplicate check).
- **Tests** in `tests/test_multiprocess_events.py`: 6 new (retry then success with the same event,
  give up after two retries, no retry on 403, a duplicate applied once, events without an id, the
  bounded memory); 2 existing tests updated (the event now has an `event_id`; an unreachable main
  process is retried twice before the warning).
- **Suite:** 1132 run, the same 4 known failures. `GEMINI_MODEL` pins `[109, 1950, 2472, 2482, 2913]`.

The row of `f7eef765` itself was not repaired: its lost event's numbers (duration, Gemini cost) are
only in the log. It still shows as `connected`. Repairing it is a manual database edit, so it waits
for the operator's word.

### Still to run

- **B:** 1 unanswered number, 1 retry, 30 min gap. Check `retry_wait`, then `attempt=2`, then
  `failed`; record the causes Plivo sends for a ring-out and for a reject.
- **Controls:** pause with a live call, resume, stop; settings editable while paused, not while
  running. Also the new tick boxes (§8).

## 8. Tick box per contact (operator, 2026-10-13, after list A)

The operator asked for a safety check: the person running the list chooses who gets called.

| Decision (operator) | Built |
|---|---|
| Every contact starts **ticked**; the operator unticks the ones not to call | new column `campaign_contacts.selected INTEGER NOT NULL DEFAULT 1`, added to the existing table on start (race-safe like the other upgrades; existing contacts became ticked) |
| A **Select / unselect all** option | an "All" tick box in the table header; it acts on the whole list, every page; it shows partly ticked when only some are |
| Can untick while paused | the boxes change only in **draft** or **paused**, and only for contacts not dialled yet (`pending`, `retry_wait`); otherwise they are locked (a call already made cannot be undone). The server enforces it too (409) |
| **Confirm on Start** | "Start calling N selected contacts? M unticked contacts are not called. ..." |
| Rows numbered **from 1** | the page shows the contact's position (the stored `sheet_row` minus the header row) in the contact table, the rejected-rows table and its CSV download |

Behaviour:
- The dispatcher dials only ticked contacts.
- Start and Resume are refused (409, "No contact is selected") when nobody is ticked. The buttons
  are greyed out in that case.
- An unticked contact shows "Waiting (not selected)", greyed.
- When a **running** list has no ticked contact left to dial and nobody on a call, it moves to
  `done`, and its unticked contacts become Cancelled with last result "Not selected".
- A **paused** list with only unticked contacts left stays paused, so the operator can still tick
  someone.
- The counts line starts with "Selected to call N of M not called yet".

New route `POST /api/campaigns/<id>/select`, JSON only:
- the body is `{"selected": true|false, "ids": [...]}` or `{"selected": ..., "all": true}`;
- it is open to both logins (`OPERATOR_API` in `web_auth.ENDPOINT_ACCESS`);
- it logs `📣 [CAMPAIGN] campaign=<id> selection changed=<n> selected=<n> of <n>`.

Also fixed: **all four buttons (Start, Pause, Resume, Stop) showed whatever the list's state.**
`.btn`'s `display` rule beat the `hidden` attribute (it was visible in the operator's screenshots,
not a browser cache issue). Fix: `[hidden] { display: none !important; }`.

Tests: `tests/test_campaign_selection.py`, 18 tests:
- the column added to an old table;
- tick / untick / all, other lists untouched, locked while running, called contacts locked;
- Start and Resume need a ticked contact;
- only the ticked are dialled, the list still finishes, a paused list stays paused;
- the API: both roles, 400 / 404 / 409 / 415 / 401;
- the tick-driven dispatcher;
- the page: boxes, All, confirm, numbering from 1, the hidden fix.

Suite: 1126 run, the same 4 known failures. `GEMINI_MODEL` pins are unchanged: the new route sits
below every pinned line. Restarted idle at 15:24 IST: startup lines unchanged, no error.

## 6. Rollback

- `CAMPAIGNS_ENABLED=0` in `.env` plus an idle restart: no dispatcher. Pages load, nothing dials,
  single calls are unaffected.
- Full rollback: check out `8dcc5b8` (or `2448772`) and restart. The new tables and WAL are harmless
  to the old code.

## 7. Uncommitted

Modified: `.gitignore`, `app.py`, `index.html`, `web_auth.py`, `tests/harness/appctl.py`,
`tests/test_preservation_4_8_resumption_recording_stats.py`, `CLAUDE.md`,
`docs/spec/campaigns-design.md`, `docs/spec/admin-system-overview.md`.

New: `campaigns.py`, `campaigns.html`, `tests/test_campaigns_store.py`, `tests/test_campaign_upload.py`,
`tests/test_campaign_dispatch.py`, `tests/test_campaign_wiring.py`, `tests/test_campaign_page.py`,
this report.

Commit and push to `origin/dev` on the operator's word.

Note (2026-10-13): steps 3 and 4 were committed by the operator (`4e08359`, `cf29926`, then
`0b0a912` moved the pages into `web/`). The tick-box change in §8 is uncommitted on top of `0b0a912`:
`campaigns.py`, `app.py`, `web_auth.py`, `web/campaigns.html`, the new
`tests/test_campaign_selection.py`, this report and `CLAUDE.md`.
