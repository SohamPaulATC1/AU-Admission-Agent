# Handoff 1: EC2 agent (2026-09-29)

For the AI agent running on the EC2 instance. It takes over from the
agent on the operator's Windows machine. That machine is where code is written
and tested, and its `dev` branch is the source of truth. EC2 is where the app
runs, places real calls and writes logs and recordings. Your job is to bring up
the code below on EC2, validate it on live calls and report back what you find.

Read these first, in order: this file, `CLAUDE.md` (the standing rules and the
session log), then `docs/HANDOFF.md` (the audio pipeline redesign history, which
is also the reference for Redesign Task 8).

---

## 1. Environment facts

- Host `ubuntu@52.66.116.33`, AWS ap-south-1 (Mumbai). App dir
  `/home/ubuntu/AU-Admission-Assist/`.
- **EC2 is not a git repo.** Code arrives by git pull
- A backup of the pre-2026-09-24 tree is at
  `/home/ubuntu/AU-Admission-Assist.bak-20260924_074935`. It contains an old
  `.env` and credential JSON. Treat it like `.env`: don't read it, copy it,
  print it or delete it.
- The server is started by hand with `python app.py` (Hypercorn inside). There is
  no systemd unit. The operator places test calls from the dashboard; you can't
  dial anyone yourself.
- **Python version matters.** The app needs `audioop`, which Python 3.13+ removed.
  Use the existing EC2 venv, and check with
  `python -c "import sys, audioop; print(sys.version)"` inside it. If it is 3.13+
  or the import fails, stop and tell the operator. Don't install packages
  or change `requirements.txt` (frozen).
- The Windows `CLAUDE.md` gives the test command as
  `venv312/Scripts/python.exe -m unittest discover -s tests -t .`. On EC2 use the
  venv's `python` with the same arguments.

## 2. Standing rules (from CLAUDE.md, still binding on EC2)

- Do not read `.env` or any credential JSON, including the new Vertex key (the
  code loads it; you never need to open it). Don't `cat` it, `grep` it or log it.
- `requirements.txt` and `.env` are frozen. `PROMPT_FILES/prompt.txt` (moved there 2026-10-07; app.py now loads `PROMPT_FILE`, default `PROMPT_FILES/prompt_au_v2.txt`) is still Senco content
  (the greeting says "Sia from Senco Gold and Diamonds"). This is known and is
  not a bug, so leave it alone.
- `aec.py` must stay byte-identical (sha256 `495a82ca...`, pinned by
  test_preservation_4_7). The path B fixes live only in `aec1.py`.
- Tests: stdlib `unittest` only. Every new `call_state` key must also go into the
  mirror in `tests/harness/appctl.py`. Re-pin the `GEMINI_MODEL` line list in the
  4_8 test whenever app.py's line count changes.
- `API_AUTH_TOKEN` issue: noted in HANDOFF §10, not to be fixed.
- Recordings, logs and `*.db` hold caller PII. Never copy them off EC2 or paste
  transcripts beyond what a finding needs.
- Make no live API calls beyond the smoke test in section 4 without the
  operator's OK. Real calls are placed by the operator.

## 3. What is new since the last EC2 sync (2026-09-24)

All of this is on the Windows machine. Parts are uncommitted there (branch `dev`,
last commit `09b1e64`). Nothing is pushed.

