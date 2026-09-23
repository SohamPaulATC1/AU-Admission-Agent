Active work: see docs/HANDOFF.md and .kiro/specs/bengali-grapheme-stutter-fix/tasks.md before touching bargein.py, app.py, or aec.py. Standing constraints: prompt.txt is still Senco content (not a code bug, don't fix), requirements.txt and .env are frozen, aec.py is unfrozen but should stay untouched unless truly necessary.

Other standing rules: do not read .env or credential JSON. Tests are stdlib unittest only (no pytest/hypothesis). API_AUTH_TOKEN issue is noted in HANDOFF §10, not to be fixed. Every new call_state key must be added to the mirror in tests/harness/appctl.py (guarded by test_harness_smoke). Run the suite with `venv312/Scripts/python.exe -m unittest discover -s tests -t .` (Python 3.12 venv; 3.14 has no audioop). Don't write test code through bash heredocs: escapes like `\x00` / `\n` get mangled. Use the Edit tool or a .py script file. Printing app log lines needs `PYTHONIOENCODING=utf-8` (Windows console is cp1252).

## Session state (2026-09-23)

### Done
- **Task 5 (closed)**, see HANDOFF §5.6 and §5.7:
  - Gate window geometry fixed (180 ms near vs 260 ms far).
  - Echo latch added, plus the `ECHO_MAX_RETURN_DB=-6` ceiling (`near-too-loud-for-echo`).
  - Latch field logging: `[LATCH-HOLD]`, `[LATCH-END]` and `[LATCH]`.
- **Task 6, tail gate (done)**, see HANDOFF §5.8:
  - The post-playback echo tail used to skip the gate and open phantom caller turns (41/42 in the harness).
  - Fixed with `ECHO_TAIL_FRAMES=8` (160 ms), the `far_silent_frames` call_state key (mirrored in appctl.py) and `far_window_has_playback()`.
  - Result: 0/36 phantoms. Cost: +60 ms for a caller who speaks right at playback end.
  - Tests: `EchoTailScenario`, `TestEchoTailIsNotAdmittedUpstream` (includes 120 ms) and `TestCallerRightAfterPlaybackIsHeard` (compared against a no-echo baseline).
  - `GEMINI_MODEL` pins in the 4_8 test re-pinned to `[51, 1381, 1845, 1855, 2257]`.
- Full suite: **211 tests OK (2 skipped, 1 expected failure)**. The skips are the retired Case C and the deferred reconnect test. The expected failure is `TestCaseDQuietCallerKnownLimitation`. Real-recording tests and Case A/B/D pass.
- HANDOFF updated: §1 hashes, §2 suite state, new §5.8, §8 #6.

### Decided
- **Preroll trim on a genuine flush: NOT implemented (operator, 2026-09-23).** Task 6 is closed with the tail gate. Rationale and the reopen condition are in HANDOFF §5.8 and §8 #6. Reopen only if live calls show Gemini reacting to its own voice at the start of a barge-in turn.

### Resume here
1. (done) Operator decision recorded.
2. **Task 7:** full verification.
   - Whole suite green.
   - aec integrity (`TestAecPyIsByteIdentical`).
   - Byte-stream neutrality where expected.
   - Reconcile HANDOFF §2/§3 historical text, and the tasks.md header that still says 5.7/5.9 are parked.
3. **Task 8:** live-call validation guidance. Build it around the `[LATCH-*]` data first (HANDOFF §8 #8), then threshold tuning. Also check real calls for phantom turns after playback end, to confirm the tail fix live.
4. Known and unaddressed: echo at 160 ms delay truncates mid-playback (frame 12). It is outside the realistic domain and predates this work.
5. Nothing is committed yet (branch `dev`, all files untracked).
