# Implementation Plan

## Standing constraints (apply to every task below)

- `aec.py` MUST remain byte-identical (requirement 3.10). Only its existing public methods (`add_far_end`, `reset_far_end`, and the existing inbound entry point) may be called from `app.py`. A content hash of `aec.py` is pinned in the test suite (task 4.7).
- `requirements.txt` MUST NOT be modified. Tests use the stdlib `unittest` runner: `venv/bin/python -m unittest discover -s tests -v`. `pytest` and `hypothesis` are NOT installed and MUST NOT be added. Property-based tests are hand-rolled seeded falsifiers over `numpy.random.default_rng(seed)` that print the seed, the case index and the failing input on first failure.
  - **UPDATE 2026-09-23:** on the Windows dev machine the suite runs as `venv312/Scripts/python.exe -m unittest discover -s tests -t .` (Python 3.12 venv; 3.14 has no `audioop`). The `venv/bin/python` commands elsewhere in this document are the original Linux form.
- `.env` MUST NOT be modified.
- **No task in this spec may modify `play_disclaimer` (app.py ~744–779).** Its missing far-end reference is out of scope for this bugfix and is tracked separately — see `## Spun Out — Separate Ticket` at the end of this document and `## Out of Scope` in bugfix.md.
- Already installed and usable: `regex` 2026.2.28 (pinned at `requirements.txt` line 177), `numpy` 2.2.6, `scipy` 1.15.3, `soundfile` 0.13.1.
- `VAD_THRESHOLD_WHILE_SPEAKING = 0.82` (app.py line 80) and `VAD_SPEECH_ONSET_FRAMES_WHILE_SPEAKING = 4` (app.py line 82) MUST be left at their current values. The design explicitly rejects raising them globally because it taxes every genuine barge-in (3.1). No task in this plan changes them.
- ~~All behavioural decision logic goes in the new pure module `bargein.py`. `app.py` gets wiring, state fields and logging only.~~
  - **AMENDED 2026-09-23 (operator):** pure decision logic goes in `bargein.py`. Gate helpers coupled to `call_state` may live in `app.py`: `apply_echo_latch`, the `ECHO_MAX_RETURN_DB` override in `evaluate_echo_gate`, and `far_window_has_playback`. No code is to be moved to satisfy this constraint.
  - **EXTENDED 2026-09-23 (operator):** these may also stay in `app.py`: the grapheme helpers (`grapheme_clusters`, `leading_clusters`, `ends_mid_grapheme_cluster`, `boundary_splits_grapheme_cluster`, `shared_leading_cluster_count`), the commit gate (`arm_commit_gate`, `disarm_commit_gate`, `commit_gate_verdict`, `commit_gate_discard`) and `delta_trace_verdict`. No code is to be moved.

### Post-Task-1 scope gate (added after the INCONCLUSIVE verdict — operator decision: instrumentation first, then redesign)

- **UPDATE 2026-09-23 — concerns (a) and (b), tasks 5.7 and 5.9, are NO LONGER PARKED. Both are IMPLEMENTED.**
  - They re-entered the plan through the design-phase revision this bullet required: `redesign-audio-pipeline.md` in this directory, carrying all three redesign inputs.
  - They are implemented as REDESIGNED, not as the original 5.7/5.9 text specifies. Where the two differ, the redesign and `docs/HANDOFF.md` are authoritative:
    - A single-threshold envelope-correlation gate over lag 0 to 160 ms, with an echo latch, a level ceiling (`ECHO_MAX_RETURN_DB`) and a post-playback echo tail gate (`ECHO_TAIL_FRAMES`).
    - No two-tier gate, and no `ECHO_AMBIGUOUS_ONSET_FRAMES`.
    - The 5.9 preroll trim was measured and deliberately not implemented (operator decision, HANDOFF §5.8).
  - Status, measurements and decisions: `docs/HANDOFF.md` §3 and §5.6–§5.8.
  - The original parked text, kept for the record: *"Concerns (a) and (b) — tasks 5.7 and 5.9 — are PARKED PENDING REDESIGN. No code may be written for them. Not a partial implementation, not a scaffold, not the five new constants, not `far_shadow`. They re-enter the plan only through a design-phase revision that carries the three redesign inputs recorded under those tasks."*
- **Task 5.8 is decoupled from concern (a)** and is not blocked behind the (a) redesign. It is gated on **one clean live call captured with the concern (d) instrumentation and the far-end persistence from task 5.4a**, and is **not greenlit for the current pass**.
  - **UPDATE 2026-09-23: 5.8 is IMPLEMENTED** (`reset_far_reference` after every `clearAudio` and on reconnect; `docs/HANDOFF.md` §3, Task 4). The greenlight was not recorded at the time. The operator ratified it on 2026-09-23.
- **SUPERSEDED 2026-09-23** by the audio-pipeline redesign (`redesign-audio-pipeline.md`), which authorised 5.5–5.9 as redesigned. Kept for the record: **Authorised scope of the current pass: tasks 2, 3, 4, 5.1, 5.2, 5.3, 5.4, 5.4a and 5.10.** Anything outside that list needs a new operator go-ahead.
- **SUPERSEDED 2026-09-23** by the redesign, which deliberately changes the AEC reference feed and the barge-in decision. Kept for the record: The current pass is deliberately **attribution-neutral**: nothing in the authorised scope may change what the caller hears, what the AEC receives, what the VAD sees, or the outbound pacing. That property is the whole point of instrumentation-first — it is what makes the next live call interpretable. Task 5.4a inherits this constraint explicitly; task 5.8 violates it, which is why it waits.
- Task 5.11 (constant tuning) stays blocked: `ECHO_CORR_THRESHOLD` cannot be seeded from the existing recording and must not be tuned against `debug_recordings/Abhishek_20260921_144651.wav`. Task 5.4a is what unblocks it.

---

