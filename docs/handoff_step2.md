# Handoff: build and deploy step 2 (call analysis + full call cost) on EC2

For a Claude agent running on the EC2 server. Written 2026-10-12 by the agent that designed step 2
with the operator on Windows. You start exactly where it stopped: **step 2 is designed and planned,
no code is written yet.** Your job is to implement the plan, deploy it, prove it on a live call, and
report back. Step 3 is not yours; the operator brings your report to the Windows agent for it.

## 1. Read these first, in this order

1. `CLAUDE.md`: standing rules for this repo (they bind you; the relevant ones are repeated in §5).
2. `docs/spec/admin-system-overview.md`: the 4-step admin system and the shared decisions.
3. `docs/spec/call-analysis-design.md`: **the spec for step 2. It is the binding authority.**
4. `docs/spec/call-analysis-plan.md`: **the implementation plan: 6 tasks with the exact code and
   tests.** Follow it task by task.
5. `docs/spec/admin-auth-design.md` (skim): step 1, already live; you build on its access table and
   its `without_cost` filter.

Do not re-design anything. If the plan's code is wrong, make the smallest fix that keeps the spec's
behaviour and record it as a deviation (see §7). If a real design question comes up, stop and ask
the operator.

## 2. Where things stand

- **Step 1 (logins and protection) is LIVE** since 2026-10-12, commit `f55e5c4`, with
  `PLIVO_SIGNATURE_CHECK=enforce`. Operator and admin logins work; Plivo webhooks are signature
  checked; `/media-stream` URLs are signed. Rollback point before step 1: `891e88f`.
- **Step 2: spec and plan approved by the operator, nothing built.** The docs reached this server
  with the commit that carries this file.
- Steps 3 (bulk Excel campaigns + dispatcher) and 4 (admin panel) come later, designed on Windows.

## 3. What step 2 does (summary; details in the spec)

After every call, `call_history.db` gets:
- a `call_analysis` row from **Claude Haiku 5.5 on Amazon Bedrock**: lead status, is the contact a
  prospective student, interest score 1-10, academics, program and school, scholarship, campus
  visit, callback, objections, English summary, next action;
