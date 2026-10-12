# Step 2 report (EC2): call analysis + full call cost

Written 2026-10-12 by the EC2 agent, for the Windows agent and the operator. No PII: calls are
named by the first 8 characters of their `call_uuid`, and no field values are quoted.

**Status: built, deployed and running on EC2 since 2026-10-12 20:10 IST. All 4 old transcripts
were analysed and costed. The live-call check (§8.7) has not happened yet.** Nothing is committed
(branch `dev`, HEAD `6a844b8` = `origin/dev`).

## 1. Suite

| Run | Result |
|---|---|
| Baseline (EC2, before any change) | Ran 705, FAILED (failures=4, skipped=4, expected failures=1) |
| Final | Ran 803, FAILED (failures=4, skipped=4, expected failures=1) |

The 4 failures are the same in both runs:
- the 3 known ones in `tests/test_prompt_catalog.py` (`test_v2_is_the_default_prompt`,
  `test_language_mix_is_critical_with_examples`, `test_size_stays_close_to_v1`);
- `tests.test_gemini_backend.TestVertexEndpoint.test_defaults`, which fails on EC2 only
  (`os.path.dirname(VERTEX_CREDENTIALS_PATH)` is `.`, not the repo path). It predates step 2 and was
  left alone.

EC2 shows 4 skips against Windows' 2 because there are no `TEST_FILES/` recordings here.
`tests/analysis_spike.py` is not collected by the suite.

## 2. Tasks

Each task had a fresh implementer subagent and a fresh reviewer subagent, and a final review
covered the whole change. My harness doesn't let me pick the subagent model, so they may not have
run on Sonnet 5.5.

| Task | Status | New tests | Plan deviations |
|---|---|---|---|
| 1 `call_store.py` | done | 19 (+1 helper `transcript_path`, used by D8) | none in the task itself |
| 2 `call_transcript.py` (no cost line, `on_done`) | done | +5 / -2 | none (docstring updated) |
| 3 `call_analysis.py` request | done | 21 + 1 | D3, D12 |
| 4 `Analyzer` + `plivo_cost.py` | done | 22 + 9 | D4-D8 |
| 5 app.py wiring | done | 19 + 3 | D1, D2, D9-D11 |
| 6 spike script + docs | done | n/a | none |

### Deviations

| # | File | Change | Why | Cost if wrong |
|---|---|---|---|---|
| D1 | app.py `init_call_history_db` | `sqlite3.connect(..., timeout=30)` | 4 processes run the schema upgrade at once on first start; with the default 5 s wait one could hit "database is locked" at import and stop | a slower start under lock contention |
| D2 | app.py `persist_call_event_sync` (`call_ended`) | if `cost_usd` is missing, parse it from the `"$x"` text | the plan's code turned a cost-text-only event into `$0.0000`; that broke `test_operator_view` and would hit any pre-step-2 media process during rollout | none for step-2 events, which always carry `cost_usd` |
| D3 | `call_analysis.user_message` | strips the old `transcription_cost_usd:` lines | spec §2.5: "The analysis ignores that line" | a few fewer tokens per old file |
| D4 | `Analyzer._worker` | per-call lock (`_running`): a call queued again waits for its run in progress | without it, a transfer's 2nd transcript was analysed in parallel with the 1st, and the older run could finish last and overwrite the full-file result (spec §2.2) | none |
| D5 | `Analyzer.analyze`, `.start` | credentials error → `error="aws credentials"`, no retry; `ImportError` → `"anthropic not installed"`, no retry; one error at start if `AWS_ACCESS_KEY_ID`/`AWS_PROFILE` is missing | spec §3 Configuration | none |
| D6 | `Analyzer.backfill`, `plivo_cost.fetch_cost` | exceptions are logged (type name only) instead of killing the detached task silently | robustness | none |
| D7 | `plivo_cost` | the first record also logs the values of an allow-list (`total_amount`, `total_rate`, `bill_duration`, `billed_duration`, `call_duration`) | spec §2.4 asks for the record to confirm currency/unit; field names alone can't show that. No numbers or names are logged | none |
| D8 | `Analyzer.analyze` + `call_store.transcript_path` | `path=None` (a leg where nobody spoke) re-reads `calls.transcript_path` | a silent transfer-return leg was overwriting the first leg's analysis with `no_conversation` | none |
| D9 | app.py `start_background_work` | the Plivo catch-up query is guarded; catch-up fetches start 2 s apart (`delays=(2*i, 180, 600)`) | a locked DB must not stop 8000 from serving; a burst of `to_thread` Plivo calls must not fill the thread pool the call path uses | catch-up takes 2 s per call longer |
| D10 | app.py `finish_transcript` | no `transcript_ready` when `call_uuid` is missing (the `no-uuid-` file is still written) | prevents orphan analysis rows and a Haiku call with no `calls` row | none |
| D11 | app.py `persist_call_event_sync` | `cost=COALESCE(?, cost)` | a `call_failed` after `call_ended` (hangup cause other than NORMAL_CLEARING) blanked the cost text | none |
| D12 | `call_analysis.SCHEMA` `primary_program` | `{"enum": [*names, None]}` with no `"type"` | **Bedrock rejected** `type: ["string","null"]` + an enum holding null: `Enum value 'M.Sc (Chemistry)' does not match declared type '['string', 'null']'` | none: same field, same values |
| D13 | `Analyzer.analyze` | the client is built in `asyncio.to_thread` | `import anthropic` + client takes about 1 s; on 8000's loop (which also serves live media) that would stall a call | none |

