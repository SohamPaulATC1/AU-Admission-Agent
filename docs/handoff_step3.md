# Handoff: build and deploy step 3 (bulk campaigns + dispatcher) on EC2

For a Claude agent running on the EC2 server. Written 2026-10-12 by the Windows agent that designed
step 3 with the operator. You start where it stopped: **step 3 is designed, planned and dry-run tested
on Windows; no code is in the repo yet.** Your job: implement the plan, deploy it, prove it with real
campaigns, and report back. Step 4 (admin panel) is not yours.

You did step 2 (`docs/handoff_step2.md`, your report `docs/handoff_step2_report.md`); this works the
same way.

## 1. Read these first, in this order

1. `CLAUDE.md`: standing rules for this repo (they bind you; the relevant ones are repeated in §5).
2. `docs/spec/admin-system-overview.md`: the 4-step admin system and its shared decisions.
3. `docs/spec/campaigns-design.md`: **the spec for step 3. It is the binding authority.**
4. `docs/spec/campaigns-plan.md`: **the implementation plan: 6 tasks with the exact code and tests.**
   Follow it task by task.
5. Skim `docs/spec/call-analysis-design.md` (step 2): the campaign page joins `call_analysis`.

Do not re-design anything. If the plan's code is wrong, make the smallest fix that keeps the spec's
behaviour and record it as a deviation (§7). If a real design question comes up, stop and ask the
operator.

## 2. Where things stand

- **Step 1 (logins, Plivo signatures, signed media URLs): LIVE** since `f55e5c4`, `PLIVO_SIGNATURE_CHECK=enforce`.
- **Step 2 (Haiku analysis + full call cost): LIVE and verified** on a real transferred call. Code
  `387a21e` (yours) plus Windows follow-up fixes `2448772` (`/dial-action` saves the human leg in the
  background; the fixed Haiku prompt part is one cached system block). **Check that EC2 runs
  `2448772`**: if `git log -1` there shows `387a21e`, the `git pull` below brings both.
- **Step 3: spec and plan approved by the operator, nothing built.** The docs reach this server with
  the commit that carries this file.
- **Dry run:** the Windows agent applied every code block of the plan to a throwaway copy of
  `2448772`: 908 tests ran, and the only failures were the 3 known `test_prompt_catalog` ones plus the
  `GEMINI_MODEL` re-pin the plan expects (Task 4 Step 10). So the plan's code works as written on
  that base; a new failure on your side means something differs and is worth a look before you change
  anything.

## 3. What step 3 does (summary; details in the spec)

- A new `/campaigns` page for both logins (operator = the calling team, and admin): upload an .xlsx
  (name + phone columns found by header; every other column kept and shown), see rejected rows with a
  reason (bad number, duplicate, do-not-call, called in the last 30 days), set the campaign's
  concurrency, calling hours and retries, then Start / Pause / Resume / Stop, and watch each contact's
  status and, once Haiku has analysed the call, its lead status, visit day and summary. No cost
  anywhere on it.
- A dispatcher in the main process (8000): every 2 s it dials what is due, at most
  `CAMPAIGN_MAX_CONCURRENT` (8) campaign calls at once, each campaign up to its own concurrency
  (default 5), only inside calling hours (09:00-21:00 IST bounds), 1 s between dials. `/plivo-hangup`
  settles the contact (answered = done; no-answer / busy = retry later, up to the campaign's retries;
  else failed). A contact with no hangup after 30 min is closed as `no_hangup_event` (never re-dialled).
  Three Plivo dial errors in a row pause the campaign.
- Everything lives in `call_history.db` (new tables `campaigns`, `campaign_contacts`, `do_not_call`),
  so a restart resumes; WAL mode is switched on.

## 4. Operator decisions (2026-10-12): do not reopen them

- Concurrency: global cap 8 in `.env`, per-campaign 1..8 (default 5), chosen by the operator.
- The operator (calling team) sees only lead status, visit day and summary per contact. No costs, no
  transcripts.
- Hours and retries per campaign, inside `.env` bounds (TRAI).
- Extra Excel columns are kept. The contact's name and extras never reach the Gemini prompt (Neha
  addresses callers neutrally; that is already how app.py works).
- States draft / running / paused / stopped / done; a new `/campaigns` page; do-not-call is a table
  only (its UI is step 4).
- Dispatcher = a polling loop in 8000 (not event-driven, not a separate service).
- No new dependency.

## 5. Rules that bind you

