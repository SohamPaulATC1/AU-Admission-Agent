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

## 5. Campaigns A and B, controls: NOT DONE YET

These are waiting for calling hours (from 09:00 IST) and the operator's test numbers. Plan (handoff §8):
- **A:** 3 internal numbers, concurrency 2, one extra column. Check: dials capped at 2 and at
  least 1 s apart, masked numbers, `status=done` hangups, the `hangup fields: [...]` line (names
  only; also confirms `To` for D8), the campaign done, lead results shown, no cost for the operator.
- **B:** 1 unanswered number, 1 retry, 30 min gap. Check `retry_wait`, then `attempt=2`, then
  `failed`; record the causes Plivo sends for a ring-out and for a reject.
- **Controls:** pause with a live call, resume, stop; settings editable while paused, not while running.
- DB check: the one-liner in handoff §8 step 7.

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
