EC2 agent: start with docs/handoff1.md (bring-up checklist and live-call tasks for the Vertex backend and aec1). Active work: see docs/HANDOFF.md and docs/spec/tasks.md (tracked copy; keep .kiro/specs/bengali-grapheme-stutter-fix/tasks.md identical) before touching bargein.py, app.py, or aec.py. Standing constraints: PROMPT_FILES/prompt.txt is old Senco content that app.py no longer loads (not a code bug, don't fix), requirements.txt and .env are frozen, aec.py is unfrozen but should stay untouched unless truly necessary.

Other standing rules: do not read .env or credential JSON. Tests are stdlib unittest only (no pytest/hypothesis). API_AUTH_TOKEN issue is noted in HANDOFF §10, not to be fixed. Every new call_state key must be added to the mirror in tests/harness/appctl.py (guarded by test_harness_smoke). Run the suite with `venv312/Scripts/python.exe -m unittest discover -s tests -t .` (Python 3.12 venv; 3.14 has no audioop). Don't write test code through bash heredocs: escapes like `\x00` / `\n` get mangled. Use the Edit tool or a .py script file. Printing app log lines needs `PYTHONIOENCODING=utf-8` (Windows console is cp1252).

Numbering: HANDOFF uses "Redesign Task N"; docs/spec/tasks.md uses 1-10 / 5.x. Mapping in HANDOFF §3.

## Current state (as of 2026-10-06)

- Suite: **473 tests run, all OK** (2 skipped, 1 expected failure). `GEMINI_MODEL` pins in the 4_8 test unchanged (app.py untouched by the find_eligible_programs and find_scholarships work). Re-pin whenever app.py's line count changes.
  - Skips: the retired Case C and the deferred reconnect test. Expected failure: `TestCaseDQuietCallerKnownLimitation`. The 6 `TestGoldenProfiles` run now that all rules are verified (they skip again if any UG rule goes back to `verified: false`).
  - `test_reply_language_counts_as_the_choice` fixed 2026-10-07: operator restored the TEST6 guard sentence ("If the user answers in Bengali or Hindi, that is their choice even if they never name a language: switch at once.") in `PROMPT_FILES/prompt_au.txt` call-flow step 1, replacing the `db6fe2b` wording.
- Branch `dev`, no upstream: nothing pushed to origin. EC2 is synced by copying files (see memory / HANDOFF).
- Never commit `.env`, credential JSON, `CALL_RECORDINGS/`, `TEST_FILES/`, `debug_recordings/`, `*.db` (all gitignored; never force-add them). Commit only when asked.

### Resume here
0. **Course catalog tools, built and tested offline, NOT yet on a live call (2026-10-05). Summary: `docs/HANDOFF2.md`.** `course_catalog.py` + `catalog/` give Gemini `list_programs` and `get_program_details` over 87 programs (2027 session). Spec `docs/spec/course-catalog-tools-design.md`, plan `docs/spec/course-catalog-tools-plan.md`.
   - Rebuild `catalog/courses.json` with `venv312/Scripts/python.exe catalog/build_catalog.py` whenever the Excel in `AU Admission Data/` changes; `test_committed_courses_json_is_fresh` fails until you do.
   - Try tools offline: `venv312/Scripts/python.exe tests/tool_playground.py` (interactive) or `... list_programs degree=B.Tech`.
   - **Live prompt is `PROMPT_FILES/prompt_au_v2.txt`** (2026-10-07, operator): the counselor rewrite, chosen by `PROMPT_FILE` in app.py (default v2; `PROMPT_FILE=PROMPT_FILES/prompt_au.txt` rolls back to v1). Startup logs `📝 Prompt: <file> (<n> chars)`. Prompt edits go in v2 now; v1 is kept as the rollback. `PROMPT_FILES/prompt.txt` is the old Senco prompt, unused. EC2 needs the `PROMPT_FILES/` folder (with v2), and the old root-level files deleted.
   - On live calls watch `🔎 [CATALOG]` lines (tool, status, chars) and the per-call `[CATALOG] calls= chars=` totals; tune `catalog/aliases.json` from `not_found` / `ambiguous` queries.
   - **`find_eligible_programs` built 2026-10-06** (spec §11, plan `docs/spec/find-eligible-programs-plan.md`). UG only; answers "I got 58% with PCB, what can I apply for?" from `catalog/eligibility_rules.json`, a hand-written, text-keyed rule set merged into `courses.json` by the build. **All 27 rules verified by the operator 2026-10-06** (only the `verified` flags changed; rule content is the draft as written), `courses.json` rebuilt (53/53 UG verified), golden profiles pass. Any later rule edit: rebuild with `catalog/build_catalog.py` and rerun the suite. Next: text-only tool test, then live calls. app.py untouched (it builds declarations from `course_catalog.TOOL_SPECS`).
   - **`find_scholarships` built 2026-10-06** (spec §13, plan `docs/spec/find-scholarships-plan.md`): best scholarship per program with rupees from its fees, `ask_about` hints, `ruled_out`; data `catalog/scholarships.json` (21 schemes, operator-verified, loaded and checked at import, no rebuild). Prompt `## SCHOLARSHIPS` added to `PROMPT_FILES/prompt_au.txt` (includes the AUAT explanation; Neha raises scholarships herself once per call, and call-flow step 4 asks for marks first). app.py untouched. Next: text-only tool test, then live calls; EC2 needs `catalog/scholarships.json` too.
   - EC2 needs: `course_catalog.py`, all of `catalog/` (the build test imports `build_catalog.py`), `app.py`, `tests/harness/appctl.py`, the new/changed tests.
   - **Known-good baseline: HEAD `a0ec061`** (operator, 2026-10-05: the code the catalog work was built on is what works correctly on EC2). The catalog work is uncommitted on top of it. Commit it on its own so one revert restores the EC2 version, and back up EC2's `app.py`, `PROMPT_FILES/prompt.txt`, `tests/harness/appctl.py` before copying anything over.
   - Test order agreed 2026-10-05: first a text-only tool test from Windows (one Gemini session with `PROMPT_FILES/prompt_au.txt` and the real declarations, typed questions, no Plivo; script goes in `TEST_FILES/`), then 3-5 live calls on EC2. Phase 2 waits for those results.
1. **Vertex AI backend, committed in `edb0bf1`, working on EC2 (operator, 2026-10-05).** `GEMINI_BACKEND` in app.py defaults to `vertex` (`studio` = AI Studio with GOOGLE_API_KEY, the rollback). Key file `silver-shift-490819-k0-1b9ed54b2663.json` in the repo root (gitignored via `silver-shift-*.json`; copy to EC2 by hand, chmod 600). Startup refreshes the token once, so a bad key fails before any dial.
   - `TEST_FILES/_vertex_smoke.py` (Windows, 2026-09-29): token in 0.39 s, Live session open in 1.42 s, greeting via `send_client_content` worked, first audio at 0.70 s, turn completed, ~$0.004. Confirms GCP is set up right (API enabled, `roles/aiplatform.user`) and the `eu` base_url override works.
   - `docs/handoff1.md` written for the EC2 agent: bring-up checklist (file hashes, key file mode, Python/SDK version, suite, the same smoke test run from EC2) plus the live-call tasks below. EC2 agent reports back in `docs/handoff1_ec2_report.md`.
   - `docs/handoff1_ec2_report.md` (2026-09-29) is out of date: it stopped at the smoke-test step, but the operator confirmed on 2026-10-05 that the `a0ec061` version (Vertex + aec1 defaults) runs correctly on EC2.
   - Default region is `us-central1` since 2026-10-05 (TEST6: `eu` sent seconds of mid-turn silence; see session log). Check EC2's `.env` does not set `VERTEX_LOCATION`, or the default is overridden.
   - Not reported either way, so still worth watching on the next calls: first-audio latency vs studio from Mumbai; reconnection/GoAway on a call past ~10 min; token refresh over a long uptime (not just once at startup). `GEMINI_BACKEND=studio` stays the rollback.
   - Cost still compounds per turn on Vertex (same prices as the app's constants; confirmed by the smoke test's usage line: prompt=1450 response=246). Capping it means `trigger_tokens` / `target_tokens` on `ContextWindowCompressionConfig`; operator declined it for now (2026-10-05), don't propose it again unless live-call bills ask for it.
2. **Path B (aec1.py), the default, committed in `edb0bf1`, part of the working EC2 version (operator, 2026-10-05).** `AEC_IMPL` in app.py defaults to `aec1`; `AEC_IMPL=aec` in the shell falls back to stock. aec.py itself is still byte-identical (hash pinned).
   - Watch live calls for: the `[AEC]` stats line (impl and aec1's counters), the `[AEC-GUARD]` share (expected about 0% with aec1 converged, higher during the ~2.4 s convergence window; 55-68% is the stock baseline) and any phantom or missed barge-ins. Include at least one speakerphone call — that's the case aec1 is for. Replay recordings offline with `TEST_FILES/_aec_compare.py`. Fall back to `AEC_IMPL=aec` if a live call regresses.
3. **Redesign Task 8 (live-call validation), in progress.** Five live calls analysed (TEST1-5, 2026-09-24). Still to do:
   - Latch/threshold tuning on more calls, especially a caller talking over the agent at normal volume. Known miss: TEST5 17:22:17, where real caller speech (raw -26 dBFS) was judged echo and the latch held 13.5 s (`ended_by=new-playback`).
   - অন্তঃ echo-trigger live check (item G).
4. **Verify** whether the 2026-09-25 CancelledError/orphan-task fix also stops the stale "Terminating call: Gemini response timeout" (then "call not found") that fired ~27 s after the caller hung up in TEST3.
5. aec.py itself stays byte-identical (hash pinned by test_preservation_4_7). The path B fixes live only in aec1.py.
6. Known and unaddressed: echo at 160 ms delay truncates mid-playback (frame 12). Outside the realistic domain; predates this work.

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

### 2026-10-05: EC2 commit `a0ec061` ("Data Extraction 1") and the fallout
- Operator pulled a commit made directly on EC2 (author `Ubuntu`), despite the sync model being file-copy only and `docs/handoff1.md` telling the EC2 agent not to edit code there. Three unrelated things landed in one commit:
  1. **app.py comment slimming.** All the historical rationale comments (measured defects, task numbers, why-not-X reasoning around AEC_IMPL, GEMINI_BACKEND, truncation clock, echo gate, commit gate, delta trace, far-end pacing, etc) were replaced with short generic one-liners. No logic changed. Operator decision (2026-10-05): keep the slim version, do not restore.
  2. **Quiet config change.** `PRICE_TEXT_INPUT/OUTPUT`, `PRICE_AUDIO_INPUT/OUTPUT`, `GEMINI_MODEL`, `PORT` went from hardcoded to env-override-able. Not mentioned in the commit message.
  3. **Unrelated feature bundled in.** Two xlsx files added under new `AU Admission Data/` (admission notification, course fee) — a separate data-extraction effort, no code wiring, no mention in commit message beyond its title.
  - Also added: `tests/_vertex_smoke.py` (tracked, not the gitignored `TEST_FILES/` copy) and `docs/handoff1_ec2_report.md`, which shows the EC2 bring-up got through hashes/key-file/Python-SDK/suite but stalled at the smoke-test step — `TEST_FILES/_vertex_smoke.py` was never copied to EC2, so the server was never started there and none of the live-call validation tasks ran.
  - **Broke the suite:** app.py's line count shifted (comment removal), so `test_model_identifier_is_recorded`'s GEMINI_MODEL line pins went stale. Re-pinned `[64, 1721, 2233, 2243, 2659]` -> `[60, 1608, 2120, 2130, 2543]`. Suite back to 251 OK, 2 skipped, 1 expected failure.
  - Update 2026-10-05 (operator): the `a0ec061` version, Vertex + aec1 included, runs correctly on EC2; the report above is out of date.

### 2026-09-29: Vertex connect smoke test, and docs/handoff1.md for EC2
- **Smoke test:** `TEST_FILES/_vertex_smoke.py` (gitignored, not committed). Reuses `create_gemini_client()` / `connect_live_with_timeout()` / the app's own `LiveConnectConfig` and greeting prompt, so it exercises the real code path with no audio and no Plivo. Run from Windows: token 0.39 s, session open 1.42 s, greeting sent via `send_client_content`, first audio 0.70 s, `turn_complete=True`, transcript matched the Senco persona, usage prompt=1450/response=246 (~$0.004). Saves `TEST_FILES/_vertex_smoke_greeting.wav`.
- **docs/handoff1.md:** written for a Claude agent running on EC2, to take the Vertex backend and aec1 the rest of the way to a validated live call. Covers: EC2 is not a git repo (file-copy sync only, backup old files first); the pre-2026-09-24 backup dir holds an old `.env`/creds, treat like `.env`; Python/`audioop`/SDK-version checks; a file-hash bring-up checklist; running the smoke test from EC2; then the live-call tasks (Vertex validation, aec1 trial, Redesign Task 8 remnants, the stale-timeout check) in priority order; env-var fallback table; where to write its findings (`docs/handoff1_ec2_report.md`) and the rule not to edit code on EC2 directly.
- `CLAUDE.md` top line now points the EC2 agent at `docs/handoff1.md` first.

### 2026-10-05: TEST6 live call (Vertex eu + aec1 + catalog prompt), debugged
- Recordings and analysis scripts in `TEST_FILES/TEST6/` (`_greeting_scan.py`, `_gap_compare.py`, `_stall_repro.py`, `_lang_repro.py`).
- **Stalls are Vertex `eu` server-side, not AEC.** Every agent turn had 3-6 s of model-sent near-silence (-65..-75 dB, not recorder pads), and every barge-in was the caller speaking into one. Vertex streams at exactly 1.0x real time (about 1.75 s lead); AI Studio bursts at about 4.4x (TEST4/5 and repro). Repro with the same prompt and config: Vertex `eu` stalled or hung in 2 of 8 sessions (one greeting became 36 s with a 7.3 s stall, then the next turn hung); `us-central1` 0 of 3; studio 0 of 6.
- **aec1 cleared:** AEC only touches caller audio to Gemini, never what the caller hears. `[AEC]` adapted 13 of 2367 far-active blocks, `[AEC-GUARD]` 0%, all barge-ins were real speech.
- **Language fixes in `prompt_au.txt`** (my shortening had cut the Bengali rule to "the same mix"): "Language mix (CRITICAL)" with Benglish/Hinglish example sentences and an English-word list; answering in Bengali/Hindi counts as the choice; the consent line must be said in the chosen language, not read as quoted English (it was, in 1 of 2 repro runs). Repro after: 4 of 4 Bengali consent with English terms. Leftover: the student/parent question still comes out in pure Bengali. Tests `test_language_mix_is_critical_with_examples`, `test_reply_language_counts_as_the_choice`. Suite 325 OK.
- AI Studio's Live API returned `1011 Internal error` for Bengali text turns (`send_client_content`), so the language repro runs on Vertex `us-central1`; real calls send audio, not text.
- **Operator decision: stay on Vertex, default `VERTEX_LOCATION` now `us-central1`** (was `eu`; `test_defaults` updated, line count unchanged so the 4_8 pins hold). More samples: mid-turn stalls Vertex `eu` 1-2 of 16 turns, `us-central1` 1 of 16 (a 22.7 s greeting with a 6.4 s stall), studio 0 of 22. So us-central1 lowers but does not remove the stalls; `GEMINI_BACKEND=studio` stays the fix if they recur. "HUNG" text-only replies to "Hello." happened on both backends (2 of 6 on studio) and look like a text-test artifact; TEST6 had none.

### 2026-10-05: Course catalog tools (uncommitted)
- Two Gemini tools over a JSON catalog built from the two AU Excel files (joined on UID, 87 records, operator-approved special cases for UIDs 121, 133, 137, 171 and two exclusions). Structured lookup, no RAG.
- **app.py:** `import course_catalog`; catalog declarations appended to `LOCAL_GEMINI_TOOLS`; a catalog branch in the tool-call loop (no terminal state); a new `else` answers unknown tools with an error (they used to get no response, which leaves Gemini waiting); the dead MCP comment block is gone; call_state `catalog_calls` / `catalog_chars` (mirrored); `[CATALOG]` stats line and diagnostic tag.
- **Tests:** `test_catalog_build` (18), `test_course_catalog` (34), `test_catalog_tool_handler` (9), `test_prompt_catalog` (6). Intentional 4_4 re-baseline (unknown tool now gets a tool_response before the deferred activityStart). 4_8 pins re-pinned.
- Final review (opus) fixes: a department-only word can no longer make a `found` ("mtech computer science" used to land on M.Tech Data Science; now a one-candidate `ambiguous`, spec §6.3 step 6 amended); prompt reuse rule lets the model fetch a field it has not fetched yet. Deferred minors listed in the session's final message ("MCP tool" wording in the except log, playground comma splitting).
- Suite: 318 OK, 2 skipped, 1 expected failure.
- **prompt_au.txt shortened** (operator: the prompt is re-billed every turn). 10,835 to 6,614 chars, about 1,050 fewer tokens per turn (6,782 after the `error` rule below). Duplicates merged (bot refusal, no-invention rule, not-available list, entrance-exam rule, goal section); the degree list dropped, since the `list_programs` enum carries it (test and spec §10 updated). No instruction removed.
- **Two deferred minors fixed** (operator, same day): the response deadline is re-armed after a non-terminal tool reply (catalog or unknown tool), so a model that goes silent after a lookup still hits "Gemini response timeout"; endCall/transferCall stay on the terminal deadline, and speech during the lookup still clears it via the deferred activityStart (`TestResponseWatchdogAfterLookup`, 4 tests). Prompt gains an `error` rule: never read the error aloud, retry once with fixed arguments, then counselor follow-up (`test_error_result_rule`). Suite: 323 OK, 2 skipped, 1 expected failure; 4_8 pins unchanged.

### 2026-10-06: `find_eligible_programs` (phase 2), built on the operator's reversal (uncommitted)
- Operator reversed the 2026-10-05 deferral: build it now as a safe fallback for the open-ended "what can I apply for?" question, rather than wait for live-call frequency data. Brainstormed and planned (spec `docs/spec/course-catalog-tools-design.md` §11, plan `docs/spec/find-eligible-programs-plan.md`), then executed inline task by task.
- **UG only** (53 of 87 records); PG/Diploma keep the phase 1 flow (ask the program, read its `eligibility`). Decisions (operator): rules hand-written and operator-reviewed, not regex-extracted; per-subject minimums and entrance exams returned as spoken `conditions`, not filtered on.
- **`catalog/eligibility_rules.json`** (new, hand-written, committed): 27 rules, one per distinct UG eligibility text (the match key, so a changed Excel sentence orphans its old rule instead of silently keeping it). Each carries `programs` (checked against the data), `min_aggregate`, `subject_options`, `conditions`, `verified`, and a reviewer-only `note`. Drafted by a throwaway script (`TEST_FILES/_draft_eligibility_rules.py`, gitignored) that pulled texts and program lists straight from `courses.json`. **All 27 rules are `verified: false`** — the tool is live but returns only `not_covered` until the operator reviews each `text`/rule/`note` and flips `verified` to `true`, then reruns `catalog/build_catalog.py`.
- **`catalog/build_catalog.py`:** new `attach_eligibility_rules` merges the rules into every UG record (`eligibility_rule`: `min_aggregate`, `subject_options`, `conditions`, `verified`) and adds top-level `subjects` (16-word vocabulary) to `courses.json`. Fails loudly on an unmatched text, a mismatched `programs` list, a duplicate text, an unknown subject, or a malformed field (e.g. `"verified": "true"` as a string).
- **`course_catalog.py`:** new tool `find_eligible_programs(aggregate_pct, subjects, degree?, keyword?)`. Only verified, judgeable programs can match; failing or unjudged programs are never named, only counted (`not_covered`). Staged like `list_programs` but at `ELIGIBLE_NAMES_MAX = 10`. New `UG_DEGREES`, `SUBJECTS`; `_keyword_filter`/`_by_degree` factored out of `list_programs` and reused. `result_status` adds `eligible=<n>`. **app.py untouched** — it builds `LOCAL_GEMINI_TOOLS` from `course_catalog.TOOL_SPECS` and routes on `TOOL_NAMES`, so no 4_8 re-pin and no new call_state key.
- **`prompt_au.txt`:** the phase 1 open-ended-eligibility rule is replaced: ask the 10+2 percentage and subjects, call the tool, say "you appear eligible for" (never "not eligible"), note that admissions confirms final eligibility, and handle a CGPA/pending-result caller by asking for or estimating the percentage.
- **Tests:** `tests/test_find_eligible_programs.py` (new, matching tests on a mocked fake rule set so they don't depend on what's verified yet; `TestGoldenProfiles`, 6 tests derived by hand from the real eligibility texts, `@skipUnless` all UG rules are verified). `test_course_catalog.py`, `test_catalog_tool_handler.py`, `test_prompt_catalog.py` updated for the third tool. Declaration budget raised 1800 → 2600 chars (measured 2514 for all three).
- Executed via superpowers:executing-plans (inline, native): all 4 tasks TDD'd task-by-task, ledger at `.superpowers/sdd/find-eligible-programs-plan/progress.md`.
- Suite: 361 run, 1 known failure, 8 skipped (2 original + the 6 unverified golden profiles), 1 expected failure. The known failure, `test_reply_language_counts_as_the_choice`, predates this work — the operator's same-day `db6fe2b` prompt rewording changed the wording that test checks; not touched here.
- Not committed. Next: operator reviews `catalog/eligibility_rules.json`, flips `verified`, rebuilds, reruns the suite (golden profiles should then pass), then the planned text-only tool test and live calls.

### 2026-10-06: `find_scholarships` (phase 3, uncommitted)
- Source: `AU Scholarship Policy AY 2026-27 Updated V2 (2).pdf` (repo root, untracked). Operator: the PDF is the only source of truth, ignore its dates.
- Data: `catalog/scholarships.json`, hand-transcribed from `pdftotext` output (the PDF tables come out scrambled, so no parsing), 21 schemes plus `general_conditions` and `pitch_lines`. Each entry carries `text` (spoken) vs `source_text` (PDF wording, reviewer only), `verified`, `note`. Operator verified all of it and answered the open notes (M.Pharm gets nothing, internal = Adamas University graduate, WBJEE bands 900/2000/5000, MAT/CMAT/ATMA paid like CAT, employee 5 years always).
- Design (spec §13): one deterministic code tool. A Groq LLM sub-agent was considered and rejected (latency during the tool call, wrong money figures). Only the highest scholarship applies (enforced in code); committee schemes (sports, merit-cum-means) only in `may_also_apply`; rupees from each program's `per_semester` and admission fee, rounded half up with `Fraction`; `ask_about` lists up to 3 unasked facts whose top tier beats `best`; `ruled_out` stops repeat questions.
- `course_catalog.py`: `check_scholarships` validates the file at import (fails at startup), `find_scholarships` handler, declaration last in `TOOL_SPECS`, `result_status` `best=<name key>`. app.py untouched.
- Budgets (measured): responses worst 1255 chars without situations, 1529 with all seven (budgets 1500/1800); four declarations 4161 (budget 4200, about 410 more tokens per turn than before).
- `prompt_au.txt`: new `## SCHOLARSHIPS` section (proactive offer after a fee, one question at a time, never promise committee schemes, AUAT/AUJET explained); entrance-exam rule now allows exam questions for scholarships; scholarships removed from NOT AVAILABLE.
- Tests: `tests/test_find_scholarships.py` (60), plus updates to `test_course_catalog`, `test_catalog_tool_handler`, `test_prompt_catalog` (+3). Suite 428 run, 1 known failure, 2 skipped, 1 expected failure.
- Final review (sonnet 5.5, operator's choice): no Critical. Fixed: a CGPA passed as `qualifying_pct` (10 or less) now errors. Operator then chose to fix the `ask_about` ordering in the prompt (marks asked first, call-flow step 4) and to have Neha bring scholarships up herself. Deferred minors: zero-fee third semester on Post-M.Sc Diploma in Medical Physics; `rules`/`pitch_lines` re-sent on every call; scheme-level (not per-tier) conditions; plain-word asks for AUAT/AUJET/MAT/CLAT; no test for a `situations` item `alumnus`.

### 2026-10-06 (evening): scholarship prompt follow-ups
- Operator chose to fix the `ask_about` ordering in the prompt: call-flow step 4 now collects marks first, so the first `find_scholarships` call doesn't surface exam-rank questions the caller can't answer yet.
- Neha now raises scholarships herself, once per call, after a fee; she quantifies the rupee benefit and ties it to the admission decision. Tests added for both; spec §13 and this file updated.

### 2026-10-07: language guard restored, prompts moved to `PROMPT_FILES/`
- **Language guard:** `test_reply_language_counts_as_the_choice` was the one known failure. The operator's `db6fe2b` rewording dropped the TEST6 guard (a caller answering in Bengali/Hindi without naming a language got English replies). Operator restored the sentence in call-flow step 1: "If the user answers in Bengali or Hindi, that is their choice even if they never name a language: switch at once." Other `db6fe2b` edits kept (including removal of the "happy to switch languages" line). Suite now 428 run, all OK, 2 skipped, 1 expected failure.
- **`PROMPT_FILES/`:** `prompt.txt` and `prompt_au.txt` moved there with `git mv`. Path updated in `app.py:200` (same line count, so the 4_8 pins hold), `text_chat.py:35` and `tests/test_prompt_catalog.py:9`. `docs/` and older session-log entries still use the old names.
- **Correction:** the earlier note that the operator copies `prompt_au.txt` into `prompt.txt` was stale. app.py has loaded `prompt_au.txt` directly since `feb4b29`.
- Still uncommitted. Next: commit the catalog work on its own, text-only tool test, then EC2 sync (including `PROMPT_FILES/` and `catalog/scholarships.json`) and live calls.

### 2026-10-07: text-only tool test run (Vertex Live, us-central1)
- Script `TEST_FILES/_text_tool_test.py` (gitignored): one Live session per scenario, same model/prompt/declarations/voice config/greeting as a call, typed caller turns, catalog tools answered by `course_catalog.handle_tool_call`. Run `PYTHONIOENCODING=utf-8 venv312/Scripts/python.exe TEST_FILES/_text_tool_test.py [fee eligible scholarship bengali parent robotics unknown]`. A tool reply starts a new model turn, so the reader loops on `receive()` until no tool call is pending. Transcripts spell numbers out ("eight lakh ..."), so checks match number words, not digits.
- Results: **fee** (`get_program_details`, spoken rupees OK), **eligible** (`find_eligible_programs`, aggregate 58 and PCB passed, "you appear eligible"), **bengali** (switched at once, Benglish mix, guard works), **parent** (talks about the ward), **robotics** (looks up with `list_programs`), **scholarship** (fee, then marks, then `find_scholarships` best=Early Bird Rs 15,000 at 82%) all behave.
- Findings: (1) "B.Tech Computer Science" sometimes goes to `list_programs` and Neha asks which specialisation; full program names go straight to `get_program_details`. (2) After `find_scholarships` returned `ask_about` (AUJET/JEE Main/WBJEE ranks) Neha did NOT ask any rank question in 2 of 2 completed runs; she said "anything else?". Prompt wording under "Present `best`" / "ask the `ask_about` facts" may need an explicit "after presenting best, ask the first ask_about fact". (3) "Underwater Basket Weaving" was answered "we do not offer that" with no tool call. (4) 3 of ~25 turns got no reply within 60 s (after "Yes." and twice after the caller gave marks): same text-turn "HUNG" artifact seen on 2026-10-05, not seen on real calls. Not retried with a nudge.

### 2026-10-07: fixes for the text-only test findings
- **Prompt (`PROMPT_FILES/prompt_au.txt`), 3 fixes, each re-tested live with `_text_tool_test.py`:**
  - `ask_about` (was finding 2): the SCHOLARSHIPS section now presents `best`, links it to admission, then "end your turn with the first `ask_about` question instead of asking if they would like to know anything else"; call-flow step 6 points to it. Result: 13 of 13 completed scholarship replies asked it (both backends).
  - Loose program names (finding 1): "A fee, eligibility, duration or seats question about a named program, even a loose one ... `get_program_details` ... never `list_programs`; it resolves loose names". Result: "B.Tech Computer Science" went to `get_program_details` (found CSE) 8 of 8. The resolver already handled loose names; only routing was wrong.
  - Not offered (finding 3): "Never say a program is not offered unless a lookup on this call found nothing". Result: `list_programs(keyword=...)` then counselor follow-up, 2 of 2.
  - Tests: `test_ask_about_question_ends_the_scholarship_turn`, `test_loose_program_name_goes_to_get_program_details`, `test_not_offered_only_after_a_lookup` (test_prompt_catalog). Suite 431 run, OK, 2 skipped, 1 expected failure.
- **Long silent turns (finding 4), investigated, NOT fixed (model-side).** The "no reply" turns are the model streaming pure digital-zero audio (-90 dB) mid-turn, billed as response tokens (a stalled turn: response=1576 tokens vs about 550; thoughts normal), then carrying on by itself after about 60 s (58.7 s, 60.5 s, 66.7 s seen on Vertex). Rejected hypotheses, each tested: (a) mid-session `send_client_content` (realtime-text input stalls too); (b) thinking (thought tokens normal in stalled turns); (c) the pre-lookup filler line (a no-filler prompt variant still stalled, including a 66.7 s stall in the plain "Yes, go ahead" turn, no tool involved). So it is the TEST6 Vertex mid-turn-silence behaviour, longer. Real filler prompt kept. On a real call `app.py`'s response watchdog does not fire (audio keeps arriving), so the caller hears dead air until they speak. AI Studio stall counts from the harness are unreliable (it bursts audio about 4x real time, so wall-clock gaps are playback, not stalls) but studio also had 60 s no-`turn_complete` turns.
  - Possible app-level mitigation (not built, needs operator decision): detect N s of all-zero model audio mid-turn and nudge or interrupt. A harness nudge (`TT_NUDGE=1`) on trailing silence desynced the conversation, so any nudge must only fire mid-turn, and it is untested on a real stall.
- **New issues found (both fixed later the same day, see below):** (5) `find_eligible_programs` with `degree` set and more than 10 matches returns `by_degree` with one group (`[{"degree": "B.Tech", "count": 18}]`), nothing to narrow on; once the model then read `list_programs` names as "you appear eligible", which the prompt forbids. (6) After Neha offers a scholarship check, a caller who gives marks plus subjects sometimes gets `find_eligible_programs` instead of `find_scholarships` (about 1 in 16 with the real prompt).
- Harness options added: `TT_DEBUG=1` (per-message trace with dB and token usage), `TT_INPUT=realtime`, `TT_TURN_TIMEOUT`, `TT_STALL_S`, `TT_NUDGE`, `TT_PROMPT=<file>`. Helper `TEST_FILES/_replies.py "<caller text>" <outputs>` prints Neha's replies to a turn. Run outputs `TEST_FILES/_tt_*.txt`.

### 2026-10-07: issues 5 and 6 fixed (stall problem left for live calls, operator)
- **Issue 5, `course_catalog.find_eligible_programs`:** more than 10 matches that are all one degree now return the first 10 names (with conditions) plus `"more": n - 10` instead of a one-group `by_degree`; several degrees still group. 82% PCM `degree=B.Tech`: count 18, 10 names, more 8, 1328 chars (budget 1500). Declaration text gains "(one degree: 10 names and more)", total 4193 of 4200. Spec 11.4 response list updated. Tests `TestStagingOneDegree` (2) and golden `test_82_pcm_btech_lists_names_not_one_group`. Prompt: "If it also returns `more`, say there are more options and ask which field interests them, then call again with `keyword`." Live: 8 of 8 (both backends) read names, said there are more, asked the field, no `list_programs`.
- **Issue 6, prompt routing:** `find_eligible_programs` "only when the caller asks what they can apply for; marks shared for a program already chosen, or in reply to the scholarship offer, go to `find_scholarships`". Live: 12 of 12 marks turns went to `find_scholarships` (was about 1 in 16 wrong); 11 of 12 then asked the `ask_about` question.
- Tests `test_eligibility_more_narrows_by_keyword`, `test_marks_for_a_chosen_program_go_to_scholarships`. Suite 436 run, OK, 2 skipped, 1 expected failure.
- Noticed, not fixed: in eligibility replies Neha adds "the admissions team confirms final eligibility" in only about 1 of 8 (pre-existing).
- Harness scenario added: `eligible_btech`. Stall problem (finding 4): operator will check it on live calls.

### 2026-10-07: prompt v2, the counselor rewrite (`PROMPT_FILES/prompt_au_v2.txt`, now live)
- **Operator goal:** the agent exists to raise admissions. Neha must lead the call and keep the student/parent engaged toward admission, not answer and ask "anything else?". The close is a **campus visit**: Neha fixes **only the day** and says "our admission team will call you to confirm the timing and directions". Operator: v2 is used in app.py from now on.
- **app.py:** `PROMPT_FILE = os.getenv("PROMPT_FILE", "PROMPT_FILES/prompt_au_v2.txt")` (rollback: `PROMPT_FILE=PROMPT_FILES/prompt_au.txt`); startup log `📝 Prompt: <file> (<n> chars)`. +1 line above the GEMINI_MODEL sites, 4_8 pins re-pinned `[61, 1613, 2130, 2140, 2553]`.
- **v2 = v1's tool, scholarship, language and guardrail rules kept verbatim, plus:** ROLE goal (visit day); `## HOW YOU LEAD (CRITICAL)` (every turn ends with one forward question; "anything else?", "do you need more information?" and any "would you like to know more" banned; recommend 1-2 programs with a reason; no invented urgency, only tool-returned limits; one gentle attempt after a no, then close); `## CAMPUS VISIT`; `## OBJECTIONS` (fee, think about it, parent decides, other colleges, busy); call flow reshaped (4 adds the goal, 5 recommend, 6 value, 7 visit + handoff, 8 summary must record the visit day or why none); `ask_about` capped at two before the visit; "after calling it, say nothing more" on `endCall`; LANGUAGE gains the reply-language rule with the TEST6 phrase "হ্যাঁ, বলুন" (consent line in English was 2 of 6, now 0 of 8). Size 14,228 chars vs v1 11,857 (cap v1 + 20%, about 590 more tokens per turn).
- **Tests (test_prompt_catalog):** every v1 check also runs on v2 (`TestPromptCatalogV2`), `TestPromptV2Counselor` (16) pins the new rules, `TestAppLoadsV2`. Suite 473 run, OK, 2 skipped, 1 expected failure.
- **Live text-only results (Vertex, harness scenarios funnel, fee_objection, think, parent_decides, firm_no):** funnel reached a fixed visit day with the confirmation line and a summary naming the day in 7 of 8 (the miss was the known stall); think 6 of 6; parent_decides 6 of 6 (invites the family); firm_no 3 of 3 (one rescue, then endCall); fee_objection reaches the visit in about 2 of 3 (the miss used both `ask_about` questions on a vague caller who then left). Replies ending on a forward question: 86-100%. Regression scenarios (scholarship, eligible_btech, unknown, parent, bengali) pass.
- **Found, not fixed (app.py, pre-existing, affects v1 too):** after app.py's `endCall` reply (`"Call will end after audio playback completes."`) the model sometimes speaks again: "This call has ended." / "The call has been ended." (about 2 of 20 closings, even with the say-nothing rule) or a repeat of the farewell. If the app plays that audio before hanging up, the caller hears it. Possible fixes: a reply that does not invite speech, or dropping model audio after the farewell already played; needs operator decision. Watch for it on live calls.
- Harness: counselor scenarios and checks (`COUNSELOR_CHECKS`, `forward_share`, `end_summary`), the terminal reply now mirrors app.py exactly and speech after it is reported as "spoken after the terminal tool reply". Runs `TEST_FILES/_v2*.txt`.