- **Never read or print `.env`, `/etc/au-app/common.env` values, or credential JSON files.** You may
  check that a key name exists, never its value. Step 3 needs no secret; the `CAMPAIGN_*` settings have
  defaults and only go in `.env` if the operator wants other values (`.env` is the place for new
  settings; `common.env` still overrides `.env` for any key it sets).
- **Never restart the services while a call is live** (it drops the call). Ask the operator to confirm
  the line is idle before every restart. Never run `python app.py` by hand while the services run.
- **Tests never touch the real `call_history.db`, Plivo or the network.** The plan's tests use temp
  databases, an in-memory SQLite and mocks. Back up the live database before the deploy (§8 step 1).
- **Commits:** only when the operator asks. Branch `dev`. Never commit `.env`, credentials,
  `CALL_RECORDINGS/`, `TEST_FILES/`, `TRANSCRIPTS/`, `debug_recordings/`, `*.db`. When asked to
  commit, push to `origin/dev` so the Windows checkout can pull it. `bedrock_summary.py` (if present,
  untracked) stays out of commits.
- Do not touch `aec.py`, `aec1.py`, `bargein.py` or the prompt files. Keep the line
  `@app.route("/trigger-call", methods=["POST"])` byte-identical.
- Tests: stdlib `unittest` only. Write files with your edit tools, never through shell heredocs.
- Every new `call_state` key is mirrored in `tests/harness/appctl.py` (none is planned); when `app.py`'s
  line count changes, re-pin `GEMINI_MODEL` in `tests/test_preservation_4_8_resumption_recording_stats.py`
  (Task 4 Step 10).
- **No PII anywhere you write:** never log or report names, phone numbers, Excel contents or
  transcript text. The plan's log lines mask numbers to the last 4 digits; keep it that way. The
  database checks in §8 print counts and statuses only.
- **Real calls cost money and reach real people.** Only dial numbers the operator gives you for
  testing, in the campaigns of §8. Never upload or start a campaign the operator did not ask for.

## 6. The server

- Repo: `~/AU-Admission-Assist`, a git clone on branch `dev` tracking `origin/dev`.
- Python: **`venv/bin/python`** (3.12). The plan's commands say `venv312/Scripts/python.exe` (the
  Windows venv): use `venv/bin/python` everywhere instead. `PYTHONIOENCODING` is not needed on Linux.
- Suite: `venv/bin/python -m unittest discover -s tests -t .`
- Services: `au-app@8000` (main: dashboard, webhooks, database, Haiku worker, and from step 3 the
  dispatcher) and `au-app@8001..8003` (media only, `EVENT_FORWARD_URL` set). nginx sends
  `/media-stream` to all four, everything else to 8000.
  - Restart: `sudo systemctl restart au-app@800{0,1,2,3}`
  - Logs: `journalctl -u 'au-app@*' -f`, and `Gemini_Assistant.log` in the repo.

## 7. How to execute the plan