- [x] 1. Offline echo-provenance check against the existing debug recording — HARD REPORTING GATE
  - **No code change, no deployment, no live phone call. This task runs first and gates every other task in this plan.**
  - **THIS IS A REPORTING CHECKPOINT, NOT MERELY A DATA DEPENDENCY.** When the measurement is complete, **STOP** and report the verdict to the operator. **No subsequent task — including tasks 2, 3, 4 and any part of task 5 — begins until the operator has seen the verdict and given the go-ahead.** Do not scaffold the test harness, do not write the exploration test, do not touch `app.py` or create `bargein.py`. Completing the measurement does not authorise task 2.
  - This gate discharges **AR-2** in bugfix.md ("the echo-trigger mechanism is inferred, not measured"), and is the same gate stated in design.md → Hypothesized Root Cause → "The offline provenance analysis is a hard gate, not a recommendation".
  - **GOAL**: confirm or refute the inferred half of the root cause — that the sub-200 ms barge-in triggers were fired by residual echo of the assistant's own outbound audio rather than by caller speech. Design: Testing Strategy → Exploratory Bug Condition Checking, test case 1; Hypothesized Root Cause → "Inferred, consistent, not proven".
  - Source: `debug_recordings/Abhishek_20260921_144651.wav` (364.57 s). Read with `soundfile` 0.13.1.
  - **What the recording actually is, verified against the code.** The debug WAV is written at `app.py` 1299–1303 from `clean_pcm_16k` — the **post-AEC, post-RNNoise, post-AGC inbound** stream. The writer is opened at `app.py` ~938–941 with `setnchannels(1)`, `setsampwidth(2)`, `setframerate(GEMINI_INPUT_RATE)` → 16 kHz mono 16-bit. Echo present in this file is therefore echo that **survived** cancellation, which is exactly the quantity of interest.
  - **Naive `sample_index / 16000` alignment is INVALID — the recording is not wall-clock continuous.** Two `continue` statements skip frames before the writer is reached: (i) `app.py` ~1231–1233, `continue` when `closing_audio_phase` or `playing_disclaimer` is set; (ii) `app.py` 1241–1243, `continue` when `aec.process()` returns empty because fewer than one block of samples has accumulated. The disclaimer plays at call start, so **the head of the file is missing that entire span**. Any conclusion drawn from a wall-clock-continuous assumption is void.
  - **Prescribed alignment method.** Each media event that reaches the writer contributes one chunk of `clean_pcm_16k` — nominally 20 ms → 320 samples at 16 kHz, block-quantised by the AEC. `app.py` logs `🔊 [AGC] Inbound RMS: X dB | Gain: +Y dB` every `LOG_EVERY_N_CHUNKS = 50` chunks (~1 Hz) at a known `chunk_count` (log site 1304–1307; `chunk_count` increments at 1340, so the logged index is the pre-increment index of the chunk written in that same iteration). Compute per-chunk RMS from the WAV and match that sequence against the logged RMS values at their known chunk indices to recover **chunk_index → wall-clock time**, anchored to `Gemini_Assistant.log` call UUID `dbcd514d-3fe8-4af9-bb1a-f430634c89fd` (session open 14:46:51,660).
  - **Correct for AGC gain when matching, or the sequences will not line up.** `rms_db` is measured at `app.py` 1271 from the **pre-gain** `clean_pcm_16k`, while the WAV is written after gain and the soft limiter (1300). Compare measured WAV chunk RMS against `logged_rms + logged_gain`, and expect divergence wherever the limiter engages.
  - **Quantify and report the residual alignment error.** Cross-check the nominal chunk size against total WAV sample count vs. total chunk count. **If the residual cannot be bounded below roughly one chunk (~20 ms), the verdict is INCONCLUSIVE** — record it as such rather than proceeding on a best guess.
  - **Known limitation, stated honestly: the far-end audio was never recorded to disk.** No file of the played-out signal exists, so true cross-correlation against the actual far-end is impossible. The analysis must instead rest on (i) inbound energy/envelope behaviour inside known assistant-playback windows, reconstructed from the `🎙️ [TIMING] AI Speech Started playing at` (app.py 1947) / `AI speech playback ended at` (app.py 1979) log pairs; (ii) the same measure inside known assistant-silent windows; and (iii) voice-character evidence distinguishing attenuated Bengali TTS from the caller's voice. This weakens the result from "correlation proven" to "**consistent / inconsistent with echo**", and that weakening must be stated in the report. A **CONFIRMED** verdict therefore requires the short-trigger and long-trigger groups to **separate cleanly** on these measures; overlapping distributions are INCONCLUSIVE, not a weak confirmation.
  - Extract ±500 ms windows around each **sub-200 ms** trigger, using the Example 4 deltas: 32, 103, 106, 106, 110, 117, 127, 128, 130, 134, 135, 138, 139, 139, 141, 145, 146, 161, 165, 193 ms. The canonical case is the 131 ms trigger at **14:50:04.126** (log lines 2040–2047).
  - Extract ±500 ms windows around each **>1 s** trigger as the control group: 1238, 1314, 1573, 1595, 1639, 1855, 2136, 2571, 2997, 3204, 3566, 3656, 3738, 4315, 5514, 5605, 5831, 5917, 6598, 11803 ms.
  - For each window compute the short-term energy envelope (10 ms bins), the envelope behaviour against the reconstructed assistant-playback intervals, and per-window RMS.
  - Report the two distributions side by side, with the measured `echo_correlation` values, so `ECHO_CORR_THRESHOLD` can be set from data instead of guessed.
  - Also record the median inbound RMS and VAD probability per group to check the circumstantial figures already in the design (−41.8 dB / 0.85 for short triggers vs −32.4 dB / 0.99 for long ones; the 14:50:04 trigger at −42.6 dB / 0.84 against a 0.82 threshold).
  - **THREE OUTCOMES AND THEIR CONSEQUENCES — record the verdict explicitly:**
    - **CONFIRMED** — short-trigger windows track assistant playback, long-trigger windows do not, and the two groups separate cleanly: proceed with concerns (a), (b), (c) and (d) as designed, and **carry the measured correlation distribution into the constant-tuning task 5.11**.
    - **REFUTED** — the short-trigger windows contain genuine caller speech: concerns **(c) and (d) survive unchanged**; concerns **(a) and (b) return to the DESIGN PHASE, explicitly before any code is written for them**. The gate is firing on real audio, so the root cause shifts toward **VAD/AGC sensitivity rather than echo**. Do not implement the far-end shadow buffer and corroborated gate (5.7) or the preroll/activity-window gate (5.9) as specified — re-hypothesise first.
    - **INCONCLUSIVE** — residual alignment uncertainty cannot be bounded below ~one chunk, or the two distributions overlap: **treat as REFUTED for scoping purposes**. Note which additional evidence would settle it (task 5.1's per-delta trace plus task 5.3's trigger provenance log on the next affected call).
  - **Concerns (c) and (d) may proceed regardless of the verdict.** They do not depend on echo being the trigger: (c) holds whether the repeat originates upstream or downstream, and (d) is the instrumentation that makes the question answerable at all — more valuable on a refutation, not less. So the operator may choose to green-light the instrumentation-first path (tasks 5.1–5.4, 5.10) even on a REFUTED or INCONCLUSIVE verdict. That is the operator's call to make after the report, not an assumption to act on unilaterally.
  - **STOP AND REPORT — final action of this task.** Report to the operator: the verdict; the two distributions side by side with their measured values; the residual alignment error and how it was bounded; the median inbound RMS and VAD probability per group; the far-end-reference limitation above; and which of the four concerns the verdict authorises. Then **halt and wait for the go-ahead**. This task is complete only once the verdict and its supporting numbers have been reported and the operator has responded. Re-planning (a) and (b) after a REFUTED or INCONCLUSIVE verdict is a **design-phase** activity, not something to patch inside this task list.
  - _Requirements: 1.4, 2.4_

  - **COMPLETION NOTES — measurement run against `debug_recordings/Abhishek_20260921_144651.wav` + `Gemini_Assistant.log` (call `dbcd514d-3fe8-4af9-bb1a-f430634c89fd`). No production file was touched; all analysis scripts were scratch-only and have been deleted.**

    **VERDICT: INCONCLUSIVE**, by the letter of this task's own criterion — the residual alignment error *was* bounded well below one chunk, but **the two trigger groups do not separate cleanly on any measure available without the far-end reference** (best single-measure AUC 0.93; best composite leaves 2/20 short triggers looking like caller speech and 2/20 long triggers looking assistant-derived). Per this task's tie-breaker, INCONCLUSIVE is treated as REFUTED for scoping: concerns **(c) and (d) are authorised**, concerns **(a) and (b) return to the design phase**.

    **This is a narrow "not proven", not a refutation.** The substantive question — is the assistant's own outbound audio present in the inbound stream, and is it what fires the sub-200 ms triggers — has strong affirmative evidence (below). What failed is the specific *clean-separation* test, and it failed for a structural reason worth carrying into the re-design: the assistant-derived burst is present at **every** turn start including the 11 never-interrupted turns, so the groups differ in the burst's **amplitude**, not its presence. Overlap is therefore expected under the hypothesis, which makes "groups separate cleanly" a poor discriminator here. Whether that justifies green-lighting (a)/(b) anyway is the operator's call.

    **Alignment (validated three ways; residual bounded at ±2.5 ms ≈ ⅛ chunk).**
    - Chunk→sample: `sample(c) = c*320 − 160`. The −160-sample head offset is measured, not assumed: a sample-resolution sweep of WAV chunk RMS against `logged_rms + logged_gain` at the 365 `🔊 [AGC]` anchors minimises at exactly −160 samples (−10.0 ms) with **median |err| = 0.03 dB** and **108/108 informative anchors within 1.5 dB**. It corresponds to the WAV's 160-sample remainder, i.e. the first chunk carried 160 samples, not 320. Offsets within +0.5 dB of the optimum span −200…−120 samples → **±2.5 ms**. At ±320 samples (one chunk) median error rises to 1.5–3.1 dB.
    - Chunk-count cross-check: WAV = 5,833,120 samples = 18,228.5 nominal chunks; the last AGC line is at `chunk_count` 18,200, so `chunk_count` ∈ [18,200, 18,249]. Consistent → cumulative drift **< 1 chunk over 364.57 s**.
    - Independent check without RMS: at the 86 assistant-silent `🔇 User speech end detected` events the mapped 200 ms before the line is 20 dB quieter than 400–600 ms earlier; at the 47 assistant-silent `🎤 User speech detected` events the mapped last 60 ms is +50.7 dB above the preceding 180 ms, and that contrast collapses (+0.0 dB) once the timebase is shifted −50 ms.
    - Chunk→wall-clock from 729 anchors (365 `🔊 [AGC]` at `chunk_count` 0,50,100,… pre-increment; 364 `🎧 [RNNoise]` at 49,99,149,… post-increment). Adjacent-chunk wall step median **20.0 ms**; leave-one-out interpolation residual median 1.0 ms / p90 6.0 ms. Only 4 spans run fast (chunks 0–200, ~5 ms/chunk) — see the correction below.

    **CORRECTION to this task's premise.** The claim that "the head of the file is missing that entire span" (the `playing_disclaimer` skip at app.py ~1231–1233) **does not hold for this call**. `stream_plivo_to_gemini` only starts after `await disclaimer_finished.wait()` (app.py ~1123), logging `Ready to stream audio from Plivo to Gemini` at 14:46:54,746 — 4 ms after `✅ Disclaimer finished` at 14:46:54,742. `playing_disclaimer` was already False, so that `continue` never fired. The ~2.9 s of inbound media buffered during the disclaimer was drained as a burst instead (chunks 0–200 at ~5 ms/chunk), and **is** in the file. Nothing is missing; the head is simply not wall-clock continuous, which the anchor-based timebase handles. Net effect on the gate: the residual is far smaller than the task feared.

    **Trigger groups re-derived from the log — both confirmed, with a 1 ms bookkeeping discrepancy.** 56 `AI Speech Started playing at` → 45 `AI Speech Interrupted at` + 11 `AI speech playback ended at`. Deltas from the embedded `[TIMING]` clock: short (<200 ms, n=20) **33, 104, 106, 106, 110, 117, 127, 128, 131, 134, 135, 138, 139, 139, 141, 145, 146, 161, 165, 193**; long (>1 s, n=20) **1238, 1314, 1573, 1595, 1639, 1855, 2136, 2571, 2997, 3204, 3566, 3656, 3738, 4315, 5514, 5605, 5832, 5917, 6598, 11803**. Design Example 4 lists 32, 103, 130 and 5831 for four of these: those come from the log-record timestamps, which are 1 ms ahead of the embedded times on those lines. Same events. Note the design's own prose quotes the embedded value (131 ms) for the canonical case while its list carries the log-record value (130 ms) — worth reconciling in the design. Example 4 also omits a middle band the log contains: **5 triggers at 231, 340, 614, 658, 706 ms**, plus 11 uninterrupted turns of 1583–15323 ms.

    **Distributions, side by side.** Windows are trigger-aligned `[t_interrupt − 120 ms, t_interrupt + 40 ms]` — the gate needs 4 frames over `VAD_THRESHOLD_WHILE_SPEAKING`, so that window is the audio that actually fired it. Both reference sets are built from the same recording: REF-CALLER = 46 windows in assistant-silent VAD spans with substantive `🗣️ [USER]` transcripts; REF-ECHO = 28 windows ≥450 ms into a playback interval with no VAD onset within 600 ms and inbound above −55 dB.

    | measure | SHORT (n=20) | LONG (n=20) | REF-CALLER (n=46) | REF-ECHO (n=28) | short-vs-long AUC |
    |---|---|---|---|---|---|
    | F0 median, Hz | **214.8** | 142.3 | 133.3 | 195.1 | 0.877 |
    | spectral centroid, Hz | **1087** | 451 | 450 | 324 | 0.920 |
    | spectral flatness | **0.088** | 0.014 | 0.013 | 0.012 | 0.897 |
    | 800–1800 Hz energy share | **0.804** | 0.096 | 0.064 | 0.022 | 0.915 |
    | voiced frame fraction | 0.529 | 0.735 | 0.941 | 0.529 | 0.172 |
    | window RMS, dB | −23.7 | −21.7 | −14.9 | −44.8 | 0.372 |

    Level does **not** discriminate (AUC 0.372); voice character does. Voice = `Kore` (app.py ~1074), a female TTS; the caller is male at F0 133 Hz. F0 split point midway between the two references is 164.2 Hz: **18/20 short triggers sit above it (assistant-pitched), 14/20 long triggers below it (caller-pitched)**; REF-ECHO 75% above, REF-CALLER 20% above. A composite discriminant trained on the two reference sets (F0, centroid, log flatness, log 800–1800 share, voiced fraction; reference separation AUC 0.970) scores SHORT median **+1.005** (18/20 positive) vs LONG median **−0.460** (7/20 positive), short-vs-long AUC 0.930 — but `max(LONG) = +0.605` exceeds `min(SHORT) = −0.720`, so **overlapping**. The two discordant short triggers are the 33 ms one (F0 128 Hz) and the 104 ms one (F0 146 Hz); both read as genuine caller speech that happened to land just after a playback start.

    **The strongest finding, and it is not circular.** Measured over all 56 turns at a fixed window `[start+40 ms, start+120 ms]` against `[start−200 ms, start−40 ms]`, an inbound burst appears just after **every** playback start, and its F0 is the same in every group: **215.5 Hz (short) / 216.2 Hz (mid) / 216.2 Hz (long) / 219.2 Hz (never-interrupted)** — the assistant's pitch, not the caller's 133 Hz, including in the 11 turns nobody ever interrupted. Only the level differs: burst median **−27.8 dB (short)** vs −39.1 (mid), −61.2 (uncut), −80.6 (long) — a 33 dB gap between the short-trigger turns and the never-interrupted ones, and 19/20 short triggers above −41 dB. So assistant audio leaks into the inbound stream at every turn start, and the gate fires on the subset where the leak is loud enough to hold above 0.82 for 4 frames. Onset is abrupt and time-locked: pre-onset audio is **99.6% exact-zero samples** (digital silence — 54.3% of the whole recording is exact zero), then +45.5 dB in 30 ms, with the onset at **+50…+90 ms after playback start for 13 of 19** short triggers (sd ≈ 13 ms over that cluster).

    **Mechanism refinement that matters for concern (a)'s design.** A +40…+120 ms onset measured from the first `plivo_ws.send()` is **too fast for an acoustic round trip** (our WS → Plivo → PSTN/handset → caller's mic → back is >150 ms on a mobile leg). So this is **not** acoustic echo returning from the handset; it is far-end signal reaching the inbound stream at lag ≈ 0. Consistent with the design's own AEC-desynchronisation finding: `add_far_end` is called at send time (app.py ~1942) and `reset_far_end()` is never called on `clearAudio` (45 times this call), so `_PFDKF`'s learned `H` is adapted against a reference the caller never heard. With near-end still silent, `e = d − y ≈ −y` — the canceller emits an inverted, band-shaped copy of the assistant's own audio. That matches the observed signature: narrow −3 dB bandwidth (median 94 Hz) at a peak frequency that varies per trigger (187–1531 Hz, median 1047 Hz vs 234 Hz for REF-ECHO and 438 Hz for REF-CALLER), high flatness, and assistant F0 preserved. Provider-side media loopback would look the same from here and is not excluded. **Design consequence:** `bargein.LagTracker` (5.5) is specified to estimate the near/far lag during assistant-only playback and evaluate `echo_correlation` at that lag. If the leak arrives at lag ≈ 0 while the tracker converges on the acoustic lag, the corroborated gate looks in the wrong place and misses exactly the case it was built for. The lag search must cover lag 0.

    **Circumstantial figures from the design — all three confirmed, and the sampling rule identified.** Reproduced exactly under "first `🔊 [AGC]` / `🎧 [RNNoise]` reading at or after the trigger": short **−41.8 dB / VAD 0.85**, long **−32.4 dB / VAD 0.99**. The canonical 14:50:04.126 trigger: **−42.6 dB / 0.84** against the 0.82 threshold — but the nearest AGC line is **+478 ms after** the trigger and the nearest VAD line **+458 ms after**, so both readings describe post-`clearAudio` audio, not the triggering frames. Under a "nearest reading" rule the separation vanishes (short −46.8 dB / 0.65, long −47.8 dB / 0.76), which shows the design's figures are an artefact of the 1 Hz sampling rather than evidence: the per-decision logging in task 5.3 is what they should have been.

    **Bogus caller turns — the loop-closing half, and the cleanest evidence in the set.** 23 turns whose transcript is implausible for the caller (`I'm sorry. I'm sorry.`, `¿Qué?`, `¿Qué tal?`, `Amén.`, `A`, `जी`, `Ata`, `Ah`, `Ami`, …) vs 32 substantive Bengali/Hindi turns, measured over the admitted VAD span: **F0 median 200.0 Hz vs 135.6 Hz**, centroid 678 vs 448 Hz, flatness 0.101 vs 0.025, 800–1800 share 0.273 vs 0.100, RMS −27.0 vs −15.1 dB. The audio credited to the bogus turns is quiet, bright, spectrally flat and pitched at the assistant's F0 — i.e. the assistant's own audio being admitted upstream and transcribed as the caller. 16 of the 23 fall inside a reconstructed playback interval. Distributions still overlap (a few bogus turns sit at 129–142 Hz), which is the same overlap that keeps the overall verdict short of CONFIRMED.

    **Far-end-reference limitation, and how much it weakens this.** The played-out signal was never persisted — `aec.add_far_end()` at app.py ~1942 is the only call site and nothing writes it to disk — so true cross-correlation against the actual far-end is impossible. Everything above is "consistent / inconsistent with assistant-derived audio" on speaker-discriminative and temporal grounds, never "correlation proven". Two concrete consequences: (1) no `echo_correlation` value was measured, so **`ECHO_CORR_THRESHOLD = 0.35` remains a guess and task 5.11 cannot be seeded from this recording** — it needs task 5.3's per-decision logging on a future affected call; (2) the ~0-lag reading is inferred from onset timing plus the code path, not from a measured impulse response. The single change that would settle this on the next occurrence is persisting the far-end alongside the existing debug WAV, which is a natural companion to task 5.7's `far_shadow`.

    **Authorised by this verdict:** concerns **(c)** and **(d)** — tasks 5.1, 5.2, 5.3, 5.4, 5.10 and their prerequisites (2, 3, 4). Concerns **(a)** and **(b)** — tasks 5.7, 5.9 — **return to the design phase**; do not write code for them. Re-design should carry three inputs from this measurement: the lag-0 leakage path, the fact that the leak is present at every turn start so amplitude rather than presence is the discriminator, and the need to persist the far-end so the next call is measurable.

    **Reproducing this.** All scratch tooling was deleted. To rebuild: parse the log from line 1377, take `chunk_count` = 0,50,100,… for `🔊 [AGC]` (pre-increment, app.py ~1304–1307) and 49,99,149,… for `🎧 [RNNoise]` (post-increment, app.py ~1340), map chunk→sample with `c*320 − 160`, compare WAV chunk RMS against `logged_rms + logged_gain`, and score only anchors in −60…−12 dB so neither the −120 dB floor nor the 0.9 soft limiter dominates.