1. **Vertex AI backend (new today, this handoff's main task).** `GEMINI_BACKEND`
   in app.py:
   - `vertex` (default): Vertex AI, authenticated with the service-account key
     `silver-shift-490819-k0-1b9ed54b2663.json` in the app dir.
   - `studio`: Google AI Studio with `GOOGLE_API_KEY`, the old path and the
     rollback.
   - Any other value stops the app at startup.
   - Other settings: `VERTEX_PROJECT=silver-shift-490819-k0` and
     `VERTEX_LOCATION=eu` (the model is only in `us`, `eu` and `us-central1`, and
     `eu` is nearest to Mumbai). `VERTEX_CREDENTIALS_PATH` defaults to
     `<app dir>/silver-shift-490819-k0-1b9ed54b2663.json`.
   - google-genai 1.66 builds the wrong hostname for `us` and `eu`, so app.py
     overrides the base URL to `https://aiplatform.eu.rep.googleapis.com/`. On
     `us-central1` the SDK's own default is used.
   - Access token: fetched once at startup, so a bad key fails before any dial.
     After that it is refreshed ahead of expiry (300 s margin) in a worker
     thread, so the SDK never refreshes synchronously on the event loop.
   - Logs: startup prints `🧬 [MODEL] Gemini backend=vertex project=... location=eu`
     and `🔐 Vertex AI credentials loaded and access token obtained`. Each
     session's `[MODEL]` line ends with `| backend=vertex ...`.
   - Verified from the Windows machine on 2026-09-29 (`TEST_FILES/_vertex_smoke.py`):
     - token in 0.39 s, Live session open in 1.42 s;
     - greeting via `send_client_content` works, first audio after 0.70 s;
     - turn completed, about $0.004.
   - Not yet run from EC2 and not yet used on a real call.
2. **aec1.py, the path B canceller, is the default** (`AEC_IMPL=aec1`, set
   2026-09-29). It gates adaptation on far activity, near level and double talk,
   and runs a two-path foreground/background design. `AEC_IMPL=aec` in the shell
   falls back to stock `aec.py`. The call stats gain an
   `🎛️ [AEC] impl=... <counters>` line. The AEC output guard (from 2026-09-24)
   stays on for both.
3. The 2026-09-25/28 dashboard, hangup-cause, call-history (SQLite
   `call_history.db`) and CancelledError work. It is committed on Windows as
   `a55f867` / `09b1e64`. The commit name "local changes before pulling from EC2
   updates" suggests some of it came from EC2, so EC2 may already have part of it.

## 4. Bring-up checklist (do in order, stop and report at the first failure)

1. **Confirm the operator copied these files** into the app dir, and check them
   against the hashes below (`sha256sum <file>`). If a hash doesn't match, don't
   start the app; tell the operator which file differs.

   ```
   app.py                                                    b324565a2ee887d2b41cdc0ec7f407a020e19b1b093c1053b7b26c7909f8757c
   aec.py                                                    495a82ca41a97460a9885aa1f62b9c856e8feee158fe4f8785223be3d20edd29
   aec1.py                                                   ff4afa8b7bcda002f0caa384455ace5f68cf4497b87d938b52f9a5c5d8d3150b
   bargein.py                                                aea4d037fbcd287b59592ac42eae307752af178eccc601eed527daf42f023600
   .gitignore                                                1c5e659ce26fcdfb74db9348c17062a77ccedf04fa8c8171b4eb518588472515
   tests/harness/appctl.py                                   3dc9106a1a26416ec84122d37345e2ee33c881e85974a78e1faee62ab72130f7
   tests/test_aec1.py                                        ad4537693eb17787919ad34ace58226a2bcbc93075a792fce9c6eabaf4ea7434
   tests/test_gemini_backend.py                              332d91cefae182a36bb610524bb9473950f1bf6b94567a47be2d4169e3cd2f89
   tests/test_preservation_4_8_resumption_recording_stats.py 7b0a27dd1bba565d8c24e841e4fc9ae1136c86eaa8e6030b127f78390979e6de
   ```

   Also expected, but not hashed (docs change often): `CLAUDE.md`,
   `docs/handoff1.md`, the rest of `tests/`, `index.html` and `AU_Logo.png`
   from the dashboard work, and `TEST_FILES/_vertex_smoke.py`. The operator
   must **not** copy the Windows `call_history.db`, `.env`, recordings or logs
   over EC2's own.

   Before the copy, the operator should back up EC2's current `app.py`,
   `index.html` and `aec*.py` (for example to
   `/home/ubuntu/AU-Admission-Assist.bak-20260929/`). If EC2 had edits of its
   own, they can then be diffed afterwards.
2. **Key file.** `ls -l silver-shift-490819-k0-1b9ed54b2663.json` should show it
   exists with mode `-rw-------` (`chmod 600` if not) and is owned by the user
   that runs the app. Check only its existence and mode; don't open it.
3. **Python.** Check the venv as in section 1. Then run
   `python -c "import google.genai, google.auth, requests; print(google.genai.__version__)"`,
   which should print `1.66.x`. If the version differs, report it before going on:
   the base URL override and the rejected-field list were checked against 1.66.
4. **Suite.** Run `python -m unittest discover -s tests -t .`. Expected:
   **251 tests OK, 2 skipped, 1 expected failure** (same as Windows). Any other
   result means the copy is inconsistent, so report it.
5. **Smoke test from EC2.** The operator has approved this one:
   `PYTHONIOENCODING=utf-8 python TEST_FILES/_vertex_smoke.py`. Expect lines `[1]`
   token OK, `[2]` session open, `[3]` first audio, `turn_complete=True`, a
   transcript and usage. Compare the timings with Windows (token 0.39 s, open
   1.42 s, first audio 0.70 s). EC2 is in Mumbai and should be similar or faster.
   If step `[2]` fails, retry with `VERTEX_LOCATION=us-central1` and report both
   results. The script writes `TEST_FILES/_vertex_smoke_greeting.wav`, which may
   be deleted afterwards.
   - Error meanings: 403 / `PERMISSION_DENIED` means the service account lacks
     `roles/aiplatform.user` or the API is not enabled. 404 / not found means a
     wrong location or model. DNS / connect errors mean an egress problem on EC2.
     All of these are for the operator to fix in GCP or AWS, not in code.
6. **Start the server** (`python app.py`) and confirm the two startup lines from
   section 3.1. Tell the operator it's ready for a test call.

## 5. Tasks, in priority order

### Task A: Vertex AI on live calls (new)
For each operator call, check the log for:
- `[MODEL] gemini-3.8-live | ... | backend=vertex project=silver-shift-490819-k0 location=eu`.
- The greeting plays after the disclaimer. The greeting is sent with
  `session.send_client_content(...)`. If Vertex ever ignores it (silence after
  the disclaimer, then `Gemini response timeout`), the fallback is
  `send_realtime_input(text=...)`. That is a code change, so propose it rather
  than applying it on EC2.
- Latency: time from the caller's end of speech to first model audio, versus
  earlier studio-backed calls in older logs.
- Resumption. Vertex sends GoAway about 60 s before its ~10 min connection
  limit. On a long call (over 10 min) the log should show
  `🔄 Gemini reconnection attempt` and then `✅ Gemini session resumed
  successfully`. Note: `MAX_CALL_DURATION_SECONDS` defaults to 900.
- `🔐 Vertex AI access token refreshed` should appear about once an hour of
  uptime, never on every call. Any auth error mid-call is a finding.
- A rollback test on one call is useful: restart with `GEMINI_BACKEND=studio`
  and confirm the line says `backend=studio`.
- **Cost:** Vertex bills the whole context window again on every turn, so cost
  per turn rises with call length, the same as AI Studio. The call-stats cost
  estimate notes this. Capping it (`trigger_tokens` / `target_tokens` on
  `ContextWindowCompressionConfig`) makes the model forget old turns, so it is
  the operator's decision, not yours. Report token counts from long calls to
  support that decision.

### Task B: aec1 live trial (Resume item 1 in CLAUDE.md)
- Per call, read the `🎛️ [AEC] impl=aec1 ...` counters and the `[AEC-GUARD]`
  share. Expected: about 0% guard fallback once converged (higher in the first
  ~2.4 s); 55-68% was the stock baseline.
- Watch for phantom barge-ins (the model is interrupted by its own echo) and missed
  barge-ins (the caller talks and isn't heard).
- Ask the operator for **at least one speakerphone call**. That is the case aec1
  exists for.
- If a call regresses, restart with `AEC_IMPL=aec` and compare.

### Task C: Redesign Task 8, live-call validation (Resume item 2)
- Latch and threshold tuning on more calls, especially a caller talking over the
  agent at normal volume. Known miss: TEST5 17:22:17, where real caller speech
  at raw -26 dBFS was judged echo and the latch held 13.5 s
  (`ended_by=new-playback`). Log tags: `[LATCH-HOLD]`, `[LATCH-END]`,
  `[LATCH]`, the echo gate lines, and `🧭 [TRIGGER] verdict=fired` for barge-ins.
- The অন্তঃ echo-trigger live check (item G in HANDOFF).
- Don't change thresholds on EC2. Report the per-event evidence (levels,
  correlation, timestamps) so the change can be made and tested on Windows.

### Task D: stale response-timeout check (Resume item 3)
In TEST3, "Terminating call: Gemini response timeout" (then "call not found")
fired about 27 s after the caller hung up. Check whether it still happens after
the 2026-09-25 CancelledError / orphan-task fix: after each call where the
caller hangs up, look for that line after the hangup.

## 6. Analysis tools on EC2

- Each call writes `debug_recordings/<name>_<timestamp>*.wav`: inbound, far end,
  and the aligned `_nearraw` / `_farref` / `_aecout` at 8 kHz. `[ALIGNED-REC]` log
  anchors map frames to wall-clock time.
- `TEST_FILES/_review_call.py <folder> <stem> <log>` gives a per-barge-in
  review (levels in the 600 ms before each trigger, near/far correlation). It
  has **Windows paths hardcoded** (`sys.path.insert` and `base = ...`). If the
  operator copies it over, change those two paths to local EC2 ones first. It
  needs numpy, which the venv already has, since aec.py needs it.
- Log file: `Gemini_Assistant.log` in the app dir. Use `PYTHONIOENCODING=utf-8`
  when printing log lines through Python; they contain emoji and Bengali.

## 7. Settings for fallback and diagnosis (shell env when starting app.py)

| Variable | Default | Use |
|---|---|---|
| `GEMINI_BACKEND` | `vertex` | `studio` = AI Studio rollback |
| `VERTEX_LOCATION` | `eu` | `us-central1` or `us` if `eu` fails or is slow |
| `VERTEX_CREDENTIALS_PATH` | app dir key | only if the key lives elsewhere |
| `AEC_IMPL` | `aec1` | `aec` = stock canceller |
| `AEC_OUTPUT_GUARD_ENABLED` | `1` | `0` disables the guard (diagnosis only) |
| `ECHO_GATE_ENABLED` | `1` | `0` disables the echo gate (diagnosis only) |

Set these only in the shell. `.env` is frozen.

## 8. Reporting back

Return the user back your thoughts. Include:
- the bring-up results (each step in section 4, pass or fail, with timings);
- for each live call: the time, backend and location, `[AEC]` counters,
  `[AEC-GUARD]` share, barge-ins with a real/phantom verdict and evidence,
  latch events, latency, token counts, and any errors quoted exactly;
- open questions for the operator.

If you think code needs to change, describe the change and why in the report
(file, function, proposed diff). Don't edit code on EC2: it isn't version
controlled and the next sync overwrites it. The one exception is an emergency the
operator explicitly asks you to fix in place. Then list every edited file and
its new sha256 in the report, so the Windows side can mirror it.
