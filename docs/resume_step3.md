# Resume here: admin system, step 3 (bulk campaigns and dispatcher)

Written 2026-10-12 at the end of a long design/build session, so a new chat can start step 3 cold.
Read this, then `docs/spec/admin-system-overview.md`, then CLAUDE.md "Resume here". Memory
(`admin-system.md`, `env-not-frozen.md`) holds the same decisions.

## Goal of the whole admin system

The agent's URL goes to another team in the organisation who run the calls. They upload bulk
contacts (Excel: number + name) and the agent calls them with concurrency up to a limit. The owner
(operator) gets an admin panel: costs, transcripts, Haiku lead results, campaign reports.

| Step | What | State |
|---|---|---|
| 1 | Logins + protection (operator login = calls page, no costs; admin login = everything) | **LIVE** (`f55e5c4`, Plivo checks `enforce`) |
| 2 | `call_analysis` table (Haiku 5.5 on Bedrock) + full per-call cost (Gemini, STT, Haiku, Plivo) | **LIVE and verified** on a real transferred call; code `387a21e` + fixes `2448772` |
| 3 | Bulk Excel campaigns + dispatcher | **next, nothing designed beyond the notes below** |
| 4 | Admin panel UI (call table, costs, leads, transcripts, campaign reports, export) | after 3; `/admin` is an empty shell today |

## What exists that step 3 builds on

- **Single calls:** `POST /trigger-call` (login session + JSON body `user_name`, `phone_number`) creates the Plivo call
  (`answer_url=/outbound-webhook?...`, hangup `/plivo-hangup`, ring `/plivo-ringing`), emits `call_queued`. Plivo's
  `request_uuid` equals the later `CallUUID` for our outbound calls, so one id keys everything.
- **Processes (EC2, c6a.xlarge):** 4 systemd services `au-app@8000..8003` behind nginx. 8000 is the **main** process:
  dashboard, all webhooks (hangup, ring, dial-action), the only database writer, the Haiku `Analyzer`, Plivo cost fetches.
  8001-8003 are media-only (`EVENT_FORWARD_URL` set); they forward events to 8000 over `/internal/call-event`.
  Safe load about 3 calls per process (one vCPU each), so 8 concurrent calls = 2 per process. Plivo caps at **10 concurrent
  calls**; target 5-8. Restart only when no call is live.
- **Database:** `call_history.db` (SQLite, WAL not yet set). `calls` table (+ step 2 cost columns), `call_analysis` table.
  Code in `call_store.py` (sync functions + `await call_store.run(db_path, fn, ...)`); `app.spawn_background(coro)` for
  fire-and-forget work started by a request. Step 3 tables (campaigns, contacts) belong in `call_store.py` or a sibling
  module, written only by 8000.
- **Hangup webhook always reaches 8000** (nginx sends everything except `/media-stream` there), so 8000 can count live calls
  and free dispatcher slots. Add a timeout backstop for a lost event.
- **Excel reading without a new dependency:** `catalog/build_catalog.py` already reads .xlsx with the standard library.
- **Access table:** every new route needs an entry in `web_auth.ENDPOINT_ACCESS` (default deny; a test keeps it equal to
  `app.url_map`). The calling team (operator login) must be able to use the campaign pages; the operator must never see a
  cost (`web_auth.without_cost` drops `cost` and every `*cost_usd` key in events; history rows drop `cost` server-side).
- **Cost reality:** a call costs about **$0.165**, of which Gemini is about **$0.14** (85%); Haiku ~$0.0026 (cached prompt
  makes repeat analyses ~$0.0006), STT ~$0.0045, Plivo ~$0.0166. A campaign multiplies this by every contact.

## Step 3 design notes already agreed (docs/spec/admin-system-overview.md, "Decisions for step 3")

- Contact queue stored in SQLite so a restart resumes it.
- Dispatcher in the main process: at most N calls at once, starts spaced out (Plivo calls-per-second limit); a slot is freed
  by the hangup webhook, with a timeout backstop.
