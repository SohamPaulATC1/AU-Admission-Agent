EC2 agent: start with docs/handoff1.md (bring-up checklist and live-call tasks for the Vertex backend and aec1). Active work: see docs/HANDOFF.md and docs/spec/tasks.md (tracked copy; keep .kiro/specs/bengali-grapheme-stutter-fix/tasks.md identical) before touching bargein.py, app.py, or aec.py. Standing constraints: prompt.txt is still Senco content (not a code bug, don't fix), requirements.txt and .env are frozen, aec.py is unfrozen but should stay untouched unless truly necessary.

Other standing rules: do not read .env or credential JSON. Tests are stdlib unittest only (no pytest/hypothesis). API_AUTH_TOKEN issue is noted in HANDOFF §10, not to be fixed. Every new call_state key must be added to the mirror in tests/harness/appctl.py (guarded by test_harness_smoke). Run the suite with `venv312/Scripts/python.exe -m unittest discover -s tests -t .` (Python 3.12 venv; 3.14 has no audioop). Don't write test code through bash heredocs: escapes like `\x00` / `\n` get mangled. Use the Edit tool or a .py script file. Printing app log lines needs `PYTHONIOENCODING=utf-8` (Windows console is cp1252).

Numbering: HANDOFF uses "Redesign Task N"; docs/spec/tasks.md uses 1-10 / 5.x. Mapping in HANDOFF §3.

## Current state (as of 2026-09-29)

- Suite: **251 tests OK** (2 skipped, 1 expected failure). `GEMINI_MODEL` pins in the 4_8 test: `[64, 1721, 2233, 2243, 2659]`. Re-pin whenever app.py's line count changes.
  - Skips: the retired Case C and the deferred reconnect test. Expected failure: `TestCaseDQuietCallerKnownLimitation`.
- Branch `dev`, no upstream: nothing pushed to origin. EC2 is synced by copying files (see memory / HANDOFF).
- Never commit `.env`, credential JSON, `CALL_RECORDINGS/`, `TEST_FILES/`, `debug_recordings/`, `*.db` (all gitignored; never force-add them). Commit only when asked.

### Resume here
0. **Vertex AI backend, connect-tested from Windows, NOT yet run on EC2 or a real call (2026-09-29).** `GEMINI_BACKEND` in app.py defaults to `vertex` (`studio` = AI Studio with GOOGLE_API_KEY, the rollback). Key file `silver-shift-490819-k0-1b9ed54b2663.json` in the repo root (gitignored via `silver-shift-*.json`; copy to EC2 by hand, chmod 600). Startup refreshes the token once, so a bad key fails before any dial.
   - `TEST_FILES/_vertex_smoke.py` (Windows, 2026-09-29): token in 0.39 s, Live session open in 1.42 s, greeting via `send_client_content` worked, first audio at 0.70 s, turn completed, ~$0.004. Confirms GCP is set up right (API enabled, `roles/aiplatform.user`) and the `eu` base_url override works.
   - `docs/handoff1.md` written for the EC2 agent: bring-up checklist (file hashes, key file mode, Python/SDK version, suite, the same smoke test run from EC2) plus the live-call tasks below. EC2 agent reports back in `docs/handoff1_ec2_report.md`.
   - Remaining live checks (now EC2's job): first-audio latency vs studio from Mumbai; reconnection/GoAway on a call past ~10 min; token refresh over a long uptime: not just once at startup; a rollback call with `GEMINI_BACKEND=studio`.
   - Cost still compounds per turn on Vertex (same prices as the app's constants; confirmed by the smoke test's usage line: prompt=1450 response=246). Capping it means `trigger_tokens` / `target_tokens` on `ContextWindowCompressionConfig`; not done, operator's call.
1. **Path B side-by-side trial (aec1.py), LIVE as of 2026-09-29.** `AEC_IMPL` in app.py now defaults to `aec1` (was parked 2026-09-28, then the operator asked for `python app.py` with no env var to run aec1 directly). `AEC_IMPL=aec` in the shell still falls back to stock. aec.py itself is still byte-identical (hash pinned).
   - Uncommitted: `aec1.py`, `tests/test_aec1.py` (new); `app.py`, `tests/harness/appctl.py`, `tests/test_preservation_4_8_resumption_recording_stats.py`, `CLAUDE.md` (modified).
   - Watch live calls for: the `[AEC]` stats line (impl and aec1's counters), the `[AEC-GUARD]` share (expected about 0% with aec1 converged, higher during the ~2.4 s convergence window; 55-68% is the stock baseline) and any phantom or missed barge-ins. Include at least one speakerphone call — that's the case aec1 is for. Replay recordings offline with `TEST_FILES/_aec_compare.py`. Fall back to `AEC_IMPL=aec` if a live call regresses.
2. **Redesign Task 8 (live-call validation), in progress.** Five live calls analysed (TEST1-5, 2026-09-24). Still to do:
   - Latch/threshold tuning on more calls, especially a caller talking over the agent at normal volume. Known miss: TEST5 17:22:17, where real caller speech (raw -26 dBFS) was judged echo and the latch held 13.5 s (`ended_by=new-playback`).
   - অন্তঃ echo-trigger live check (item G).
3. **Verify** whether the 2026-09-25 CancelledError/orphan-task fix also stops the stale "Terminating call: Gemini response timeout" (then "call not found") that fired ~27 s after the caller hung up in TEST3.
4. aec.py itself stays byte-identical (hash pinned by test_preservation_4_7). The path B fixes live only in aec1.py.
5. Known and unaddressed: echo at 160 ms delay truncates mid-playback (frame 12). Outside the realistic domain; predates this work.

## Session log (oldest first)

### 2026-09-23: Audio pipeline redesign, Tasks 5-7 (closed)
- **Redesign Task 5**, HANDOFF §5.6 and §5.7:
  - Gate window geometry fixed (180 ms near vs 260 ms far).
  - Echo latch added, plus the `ECHO_MAX_RETURN_DB=-6` ceiling (`near-too-loud-for-echo`).
  - Latch field logging: `[LATCH-HOLD]`, `[LATCH-END]`, `[LATCH]`.
- **Redesign Task 6**, HANDOFF §5.8:
  - Post-playback echo tail used to skip the gate and open phantom caller turns (41/42 in the harness).
  - Fixed with `ECHO_TAIL_FRAMES=8` (160 ms), the `far_silent_frames` key and `far_window_has_playback()`. Result 0/36 phantoms; cost +60 ms for a caller who speaks right at playback end.
  - Preroll trim on a genuine flush: NOT implemented (operator). Reopen only if live calls show Gemini reacting to its own voice at the start of a barge-in turn.
- **Redesign Task 7**, HANDOFF §8 #7:
  - Checks: suite, aec integrity (also in a fresh clone), kill switch, gate byte-neutrality.
  - All 8 non-reconciling items resolved per operator decisions. `TestDesignLiterals` added; the `not-measured[concern-a-parked]` log string is kept verbatim on purpose.
- Commits: `84eb866`, `be6c19f`, `52be2ce`, `35f75c3`, `770ce00` (task 4 re-baselines approved, 5.13 ticked).

### 2026-09-24: Live calls TEST1-5 and the AEC output guard
- **Aligned recordings:** `ALIGNED_RECORDING_KINDS` / `write_aligned_frames` write `_nearraw` (Plivo audio before AEC), `_farref` (the playout-paced reference) and `_aecout` (AEC output, before the guard) at 8 kHz, frame-for-frame aligned. `[ALIGNED-REC]` anchors map frames to wall clock. Operator approved the `wave.open` count re-baseline (3 to 4).
- **TEST2 root cause:** the handset already cancels echo (raw near about -72 dBFS during playback), but aec.py's filter adapts on near-silent reference frames and drifts, so it subtracts a filtered copy of the agent from a silent line (AEC output -48..-27 dBFS). That injected echo caused the phantom barge-ins. The earlier "600 ms acoustic delay" theory was wrong.
- **Fix, option A (operator's choice):** `guard_aec_output` in app.py. Per 20 ms frame, if the AEC output RMS is above the raw input's, the raw frame is passed on instead. aec.py is untouched.
  - Kill switch `AEC_OUTPUT_GUARD_ENABLED=0`, default on.
  - Counters `aec_guard_frames_checked` / `aec_guard_fallback_frames` (mirrored in appctl.py).
  - `[AEC-GUARD]` line in call stats, plus `aec_guard_fallbacks=` on the `[ALIGNED-REC]` anchors. No in-call log line, so the falsifier's pinned sequences are unchanged.
  - Tests: `TestAecOutputGuard` (8 tests) in test_instrumentation_5_4.py.
- **TEST3-5 verdict (guard live):** no echo-driven phantoms. All 26 barge-ins trace to real caller-side sound. The gate caught one real echo (TEST3 17:01:18, correlation 0.98). The 1000 closure at call end is our own close after the caller hangs up, not Gemini dying (this also explains TEST1). Open items are under "Resume here".
- Analysis scripts live in `TEST_FILES/` (gitignored, holds caller PII): `_review_call.py <folder> <stem> <log>` for per-call review; `TEST2/_replay_aec.py`, `_h_growth.py` and `_aec_counterfactual.py` for AEC forensics.
- Committed later as part of `a55f867` (app.py) and `09b1e64` (tests, appctl, .gitignore).

### 2026-09-25: Dashboard, hangup handling, error resilience
- **Dashboard UI:**
  - Pipeline text is now Dialing -> Answered -> Live -> Ended.
  - The generic failed pill is replaced with descriptive labels (Call Not Received, Busy / Unreachable, Voicemail).
  - Logo changed to AU_Logo.png.
- **Plivo:**
  - `/plivo-hangup` webhook captures and emits detailed hangup causes.
  - `machine_detection` (AMD) removed, because it produced false-positive hangups on humans (early media, background noise).
- **Resilience:**
  - `Task exception was never retrieved` fixed by catching `asyncio.CancelledError` in `coordinate_call_tasks` and cleaning up orphaned tasks on an abrupt Plivo disconnect.
  - `log_call_stats` emits `call_failed` instead of `call_ended` on internal errors.
  - The `index.html` SSE `onerror` handler fails stranded "Live" calls on a server disconnect or restart.

### 2026-09-28: Persistent call history
- **Storage and API:**
  - SQLite `call_history.db` persists `call_queued`, `call_ringing`, `call_connected`, `call_failed` and `call_ended` across restarts.
  - Paginated `GET /api/call-history` with date range, hour range, name search and status filters.
- **Frontend:**
  - Filter Bar in index.html with a vanilla-JS dual-month DatePicker.
  - Live SSE status is merged with the paginated history in the Call Feed.
  - Restyled filter bar with SVG icons.
- **Server:** Hypercorn `graceful_timeout=0.5` s, so Ctrl+C doesn't hang on open SSE connections.
- Commits: `a55f867` (UI changes, including app.py), `09b1e64` (local changes before pulling from EC2).

### 2026-09-28: Path B trial, aec1.py (uncommitted)
- **aec1.py:** a copy of aec.py with the path B fixes. Same public API and constructor. aec.py is untouched.
  - Adaptation happens only when the far end is active, the near end is above -60 dBFS, and the Geigel double-talk detector (threshold 0.5, 200 ms hangover) sees no double-talk.
  - Normalised step capped at `MU_MAX=1.0` (stock reaches about 2, the stability edge), with a noise floor in the Kalman gain and `LEAKAGE=5e-4`.
  - Two-path design: the background filter learns. The foreground filter produces the output and only takes the background weights when they remove at least 3 dB. It resets to zero if its output stays louder than the input. A diverging background is also reset.
  - The residual echo suppressor is skipped when the echo estimate is louder than the mic signal.
  - A `stats` dict of counters.
- **Offline results on TEST2-5:**
  - Real handset lines: output equals input (0% of frames louder, versus 55-69% for stock).
  - Synthetic speakerphone echo: 11-27 dB ERLE, versus -7 to +12 dB for stock.
  - Also better on loud-speaker, path-change and 420 ms delay cases.
  - Convergence about 2.4 s; CPU about 0.3 ms per frame, same as stock.
  - Scripts: `TEST_FILES/_aec_compare.py`, `_aec1_extra.py`, `_aec1_sweep.py`.
- **app.py:**
  - `AEC_IMPL` env var, read after `load_dotenv()`: `aec1` (default) or `aec`. Any other value raises at startup.
  - New call-stats line `🎛️ [AEC] impl=... <aec1 stats>`. `[AEC]` is added to DIAGNOSTIC_LOG_TAGS.
  - The guard stays on for both.
- **Tests:** `tests/test_aec1.py` (12 tests), plus the 4_8 pins re-pinned.

### 2026-09-29: aec1 becomes the default (uncommitted)
- Operator: "make a small change in app.py, I want a variable AEC_IMPL=aec1 in the code, I will start the app normally by python app.py". `AEC_IMPL = os.getenv("AEC_IMPL", "aec1")` -- flipped from the `aec` default. `AEC_IMPL=aec` in the shell still falls back to stock; the comment above it says so.
- `tests/test_aec1.py` renamed `test_default_is_stock_aec` / `test_aec1_is_selectable` to `test_default_is_aec1` / `test_stock_aec_is_selectable`, asserting the new default.
- 4_8 pins re-pinned again: `[64, 1629, 2143, 2153, 2568]` (the comment above `AEC_IMPL` grew a line).
- Suite: 235 OK, 2 skipped, 1 expected failure. aec.py hash still `495a82ca...`, unchanged.

### 2026-09-29: Vertex AI backend switch (uncommitted)
- Operator: connect through Vertex AI with the silver-shift service-account key; "a variable in the app.py that takes in either "studio" or "vertex"".
- **app.py:**
  - Config after `FROM_NUMBER`: `GEMINI_BACKEND` (default `vertex`, unknown value raises), `VERTEX_PROJECT` (`silver-shift-490819-k0`), `VERTEX_LOCATION` (`eu`, nearest supported to ap-south-1; gemini-3.8-live is only in us, eu, us-central1), `VERTEX_CREDENTIALS_PATH` (repo-root key file), `VERTEX_TOKEN_REFRESH_MARGIN_SECONDS=300`.
  - Helpers above `connect_live_with_timeout`: `vertex_base_url`, `load_vertex_credentials` (lazy, cached; import never reads the key), `vertex_token_needs_refresh`, `ensure_vertex_token` (refresh in `asyncio.to_thread` under a lock, because the SDK's own refresh inside live.connect is synchronous and would block the loop), `create_gemini_client`, `describe_gemini_backend`.
  - `connect_live_with_timeout` ensures the token first (vertex only). `handle_media_stream` uses `create_gemini_client()`. Session `[MODEL]` line gains `| backend=...`. `__main__` logs the backend and does one synchronous token refresh.
- **Tests:** `tests/test_gemini_backend.py` (16 tests, fakes only, no network, no key read). 4_8 pins re-pinned. `.gitignore` gains `silver-shift-*.json`.
- Suite: 251 OK, 2 skipped, 1 expected failure.

### 2026-09-29: Vertex connect smoke test, and docs/handoff1.md for EC2
- **Smoke test:** `TEST_FILES/_vertex_smoke.py` (gitignored, not committed). Reuses `create_gemini_client()` / `connect_live_with_timeout()` / the app's own `LiveConnectConfig` and greeting prompt, so it exercises the real code path with no audio and no Plivo. Run from Windows: token 0.39 s, session open 1.42 s, greeting sent via `send_client_content`, first audio 0.70 s, `turn_complete=True`, transcript matched the Senco persona, usage prompt=1450/response=246 (~$0.004). Saves `TEST_FILES/_vertex_smoke_greeting.wav`.
- **docs/handoff1.md:** written for a Claude agent running on EC2, to take the Vertex backend and aec1 the rest of the way to a validated live call. Covers: EC2 is not a git repo (file-copy sync only, backup old files first); the pre-2026-09-24 backup dir holds an old `.env`/creds, treat like `.env`; Python/`audioop`/SDK-version checks; a file-hash bring-up checklist; running the smoke test from EC2; then the live-call tasks (Vertex validation, aec1 trial, Redesign Task 8 remnants, the stale-timeout check) in priority order; env-var fallback table; where to write its findings (`docs/handoff1_ec2_report.md`) and the rule not to edit code on EC2 directly.
- `CLAUDE.md` top line now points the EC2 agent at `docs/handoff1.md` first.