- [x] 2. Test harness scaffolding (no production code, prerequisite for tasks 3 and 4)
  - Create `tests/` with `__init__.py` and a stdlib-only layout discoverable by `venv/bin/python -m unittest discover -s tests -v`. Design: Testing Strategy → Validation Approach.
  - `FakePlivoWS` — records every JSON frame that would have been sent (`playAudio` payloads, `clearAudio`, `checkpoint`), with monotonic send times from the fake clock, so framing (160 μ-law bytes) and 20 ms pacing are assertable.
  - `FakeSession` — yields a scripted sequence of `server_content` responses covering `output_transcription` deltas, `inline_data` audio chunks, `interrupted`, `turn_complete`, plus tool-call and `GeminiSessionDisconnected` scripts.
  - `FakeClock` — drives the 20 ms pacing loop and the timeout branch of `send_plivo_audio` with no real sleeps.
  - Synthetic echo path helper — delay (20–400 ms) + attenuation (6–30 dB) + additive noise over any `playback_audio_files/*.wav` fixture, turning a far-end fixture into a near-end echo signal.
  - Seeded falsifier helper — `run_property(name, generate, check, cases=500..1000, seed=...)` looping over `numpy.random.default_rng(seed)`, printing seed, case index and the failing input on first failure. This is the stand-in for `hypothesis`, which is not installed.
  - Bengali fixture table — base consonant × matra × {visarga, anusvara, candrabindu, none} × conjunct, with the verified expectations: `দুঃখিত → ['দুঃ','খি','ত']`, `নিঃশব্দ → ['নিঃ','শ','ব্দ']`, `অন্তঃসত্ত্বা → ['অ','ন্তঃ','স','ত্ত্বা']`, `পুনঃ → ['পু','নঃ']`.
  - Assert the harness runs green with zero tests before anything depends on it.
  - _Requirements: 2.9, 3.1, 3.4, 3.5, 3.9_

- [x] 3. Write bug condition exploration test
  - **Property 1: Bug Condition** - Echo-Driven Truncation Cuts a Grapheme Cluster
  - **CRITICAL**: This test MUST FAIL on unfixed code — the failure confirms the bug exists.
  - **DO NOT attempt to fix the test or the code when it fails.**
  - **NOTE**: This test encodes the expected behaviour from Property 1; it becomes the fix validator in task 5.10.
  - **GOAL**: surface counterexamples that demonstrate the bug on unfixed code.
  - **Scoped PBT approach**: the bug is deterministic given a trigger offset, so scope the property to concrete failing cases — trigger offset in 100–350 ms with near-end that is a delayed, attenuated copy of the far-end — then widen via the falsifier over echo delay 20–400 ms, attenuation 6–30 dB, noise level, trigger offset 20–349 ms, and utterance prefix drawn from the Bengali cluster table.
  - Bug condition under test, from the design's `isBugCondition`: `assistant_speaking` AND the while-speaking VAD gate would fire at `VAD_THRESHOLD_WHILE_SPEAKING` / `VAD_SPEECH_ONSET_FRAMES_WHILE_SPEAKING` AND `delivered_ms < ANOMALOUS_TRUNCATION_MS` (350) AND the near-end envelope is explained by the far-end reference at the tracked lag.
  - Case A — truncation reproduction: script `FakeSession` to emit a turn transcribed `আমি দুঃখিত, আমি…`, start playback, inject echo-correlated near-end frames at t=130 ms. Assert on unfixed code that `clearAudio` is sent and `delivered_ms < 350`.
  - Case B — upstream admission: same setup, assert on unfixed code that `activityStart` is sent and the flushed preroll contains echo-correlated frames (the mechanism behind `🗣️ [USER]: I'm sorry. I'm sorry.`).
  - Case C — loop reproduction: chain three apology-prefixed turns each truncated at ~130 ms, assert the delivered audio contains the leading cluster's partial audio three times.
  - Case D — anomaly invisibility: assert that on unfixed code no log line distinguishes this truncation from a legitimate barge-in, and no delta trace or model identifier exists.
  - Assertions encode the fixed behaviour from Property 1: not truncated, no `clearAudio`, no `activityStart`, anomaly logged with `delivered_ms` and `echo_correlation`, leading cluster delivered at most once.
  - Run on UNFIXED code. **EXPECTED OUTCOME: test FAILS.**
  - Document every counterexample found (e.g. "trigger at 130 ms with echo at −18 dB, 60 ms delay → `clearAudio` sent, `delivered_ms` = 131, `দুঃ` delivered 3×").
  - Mark complete when the test is written, run, and the failure is documented.
  - _Requirements: 1.1, 1.2, 1.4, 1.5, 1.6_