Also: `.gitignore` gained `*.db.bak*` (the pre-deploy DB backup holds names and numbers), and the
GEMINI_MODEL pins are re-pinned to `[107, 1874, 2400, 2410, 2841]`.

Reviewer notes left open (minor): the `/dial-action` B-leg write is awaited before the XML reply
(milliseconds normally, up to 10 s on a locked DB); for a no-turns leg, `call_context` can be read
just before that leg's `call_ended` is committed (only `transferred` could be stale); backfill
treats `skipped` rows as finished (`FINISHED` includes it; spec §2.5 names only ok/failed).

## 3. Spike (real Bedrock call, before the restart)

On one existing transcript (`fc297b22`):
- First try: `403 Authentication failed`. My tool shell carries the IDE's own
  `AWS_BEARER_TOKEN_BEDROCK`, which the SDK prefers over access keys. The services don't have it
  (checked by name in 8000's process environment, the unit file, `.env` and `common.env`). The
  operator also unset it. The spike was rerun without it, using the `.env` keys.
- Second try: `400` schema error on `primary_program`, fixed by D12.
- Third try: `stop_reason: end_turn  problem: None`, 26 fields, tokens in=8708 out=542,
  cost $0.00126.
- Model, region and permissions are fine. The schema needed the one change above. The other
  nullable fields and the 87-value `programs_of_interest` enum were accepted.

## 4. Deploy

- DB backup: `call_history.db.bak-20261012-1230`. The suite's `import app` runs the schema
  upgrade, so the new columns already existed when the backup was taken (no row data had changed).
- SDK: `anthropic 1.13.0` was already installed in `venv`; `requirements.txt` pins
  `anthropic[bedrock]==1.13.0`. `pip check` is clean.
- AWS keys: the operator put them in **`.env`**, not `/etc/au-app/common.env`
  (`common.env` has none). Only the names were checked.
- Restart 2026-10-12 20:10 IST, after the operator confirmed the line was idle. All 4 services active.

Startup lines seen:
- `🔐 [AUTH] logins configured, plivo checks=enforce` four times.
- 8000 only: `🧠 [ANALYSIS] worker started model=us.anthropic.claude-haiku-5-5 concurrency=2`
  and `🧠 [ANALYSIS] backfill queued=4 of 4`. The media processes logged neither.
- No traceback and no `❌` line.

Catch-up: **4 of 4 analysed, 0 failed**, each on its first attempt, in 4.5-9.7 s.

| Call | lead | prospective | tokens in/out | Haiku $ | school_from | Plivo $ |
|---|---|---|---|---|---|---|
| 35b247bf | not_interested | no | 8125/374 | 0.0011 | none | 0.00950 |
| 35d5ad45 | not_interested | no | 7560/372 | 0.0010 | none | 0.00475 |
| de8cb782 | callback_requested | unclear | 7753/846 | 0.0013 | none | 0.00475 |
| fc297b22 | hot | yes | 8787/1449 | 0.0018 | haiku | 0.01662 |

The old cost lines were read into `stt_cost_usd`: $0.0082 in total over the 4 calls. These
pre-step-2 calls have `legs` / `gemini_cost_usd` empty; their Gemini cost is only in the old
`cost` text.

Plivo (the catch-up covered the last 24 h, 4 calls, all `ok`):
- `💰 [PLIVO-COST] record fields: [... 'bill_duration', 'billed_duration', 'call_duration', ...
  'total_amount', 'total_rate', ...]` (names only).
- `record billing: {'total_amount': '0.01662', 'total_rate': '0.00475', 'bill_duration': 189,
  'billed_duration': 210, 'call_duration': 189}`. That is a per-minute rate of 0.00475 on 60 s
  billing blocks (210 s = 3.5 min × 0.00475 = 0.016625). `total_amount` is the right field. The
  record has no currency field.

## 5. Live call (§8.7): NOT DONE YET

No call has been made since the restart. Still to check on the first one (ideally with a transfer
and the return):
- the `📝 [TRANSCRIPT]` line, then `🧠 [ANALYSIS] call=... status=ok`, then
  `💰 [PLIVO-COST] ... legs=<1 or 2>` about 60 s after hangup;
- the new transcript file has no `transcription_cost_usd:` line;
- the DB row (`legs`, the four costs, `cost` text, the analysis status/lead/score/school/transferred/tokens), using the query in `docs/handoff_step2.md` §8.7;
- `plivo_cost_usd` against the Plivo console, and the currency;
- operator login: no cost on the calls page or in the live feed.

## 6. Cost per call in practice

Haiku over 5 real runs (4 catch-up + spike): about 8,100 input / 720 output tokens per analysis,
**about $0.0013 per call** ($0.0010-0.0018). The input is dominated by the system prompt plus the
87 program names (about 7k tokens), so short calls cost nearly as much as long ones. Plivo was
$0.00475 per started minute on these calls. Gemini and STT are measured per call from the next
call onwards.

## 7. Open items

1. **Live call (§5).**
2. Bedrock price: `NLP_PRICE_INPUT/OUTPUT` = 0.11/0.55 USD per million tokens, to check against the
   first AWS bill.
3. Plivo currency (assumed USD) and one amount against the console.
4. The AWS keys live in `.env`. `/etc/au-app/common.env` overrides `.env` for every variable it
   sets, and it sets no AWS variable, so this works. Move them to `common.env` if that is the
   standard place for secrets on this box.
5. Reviewer minors in §2 (B-leg write before the XML reply, `skipped` counted as finished).
6. Prompt-caching the fixed system prompt and program list would cut the ~7k fixed input tokens.
   Not built; it's a step 3/4 question if volume grows.

## 8. Rollback

- `ANALYSIS_ENABLED=0` and/or `PLIVO_COST_ENABLED=0` in `/etc/au-app/common.env`, then restart
  (idle line). Transcripts and calls keep working.
- Full: check out `6a844b8` (or `f55e5c4`) and restart. The new columns and table are harmless to
  the old code.

## 9. Uncommitted

Everything in step 2 is uncommitted on `dev`. Modified: `.gitignore`, `app.py`,
`call_transcript.py`, `requirements.txt`, `web_auth.py`, `tests/harness/appctl.py`,
`tests/test_call_transcript.py`, `tests/test_preservation_4_8_resumption_recording_stats.py`,
`tests/test_transcript_capture.py`, `tests/test_web_auth.py`, `CLAUDE.md`,
`docs/spec/call-analysis-design.md`, `docs/spec/admin-system-overview.md`. New: `call_store.py`,
`call_analysis.py`, `plivo_cost.py`, `tests/analysis_spike.py`, `tests/test_call_store.py`,
`tests/test_call_analysis.py`, `tests/test_analysis_worker.py`, `tests/test_plivo_cost.py`,
`tests/test_analysis_wiring.py`, this report. Commit and push to `origin/dev` on the operator's word.
