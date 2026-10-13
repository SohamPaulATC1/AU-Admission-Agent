# Handoff: build and deploy step 4 (the admin panel) on EC2

For a Claude agent running on the EC2 server. Written 2026-10-13 by the Windows agent that designed
step 4 with the operator. You start where it stopped: **step 4 is designed, planned and dry-run tested
on Windows; no step 4 code is in the repo yet.** Your job: implement the plan, deploy it, check it with
the operator on the live server, and report back. It is the last step of the admin system.

You built steps 2 and 3 (`docs/handoff_step2.md`, `docs/handoff_step3.md`, your reports
`docs/handoff_step2_report.md`, `docs/handoff_step3_report.md`); this works the same way.

## 1. Read these first, in this order

1. `CLAUDE.md`: standing rules for this repo (they bind you; the relevant ones are repeated in §5).
2. `docs/spec/admin-system-overview.md`: the 4-step admin system and its shared decisions.
3. `docs/spec/admin-panel-design.md`: **the spec for step 4. It is the binding authority.**
4. `docs/spec/admin-panel-plan.md`: **the implementation plan: 7 tasks with the exact code and tests.**
   Follow it task by task. Its Global Constraints and Review Focus sections apply to every task.
5. Skim `docs/spec/call-analysis-design.md` (step 2: the `calls` cost columns, `call_analysis`, the
   `Analyzer`) and `docs/spec/campaigns-design.md` (step 3: `campaigns`, `campaign_contacts`,
   `do_not_call`). Step 4 reads all of them and changes none of their behaviour.

Do not re-design anything. If the plan's code is wrong, make the smallest fix that keeps the spec's
behaviour and record it as a deviation (§7). If a real design question comes up, stop and ask the
operator.

## 2. Where things stand

- **Step 1 (logins, Plivo signatures, signed media URLs): LIVE** since `f55e5c4`, `enforce`.
- **Step 2 (Haiku analysis + full call cost): LIVE and verified** (`387a21e` + `2448772`).
- **Step 3 (campaigns + dispatcher): built and deployed by you** (`4e08359`), reviewed on Windows. Its
  live campaigns A/B and the controls check (your step 3 handoff §8 steps 4-7) **have not run yet**.
  They are not part of this handoff; if the operator wants them done in the same session, do them
  before or after step 4 as the operator says, and report them in `docs/handoff_step3_report.md`.