- [x] 4. Write preservation property tests (BEFORE implementing the fix)
  - **Property 2: Preservation** - Non-Echo Inputs Behave Identically
  - **IMPORTANT**: follow observation-first methodology. Run the UNFIXED code, record the actual outputs as golden records, then write tests that assert those recorded outputs. Do not assert assumed behaviour.
  - **EXPECTED OUTCOME for every sub-item: tests PASS on UNFIXED code**, establishing the baseline to preserve.
  - Golden records to capture per scenario: the full outbound μ-law byte stream, per-frame send timestamps, `clearAudio` timing, barge-in frame count, preroll contents at flush, and the ordered sequence of existing log lines.
  - Non-bug input domain for the falsifier: far-end inactive, OR near-end uncorrelated with far-end, OR trigger arriving after `ANOMALOUS_TRUNCATION_MS` of delivered playback.
  - Mark complete when all sub-items are written, run, and passing on unfixed code.
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9, 3.10, 3.11_

  - [x] 4.1 Genuine barge-in latency and side-effect ordering
    - Observe on unfixed code that sustained uncorrelated near-end during playback cuts at 4 frames / 80 ms, and record the side-effect order: queue drained → `clearAudio` sent → `activityStart` sent.
    - Include the >1 s trigger mode from Example 4 (1238–11803 ms) as genuine barge-ins that must keep cutting promptly.
    - _Requirements: 3.1_

  - [x] 4.2 Assistant-silent path
    - Observe that inbound frames while the assistant is silent take the `VAD_THRESHOLD` / 3-frame path, byte-for-byte.
    - _Requirements: 3.1, 3.2, 3.3_

  - [x] 4.3 Uninterrupted turn and interrupted-turn log formats
    - Observe the full outbound byte stream, the `🤖 [GEMINI]:` line, and `AI speech playback ended at … (calculated duration: N.NNs)` computed from `current_utterance_bytes / 8000.0`.
    - Observe `🎙️ [TIMING] AI Speech Interrupted at: HH:MM:SS.mmm` and `🤖 [GEMINI] (Interrupted): …` exactly as emitted today.
    - _Requirements: 3.4, 3.5_

  - [x] 4.4 Tool-call deferral and silence watchdog
    - Observe that speech during `tool_call_in_progress` lands in `preroll_pcm16` and is prepended to `gemini_input_buffer` on completion (app.py 1620–1622), and record the exact ordering.
    - Observe the `silence_watchdog` schedule under `SILENCE_FOLLOWUP_SECONDS` / `MAX_SILENCE_FOLLOWUPS`, and `VAD_SILENCE_OFFSET_FRAMES` end-of-turn detection.
    - _Requirements: 3.7, 3.8_

  - [x] 4.5 Framing, pacing and byte conservation
    - Observe that every `playAudio` payload decodes to exactly `PLIVO_ULAW_CHUNK_SIZE = 160` μ-law bytes at 20 ms spacing.
    - Falsifier over randomised turn shapes and chunk sizes: bytes sent to Plivo never exceed bytes received from the model for a turn, and no chunk hash is ever sent twice. This is the byte-conservation invariant on the outbound μ-law stream, and the downstream half of the gap-1 discriminator asserted as an invariant rather than only logged.
    - _Requirements: 3.9_

  - [x] 4.6 Non-Bengali and cluster-free Bengali rendering
    - Observe rendering for English, Hindi, and Bengali containing no consonant + matra + visarga cluster; capture as byte-exact golden records.
    - _Requirements: 3.2, 3.3_

  - [x] 4.7 `aec.py` integrity and placement
    - Pin a content hash of `aec.py` in the test suite and assert it never changes.
    - Assert `app.py` calls only existing public methods of the module, and that AEC still runs ahead of RNNoise in the inbound chain.
    - _Requirements: 3.10_

  - [x] 4.8 Session resumption, debug recording and call stats
    - Observe the resumption handle flow, `MAX_GEMINI_RECONNECTS`, and context restoration.
    - Observe the debug WAV byte count and `log_call_stats` output.
    - _Requirements: 3.6, 3.11_

  - [x] 4.9 Property 2 falsifier over the non-bug domain
    - **Status 2026-09-23: done. The correlation-at-threshold boundary is exercised on the bargein surface (`TestBoundaryCorrelationAtThreshold`). The activity-floor boundary is exercised by `test_far_end_active_floor_boundary`; its sibling constant check was renamed from `test_activity_floor_constant_is_deferred` to `test_activity_floor_constant_is_defined`.**
    - 500–1000 seeded cases asserting the unfixed system's outbound byte stream, barge-in frame count, side-effect ordering and existing-log-line sequence match the golden records.
    - Include the boundary cases the design calls out: trigger at exactly 350 ms, far-end energy exactly at the activity floor, correlation exactly at `ECHO_CORR_THRESHOLD`, and a turn whose first chunk is shorter than 20 ms.
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9, 3.10, 3.11_

