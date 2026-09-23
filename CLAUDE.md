Active work: see docs/HANDOFF.md and docs/spec/tasks.md (tracked copy; keep .kiro/specs/bengali-grapheme-stutter-fix/tasks.md identical) before touching bargein.py, app.py, or aec.py. Standing constraints: prompt.txt is still Senco content (not a code bug, don't fix), requirements.txt and .env are frozen, aec.py is unfrozen but should stay untouched unless truly necessary.

Other standing rules: do not read .env or credential JSON. Tests are stdlib unittest only (no pytest/hypothesis). API_AUTH_TOKEN issue is noted in HANDOFF §10, not to be fixed. Every new call_state key must be added to the mirror in tests/harness/appctl.py (guarded by test_harness_smoke). Run the suite with `venv312/Scripts/python.exe -m unittest discover -s tests -t .` (Python 3.12 venv; 3.14 has no audioop). Don't write test code through bash heredocs: escapes like `\x00` / `\n` get mangled. Use the Edit tool or a .py script file. Printing app log lines needs `PYTHONIOENCODING=utf-8` (Windows console is cp1252).

## Session state (2026-09-23)

Numbering: HANDOFF uses "Redesign Task N"; docs/spec/tasks.md uses 1-10 / 5.x. Mapping in HANDOFF §3.

### Done
- **Redesign Task 5 (closed)**, see HANDOFF §5.6 and §5.7:
  - Gate window geometry fixed (180 ms near vs 260 ms far).
  - Echo latch added, plus the `ECHO_MAX_RETURN_DB=-6` ceiling (`near-too-loud-for-echo`).
  - Latch field logging: `[LATCH-HOLD]`, `[LATCH-END]` and `[LATCH]`.
- **Redesign Task 6 (closed)**, see HANDOFF §5.8:
  - The post-playback echo tail used to skip the gate and open phantom caller turns (41/42 in the harness).
  - Fixed with `ECHO_TAIL_FRAMES=8` (160 ms), the `far_silent_frames` call_state key (mirrored in appctl.py) and `far_window_has_playback()`.
  - Result: 0/36 phantoms. Cost: +60 ms for a caller who speaks right at playback end.
  - Preroll trim on a genuine flush: NOT implemented (operator, 2026-09-23). Reopen only if live calls show Gemini reacting to its own voice at the start of a barge-in turn.
- **Redesign Task 7 (closed)**, see HANDOFF §8 #7:
  - Checks: suite, aec integrity (also in a fresh clone), kill switch and gate byte-neutrality.
  - All 8 non-reconciling items resolved per the operator's decisions: `.gitattributes`, gitignore, spec copies in docs/spec/, the amended bargein.py constraint, 5.8 ratified, checkboxes ticked, "Redesign Task N" naming, stale "parked" text fixed, §6 commands.
  - `TestDesignLiterals` added. The `not-measured[concern-a-parked]` log string is kept verbatim on purpose.
- Full suite: **212 tests OK (2 skipped, 1 expected failure)**. The skips are the retired Case C and the deferred reconnect test. The expected failure is `TestCaseDQuietCallerKnownLimitation`.
- `GEMINI_MODEL` pins in the 4_8 test: `[51, 1381, 1845, 1855, 2257]`. Re-pin whenever app.py's line count changes.

### Committed (dev, not pushed)
- `84eb866`: the state through Redesign Task 6.
- `be6c19f`: `.gitattributes`, gitignore (`CALL_RECORDINGS/`, `*.m4a`, `*.kiro-halt`), docs/spec/ copies.
- `52be2ce`: the docs-and-comments commit for Redesign Task 7.
- `35f75c3`: the A-G docs/test-name commit (HANDOFF §8 #7): amendment extended, 5.13 amended, fresh-clone note in §6, test rename, design.md and bugfix.md copied to docs/spec/ with the caller number redacted.
- Then the item B commit: all task 4 re-baselines approved by the operator (recorded in HANDOFF §4), 5.13 ticked, and the stale app.py grapheme-helper comment fixed (comment-only, same line count).
- Never committed: `.env`, credential JSON, `CALL_RECORDINGS/`.

### Resume here
1. **Redesign Task 8:** live-call validation guidance. Build it around the `[LATCH-*]` data first (HANDOFF §8 #8), then threshold tuning. Also check real calls for phantom turns after playback end, to confirm the tail fix live, and run the অন্তঃ echo-trigger live check (item G). Not started; wait for the operator.
2. Known and unaddressed: echo at 160 ms delay truncates mid-playback (frame 12). It is outside the realistic domain and predates this work.
3. Nothing is pushed. Commit only when asked, and never commit `.env`, credential JSON or `CALL_RECORDINGS/`.