- four costs on the `calls` row: Gemini Live (summed over the legs of a transfer), transcript STT,
  Haiku, and Plivo telephony (from Plivo's call record, including the human leg of a transfer).

The transcript files stop carrying the cost line; the STT cost goes into the database. Old
transcripts already in `TRANSCRIPTS/` are analysed once on first start.

## 4. Operator decisions (2026-10-12): do not reopen them

- Haiku runs in the US: model `us.anthropic.claude-haiku-5-5` (Bedrock US cross-region profile; it
  already works from `bedrock_summary.py`).
- Client: the official Anthropic SDK, `anthropic[bedrock]` (`AsyncAnthropicBedrock`), added to
  `requirements.txt`. That is the only new dependency.
- The analysis worker runs only in the main process (port 8000), the only database writer.
- SQLite (`call_history.db`), not DynamoDB.
- All analysis fields kept; `interest_score` 1-10.
- Old transcripts are analysed once (catch-up).
- The operator (calling team) must never see costs; only the admin does.

## 5. Rules that bind you

- **Never read or print `.env`, `/etc/au-app/common.env` values, or credential JSON files.** You may
  check that a key name exists (`grep -c '^AWS_ACCESS_KEY_ID=' /etc/au-app/common.env`), never its
  value. The operator puts the AWS keys in place.
- **Never restart the services while a call is live** (it drops the call). Ask the operator to
  confirm the line is idle before every restart. Never run `python app.py` by hand while the
  services run (port clash, `Address already in use`).
- **Tests never touch the real `call_history.db`, AWS, Bedrock or Plivo.** The plan's tests use temp
  databases and fakes. Back up the live database before the first deploy (§8 step 1).
- **Commits:** only when the operator asks. Work on branch `dev`. Never commit `.env`, credentials,
  `CALL_RECORDINGS/`, `TEST_FILES/`, `TRANSCRIPTS/`, `debug_recordings/`, `*.db` (all gitignored).
  When asked to commit, push to `origin/dev` so the Windows checkout can pull it.
- Do not touch `aec.py`, `aec1.py`, `bargein.py` or the prompt files. Keep the line
  `@app.route("/trigger-call", methods=["POST"])` byte-identical.
- Tests: stdlib `unittest` only. Write files with your edit tools, never through shell heredocs.
- Every new `call_state` key must be mirrored in `tests/harness/appctl.py`; when `app.py`'s line count
  changes, re-pin the `GEMINI_MODEL` list in `tests/test_preservation_4_8_resumption_recording_stats.py`
  (both are steps in the plan).
- Never log transcript text, names or phone numbers. Your report must not contain them either
  (transcripts are student PII).

## 6. The server

- Repo: `~/AU-Admission-Assist`, a git clone on branch `dev` tracking `origin/dev`.
- Python: **`venv/bin/python`** (Python 3.12). The plan's commands say `venv312/Scripts/python.exe`
  (the Windows venv): use `venv/bin/python` instead everywhere. `PYTHONIOENCODING` is not needed on
  Linux.
- Suite: `venv/bin/python -m unittest discover -s tests -t .`
- Services: systemd template `au-app@.service`, instances `au-app@8000` (main: dashboard, webhooks,
  database, and from step 2 the analysis worker) and `au-app@8001..8003` (media only). nginx sends
  `/media-stream` to all four (least_conn), everything else to 8000.
  - Restart: `sudo systemctl restart au-app@800{0,1,2,3}`
  - Logs: `journalctl -u 'au-app@*' -f`, and the shared file `Gemini_Assistant.log` in the repo.
  - Environment: `/etc/au-app/common.env` (all four) overrides `.env`; `/etc/au-app/800{1,2,3}.env`
    set `EVENT_FORWARD_URL` (that is what makes a process "media only").

## 7. How to execute the plan

1. **Baseline first.** `git status` (should be clean), `git log --oneline -3`, then run the suite and
   write down the numbers. On Windows the baseline was 705 run, 3 failures (all in
   `tests/test_prompt_catalog.py`, known and pre-existing: leave them), 2 skipped, 1 expected
   failure. EC2 usually shows more skips (no `TEST_FILES/` recordings here); your own numbers are the
   baseline. "Green" means no failure beyond those 3.
2. **Tasks 1 to 6 in order**, each exactly as the plan writes it: failing tests first, run them and
   confirm the stated failure, implement, run them green, then the full suite. Do not start a task
   before the previous one is green.
3. **Execution mode.** The operator's preference is subagent-driven: a fresh implementer per task
   and a fresh reviewer after it, **all on Sonnet 5.5**, with a final whole-change review. If your
   harness has subagents, do that (skill `superpowers:subagent-driven-development` if available).
   Otherwise implement inline, then review your own full diff against the spec before deploying.
4. **Deviations.** Each one goes in your report: file, what changed, why, cost if wrong.
5. **Stop and ask the operator** if: a design decision is needed; the plan and spec contradict each
   other in a way you cannot settle by the spec; a test would have to be weakened; anything needs a
   secret value.

## 8. Deploy and prove it (after Task 6, suite green)

Do these in order. Steps marked (operator) need the operator.

1. Back up the live database: `cp call_history.db call_history.db.bak-$(date +%Y%m%d-%H%M)`.
2. Install the SDK: `venv/bin/pip install "anthropic[bedrock]"`, then `venv/bin/pip show anthropic`;
   pin the installed version in `requirements.txt` (replace `anthropic[bedrock]>=1.0` with
   `anthropic[bedrock]==<version>`).
3. (operator) AWS keys `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` and `AWS_DEFAULT_REGION` in
   `/etc/au-app/common.env`. Check only that the three names exist.
4. **Spike: one real Haiku call** before any restart (about $0.001):
   ```
   set -a; . /etc/au-app/common.env; set +a
   venv/bin/python tests/analysis_spike.py TRANSCRIPTS/<an existing call_uuid>.txt
   ```
   It must print `stop_reason: end_turn  problem: None`, token counts and the fields.
   - If Bedrock rejects the JSON schema (for example the nullable `"type": [..., "null"]` fields or
     the 87-value program enum), make the smallest change in `call_analysis.SCHEMA` that keeps every
     field, fix the tests, rerun the suite and the spike. Record it as a deviation.
   - A model, region or permission error: report it to the operator; do not deploy.
5. (operator confirms the line is idle) Restart all four services.
6. Startup checks in the log:
   - 8000 only: `🧠 [ANALYSIS] worker started model=us.anthropic.claude-haiku-5-5 concurrency=2`
     and `🧠 [ANALYSIS] backfill queued=<n> of <m>`. The media processes must not log these.
   - `🔐 [AUTH] logins configured, plivo checks=enforce` four times (step 1 still fine).
   - The old transcripts get analysed: `🧠 [ANALYSIS] call=... status=ok ...` lines, a few at a time.
7. (operator) One live call, ideally with a transfer to a human and back. Then check:
   - `📝 [TRANSCRIPT]` line for the call, then `🧠 [ANALYSIS] call=<8 chars> status=ok lead=...`.
   - About a minute after hangup: `💰 [PLIVO-COST] record fields: [...]` (first time only) and
     `💰 [PLIVO-COST] call=... status=ok total=$... legs=<1 or 2>`.
   - The new transcript file has no `transcription_cost_usd:` line.
   - The database row (prints no names or numbers):
     ```
     venv/bin/python - <<'EOF'
     import sqlite3
     c = sqlite3.connect("call_history.db")
     uuid = c.execute("SELECT call_uuid FROM calls ORDER BY datetime(created_at) DESC LIMIT 1").fetchone()[0]
     print(c.execute("SELECT legs, gemini_cost_usd, stt_cost_usd, nlp_cost_usd, plivo_cost_usd, plivo_cost_status, cost FROM calls WHERE call_uuid=?", (uuid,)).fetchone())
     print(c.execute("SELECT status, error, lead_status, is_prospective, interest_score, school_code, transferred, input_tokens, output_tokens FROM call_analysis WHERE call_uuid=?", (uuid,)).fetchone())
     EOF
     ```
     (This heredoc is a shell command for checking, not test code, so the no-heredoc rule does not apply.)
   - (operator) Compare `plivo_cost_usd` with the Plivo console for that call, and confirm the
     currency (assumed USD).
   - Log in as the operator on the calls page: no cost visible anywhere, including the live feed.
8. If something is wrong live: `ANALYSIS_ENABLED=0` and/or `PLIVO_COST_ENABLED=0` in
   `/etc/au-app/common.env` plus a restart turns that part off; transcripts and calls keep working.
   Full rollback: check out `f55e5c4` and restart (the new database columns are harmless to the old
   code).

## 9. Your report

Write `docs/handoff_step2_report.md` (no PII), then tell the operator it is ready. Include:

- Per task: status, test counts, deviations (file, change, reason).
- Suite: your baseline and final numbers, pasted from the real output.
- Spike: stop reason, tokens, cost, whether the schema needed a change. Not the field values (PII).
- Deploy: the startup lines seen, how many old transcripts the catch-up analysed and how many failed
  (with the error kinds).
- The live call: the database row values from §8.7 (costs, status, lead, prospective, score, school
  code, transferred, tokens), the Plivo field names line, the Plivo amount vs the console, the
  currency.
- Cost per call in practice: Haiku tokens and cost per analysis on real calls (average over the
  catch-up and the live call).
- Open items still to confirm: the Bedrock price (`NLP_PRICE_INPUT/OUTPUT` = 0.11/0.55 USD per million)
  against the first AWS bill; the Plivo currency; anything you deferred.
- Whether anything is uncommitted, and the commit hash if the operator asked you to commit.

Then update `CLAUDE.md`: the "Resume here" item `0000.` (step 2) with the live result, and the suite
line.