- [ ] 5. Fix for the echo-driven false barge-in loop that truncates the leading grapheme cluster
  - **Status 2026-09-23: open. 5.11 needs a live call and 5.13 waits on the re-baseline approvals (see its note). Everything else under 5 is done.**

  - [x] 5.1 Concern (d) — per-delta trace instrumentation in `app.py`
    - **Land this before any behavioural change.** It is what definitively settles upstream-vs-downstream (gap 1), it is low-risk, and it is independently valuable even if the offline verdict in task 1 re-scopes concerns (a) and (b).
    - Add a bounded per-turn `delta_trace`. At each `model_turn` part carrying `inline_data` and at each `output_transcription` delta, append: sequence number, monotonic arrival time, byte count, cumulative bytes for the turn, a short content hash of the audio bytes, and for text the delta string plus whether it ends mid-grapheme-cluster.
    - Replaces the commented-out per-chunk line at app.py 1692 (`#logger.info("⚡ Gemini Audio Chunk Received!")`).
    - Maintain the three counters per turn: `model_audio_bytes_received`, `queued_bytes` (into `plivo_output_queue`), `sent_bytes` (to Plivo).
    - Log at DEBUG during normal operation; dump the whole trace at INFO when an anomalous truncation fires, so evidence survives without enabling global DEBUG in production.
    - Encode the three mutually exclusive verdicts as derived fields: upstream duplication (duplicate chunk hash or duplicate leading-cluster text sequence within one turn, counters equal), downstream re-delivery (model hashes distinct but a hash sent twice, or `sent_bytes > model_audio_bytes_received`), neither (hashes distinct both directions, counters equal, repeated `দুঃ` appearing as separate short-`delivered_ms` turns — the false-barge-in signature this design predicts).
    - _Bug_Condition: isBugCondition(input) — trigger within ANOMALOUS_TRUNCATION_MS while assistant_speaking_
    - _Expected_Behavior: expectedBehavior(result) — per-delta arrival time, byte count and text boundary recorded at diagnostic level, sufficient to prove upstream vs downstream origin_
    - _Preservation: existing `🤖 [GEMINI]:` / `(Interrupted):` lines and `ai_text_buffer` accumulation unchanged (3.4, 3.5)_
    - _Requirements: 1.7, 2.7_

  - [x] 5.2 Concern (d) — model identity logging
    - Log `GEMINI_MODEL` (app.py line 46, currently `"gemini-3.8-live"`) at session connect and at each resumption, together with `gemini_reconnect_count`, and include it in `log_call_stats`.
    - **This task is not needed to attribute the affected call.** That attribution is already settled by external evidence: the call ran on `gemini-3.1-flash-live-preview` with `thinking_level="low"` (see `## Affected Configuration` in bugfix.md).
    - It exists because **defect 1.8 still stands for every future call**: nothing in the log records the model identifier, so the next affected call would be just as unattributable from its own log alone. The evidence chain that settled this one — a find-and-replace match target observed during a working session — is not repeatable and will not exist next time.
    - This is the mitigation for **AR-1** in bugfix.md (the defect has not been reproduced on `gemini-3.8-live`): model-identity logging plus the requirement-2.6 anomaly logging converts the next occurrence from an ambiguity into a measurement.
    - _Bug_Condition: session established or re-established_
    - _Expected_Behavior: model identifier and reconnect attempt count recorded_
    - _Preservation: resumption handle, MAX_GEMINI_RECONNECTS and context restoration unchanged (3.6)_
    - _Requirements: 1.8, 2.8_

  - [x] 5.3 Concern (d) — trigger provenance and anomalous-truncation logging
    - On every barge-in decision, fired **or suppressed**, log `avg_prob`, `rnnoise_speech_count`, the active threshold and onset-frame count, `far_end_active`, `echo_correlation`, the tracked lag, `delivered_ms`, and the verdict. This is the measurement that converts the inferred half of the root cause into fact, and it complements task 1 rather than replacing it.
    - Add a distinct `⚠️ [ANOMALY] Truncation` line carrying `delivered_ms`, delivered bytes, the leading grapheme clusters of `ai_text_buffer`, the trigger source and the correlation evidence.
    - New line with a new prefix only. The existing `🎙️ [TIMING] AI Speech Interrupted at:` and `🤖 [GEMINI] (Interrupted):` lines keep their exact current format.
    - Note the existing sampling limitation this fixes: `🔊 [AGC]` and `🎧 [RNNoise]` emit every `LOG_EVERY_N_CHUNKS = 50` (~1 Hz), so today's nearest readings can be up to a second after the trigger. The new lines are emitted per decision.
    - _Bug_Condition: playback truncated after less than ANOMALOUS_TRUNCATION_MS of delivered audio_
    - _Expected_Behavior: anomalous truncation logged with delivered duration, leading text fragment, trigger source and correlation evidence_
    - _Preservation: existing interrupted-turn log format byte-identical (3.5)_
    - _Requirements: 1.4, 1.6, 2.4, 2.6_

  - [x] 5.4 Concern (d) — integration test proving the instrumentation discriminates
    - **The instrumentation is not trusted until this passes.** Design: Integration Tests → "Upstream/downstream discrimination".
    - Inject a deliberately duplicated model audio chunk via `FakeSession`; assert the delta trace flags it **upstream** (duplicate model chunk hash within one turn, `sent_bytes ≈ queued_bytes ≈ model_audio_bytes_received`).
    - Separately inject a deliberate local re-queue into `plivo_output_queue`; assert it is flagged **downstream** (model hashes distinct, a hash appears twice in the sent stream, or `sent_bytes > model_audio_bytes_received`).
    - Assert the "neither" verdict on a clean scripted loop of three short truncated turns with matching leading clusters.
    - Assert field completeness for requirements 2.4, 2.6, 2.7 and 2.8, and that no existing log line changed.
    - _Requirements: 2.4, 2.6, 2.7, 2.8_

  - [x] 5.4a Concern (d) — persist the far-end signal to disk alongside the inbound debug recording
    - **AUTHORISED IN THE CURRENT PASS.** This is the one gap that makes the next occurrence properly measurable. Task 1 could not report a single measured `echo_correlation` value — not because the analysis was weak but because **no recording of the played-out signal exists**. That is the sole reason `ECHO_CORR_THRESHOLD = 0.35` is still a guess and why task 5.11 cannot be seeded from `debug_recordings/Abhishek_20260921_144651.wav`. Everything in task 1's report is "consistent / inconsistent with assistant-derived audio" rather than "correlation proven" for this one reason.
    - **What to write.** A second WAV per call carrying the **outbound far-end** signal, written next to the existing inbound debug recording, so the next affected call has **both sides** on disk and true cross-correlation becomes possible for the first time.
    - **Where to tap it — the existing `aec.add_far_end()` call site, `app.py` ~1942, and nowhere else.** Reuse the **same `ulaw_to_pcm(chunk)` result already computed on that line**. Do **not** add a second μ-law decode, do **not** move or duplicate the `add_far_end` call, and do **not** introduce a new place where outbound chunks are touched. Task 1 established that line 1942 is the only `add_far_end` call site in the file; this task must not change that.
    - **Format — 8 kHz, not 16 kHz. Do not assume a shared sample rate with the inbound writer.** The far-end at this point is pre-resample telephony audio: mono, 16-bit PCM, **8000 Hz** (`setnchannels(1)`, `setsampwidth(2)`, `setframerate(8000)`). The existing inbound writer is 16 kHz (`setframerate(GEMINI_INPUT_RATE)`, app.py ~942) because it records post-resample `clean_pcm_16k`. Any future cross-correlation must resample one side; recording the far-end at the wrong declared rate would silently corrupt every lag estimate.
    - **Mirror the existing inbound writer's structure**, so there is one pattern to reason about rather than two: open it beside the inbound writer in the same `try` block (app.py ~933–944, after `os.makedirs("debug_recordings", exist_ok=True)` and the `safe_name` / `timestamp_str` computation), write it at the tap site the way the inbound stream is written at app.py ~1299–1303, and close it in the call teardown alongside the inbound writer (app.py ~1198–1203).
    - **Name it so the pairing is obvious**: `debug_recordings/{safe_name}_{timestamp}_farend.wav` next to the existing `debug_recordings/{safe_name}_{timestamp}.wav`, reusing the *same* `safe_name` and `timestamp_str` values so the two files sort together and the pairing is unambiguous without parsing logs.
    - **ACCEPTANCE CRITERION — attribution-neutral, stated explicitly.** This task only **observes a buffer that already exists at that point in the code**. It MUST NOT alter what is sent to Plivo, what the AEC receives via `add_far_end`, what the VAD sees on the inbound path, or the outbound 20 ms pacing. The byte stream to Plivo must be identical with the recorder on and off, and `PLIVO_ULAW_CHUNK_SIZE = 160` framing and 20 ms spacing must be unaffected (3.9). This is what keeps the current pass interpretable: if the next live call behaves differently, that difference must be attributable to reality, never to our instrumentation.
    - **Record the alignment metadata that Task 1 had to reverse-engineer.** At each write, log the monotonic timestamp and the cumulative far-end sample count (and, for the outbound side, the cumulative chunk index). Task 1 was forced to invent an RMS-anchor matching trick to recover a timebase — per-chunk WAV RMS matched against `logged_rms + logged_gain` at the ~1 Hz `🔊 [AGC]` anchors, with the chunk→sample map `sample(c) = c*320 − 160` whose −160-sample head offset had to be found by sweep. **Cross-reference Task 1's "Reproducing this" note: this task is what obsoletes it.** A future analysis should read alignment directly out of the log instead of rebuilding it, and should not need to bound a residual by hand.
    - **Non-fatal on failure, matching the existing inbound writer.** Wrap the open in `try/except` with a warning (as at app.py ~945–946) and the per-chunk write in the same bare `try/except: pass` shape used at app.py ~1300–1303. A debug recorder must never break a live call — if the disk is full or the file cannot be opened, the call proceeds with far-end recording silently disabled.
    - **Gitignore the recordings — currently they are NOT ignored.** `.gitignore` covers neither `debug_recordings/` nor `*.wav`, so the existing inbound recordings *and* these new far-end files would be committed. Add `debug_recordings/` (and `*.wav`) before this lands. Call-audio recordings are **caller PII** and must not enter version control.
    - **Unblocks task 5.11.** With both sides on disk, `ECHO_CORR_THRESHOLD` can finally be set from a measured distribution rather than guessed. Also supplies the direct test of task 1's inferred lag-0 leakage: the ~0-lag reading is currently inferred from onset timing plus the code path, not from a measured impulse response.
    - Mark complete when both files are written for a test call, the pairing and the 8 kHz/16 kHz rates are verified by reading them back, the gitignore entry is in place, and the outbound byte stream is confirmed byte-identical to the recorder-off baseline.
    - _Bug_Condition: any call — this is unconditional observability, not gated on the bug firing_
    - _Expected_Behavior: the played-out far-end signal is persisted per call at its true 8 kHz rate with alignment metadata, so cross-correlation against the inbound recording is possible for the next occurrence_
    - _Preservation: attribution-neutral — outbound byte stream, AEC input, VAD input and 20 ms pacing all unchanged (3.9); `aec.py` untouched (3.10); existing inbound debug recording unchanged (3.11)_
    - _Requirements: 1.7, 2.7_

  - [x] 5.5 Create `bargein.py` — pure, side-effect-free decision logic
    - **Status 2026-09-23: done as redesigned. `bargein.py` holds the envelope, correlation and `should_barge_in` (single threshold, per-onset lag search instead of `LagTracker`). The grapheme helpers, truncation classification and commit-gate arithmetic were implemented in `app.py`, not `bargein.py`, and have no `unicodedata` fallback walker (see the amended constraint above).**
    - **Purity is the design's explicit reason for the module split: it is what makes this logic testable without a phone call. Keep every function free of I/O, logging, timers and mutable global state; pass state in and return it out.**
    - `envelope(pcm16, bin_ms=10)` — short-term energy envelope.
    - `LagTracker` — estimate near/far lag from a wider envelope correlation during known assistant-only playback, once per second; hold the estimate between updates; re-estimate after reset.
    - `echo_correlation(near_env, far_env, lag)` — normalised envelope correlation over a ~300 ms window. Envelope domain, not waveform: ~30 bins × ~40 candidate lags instead of 3200 waveform lags per 20 ms frame. Use `numpy`; `scipy.signal.correlate` is available if profiling calls for it. Near-end input is **pre-AEC 8 kHz**, so the canceller's own suppression cannot hide the evidence.
    - `grapheme_clusters(text)` — UAX #29 clusters via `regex`'s `\X`, with the `unicodedata` fallback walker (new cluster at a base character, extend while the next code point is `Mn`/`Mc`/`Me`, is U+200D, or follows U+09CD).
    - `classify_truncation(delivered_ms, leading_text)` — anomalous vs normal against `ANOMALOUS_TRUNCATION_MS`, plus arm/disarm state transitions.
    - `shared_leading_clusters(prev_text, new_text)` — leading-cluster prefix comparison.
    - Commit-gate arithmetic — how much audio to withhold and when to release against `GRAPHEME_COMMIT_MS`.
    - `should_barge_in(...)` — the two-tier verdict, returning the verdict plus the evidence fields task 5.3 logs.
    - `preroll_bytes(records)` — reconstruct a flat buffer from tagged preroll records so both existing flush sites keep working.
    - _Bug_Condition: isBugCondition(input) from design — while-speaking trigger under ANOMALOUS_TRUNCATION_MS with near-end explained by far-end_
    - _Expected_Behavior: expectedBehavior(result) from design — corroboration required before truncation; leading cluster delivered once, whole, contiguous_
    - _Preservation: pure functions, no side effects; every non-bug input path returns today's verdict (3.1, 3.2, 3.3)_
    - _Requirements: 2.1, 2.2, 2.3, 2.5_

  - [x] 5.6 `bargein.py` unit tests
    - **Status 2026-09-23: done as redesigned. `test_bargein_unit` covers correlation, lag search, the latch and the real-recording triggers; `test_instrumentation_5_4` covers grapheme helpers, the cluster-invariance falsifier and the commit gate. No fallback-agreement or `LagTracker` tests, since neither exists.**
    - Grapheme segmentation, table-driven: the four verified Bengali cases, plus English, Hindi, mixed-script, empty string, lone combining mark, and a string ending mid-cluster. Run against both the `regex` implementation and the `unicodedata` fallback and assert they agree on the Bengali table.
    - Echo correlation: identical signals → ~1 at lag 0; delayed copy → ~1 at the true lag; uncorrelated speech → below threshold; far-end silent → activity-floor short-circuit; **double-talk (echo + caller speech) → below threshold**, which is the case that protects 3.1.
    - Lag tracker: converges to a known injected delay; holds through a silent gap; re-estimates after reset.
    - Truncation classifier: `delivered_ms` boundaries at 0, 349, 350, 351; arm and disarm transitions.
    - Prefix comparison: `আমি দুঃখিত, আমি` vs `আমি দুঃখিত, কিন্তু আমি এই ধরনের` (both real interrupted transcripts from the log) → shared prefix detected; `আমি দুঃখিত…` vs `আমি কি আপনাকে…` → no shared leading cluster beyond `আমি`.
    - Commit-gate arithmetic: releases at exactly `GRAPHEME_COMMIT_MS`; passes through unchanged when not armed; never emits a partial fragment when a trigger arrives mid-window.
    - Preroll tagging and trimming: leading echo frames dropped, genuine onset preserved, ~200 ms bound respected, and flat-bytes reconstruction matches today's `bytearray` exactly for all-genuine input.
    - Cluster-invariance falsifier over the base × matra × {visarga, anusvara, candrabindu, none} × conjunct table: segmentation never splits a cluster and never merges two.
    - _Requirements: 2.1, 2.2, 2.3, 2.5_

  - [x] 5.7 Concern (a) — ~~PARKED — PENDING REDESIGN~~ **IMPLEMENTED AS REDESIGNED (2026-09-23)** — far-end shadow buffer, ~~two-tier~~ corroborated gate, new constants
    - **UPDATE 2026-09-23:** unparked and implemented via `redesign-audio-pipeline.md`. It uses a playout-paced `far_shadow`, the `bargein.should_barge_in` single-threshold gate, the echo latch and the echo return ceiling. See `docs/HANDOFF.md` §3, §5.6 and §5.7. The PARKED note below is historical.
    - **PARKED — PENDING REDESIGN. NO CODE.** Task 1 returned **INCONCLUSIVE**, treated as REFUTED for scoping, and the operator chose instrumentation first, then redesign. Nothing below is implemented as specified: not `far_shadow`, not the two-tier gate, not the five constants. This item re-enters the plan only via a design-phase revision. The specification retained below is **input to that redesign, not a work order**.
    - **Redesign input 1 — lag-0 leakage, not acoustic echo.** The leak onset is **+50–90 ms after playback start for 13 of 19** short triggers (sd ≈ 13 ms), measured from the first `plivo_ws.send()`. That is **too fast for an acoustic round trip** — our WS → Plivo → PSTN/handset → caller's mic → back is >150 ms on a mobile leg. So far-end signal is reaching the inbound path at **lag ≈ 0**, not returning from the handset. `bargein.LagTracker` as specified in task 5.5 estimates the *acoustic* lag and evaluates `echo_correlation` **at** that lag, so it would look in the wrong place and **miss exactly the case it was built for**. **The lag search MUST cover lag 0.** Note also that **provider-side media loopback would present identically** from the available evidence and is **not excluded**; the redesign must not assume the AEC-desynchronisation path is the only candidate.
    - **Redesign input 2 — amplitude, not presence, is the discriminator.** The assistant-derived burst appears at **all 56 turn starts**, including the **11 never-interrupted turns**, and at the assistant's F0 in every group (**215.5 / 216.2 / 216.2 / 219.2 Hz** across short / mid / long / never-interrupted, versus the caller's **133 Hz**). Only **level** separates them: **−27.8 dB** on short-trigger turns versus **−80.6 dB** on long-trigger turns — a **33 dB gap** — with **19/20 short triggers above −41 dB**. A **presence test cannot work**; the gate must key on **level, or on level relative to the far-end**. This is also why task 1's clean-separation criterion failed: overlap is *expected* under the hypothesis.
    - **Redesign input 3 — thresholds must wait for measured data.** `ECHO_CORR_THRESHOLD` **cannot be seeded from the existing recording**, because the far-end was never persisted and no `echo_correlation` value was measured at all. Finalise it only after **task 5.4a plus task 5.3** have captured a live call. **Do not tune it against `debug_recordings/Abhishek_20260921_144651.wav`.**
    - **Two premise corrections established by Task 1 — do not let the redesign re-inherit them.**
      - The design's circumstantial figures (**−41.8 dB / VAD 0.85** for short triggers versus **−32.4 dB / 0.99** for long ones, and **−42.6 dB / 0.84** for the canonical 14:50:04 trigger) are an **artefact of 1 Hz sampling**, not evidence. The nearest AGC reading is **+478 ms** and the nearest VAD reading **+458 ms** *after* the trigger, so both describe **post-`clearAudio`** audio rather than the triggering frames. Under a nearest-reading rule the separation vanishes entirely (short −46.8 dB / 0.65 versus long −47.8 dB / 0.76). Task 5.3's per-decision logging is what these figures should have been.
      - `debug_recordings/Abhishek_20260921_144651.wav` is **NOT** missing the disclaimer span. The `playing_disclaimer` `continue` (app.py ~1231–1233) never fired for this call, because `stream_plivo_to_gemini` only starts after `await disclaimer_finished.wait()`. The ~2.9 s buffered during the disclaimer was drained as a burst (chunks 0–200 at ~5 ms/chunk) and **is** in the file. The head is not wall-clock continuous, but nothing is missing.
    - **Original specification, retained verbatim as redesign input — NOT to be implemented as written:**
    - Add `far_shadow`: a bounded ring of 8 kHz PCM16 holding the most recent ~600 ms, appended at the same site as `aec.add_far_end()` in `send_plivo_audio` (app.py ~1942) from the same `ulaw_to_pcm(chunk)` result. `aec.py`'s own FIFO is consumed by `_pop_far` and cannot be read without modifying the frozen module; the shadow is how `app.py` gets its own view. Cost ~10 KB and one `numpy` copy per 20 ms chunk.
    - Replace the raw `speech_started and assistant_speaking` test at app.py 1370 with `bargein.should_barge_in`:
      - **Tier 1 — far-end inactive** (shadow energy below `FAR_END_ACTIVE_FLOOR_DB`): return today's verdict unchanged, with no correlation computed. This path covers the overwhelming majority of frames and is what makes Property 2 hold for 3.2, 3.3 and most of 3.1.
      - **Tier 2 — far-end active, correlation below `ECHO_CORR_THRESHOLD`**: it is the caller. Barge in at the **existing** 4-frame / 80 ms latency.
      - **Tier 2 — correlation at or above threshold**: ambiguous or echo. Withhold truncation until sustained speech reaches `ECHO_AMBIGUOUS_ONSET_FRAMES`. Echo tracks the far-end and dies with it, so it does not reach that count; a real double-talking caller does.
    - Add the five constants near app.py lines 77–92: `ECHO_CORR_THRESHOLD = 0.35`, `ECHO_AMBIGUOUS_ONSET_FRAMES = 9`, `FAR_END_ACTIVE_FLOOR_DB = -60.0`, `ANOMALOUS_TRUNCATION_MS = 350`, `GRAPHEME_COMMIT_MS = 240`. All five are initial estimates; `ECHO_CORR_THRESHOLD` is a **guess** until task 1's measurement lands. Tuning is task 5.11.
    - **Do not touch `VAD_THRESHOLD_WHILE_SPEAKING` or `VAD_SPEECH_ONSET_FRAMES_WHILE_SPEAKING`.**
    - _Bug_Condition: isBugCondition(input) — vadGateWouldFire AND delivered_ms < 350 AND nearEndIsExplainedByFarEnd_
    - _Expected_Behavior: expectedBehavior(result) — trigger corroborated as genuine caller speech before playback is truncated; no clearAudio on an uncorroborated trigger_
    - _Preservation: Preservation Requirements 3.1, 3.2, 3.3, 3.9 — Tier 1 and uncorrelated Tier 2 keep today's frame counts and byte streams exactly_
    - _Requirements: 2.1, 2.4, 3.1_

  - [x] 5.8 **Standalone — AEC reference hygiene (decoupled from concern (a))**, using only existing public methods
    - **NOT part of concern (a) any more, and NOT blocked behind the (a) redesign.** It was previously filed under (a) and therefore inherited (a)'s gate; that was wrong. It is separable, and it is now gated on its own condition below.
    - **NEW GATE: unblocks when ONE clean live call has been captured with the concern (d) instrumentation and the far-end persistence from task 5.4a.** It does **NOT** wait on the (a)/(b) redesign. Stating this explicitly so it is not accidentally re-buried under a parked concern.
    - ~~**NOT greenlit for the current pass.**~~ **IMPLEMENTED (2026-09-23).** The greenlight was not recorded; the operator ratified it on 2026-09-23. Every `clearAudio` site and the reconnect path call `reset_far_reference(call_state)`, which calls `aec.reset_far_end()` and clears `farend_ref_queue` and `far_shadow`. See `docs/HANDOFF.md` §3, Task 4. The text below is the original gating rationale, kept for the record.
    - **Separability from concern (a)'s design — YES, established.** No dependency on `far_shadow`, on `echo_correlation`, on `LagTracker`, or on any of the five new constants. It uses only `aec.py`'s **existing public API** (`reset_far_end` at aec.py 116, `add_far_end` at aec.py 103). Its correctness argument **does not depend on echo being the trigger**: filtering against audio the caller never heard is wrong regardless of what fires the VAD. `aec.py`'s own `reset_far_end` docstring names **desynchronized far/near timing** as the reset condition, which is exactly what `clearAudio` causes. And Task 1's measured signature — an **inverted, band-shaped copy of the assistant's audio at the assistant's F0, at lag ≈ 0** — is consistent with `e = d − y` where `H` has adapted against a poisoned reference.
    - **Why it is nonetheless not greenlit now — the reason is attribution, not correctness.** The current pass is otherwise **pure observability**: it changes nothing the caller hears and nothing the VAD sees. Task 5.8 **does** change the inbound path. Shipping both together makes the next live call **uninterpretable** — a behavioural difference could not be attributed to the instrumentation *revealing* reality versus 5.8 *altering* it, which defeats the entire purpose of instrumentation-first.
    - **Two further costs of landing it early.** (i) It **perturbs task 4's preservation golden records**: post-`clearAudio` VAD decisions feed the barge-in frame counts recorded in task 4.1, so the baseline would have to be re-observed. (ii) **No measurement capability exists yet to verify it helps** — Task 1 could not measure `echo_correlation` at all, so there is currently no way to tell whether the reset improves anything.
    - **Second-order effects to verify when it does land.** Both are almost certainly improvements over injecting `−y`, but both are **unmeasured** and must be checked against the instrumented call:
      - `reset_far_end()` also discards `_near_buf`, throwing away up to `M − 1` = **159 samples ≈ 20 ms of near-end audio per invocation**.
      - With `_far_fifo` zeroed while `H` and `P` remain adapted, `_pop_far` zero-pads, so `Y` **decays to zero over `N` = 24 blocks ≈ 480 ms** after each `clearAudio`. **45 such events occurred in the affected call.**
    - Call `call_state["aec"].reset_far_end()` immediately after every `clearAudio` send, and clear `far_shadow` alongside **if and when `far_shadow` exists** (it belongs to the parked task 5.7; this task does not depend on it and must not introduce it). This stops audio the caller never heard from poisoning the reference — contributing mechanism 3, self-reinforcing across the 45 `🛑 Cleared Plivo playback buffer` events and 52 confirmed interruptions in the affected call.
    - **Site set, verified and closed**: exactly **three** `clearAudio` sends exist in `app.py` — **lines 1389, 1672 and 1721** — and those three are the complete set for this fix. An earlier version of the design counted "four sites (app.py ~1388, ~1671, ~1721, and the disclaimer path)"; that fourth site does not exist. `play_disclaimer` is **out of scope and MUST NOT be modified** (see the standing constraints and the spun-out ticket at the end of this document).
    - **Reconcile before implementing — two non-`clearAudio` playback-discard points**, still open implementation-time decisions: `out_buffer.clear()` in the ending-call path (app.py ~1849) and `out_buffer.clear()` in the `interrupting or user_activity_open` branch of `send_plivo_audio` (app.py ~1933). Both discard already-queued outbound audio without sending `clearAudio`, so both leave the far-end reference ahead of what the caller heard. Decide per site whether a `reset_far_end()` is warranted and document the decision: the ending-call path is terminal, so a reset there may be pointless, while the `send_plivo_audio` branch runs inside the barge-in flow already covered by the three sites above and may be redundant with them.
    - Record, at the `add_far_end` site, the monotonic timestamp of each chunk, so send-vs-playout skew against the canceller's modeled 480 ms path (`_PFDKF` with `N=24, M=160` → 3840 samples at 8 kHz) becomes measurable rather than assumed. The existing comment claiming the reference is "aligned to real playback time" is wrong and should be corrected to say it is timestamped at send.
    - `aec.py` MUST remain byte-identical — `reset_far_end()` and `add_far_end()` already exist and are public (aec.py 103, 116).
    - _Bug_Condition: isBugCondition(input) — residual echo strong enough to be explained by the far-end reference_
    - _Expected_Behavior: expectedBehavior(result) — cancellation is not degraded by discarded audio, so residual echo does not escalate across successive truncations_
    - _Preservation: Preservation Requirement 3.10 — aec.py unmodified, still invoked ahead of RNNoise in the inbound chain_
    - _Requirements: 2.1, 2.4, 3.10_

  - [x] 5.9 Concern (b) — ~~PARKED — PENDING REDESIGN~~ **IMPLEMENTED AS REDESIGNED (2026-09-23)** — echo not admitted upstream
    - **UPDATE 2026-09-23:** unparked and implemented.
      - Gate-suppressed onsets never open `activityStart`, during playback and in the post-playback echo tail (`ECHO_TAIL_FRAMES`; `docs/HANDOFF.md` §5.8).
      - The preroll tagging and trimming below was NOT implemented. It was measured on a level basis, as required: 40–80 ms echo-led, level-inseparable from the caller's onset. Operator decision: skip it, and reopen only if live calls show Gemini reacting to its own voice at the start of a barge-in turn.
      - The PARKED note below is historical.
    - **PARKED — PENDING REDESIGN. NO CODE.** Task 1 returned **INCONCLUSIVE**, treated as REFUTED for scoping; the operator chose instrumentation first, then redesign. The preroll tagging, the flush-time trimming and the `activityStart` gating are **not implemented as specified**. This item re-enters the plan only via a design-phase revision. The specification retained below is **input to that redesign, not a work order**.
    - **The three redesign inputs recorded under task 5.7 apply here in full** and are not restated: (1) lag-0 leakage rather than acoustic echo, so the lag search must cover lag 0; (2) amplitude rather than presence is the discriminator, since the assistant-derived burst is present at all 56 turn starts including the 11 never-interrupted ones and only a 33 dB level gap separates the groups; (3) thresholds must wait for measured data from task 5.4a plus 5.3, and must not be tuned against `debug_recordings/Abhishek_20260921_144651.wav`. The two premise corrections under 5.7 — the 1 Hz sampling artefact, and the recording *not* missing the disclaimer span — likewise apply.
    - **Specifically for (b):** this concern's trimming rule is "drop leading frames tagged far-end-active-**and-correlated**", which is a **presence test** and is exactly the form redesign input 2 rules out. The redesign must re-derive the trim criterion on a **level basis**. Note that task 1's strongest evidence for (b)'s existence is intact and unaffected by the parking: the 23 bogus caller turns measure **F0 200.0 Hz versus 135.6 Hz** for substantive turns, with 16 of the 23 falling inside a reconstructed playback interval — the assistant's own audio being admitted upstream and transcribed as the caller.
    - **Original specification, retained as redesign input — NOT to be implemented as written:**
    - Tag preroll frames **at capture** (app.py 1349–1353): `preroll_pcm16` becomes a deque of `(pcm16_frame, far_end_active, echo_correlation)` records instead of a flat `bytearray`, with the same ~200 ms bound (`PREROLL_MAX_BYTES_PCM16 = 6400`) expressed in frames.
    - Trim echo on flush at **both** sites: the barge-in flush (app.py 1408–1412) and the tool-call-completion prepend (app.py 1620–1622). Drop leading frames tagged far-end-active-and-correlated, keep everything from the first genuine frame onward, so a genuine onset is still not clipped — which is the whole reason the preroll exists.
    - Gate the activity window on the same verdict: `activityStart` and the `gemini_input_buffer` stream open only when `should_barge_in` corroborates. An uncorroborated trigger produces no `activityStart`, so the model receives no phantom caller turn — the mechanism behind `I'm sorry. I'm sorry.`, `¿Qué?`, `¿Qué tal?`, `Amén.`, `A`, `जी`, `Ata`.
    - Keep the `tool_call_in_progress` branch's shape and ordering unchanged; it only consumes the trimmed preroll via `bargein.preroll_bytes`. This is what preserves 3.7.
    - _Bug_Condition: isBugCondition(input) AND the preroll contains echo-tagged frames at flush time_
    - _Expected_Behavior: expectedBehavior(result) — echo never admitted to the model input path, never opens a caller activity window, never produces a caller turn_
    - _Preservation: Preservation Requirement 3.7 — tool-call deferral and preroll prepend ordering identical; genuine onset never clipped_
    - _Requirements: 2.5, 3.7_

  - [x] 5.10 Concern (c) — leading-fragment commit gate with armed state
    - Survives regardless of task 1's verdict, and is deliberately designed to hold whether the repeat originates upstream or downstream, because gap 1 cannot yet be settled.
    - On any truncation, capture `delivered_ms = current_utterance_bytes / 8000.0 * 1000`, the `ai_text_buffer` content, and its leading grapheme clusters.
    - Classify: `delivered_ms < ANOMALOUS_TRUNCATION_MS` → anomalous truncation. Enter the **armed state** and emit the task 5.3 anomaly log.
    - While armed, compare the new turn's leading clusters against the truncated turn's with `bargein.shared_leading_clusters`; a match means the caller is about to hear the same partial sound again.
    - While armed, hold the new turn's outbound audio in the sender until `GRAPHEME_COMMIT_MS` (240 ms) has accumulated, then release and resume normal 20 ms pacing. The fragment becomes all-or-nothing: if a spurious trigger fires inside that window the caller hears **nothing** rather than another `দুঃ`, which is how "at most once" in requirement 2.2 is satisfied.
    - Disarm on the first turn that completes past `GRAPHEME_COMMIT_MS` without truncation. Healthy calls are never armed, so steady-state latency is unchanged.
    - Do **not** implement the rejected alternative — trimming N ms off the front of a regenerated turn to "skip" the already-played fragment. The Live API exposes no time alignment between `output_transcription` deltas and `inline_data` chunks, so the byte offset for a text prefix is unknown and guessing it would clip genuine speech.
    - _Bug_Condition: armed state active AND the new turn shares leading grapheme clusters with the truncated turn_
    - _Expected_Behavior: expectedBehavior(result) — any given partial grapheme is delivered at most once; the cluster is delivered whole and contiguous or not at all_
    - _Preservation: Preservation Requirements 3.4, 3.9 — pacing and framing unchanged while armed; not-armed path is pass-through_
    - _Requirements: 2.2, 2.3_

  - [ ] 5.11 Tune the five new constants against the debug recording
    - **Status 2026-09-23: open, needs live calls (Redesign Task 8). `ECHO_CORR_THRESHOLD` is 0.88, not the 0.35 guess above, and `ECHO_AMBIGUOUS_ONSET_FRAMES` no longer exists.**
    - Use task 1's measured `echo_correlation` distributions to set `ECHO_CORR_THRESHOLD` from data. Until that measurement lands, the value **stays marked as a guess** in the code comment, and this task is not complete.
    - Choose the threshold to separate the short-trigger group from the long-trigger group with margin, and record the resulting false-suppress / false-barge-in trade-off at the chosen value.
    - Sanity-check `FAR_END_ACTIVE_FLOOR_DB = -60.0` against the recording's measured far-end-silent floor, `ANOMALOUS_TRUNCATION_MS = 350` against the 100–350 ms band in requirement 1.1 and the observed 32–193 ms short mode, `ECHO_AMBIGUOUS_ONSET_FRAMES = 9` against the double-talk unit tests, and `GRAPHEME_COMMIT_MS = 240` against the measured audio duration of one Bengali CV+visarga cluster.
    - Re-run tasks 5.6 and 4.9 after any constant change; boundary tests sit exactly on these values.
    - Note explicitly which values remain estimates pending a live call (see task 9).
    - _Requirements: 2.1, 2.4, 2.6_

  - [x] 5.12 Verify bug condition exploration test now passes
    - **Status 2026-09-23: done. Cases A, B and D and the realistic-domain sweep pass. Case C is retired (skipped) and `TestCaseDQuietCallerKnownLimitation` is an expected failure; see `docs/HANDOFF.md` §2 and §5.7. The sweep covers the realistic 0-80 ms band, not 20-400 ms.**
    - **Property 1: Expected Behavior** - Echo-Driven Truncation Never Cuts a Grapheme Cluster
    - **IMPORTANT**: re-run the SAME test from task 3. Do NOT write a new test. That test encodes the expected behaviour; its passing is the fix validation.
    - Assert, for all inputs where `isBugCondition` holds: playback not truncated, no `clearAudio` sent, no `activityStart` sent, anomaly logged with `delivered_ms` and `echo_correlation` recorded, and the leading cluster occurring at most once in the delivered audio.
    - Re-run the full falsifier domain (far-end fixture choice, echo delay 20–400 ms, attenuation 6–30 dB, noise level, trigger offset 20–349 ms, Bengali prefix table) with a recorded seed.
    - **EXPECTED OUTCOME: test PASSES** (confirms the bug is fixed).
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6_

  - [ ] 5.13 Verify preservation tests still pass
    - **AMENDED 2026-09-23 (operator):** the only re-baselines allowed are the deliberate ones listed in `docs/HANDOFF.md` §4.
    - **Status 2026-09-23: NOT ticked.** The task 4 tests pass, but the tick waits on two things, both listed for the operator in `docs/HANDOFF.md` §8 #7 item B:
      - The §4 re-baselines of task 4 tests (4_7 and 4_9) are recorded as intentional, but not as operator-approved.
      - Several task 4 tests were re-baselined for tasks 5.2, 5.3, 5.4a and 5.10 and are not in §4 at all, so the amended rule does not yet allow them.
    - **Property 2: Preservation** - Non-Echo Inputs Behave Identically
    - **IMPORTANT**: re-run the SAME tests from task 4 against the same golden records. Do NOT write new tests and do NOT re-baseline the golden records against the fixed code, except for the re-baselines listed in `docs/HANDOFF.md` §4 (amendment above).
    - Assert equality of the outbound μ-law byte stream, barge-in frame count, side-effect ordering, preroll flush contents, tool-call deferral sequence, silence watchdog schedule, session resumption path, debug recording, and every existing log line's format — excluding the new `⚠️ [ANOMALY]` and diagnostic lines, which are permitted additions.
    - Confirm the byte-conservation invariant from task 4.5 still holds on the fixed outbound stream.
    - Confirm the `aec.py` content hash from task 4.7 is unchanged, and that `requirements.txt` and `.env` are unmodified.
    - **EXPECTED OUTCOME: tests PASS** (confirms no regressions).
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9, 3.10, 3.11_