- **Prompt v4 (Neha never uses a caller's name), done on Windows, arrives with this handoff's commit:**
  `PROMPT_FILES/prompt_au_v4.txt`, `app.py` (`PROMPT_FILE` default v4, nameless `GREETING_EVENT`),
  `tests/test_prompt_v4.py` (71 tests), `tests/_vertex_smoke.py`, the `GEMINI_MODEL` pins
  `[108, 1906, 2428, 2438, 2869]`. It goes live with the step 4 restart (§8); nothing to build for it.
- **Step 4: spec and plan approved by the operator, nothing built.** The plan was reviewed on Windows
  (by Opus 5.5) and revised: a re-run needs the transcript file (else 409, so a good analysis is never
  wiped), the calls CSV is built in the thread pool, the page's re-run poll waits past `pending`, and
  the `GEMINI_MODEL` re-pin sits in Task 4.
- **Dry run:** the Windows agent applied every code block of the plan, exactly as written, to a
  throwaway copy of `4e08359` plus the v4 files: **1098 tests ran, only the 3 known `test_prompt_catalog`
  failures.** A script then checked every line of that code against the plan. So the plan works as
  written on that base; a new failure on your side means something differs and is worth a look before
  you change anything.

## 3. What step 4 does (summary; details in the spec)

- `/admin` (admin login only; an operator is sent to `/`) becomes one page with five views, switched by
  the URL hash: **Overview** (calls, connected, hot, warm, visits fixed; total cost and its parts
  Gemini / STT / Haiku / Plivo / "Earlier calls (no split)"; cost per call and per hot lead; notes for
  failed analyses and pending Plivo costs), **Calls & leads** (50 a page, filters, search, Export CSV,
  "Re-run all failed"), **Call detail** (every field, the four costs, the transcript, Re-run analysis),
  **Campaign reports** (funnel and cost per campaign, Export CSV) and **Do-not-call** (add, remove,
  search, Export CSV). A date range (default the last 7 days, IST) applies to the first three.
- `admin_data.py` (new) holds every query and the CSV writer; `app.py` gets twelve `/api/admin/` routes,
  all `ADMIN_API` in `web_auth.ENDPOINT_ACCESS`; `Analyzer` gets `is_busy`; one new index
  `calls_created_at`.
- Three log lines in `app.py` stop printing the full phone number (and one the name):
  `📞 Outbound call answered by:`, `Client connected to Quart WebSocket. Caller:`,
  `Initiating outbound call to`. They now show `xxxxxx1234`.

## 4. Operator decisions (2026-10-13): do not reopen them

- Five views as above, each table with a CSV export; no charts.
- Only the admin login uses `/admin`; the operator login is unchanged and never sees a cost.
- CSV only (UTF-8 with a BOM), no `.xlsx`.
- Hot lead = `lead_status 'hot'`, warm = `'warm'`, visits fixed = `campus_visit_agreed = 1`; cost per hot
  lead = all calls' cost in the range / hot count.
- Re-run: one call from Call detail, plus "Re-run all failed" at most 50 per click; a re-run overwrites
  the analysis and adds its cost to the call's Haiku cost.
- Range default the last 7 days, quick buttons Today / 7 days / 30 days / This month / All time, custom
  from/to, kept in the URL.
- One `admin.html` with tabs + JSON APIs + `admin_data.py`. The nightly S3 copy of the database is
  **not** part of step 4.
- No new dependency, no new `.env` key, no kill switch (the views are read-only; rollback is a revert).

## 5. Rules that bind you

- **Never read or print `.env`, `/etc/au-app/common.env` values, or credential JSON files.** You may
  check that a key name exists, never its value. Step 4 needs no secret and no new setting.
- **Never restart the services while a call is live** (it drops the call), and never while a campaign
  is running. Ask the operator to confirm the line is idle and no campaign is `running` before every
  restart. Never run `python app.py` by hand while the services run.
- **Tests never touch the real `call_history.db`, Plivo, Bedrock or the network.** The plan's tests use
  temp databases and in-memory SQLite, and a real `Analyzer` object whose queue they only inspect.
- **Commits:** only when the operator asks. Branch `dev`. Never commit `.env`, credentials,
  `CALL_RECORDINGS/`, `TEST_FILES/`, `TRANSCRIPTS/`, `debug_recordings/`, `*.db` or database backups.
  When asked to commit, push to `origin/dev` so the Windows checkout can pull it. `bedrock_summary.py`
  (if present, untracked) stays out of commits.
- Do not touch `aec.py`, `aec1.py`, `bargein.py` or the prompt files. Keep the line
  `@app.route("/trigger-call", methods=["POST"])` byte-identical (Task 7 changes one log line inside
  that function, not the route line).
- Tests: stdlib `unittest` only. Write files with your edit tools, never through shell heredocs.
- No new `call_state` key is planned (if one appears, mirror it in `tests/harness/appctl.py`). When
  `app.py`'s line count changes, re-pin `GEMINI_MODEL` in
  `tests/test_preservation_4_8_resumption_recording_stats.py` (Task 4 Step 6).
- **No PII anywhere you write:** never log or report names, phone numbers, transcript text, analysis
  summaries or CSV contents. The admin page shows full numbers and transcripts to the admin; that is the
  point of it, but none of it goes into your report, the log you quote, or a commit message. The
  database checks in §8 print counts and sums only.
- **Re-runs cost money (Bedrock, about $0.001 each).** Trigger a re-run only when the operator asks, on
  the calls they choose. "Re-run all failed" only with the operator's go-ahead.
- **Do-not-call changes are real:** a number on the list is never dialled by a campaign. Add only a
  number the operator gives you for the test, and remove it again afterwards if they say so.

## 6. The server

- Repo: `~/AU-Admission-Assist`, a git clone on branch `dev` tracking `origin/dev`.
- Python: **`venv/bin/python`** (3.12). The plan's commands say `venv312/Scripts/python.exe` (the
  Windows venv): use `venv/bin/python` everywhere instead. `PYTHONIOENCODING` is not needed on Linux.
  The plan's note about dummy env vars for a shell without `.env` does not apply here.