1. **Sync and baseline.** `git status` (clean), `git pull`, `git log --oneline -3` (expect this
   handoff's commit on top of `2448772`). Run the suite and write the numbers down. Windows baseline:
   808 run, 3 failures (all `tests/test_prompt_catalog.py`, known), 2 skipped, 1 expected failure. EC2
   shows more skips and one more known failure (`test_gemini_backend.TestVertexEndpoint.test_defaults`).
   Your numbers are the baseline; "green" means no failure beyond the known ones.
2. **Tasks 1 to 6 in order**, each as the plan writes it: failing tests first, run them and see the
   stated failure, implement, run them green, then the full suite. Do not start a task before the
   previous one is green.
3. **Execution mode.** The operator's preference is subagent-driven: a fresh implementer per task and a
   fresh reviewer after it, **all on Sonnet 5.5**, then one final whole-change review. If your harness
   cannot choose the subagent model, say so in the report (it could not for step 2) and use what it
   gives you. Without subagents, implement inline and review your own full diff against the spec
   before deploying.
4. **Deviations.** Each one goes in your report: file, what changed, why, cost if wrong.
5. **Stop and ask the operator** if: a design decision is needed; plan and spec contradict each other in
   a way the spec does not settle; a test would have to be weakened; anything needs a secret value.

## 8. Deploy and prove it (after Task 6, suite green)

Do these in order. Steps marked (operator) need the operator.

1. Back up the live database: `cp call_history.db call_history.db.bak-$(date +%Y%m%d-%H%M)`.
2. (operator confirms the line is idle) Restart all four services.
3. Startup checks in the log:
   - 8000 only: `📣 [CAMPAIGN] dispatcher started max_concurrent=8 hours=09:00-21:00 tick=2.0s`. The media
     processes must not log it.
   - Still there: `🧠 [ANALYSIS] worker started ...` (8000) and `🔐 [AUTH] logins configured, plivo checks=enforce` (four times).
   - WAL is on: `venv/bin/python -c "import sqlite3; print(sqlite3.connect('call_history.db').execute('PRAGMA journal_mode').fetchone())"` prints `('wal',)`.
4. (operator) Log in as the **operator** and open `/campaigns` (the calls page has a "Campaigns" link).
   Then **campaign A**: an .xlsx the operator makes with 3 internal test numbers (theirs or the team's),
   concurrency 2, default hours (the test must run between 09:00 and 21:00 IST), and one extra column
   such as City. Upload; check the rejected list is empty (or only what the operator expects), Start.
   Check:
   - `📣 [CAMPAIGN] dial ...` lines: at most 2 calls at once, at least 1 s apart, numbers masked
     (`xxxxxx1234`).
   - Each call rings, Neha talks, the calls page shows them as usual.
   - After each hangup: `📣 [CAMPAIGN] hangup ... status=done cause=...`. The first one also prints
     `📣 [CAMPAIGN] hangup fields: [...]`: copy that list into the report (names only).
   - When all 3 are finished: `📣 [CAMPAIGN] campaign=<id> done ...`, and the page shows the state as done.
   - After the analysis runs (a minute or so): the contacts show lead status / visit day / summary.
   - Logged in as the operator: no cost anywhere on `/campaigns` (and still none on the calls page).
5. (operator) **Campaign B, retries:** one number that will not be answered (the operator lets it ring
   out or rejects it), 1 retry, 30 min gap. Check: the first hangup gives `status=retry_wait` with the
   cause; about 30 min later a second dial (`attempt=2`); after that hangup `status=failed`.
   - **Check the cause names.** If a not-answered or busy call's `HangupCause` / `CallStatus` (from the
     hangup log line) is **not** in `campaigns.RETRY_CAUSES` / `RETRY_CALL_STATUSES`, it was wrongly
     marked `failed`: add the name to the set, add a case to `TestHangupOutcome.test_retry`, rerun the
     suite, restart when idle, and record it as a deviation. Same for an answered call that did not
     come out `done` (the answered check is `AnswerTime` or `BillDuration`; the field-names line tells
     you what Plivo really sends).
6. (operator) **Controls:** a campaign with a few numbers: Pause while a call is live (the live call
   finishes, nothing new dials), Resume (dialling continues), then Stop (waiting contacts become
   Cancelled; a live call finishes). Settings can be edited while paused, not while running.
7. Database check (prints no names or numbers):
   ```
   venv/bin/python -c "import sqlite3; c=sqlite3.connect('call_history.db'); print(c.execute('SELECT id, state, state_reason, concurrency, max_retries FROM campaigns').fetchall()); print(c.execute('SELECT campaign_id, status, attempts, last_cause FROM campaign_contacts ORDER BY campaign_id, id').fetchall())"
   ```
8. (operator) Then a small real list, when the operator chooses.
9. If something is wrong live: `CAMPAIGNS_ENABLED=0` in `.env` plus an idle restart stops the
   dispatcher (pages still load, nothing dials; single calls unaffected). Full rollback: check out
   `2448772` and restart (the new tables are harmless to the old code).

## 9. Your report

Write `docs/handoff_step3_report.md` (no PII), then tell the operator it is ready. Include:

- Per task: status, test counts, deviations (file, change, reason).
- Suite: your baseline and final numbers, pasted from the real output, and the new `GEMINI_MODEL` pins.
- Whether the subagents ran on Sonnet 5.5.
- Deploy: the startup lines seen, WAL on.
- Campaign A: dials spaced and capped, hangup statuses, the hangup field-names list, lead results
  shown, operator saw no cost.
- Campaign B: the causes Plivo sent for a ring-out and for a reject, whether retry_wait / failed came
  out right, any cause you had to add.
- Controls: pause / resume / stop behaviour seen.
- Anything odd: a `backstop` line (a lost hangup), a `tick failed` line, `dial failed` lines.
- Whether anything is uncommitted, and the commit hash if the operator asked you to commit.

Then update `CLAUDE.md`: a "Resume here" item `00000.` for step 3 with the live result, and the suite
line.