- [ ] 6. Requirement 2.9 regression test — prove the bug class is closed, not just দুঃখিত
  - **Status 2026-09-23: partial. `নিঃ` and `পুনঃ` prefixes are in the realistic-domain sweep (no truncation, no `activityStart`). No per-cluster "delivered exactly once" assertion yet. At the grapheme-helper level `অন্তঃ` is already unit-tested: `অন্তঃসত্ত্বা` is one of the four verified segmentations checked against `app.grapheme_clusters`, and the conjunct+visarga shapes are in the cluster-invariance table. The `অন্তঃ` echo-trigger scenario itself is not a plain unit test, so it is left as a Redesign Task 8 live check (operator, 2026-09-23).**
  - **Must use a consonant + matra + visarga cluster other than `দুঃ`.** This is the headline regression test and it deliberately avoids the single reported word. Design: Integration Tests → "Requirement 2.9 — different cluster, bug class closed".
  - **Primary case — `নিঃ` in `নিঃশব্দে`**: different consonant *and* different matra from `দুঃ`. Cluster = ন U+09A8 + ি U+09BF + ঃ U+0983; verified segmentation `নিঃশব্দ → ['নিঃ','শ','ব্দ']`. Script a turn transcribed `আমি নিঃশব্দে বলছি`, inject an echo-correlated trigger at 130 ms, and assert: no truncation, `নিঃ` present exactly once in the delivered audio, outbound bytes equal model bytes, and the suppressed trigger logged with its correlation evidence.
  - **Conjunct + visarga case — `অন্তঃ`**: the hardest shape, verified as `অন্তঃসত্ত্বা → ['অ','ন্তঃ','স','ত্ত্বা']` where `ন্তঃ` is U+09A8 + U+09CD + U+09A4 + U+0983. Same scenario, same assertions.
  - **Visarga on a bare consonant, no matra — `পুনঃ`**: verified as `পুনঃ → ['পু','নঃ']`. Same scenario, same assertions.
  - Determinism comes entirely from `FakePlivoWS` / `FakeSession` / `FakeClock` plus a fixed seed and the synthetic echo path built over the existing `playback_audio_files/*.wav`. **No phone call and no live model.**
  - Also assert the commit gate never releases a partial cluster's worth of audio for any of the three clusters.
  - _Requirements: 2.3, 2.9_