- Suite: `venv/bin/python -m unittest discover -s tests -t .`
- Services: `au-app@8000` (main: dashboard, webhooks, database, Haiku worker, dispatcher, and from
  step 4 every `/api/admin/` route) and `au-app@8001..8003` (media only, `EVENT_FORWARD_URL` set).
  nginx sends `/media-stream` to all four and everything else to 8000. **The re-run routes depend on
  that:** only 8000 has the `Analyzer`; any other process answers 503.
  - Restart: `sudo systemctl restart au-app@800{0,1,2,3}`
  - Logs: `journalctl -u 'au-app@*' -f`, and `Gemini_Assistant.log` in the repo.

## 7. How to execute the plan

1. **Sync and baseline.** `git status` (clean; if not, stop and ask: earlier EC2 sessions committed or
   left edits), `git pull`, `git log --oneline -3` (expect this handoff's commit on top of `4e08359`).
   Run the suite and write the numbers down. Expected on EC2: **995 run, 4 failures** (the 3 known
   `tests/test_prompt_catalog.py` ones plus the EC2-only
   `test_gemini_backend.TestVertexEndpoint.test_defaults`), 4 skipped, 1 expected failure. Check the
   `GEMINI_MODEL` test passes at `[108, 1906, 2428, 2438, 2869]`: the plan's re-pin assumes it. Your
   numbers are the baseline; "green" means no failure beyond the known ones.
2. **Tasks 1 to 7 in order**, each as the plan writes it: failing tests first, run them and see the
   stated failure, implement, run them green, then the full suite. Do not start a task before the
   previous one is green. The plan's expected totals are Windows numbers (995 base): on EC2 the counts
   are the same and the failures are those 4.
3. **Execution mode.** The operator's preference is subagent-driven: a fresh implementer per task and a
   fresh reviewer after it, **all on Sonnet 5.5**, then one final whole-change review. If your harness
   cannot choose the subagent model, say so in the report (it could not for steps 2 and 3) and use what
   it gives you. Without subagents, implement inline and review your own full diff against the spec
   before deploying. Reviewers should check against the spec and the plan's Review Focus, not only
   that the code was copied.
4. **Deviations.** Each one goes in your report: file, what changed, why, cost if wrong.
5. **Stop and ask the operator** if: a design decision is needed; plan and spec contradict each other in
   a way the spec does not settle; a test would have to be weakened; anything needs a secret value.
6. **Known small items, deliberately not in the plan; do not fix them unasked:** the overview's
   "Plivo costs not fetched yet" note also counts old answered calls that will never be fetched; the
   do-not-call search sends one request per keystroke; `test_a_number_added_here_stops_a_campaign_upload`
   checks the stored row only. Mention them in the report only if you see them matter live.

## 8. Deploy and check it (after Task 7, suite green)

Do these in order. Steps marked (operator) need the operator.

1. **Back up the live database with the SQLite backup API** (it is in WAL mode, so `cp` can miss the
   newest writes):
   ```
   venv/bin/python -c "import sqlite3, time; s=sqlite3.connect('call_history.db'); d=sqlite3.connect(time.strftime('call_history.db.bak-%Y%m%d-%H%M')); s.backup(d); d.close(); print('ok')"
   ```
2. (operator confirms the line is idle and no campaign is running) Restart all four services.
3. **Startup checks** in the log:
   - `📝 Prompt: PROMPT_FILES/prompt_au_v4.txt (...)` (prompt v4 is live now).
   - Still there: `🧠 [ANALYSIS] worker started ...` and `📣 [CAMPAIGN] dispatcher started ...` (8000 only),
     `🔐 [AUTH] logins configured, plivo checks=enforce` (four times).
   - The index exists:
     `venv/bin/python -c "import sqlite3; print([r[1] for r in sqlite3.connect('call_history.db').execute('PRAGMA index_list(calls)')])"`
     lists `calls_created_at`.
4. (operator) **Access.** Logged in as the **operator**: `/admin` sends them to `/`, and opening
   `/api/admin/overview` in the browser gives a 403 JSON. Logged out: `/admin` goes to `/login`.
5. (operator) **The five views**, logged in as the **admin** (the calls page has an "Admin" link):
   - Overview with "All time": the counts and the total cost. Compare with the database (counts and
     sums only, no PII):
     ```
     venv/bin/python -c "import sqlite3; c=sqlite3.connect('call_history.db'); print(c.execute(\"SELECT COUNT(*), SUM(connected_at IS NOT NULL), ROUND(SUM(COALESCE(gemini_cost_usd, CAST(REPLACE(cost,'\$','') AS REAL), 0) + COALESCE(stt_cost_usd,0) + COALESCE(nlp_cost_usd,0) + COALESCE(plivo_cost_usd,0)), 4) FROM calls\").fetchone()); print(c.execute(\"SELECT lead_status, COUNT(*) FROM call_analysis GROUP BY 1\").fetchall())"
     ```
     The page's Calls / Connected / Total / Hot / Warm must match.
   - The quick range buttons change the numbers and the URL; a refresh keeps the range.
   - Calls & leads: a filter (Lead = hot), the search, Next/Previous, a row opens Call detail.
   - Call detail: the four costs, the analysis fields, the transcript (Bengali shows correctly).
   - Campaign reports: the step 3 campaigns (if any have run) with their funnel and cost.
   - Export CSV from Calls, Campaigns and Do-not-call: each downloads and opens in Excel with Bengali
     names readable. Do not paste any of it into the report; say only that it opened and how many rows.
6. (operator) **Re-run one call** the operator picks (one with a transcript): Re-run analysis shows
   "Queued…", then "Done." within about a minute; the log shows `🛠️ [ADMIN] analysis re-run queued
   call=<8 chars>` and then a `🧠 [ANALYSIS] call=... status=ok` line; the call's Haiku cost on the page
   went up by about $0.001. A call without a transcript has the button greyed out.
7. (operator) "Re-run all failed": only if the overview shows failed analyses and the operator wants
   it. Report the reply's counts (queued, busy, remaining, without a transcript).
8. (operator) **Do-not-call:** add a test number the operator gives you, check it is listed and the log
   shows `🛠️ [ADMIN] do-not-call added xxxxxx....`; optional: upload a one-row campaign with that number
   and see it rejected as on the do-not-call list (do not start it); then remove the number if the
   operator says so.
9. **Log masking:** on the next real call (the operator's test call or normal traffic, never one you
   start on your own), the three lines show `xxxxxx` plus 4 digits and no name. Check with
   `grep -c "Outbound call answered by: xxxxxx" Gemini_Assistant.log` and that no newer line has a full
   10-digit number after `answered by:` (count only; do not print the lines).
10. If something is wrong live: the panel is read-only apart from re-run and do-not-call, so nothing
    needs switching off in a hurry. Rollback: `git revert` the step 4 commit (or check out the commit
    before it) and an idle restart. The index is harmless to older code. Prompt v4's own rollback is
    `PROMPT_FILE=PROMPT_FILES/prompt_au_v3.txt` in `.env` plus an idle restart.

## 9. Your report

Write `docs/handoff_step4_report.md` (no PII), then tell the operator it is ready. Include:

- Per task: status, test counts, deviations (file, change, reason, cost if wrong).
- Suite: your baseline and final numbers, pasted from the real output, and the new `GEMINI_MODEL` pins.
- Whether the subagents ran on Sonnet 5.5; what the per-task and final reviews found and what you
  changed because of them.
- Deploy: the startup lines seen (including prompt v4), the index present.
- Access: operator 403 / redirect, logged-out redirect.
- Views: Overview numbers matched the database check (say yes/no and which differed, not the values
  if the operator prefers), range buttons, filters, detail, campaign reports, the three CSV exports.
- Re-run: one call (latency, cost added), "Re-run all failed" counts if run.
- Do-not-call: add / list / remove, masked log line, the optional upload rejection.
- Log masking seen on a real call.
- Anything odd in the log (`❌`, tracebacks, 500s on `/api/admin/`).
- Whether anything is uncommitted, and the commit hash if the operator asked you to commit.

Then update `CLAUDE.md`: Task 7 adds a "Resume here" item `00000000.` for step 4; amend it with the live
result, and update the EC2 suite line.