- Validate numbers (10-digit Indian), drop duplicates within a campaign and against recent calls, a do-not-call list,
  calling hours (e.g. 09:00-21:00 IST, TRAI), retries for no-answer/busy (e.g. 2 retries, 2 h apart).
- **Open decisions for the operator (not yet asked):** concurrency default and who can change it; whether the operator
  (calling team) sees lead results for their campaigns; calling hours and retry numbers; Excel column names and whether extra
  columns are kept; pause/resume/stop controls; what "done" looks like per contact (final status); a page for the calling
  team (upload + live status) vs everything in the existing calls page.
- Pages: the calling team needs a campaign page (upload, start/pause/stop, per-contact live status); the admin page stays for
  step 4. Login page and the two roles exist.

## How the operator likes to work

- Full process per step: brainstorm (one question at a time, with a recommendation) -> written spec in `docs/spec/` ->
  implementation plan in `docs/spec/` -> execution. Approve each stage in writing before the next.
- **Execution:** subagent-driven, a fresh implementer and a fresh reviewer per task, **every subagent on Sonnet 5.5**
  (explicit instruction), then a final whole-change review. The operator also hands whole plans to a Claude agent on EC2 via a
  handoff file (`docs/handoff_step2.md` is the template; it reports in `docs/handoff_step2_report.md`). The EC2 agent did
  step 2 well but its harness cannot pick the subagent model.
- **No local browser checks:** the operator tests directly on EC2. Do not ask them to run the app locally.
- Wants progress as percentages and effort/difficulty ranked when asked. Terse caveman-style answers.
- Commit/push only when asked. Never commit `.env`, credentials, `*.db`, `TRANSCRIPTS/`, `TEST_FILES/`. `bedrock_summary.py`
  is an untracked scratch file: leave it out of commits.
- `.env` is NOT frozen and is where new secrets/settings go, locally and on EC2 (`/etc/au-app/common.env` is not needed for
  new keys, but still holds older values and overrides `.env` for any key it sets). Never read `.env`.
- Tests: stdlib unittest only; suite `venv312/Scripts/python.exe -m unittest discover -s tests -t .` (Windows). Baseline
  **808 run, 3 known failures** (all in `tests/test_prompt_catalog.py`), 2 skipped, 1 expected failure. The tests need
  `PLIVO_AUTH_ID`/`PLIVO_AUTH_TOKEN` in the environment (a `.env` does that locally). Re-pin the `GEMINI_MODEL` line list in
  `tests/test_preservation_4_8_resumption_recording_stats.py` whenever `app.py`'s line count changes (now
  `[107, 1890, 2416, 2426, 2857]`). Every new `call_state` key is mirrored in `tests/harness/appctl.py`.

## Still open from steps 1-2 (small, none block step 3)

1. Log in as the operator on EC2 and confirm no cost shows on the calls page or the live feed (step 1 and 2 promise this).
2. Plivo: compare one call's `plivo_cost_usd` with the Plivo console and confirm the currency (assumed USD).
3. Bedrock price: `NLP_PRICE_INPUT/OUTPUT` = 0.11/0.55 USD per million tokens against the first AWS bill.
4. The EC2 agent's minor notes: `FINISHED` includes `skipped` rows in the backfill; old calls (before step 2) have no per-leg
   Gemini cost, only the old `cost` text, so step 4 totals must fall back to it for them.
5. `STT_PRICE_*` (Vertex transcription) still unconfirmed against billing; the transcript `LATIN_TERMS` list needs extending
   from live files.
6. Optional: set SQLite WAL mode when step 3 adds write traffic.

## Where things are

Specs and plans: `docs/spec/admin-system-overview.md` (shared), `admin-auth-design.md` / `admin-auth-plan.md` (step 1),
`call-analysis-design.md` / `call-analysis-plan.md` (step 2). Handoffs: `docs/handoff_step2.md`, report
`docs/handoff_step2_report.md`. Code: `web_auth.py`, `call_store.py`, `call_analysis.py`, `plivo_cost.py`,
`call_transcript.py`, `app.py`, `index.html`, `login.html`, `admin.html`.