- [ ] 7. Latency budget assertions
  - **Status 2026-09-23: partial, and the design's table is superseded (there is no +100 ms ambiguous tier). Covered: genuine barge-in at 4 frames (`TestGenuineBargeInLatency`), the +60 ms post-playback cost (`TestCallerRightAfterPlaybackIsHeard`), framing and pacing (4.5), commit gate inert until armed.**
  - Assert the design's latency table case by case, as executable tests over the harness's frame counts:
    - Assistant silent / far-end inactive: 3 frames / 60 ms before and after the fix — **zero added latency**, and assert no correlation is computed on this path.
    - Assistant speaking, near-end uncorrelated: 4 frames / 80 ms before and after — **zero added latency**.
    - Assistant speaking, correlated or ambiguous: 4 frames / 80 ms → 9 frames / 180 ms — **+100 ms**, and only in this case.
    - Turn onset while armed: up to **+240 ms one-time**, and assert it cannot occur on a call that was never armed.
  - Assert `PLIVO_ULAW_CHUNK_SIZE = 160` payloads at 20 ms spacing hold in all four cases, including while armed (3.9).
  - Note in the test docstring that frame counts are assertable offline but perceived responsiveness is not — that belongs to task 9.
  - _Requirements: 3.1, 3.9_

- [ ] 8. Remaining post-fix integration scenarios
  - **Status 2026-09-23: partial. The echo-tail scenarios cover phantom-turn prevention; the reconnect test is deferred (skipped); the AEC desynchronisation ERLE measurement was not written.**
  - **Full loop closure**: three chained apology-prefixed turns with echo triggers; assert the loop does not form, no phantom caller turn is created, and the third turn completes. This is the executable counterpart of Examples 1 and 3.
  - **Genuine barge-in end to end**: uncorrelated sustained near-end during playback cuts at 80 ms, `clearAudio` is sent, `activityStart` follows, and the trimmed preroll still carries the caller's onset (3.1).
  - **Context switching**: tool call in progress → echo trigger → tool completes → genuine speech; assert the deferral ordering and preroll prepend are unchanged (3.7).
  - **Reconnect path**: simulate `GeminiSessionDisconnected` mid-turn; assert `reset_far_end`, `far_shadow` clear, lag re-estimation, and that resumption logs the model identifier with the attempt count (3.6, 2.8).
  - **AEC desynchronisation measurement** (edge case, read-only use of `aec.py`): feed far-end through `add_far_end` without a matching `reset_far_end` across a simulated `clearAudio`, measure echo-return-loss enhancement on a known echo signal before and after, and quantify contributing mechanism 3. Then assert the hygiene fix from task 5.8 removes the degradation.
  - _Requirements: 2.5, 2.8, 3.1, 3.6, 3.7, 3.10_

- [x] 9. Checkpoint — ensure all tests pass
  - **Status 2026-09-23: done (Redesign Task 7). 212 tests OK, 2 skipped, 1 expected failure; `aec.py` hash unchanged; `requirements.txt` and `.env` unmodified; both while-speaking VAD constants unchanged.**
  - Run the full suite: `venv/bin/python -m unittest discover -s tests -v`. Ensure every test passes, and ask the user if questions arise.
  - Confirm `aec.py`, `requirements.txt` and `.env` are unmodified, and that `VAD_THRESHOLD_WHILE_SPEAKING` and `VAD_SPEECH_ONSET_FRAMES_WHILE_SPEAKING` still hold their original values.
  - Confirm Property 1 passes and Property 2 passes, with recorded seeds for both falsifiers.

- [ ] 10. Record the validation boundary — what was proved offline vs what still needs a live call
  - **Status 2026-09-23: open (Redesign Task 8).**
  - **Validated offline** (no phone call): grapheme segmentation; correlation and lag arithmetic; truncation classification and arming; commit-gate timing; preroll trimming; prefix comparison; byte conservation and chunk-hash uniqueness; log formats and field completeness for 2.4/2.6/2.7/2.8; barge-in frame-count equality for non-bug inputs; μ-law framing and 20 ms pacing invariants; `aec.py` integrity; the requirement 2.9 regression across `নিঃ`, `অন্তঃ` and `পুনঃ`; and the retrospective echo measurement against `debug_recordings/Abhishek_20260921_144651.wav`.
  - **Still requires a live call**, stated plainly so the offline suite is not mistaken for full coverage:
    - Real acoustic echo on a real handset or speakerphone, and therefore the true `echo_correlation` distribution that `ECHO_CORR_THRESHOLD` must be tuned against. The synthetic echo path is a model, not the room.
    - Actual AEC convergence with the reference-hygiene fix in place, including send-vs-playout skew against the canceller's modeled 480 ms path.
    - Perceived barge-in responsiveness under the +100 ms ambiguous-case budget. The frame count is unit-testable; whether a caller notices is not.
    - Plivo's real `clearAudio` timing and jitter-buffer behaviour, and end-to-end latency against baseline (3.9).
    - Mid-call session resumption under real network conditions (3.6) — the affected call had no mid-call reconnect, so the incident itself does not exercise this path.
    - Whether the stutter reproduces at all on `gemini-3.8-live` (gap 2). Unverified and possibly unreproducible. The fix is not contingent on it: every link in the loop is implementation-side and model-independent.
    - The final upstream-vs-downstream verdict (gap 1). Task 5.4 proves the instrumentation discriminates correctly; only a real affected call supplies the data to discriminate.
  - List which of the five constants remain estimates after task 5.11, and what live measurement would finalise each.
  - _Requirements: 2.4, 2.7, 2.8, 2.9_
---

## Spun Out — Separate Ticket (not part of this fix)

**This section is not a task in this plan. It MUST NOT be started as part of this spec.** It is written self-contained so it can be lifted straight into a tracker.

### `play_disclaimer` never enters the echo canceller's far-end reference

**Status:** unscheduled. **Not a blocker for the bengali-grapheme-stutter-fix bugfix.**

**What the code does.** `play_disclaimer` (`app.py` ~744–779) sends `playAudio` frames directly to Plivo but never calls `aec.add_far_end()`. The only `add_far_end` call site in the file is **line 1942**, inside `send_plivo_audio`. Disclaimer audio therefore never enters the echo canceller's reference, so the canceller has no model of it.

**Why it has not caused a visible failure.** Inbound audio is separately discarded while `playing_disclaimer` is set — the `continue` at `app.py` ~1231–1233 skips every media event for the duration of the disclaimer. With no inbound audio being processed, the missing reference has no observable effect today.

**Risk if that guard is ever relaxed, or if the disclaimer overlaps live audio.** The canceller would have no reference for the disclaimer, so disclaimer echo would be **uncancellable** — it would pass straight through the inbound chain into VAD, AGC and the model input path, with the same class of consequence this bugfix addresses for assistant TTS echo.

**Relationship to this bugfix.** This bugfix neither fixes nor depends on the gap. It is excluded at the operator's instruction and recorded in `## Out of Scope` in bugfix.md. Task 5.8 covers the three real `clearAudio` sites (1389, 1672, 1721) and nothing in this spec touches `play_disclaimer`.
