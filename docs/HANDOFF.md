# Audio Pipeline Redesign — Session Handoff

Snapshot for resuming the AU Admission Assist audio-pipeline redesign (echo
rejection, barge-in, VAD) on `gemini-3.8-live`.

**Current state (2026-09-23, after Redesign Task 7):** Redesign Tasks 1-7 are
done. The suite is green (section 2). Committed on `dev` as `84eb866`, then
`be6c19f` (line endings, gitignore, tracked spec copies) and a docs-and-comments
commit on top; not pushed. Remaining: Redesign Task 8, live-call validation,
which needs real calls.

**Numbering.** This file numbers its own work "Redesign Task 1-8". The spec
(`docs/spec/tasks.md`) numbers tasks 1-10 with subtasks 5.x. The mapping is in
section 3. In the historical text (sections 4, 5 and 7), a bare "Task N" means
Redesign Task N.

This file was first written at a deliberate halt mid-Task-5. Sections 5.1-5.5
and 7 are that halt's diagnosis trail. They are kept for the record and are
resolved by sections 5.6-5.8.

---

## 1. File hashes (sha256) at halt

```
aec.py     495a82ca41a97460a9885aa1f62b9c856e8feee158fe4f8785223be3d20edd29   (UNCHANGED all session)
bargein.py 35bc62ea880e01868931549442ca874d85e7e1980b6accb2014961f4cf3a7439
app.py     019ed33f9e9f73db34b57fed9d90db44d604ece5b4497432de6dc605fa4b1b3e
```

Updated 2026-09-23 after the gate-window fix (section 5.6); aec.py and bargein.py unchanged:

```
app.py     f8755f2c31c94606c3770c69ac2a2543747bf0a3b365873379b2bf912df92c0f   (after echo latch)
```

Updated again 2026-09-23 after the echo return ceiling and latch field logging
(section 5.7); aec.py and bargein.py still unchanged:

```
app.py                                                    7d3dd640020be46184e4c2bc560f5d233a91e4eb0a8b4412059f6aa04ab1964e
tests/harness/appctl.py                                   f3aa41231173a8f1d6ec2a30c9fafcb7595edefb0ab064301acfe44b904095ef
tests/test_bug_condition_exploration.py                   57e3fb0f3d10a6c2e163af80f5b2a9848df2729fd6248dcf9eeab2d106541b1a
tests/test_bargein_unit.py                                a8a31221ae47ba1a40f5bfb80aff7654f3273e746b428459bd89301329b912c8
tests/test_preservation_4_8_resumption_recording_stats.py 1e33e4aca113a7d86e627d89f95725393424904bc4b2f0cbada390e8e0926a8e
```

Updated again 2026-09-23 after the post-playback echo tail gate (section 5.8);
aec.py and bargein.py still unchanged, test_bargein_unit.py unchanged:

```
app.py                                                    69870a34bf6aad5d3ff725057a4eba21470bdb954ed3c5e3008915f0b13b568d
tests/harness/appctl.py                                   26cdb60479f7604efbc878bcbaaff7669290043c87bf6d312445eb3ac871174f
tests/test_bug_condition_exploration.py                   a7baf73d92cf188feb9acb36ffedb80f66eb0a81879779240bbcb76345428473
tests/test_preservation_4_8_resumption_recording_stats.py 4996b08736aeaca63a5872acc55ee09adb3fe60ef115c5d9014e747bc11fda1e
```

Updated again 2026-09-23 after Redesign Task 7's comment and docstring fixes
(section 8, #7 item 5). aec.py unchanged. bargein.py changed for the first time
since the halt (the `should_barge_in` docstring and the spec citation; no code change). app.py
changed in comments only, with the same line count, so the `GEMINI_MODEL` pins
still hold. test_bug_condition_exploration.py gained `TestDesignLiterals`:

```
bargein.py                                                aea4d037fbcd287b59592ac42eae307752af178eccc601eed527daf42f023600
app.py                                                    0f1e075b93ab4015567b44e3e8237de193157e0f8047acf6b297b9558eceef8c
tests/test_bug_condition_exploration.py                   4ccb3e074cdc58524fc50f33b3f5bc1d5bff61cfffd25feadc754384420d0ed8
```

These hashes are of the LF working tree. Since `be6c19f` the repo has a
`.gitattributes` with `* text=auto eol=lf`. A fresh clone on this machine
(`core.autocrlf=true`) checks out `aec.py` with the same sha256, and
`tests.test_preservation_4_7_aec_integrity` passes 11/11 there, so no re-pin
was needed.

`aec.py` is byte-identical to session start and pinned by
`tests/test_preservation_4_7_aec_integrity.py::TestAecPyIsByteIdentical`. The
redesign has NOT needed to modify the canceller — only how `app.py` drives it.

## 2. Test suite state

**Current (2026-09-23, after Redesign Task 7):**
`venv312/Scripts/python.exe -m unittest discover -s tests -t .` gives **212
tests, OK (2 skipped, 1 expected failure)**. That was 208 after section 5.7,
plus 3 echo-tail tests, plus `TestDesignLiterals` (the test file's
`ANOMALOUS_TRUNCATION_MS = 350` literal must equal `app.ANOMALOUS_TRUNCATION_MS`).
- The skips are the pre-existing Case C retirement and the reconnect-loop
  deferral.
- The expected failure is
  `TestCaseDQuietCallerKnownLimitation.test_quiet_genuine_caller_barges_in`
  (section 5.7).
- `tests.test_preservation_4_7_aec_integrity`: 11/11 OK, including
  `TestAecPyIsByteIdentical`.

Redesign Task 7 neutrality checks (ad hoc, scratch script, not in the suite):
- With `ECHO_GATE_ENABLED` off, pre-gate behaviour returns. Echo truncates
  (clearAudio 1, activityStart 1, 9600 bytes to the model) and the echo tail
  opens a phantom turn.
- A genuine barge-in and a no-echo caller after playback produce identical
  outbound Plivo bytes and model input with the gate on and off.

Existing in-suite neutrality proofs still pass:
- The far-end recorder on vs off is byte-identical.
- The commit gate is inert until armed.
- The 4_x preservation golden records hold.

### Historical: state at the mid-Task-5 halt (resolved, kept for the record)

`venv/bin/python -m unittest discover -s tests`  →  **198 tests, 4 failures,
1 error, 2 skipped.**

All 5 non-passing are in `tests/test_bug_condition_exploration.py` and all stem
from ONE harness-fidelity issue (section 5), not a production defect:

```
ERROR: test_anomalous_truncation_is_logged        (TestCaseDAnomalyInvisibility)
FAIL:  test_leading_cluster_turn_is_not_truncated_by_echo   (TestCaseA...)
FAIL:  test_echo_never_opens_an_activity_window_or_reaches_the_model (TestCaseB...)
FAIL:  test_gate_suppresses_every_realistic_echo  (TestGateSuppressesEchoAcrossTheRealisticDomain)
FAIL:  test_model_identifier_is_recorded          (TestCaseDAnomalyInvisibility)
```

Everything else passes: all `test_preservation_4_*`, all `test_instrumentation_5_4`,
`test_harness_smoke`, and crucially **`test_bargein_unit` (13 tests) including the
real-recording validation that proves the gate suppresses actual echo at 0.97
correlation.**

> (Resolved: all five were fixed by sections 5.6-5.7. The pin is re-pinned
> after every app.py change; current values are in section 5.8.)
>
> NOTE on `test_model_identifier_is_recorded`: this is a line-number pin in
> `test_preservation_4_8_resumption_recording_stats.py`. Task 5 added lines to
> `app.py` (the gate + helpers + constants) so the `GEMINI_MODEL` reference line
> numbers shifted again. It is a mechanical re-pin (same as done twice before),
> NOT the harness-fidelity issue. Re-grep `grep -n GEMINI_MODEL app.py` and update
> the pinned list in that test. It was passing before Task 5's edits.

---

## 3. What is DONE and wired (Redesign Tasks 1–7 complete)

Task list lives in the session todo and in `docs/spec/tasks.md` (tracked
copy; the Kiro working copy under the gitignored
`.kiro/specs/bengali-grapheme-stutter-fix/` is kept identical). Progress:
**7/8 complete; Redesign Task 8 needs live calls.** Redesign Task 6 is in
section 5.8, Redesign Task 7 in section 8.

Mapping from this file's Redesign Tasks to the spec's numbering. Spec tasks
2-4 and 5.1-5.4a, 5.10 (harness, exploration and preservation tests, the
concern (c)/(d) instrumentation and commit gate) were done before the redesign
started and have no Redesign Task.

| Redesign Task | What | Spec task(s) |
|---|---|---|
| 1 | Grounding: map the audio path | none (spec task 1, the offline provenance check, is a separate earlier measurement) |
| 2 | Design note, `docs/spec/redesign-audio-pipeline.md` | the "design-phase revision" the spec's scope gate required for 5.7/5.9 |
| 3 | `bargein.py`, pure module plus unit tests | 5.5, 5.6 (as redesigned) |
| 4 | Far-end alignment: playout-paced reference, `far_shadow`, `reset_far_reference` | 5.7 (the shadow buffer), 5.8 |
| 5 | Corroborated gate, echo latch, echo return ceiling | 5.7 (the gate), 5.3's evidence fields |
| 6 | Concern (b): post-playback echo tail gate; preroll trim declined | 5.9 |
| 7 | Verification and doc reconciliation | 5.12, 5.13, 9 |
| 8 | Live-call validation and tuning | 5.11, 10 |

### Redesign Task 1 — grounding (DONE)
Full audio path mapped. Key facts:
- INBOUND `stream_plivo_to_gemini`: raw 8k → `aec.process` (AEC runs FIRST on
  raw 8k) → upsample 48k → RNNoise `denoise_chunk` (per-frame speech_prob) →
  downsample 16k → AGC + soft limiter → `clean_pcm_16k`. `avg_prob = mean(speech_probs)`.
  VAD gate: while `assistant_speaking and not is_speaking` uses
  `VAD_THRESHOLD_WHILE_SPEAKING=0.82` / `VAD_SPEECH_ONSET_FRAMES_WHILE_SPEAKING=4`
  (80 ms); else `VAD_THRESHOLD=0.75` / `VAD_SPEECH_ONSET_FRAMES=3`. Preroll
  (~200 ms / `PREROLL_MAX_BYTES_PCM16=6400`) on `clean_pcm_16k` while not
  `user_activity_open`.
- OUTBOUND `send_plivo_audio` → `emit_chunk`: NO pacing, frames dumped
  back-to-back (measured 0.0 ms inter-frame). Playback-ended detected via wall
  clock vs `current_utterance_bytes/8000`.
- CONFIG (~line 1503): `gemini-3.8-live`, temp 0.2, AUDIO, in/out transcription,
  voice Kore, `automatic_activity_detection` DISABLED (manual VAD),
  `activity_handling=START_OF_ACTIVITY_INTERRUPTS`, session resumption +
  context window compression.

### Redesign Task 2 — design note (DONE)
`docs/spec/redesign-audio-pipeline.md`. Two
coupled defects:
- **D1** far-end reference fed at send-time (no pacing) → `buffer_lead` grows
  208→558 ms > the AEC's 480 ms modelled window → cancellation degrades →
  residual echo crosses the VAD gate.
- **D2** barge-in gate cannot tell echo from speech → truncates (the stutter) and
  flushes echo upstream (the garbage caller turns).

### Redesign Task 3 — `bargein.py` (DONE, pure module, unit-tested)
Pure, no I/O/logging/state. Surface:
- `pcm16_to_float(bytes)`
- `envelope(samples, *, rate, bin_ms=10)` — short-term RMS energy envelope
- `rms_dbfs(samples)`
- `far_end_active(samples, *, floor_dbfs)`
- `echo_correlation(near_pcm16, far_pcm16, *, near_rate, far_rate, bin_ms=10, lag_search_ms=80, floor_dbfs=-60)`
  → `CorrelationResult(correlation, lag_bins, far_active, near_dbfs, far_dbfs)`
- `should_barge_in(..., threshold, ...)`
  → `BargeInDecision(barge_in, is_echo, reason, correlation, lag_bins, far_active, near_dbfs, far_dbfs)`
  reasons: `far-end-inactive` | `echo-correlated` | `uncorrelated-double-talk`.

Design facts baked in (measured, not guessed):
- ENVELOPE domain, never waveform (waveform corr flipped sign −0.47…+0.58; envelope was 0.976–0.993).
- Lag search default **80 ms** — measured real best-lag was 50–60 ms, NOT 0.
- `test_bargein_unit.py` (13 tests at Task 3; 20 now, after
  `TestProductionGateCatchesRealisticEchoDelays` (section 5.6) and
  `TestEchoLatch` (section 5.7); PASSING): synthetic tests MUST use
  amplitude-MODULATED signals (a pure sine has a flat envelope → 0 variance →
  0 correlation). Real-recording tests parse `Gemini_Assistant.log` 17:05 call,
  align per-trigger via far-end `t_mono` anchors + AGC inbound anchors
  (`isamp=(50n*320−160)/2` for 8k), window 180 ms before each trigger →
  **median corr 0.97**, and `should_barge_in` classifies ≥ (total−1) of the real
  triggers as echo. **This is the authoritative proof the gate works on real
  residual.** Tests skip gracefully if recordings/log slice absent.

KEY FINDING from Task 3: the two on-disk WAVs (`debug_recordings/Soham_Paul_20260922_170459.wav`
inbound 16k, `..._farend.wav` 8k) CANNOT be globally aligned — the far-end is
wall-clock-continuous (zero-padded) but the inbound recorder skips frames during
disclaimer/closing (compressed gaps). Alignment MUST be per-trigger via log
anchors. In the LIVE system the shadow buffer is built inline against the
inbound clock, so alignment is controlled by construction — which is why Task 4
(playout-aligned shadow) is essential.

### Redesign Task 4 — far-end alignment fix (DONE)
Implemented in `app.py`:
- Imports: `import collections`, `import bargein`.
- Constants (after `FAREND_MAX_PAD_SAMPLES`):
  - `ECHO_CORR_THRESHOLD = float(os.getenv("ECHO_CORR_THRESHOLD","0.88"))`
  - `ECHO_CORR_WINDOW_MS = 180`
  - `ECHO_LAG_SEARCH_MS = 80`
  - `FAR_END_ACTIVE_FLOOR_DB = -60.0`
  - `FAR_SHADOW_FRAMES = 40`
  - `ECHO_NEAR_BYTES_16K = int(round(ECHO_CORR_WINDOW_MS/1000*GEMINI_INPUT_RATE))*2`  (= 5760)
  - ~~`ECHO_FAR_FRAMES = ceil((ECHO_CORR_WINDOW_MS + 2*ECHO_LAG_SEARCH_MS)/20)`  (= 17)~~
    replaced 2026-09-23 (section 5.6) by `ECHO_FAR_MS = ECHO_CORR_WINDOW_MS + ECHO_LAG_SEARCH_MS`
    (= 260), `ECHO_FAR_BYTES_8K` (= 4160), `ECHO_FAR_FRAMES = ceil(ECHO_FAR_MS/20)` (= 13)
  - `_FAR_SILENCE_FRAME = b"\x00"*(PLIVO_ULAW_CHUNK_SIZE*2)`  (= 320 bytes)
  - `ECHO_GATE_ENABLED = os.getenv("ECHO_GATE_ENABLED","1") in truthy`  (default ON, kill-switch)
- `call_state` keys (added in BOTH `app.py` and `tests/harness/appctl.py` mirror,
  and appctl got `import collections`):
  - `"farend_ref_queue": collections.deque()`
  - `"far_shadow": collections.deque(maxlen=FAR_SHADOW_FRAMES)`
  - Added later, also in both places: `"echo_latch_erl_db"`, `"latch_episode"`
    and `"latch_stats"` (section 5.7), and `"far_silent_frames"` (section 5.8).
- `reset_far_reference(call_state)` helper (defined before `reset_delta_trace`):
  calls `aec.reset_far_end()` + clears `farend_ref_queue` + clears `far_shadow`.
  Since sections 5.7/5.8 it also drops the echo latch (closing the episode as
  `ended_by=reset`) and sets `far_silent_frames = ECHO_TAIL_FRAMES`.
  Replaced all 4 bare `reset_far_end()` call sites with it (barge-in,
  model-confirmed-interrupt, ringback, reconnect). The ONLY direct
  `aec.reset_far_end()` is now inside this helper.
- `emit_chunk`: `far_pcm8 = ulaw_to_pcm(chunk)`; now `farend_ref_queue.append(far_pcm8)`
  instead of `add_far_end`. WAV recorder unchanged (still records `far_pcm8`, send-side).
- INBOUND loop, immediately before `aec.process`: pop one far frame from
  `farend_ref_queue` (or `_FAR_SILENCE_FRAME` if empty), `add_far_end(far_ref)`,
  `far_shadow.append(far_ref)`. → the AEC reference is paced to realtime (inbound
  cadence) and `far_shadow` advances frame-for-frame with the preroll, so the gate's
  near/far windows are aligned by construction with no cross-timebase math.
  `add_far_end` now has exactly ONE call site, in the inbound loop.

DESIGN of Task 4: pace the REFERENCE feed, not the Plivo send (send stays fast;
preservation tests pin it). Inbound blocks arrive at realtime (Plivo 20 ms), so
consuming one far frame per inbound block paces the reference to playout.

### Redesign Task 5 — corroborated barge-in gate (DONE)
The as-first-wired description is below. The final logic differs in four ways;
section 5.7 is authoritative:
- The gate condition is `assistant_speaking or far_window_has_playback()`
  (the echo tail, section 5.8).
- `apply_echo_latch` sits between the verdict and the suppression.
- `ECHO_MAX_RETURN_DB` overturns loud "echo" to `near-too-loud-for-echo`.
- The `[ECHO-GATE]` line also carries `erl_db` and `latched`.

Implemented in `app.py`:
- `evaluate_echo_gate(call_state)` helper (defined before `reset_delta_trace`):
  near = tail `ECHO_NEAR_BYTES_16K` of `preroll_pcm16` (16k); far = last
  `ECHO_FAR_FRAMES` of `far_shadow` joined, trimmed to the last
  `ECHO_FAR_BYTES_8K` (8k, 260 ms; section 5.6); returns
  `bargein.should_barge_in(near, far, near_rate=GEMINI_INPUT_RATE,
  far_rate=PLIVO_SAMPLE_RATE, threshold=ECHO_CORR_THRESHOLD,
  lag_search_ms=ECHO_LAG_SEARCH_MS, floor_dbfs=FAR_END_ACTIVE_FLOOR_DB)`.
- Wired into `stream_plivo_to_gemini` at the top of the `if speech_started:`
  block, BEFORE "User speech detected": if `ECHO_GATE_ENABLED and
  assistant_speaking`, call `evaluate_echo_gate`; if `decision.is_echo`, log
  `🛡️ [ECHO-GATE] suppressed false onset | corr=… | lag_ms=… | near=…dB far=…dB
  | avg_prob=… | reason=…`, revert the false onset (`is_speaking=False`,
  `rnnoise_speech_count=0`), emit `echo_suppressed` event, and `continue`. This
  single revert-and-continue prevents BOTH truncation (concern a) AND opening a
  caller activity window / upstream flush (concern b).
- `_format_echo_evidence(decision)` helper: renders `corr=…,lag_ms=…,far_active=…,
  reason=…` for the `[TRIGGER]` and `[ANOMALY]` logs, or the
  `not-measured[concern-a-parked]` placeholder when no decision was taken. Both
  the `[TRIGGER]` line and the `[ANOMALY]` line now use it (replacing the old
  `FAR_END_EVIDENCE_NOT_MEASURED` literal).
- `echo_gate_decision` local is initialised to `None` before the `speech_started`
  block and threaded into both log lines.

Manual VAD kept (`automatic_activity_detection` stays disabled) per design Part 4.

---

## 4. Test reconciliations already applied (all intentional baseline updates)

- `test_harness_smoke.py`: `test_parked_constants_absent` → renamed
  `test_corroborated_gate_constants` (ECHO_CORR_THRESHOLD + FAR_END_ACTIVE_FLOOR_DB
  now exist; `ECHO_AMBIGUOUS_ONSET_FRAMES` intentionally NEVER implemented — the
  gate is single-threshold, not two-tier). `test_bargein_module_not_created` →
  `test_bargein_module_exists_with_its_pure_surface`.
- `test_instrumentation_5_4.py`: `test_bargein_module_still_does_not_exist` →
  `test_bargein_module_now_exists_after_redesign`; far-end recorder test now
  asserts recorder decoupled from `add_far_end` (`written == ulaw_to_pcm(outbound)`,
  `far_end_fed == b""`).
- `test_preservation_4_7_aec_integrity.py`: `add_far_end` now `add_far_end(far_ref)`
  in the inbound loop (one site); `clearAudio` sites now paired with
  `reset_far_reference` (×4), bare `reset_far_end` ×1 in helper; call-order test
  now expects `add_far_end, process, rnnoise` per frame.
- `test_preservation_4_9_falsifier.py`: two previously-deferred boundary tests
  flipped to real tests; added `import bargein`.
- `test_bug_condition_exploration.py`: extensively reworked — see section 5.

---

## 5. THE OPEN PROBLEM — Case A/B harness fidelity (where we stopped)

### 5.1 Symptom
Case A/B and the gate-suppression sweep fail with `echo_suppressed=False`: the
gate runs but measures correlation BELOW the 0.88 threshold in the synthetic
harness, so it classifies the onset `uncorrelated-double-talk` and allows the
barge-in (clearAudio ×1, activityStart ×1, echo admitted upstream).

### 5.2 Root cause identified (harness, not production)
The synthetic echo (`tests/harness/echo.py::synth_echo`) is a CLEAN
delay-and-attenuate copy of the far-end — exactly the linear transform the real
PFDKF canceller models. So the real `AcousticEchoCanceller` cancels it almost
perfectly and leaves NO residual for the gate to correlate against. Real
acoustic echo is a complex, partly non-linear path the linear canceller cannot
fully remove — which is why real post-AEC residual still correlated 0.97 with
the far-end on the reproduction call. `echo.py` already documents this
limitation ("insufficient to say anything about real echo_correlation
distributions"). So the synthetic harness OVER-cancels and cannot exercise the
gate on realistic residual.

### 5.3 Correlation / alignment measurements taken (the diagnosis trail)

1. **Test-side perfectly-aligned correlation** (Case A input characterisation,
   `echo.normalised_envelope_correlation`, lag 6 bins): **1.000**. Confirms the
   constructed near-end IS echo-correlated in principle.

2. **First live-gate run (BEFORE the harness rework — 7 far frames enqueued,
   13 near frames fed):** `[TRIGGER]` line showed
   `far_end_evidence=corr=0.685,lag_ms=80,far_active=True,reason=uncorrelated-double-talk`.
   Cause: only 7 far frames enqueued but 13 near frames fed, so `far_shadow`
   drained to silence while echo near-frames kept arriving → near/far misaligned.

3. **After reworking the harness to enqueue the FULL far slice**
   (`playback_frames + near_frames` = 20 frames enqueued) so `far_shadow` stays
   populated: still `echo_suppressed=False`. Instrumented frame-by-frame
   (`delay_ms=60, attenuation=18`):
   ```
   after near frame 6: is_speaking=False rnnoise_cnt=3 far_shadow_len=7 preroll_len=4160
   after near frame 7: is_speaking=True  rnnoise_cnt=4 far_shadow_len=0 preroll_len=0   <-- onset; barge-in fired then reset_far_reference cleared far_shadow+preroll
   ```
   `farend_ref_queue` had all 20 frames after send; `current_utterance_bytes=3200`.
   `ECHO_GATE_ENABLED=True` confirmed in test env. So the gate DID run at the
   onset (frame 7) but returned not-echo → barge-in → `reset_far_reference`
   cleared the buffers (that's why the post-frame-7 snapshot shows 0/0).

4. **Added `_PassthroughAec` stub** (`add_far_end`/`reset_far_end` no-ops,
   `process` returns input unchanged) and set `call_state["aec"] =
   _PassthroughAec()` in the scenario, to stop the real AEC over-cancelling the
   clean synthetic echo. **RESULT: still `echo_suppressed=False` across all 30
   sweep cases (delays 0/20/40/60/80 ms × attenuation 6/18/30 dB × noise
   −90/−70).** So even with cancellation neutralised, the correlation the gate
   computes through the RNNoise/AGC chain is still < 0.88.

### 5.4 THE NEXT HYPOTHESIS I WAS ABOUT TO TEST (resume here)
With `_PassthroughAec`, the near-end still passes through **RNNoise `denoise_chunk`
then AGC + soft limiter** before landing in `preroll_pcm16`. The gate correlates
that post-RNNoise/AGC near-end against the RAW `far_shadow` (which is the
untouched far reference). Hypothesis: **RNNoise and/or AGC are altering the
near-end envelope enough to drop the correlation below 0.88** in the synthetic
case (whereas on the real call the whole chain still yielded 0.97 because real
residual is dominated by the echo). I was mid-way through a standalone script to
measure this directly. The script (interrupted) was measuring:
- `bargein.echo_correlation(near8, far8, near_rate=8000, far_rate=8000, lag_search_ms=80)`
  on the raw synthetic near vs far (no DSP) — expected ceiling near 1.0.
- then the same with near upsampled 8k→16k (as preroll is) — to see if the
  rate/resample step alone drops it.
- The still-untested step: run near through RNNoise+AGC (as the real inbound
  loop does) and correlate THAT against far_shadow, to see if the DSP chain is
  what kills the correlation.

**Decision pending after that measurement:**
- If raw/16k correlation is high but post-RNNoise/AGC is low → the harness's
  synthetic echo is being mangled by RNNoise (likely because it's a too-clean
  tone-like signal RNNoise treats as noise, unlike real speech). Fix options:
  (a) drive the near-end with a real speech fixture shaped to correlate (use
  `recorded.wav`-derived near that survives RNNoise), or (b) accept that the
  end-to-end synthetic path can't produce faithful residual and RESCOPE Case A/B
  to validate the gate WIRING only (that `evaluate_echo_gate` is called and its
  verdict acted upon), leaving the correlation-THRESHOLD validation to the
  real-recording `test_bargein_unit` (which already passes at 0.97).
- Option (b) is the likely resolution and is honest: the real-recording unit
  test is the authoritative correlation proof; the synthetic end-to-end harness
  should assert control flow, not DSP-faithful correlation values.

### 5.5 Case D specifics
`TestCaseDAnomalyInvisibility` was reworked to use `near_kind="genuine"` (far
reference = a DISTINCT speech region, low correlation) so the gate correctly
ALLOWS a real barge-in and the `[TRIGGER]`/`[ANOMALY]` diagnostic logging fires.
The provenance assertion was updated to expect MEASURED evidence
(`far_end_evidence=corr=…`, `reason=uncorrelated-double-talk`, and NOT
`not-measured`). `test_anomalous_truncation_is_logged` currently ERRORS — needs
checking once the scenario alignment issue (5.4) is resolved, because the
"genuine" path shares the same `_near_and_ref` / `run` machinery whose alignment
is under investigation. The genuine case depends on the far reference being a
distinct region of `_FULL_8K` (`echo.load_pcm8k("recorded.wav")`) — verify that
region is far-active (RMS > −60 dBFS) or the gate short-circuits to
`far-end-inactive`.

---

### 5.6 Gate window geometry bug, found and fixed 2026-09-23

`bargein.echo_correlation` aligns near and far envelopes at their STARTS and
searches +/- 8 bins from there. With near = 180 ms and far = 340 ms both ending
"now", lag 0 meant a 160 ms echo delay and the search covered 80-240 ms, so the
measured 50-60 ms (and 0 ms) sat outside it or at its edge. The
`lag_ms=80` in 5.3 item 2 is that edge. The real-recording unit tests never saw
it because they handed `echo_correlation` two equal 180 ms windows, not what
production slices.

Measured against the REAL `evaluate_echo_gate` (lifted from app.py source by
`tests/test_bargein_unit.py::load_production_gate`, so no quart/plivo needed):
- 17:05 reproduction call, 5 logged false triggers: old geometry suppressed
  **0/5** (corr 0.67-0.85, lag pinned at -5..-8); new geometry **5/5**
  (corr 0.973-0.990, echo delay 40-60 ms).
- Synthetic delayed echo, 0-60 ms: old geometry missed about half of the onset
  positions; new geometry catches all of them, 0-120 ms.

Fix (app.py only; bargein.py untouched): far window = near window + 80 ms of
LEAD, ending at the same frame (`ECHO_FAR_MS`). Echo only lags playout, so lag 0
is now an 80 ms delay and the search spans 0-160 ms, with full overlap from 0 to
80 ms. Strictly equal windows were tried first: they catch 0-60 ms but lose
overlap and miss from 80 ms up (0.45 catch rate at 120 ms), so they were not used.
`lag_ms` in the `[ECHO-GATE]` / evidence logs keeps its old meaning (bins*10);
a `delay_ms` field (= 80 + lag_ms) now sits beside it.

Tests: `TestProductionGateCatchesRealisticEchoDelays` (written first; it failed at
0/30/50/60 ms before the fix), and a spy test pinning the window lengths and
end-alignment the real function passes to `bargein`. `TestRealRecordingGroundsTheThreshold`
now runs through the real gate and reads the WAVs with stdlib `wave`, so it no
longer skips when soundfile/scipy are absent. It would fail on the old geometry.

The A/B harness now warm-starts (`EchoTruncationScenario._warm_frames`,
`WARM_START_FRAMES = 25`): 500 ms of noise-floor near-end with an empty far
queue before the turn, so RNNoise/AGC/ratecv are settled and preroll/far_shadow
are full at the onset, as between turns on a real call. **Not yet run:** the
harness imports app (quart, plivo, pyrnnoise), which were not installed on the
machine this was done on. Re-run section 6 commands; rescope A/B to wiring-only
(5.4 option (b)) only if they still fail. Case D is also worth watching: the new
windows correlate more readily, and its "genuine" far region could now clear
0.88 by chance (see section 10).

**Run 2026-09-23** (Python 3.12 env `venv312`, made with uv from the pinned
versions of the modules app/tests import, `-c requirements.txt`; the 3.14
`venv` cannot import app because `audioop` was removed in 3.13):
`venv312/Scripts/python.exe -m unittest discover -s tests -t .` -> **200 tests,
3 failures, 2 skipped**. Failing: Case A, Case B, the suppression sweep. Case D
and all preservation tests pass.

Why A/B still fail, and why they were NOT rescoped to wiring-only: the gate now
suppresses the FIRST onset (corr 0.979-0.999; the sweep logs
`echo_suppressed=True` in 30/30 cases). The revert resets `rnnoise_speech_count`,
so VAD re-fires about 4 frames (80 ms) later and the gate runs again on the next
window. That window scores 0.80-0.85, gets `uncorrelated-double-talk` and
truncates. Suppression is 10/30 in the sweep.
The real 17:05 recording shows the same pattern. Sliding the production gate
every 80 ms around each real trigger, correlation is 0.90-1.00 only at the onset
(first ~160 ms of the turn), then 0.1-0.89 with only occasional windows >= 0.88.
So either the 0.97 "premise" mostly measures the turn-onset energy step, or
log-anchor alignment drifts mid-turn (inbound WAV has compressed gaps). Both
say one gate verdict per 80 ms re-onset is not reliable across a whole turn.
This is likely a production gap, not only harness fidelity. Next decision is a
design one (e.g. latch an echo verdict for the rest of the far-active turn,
longer window, or gate only the first onset per turn), each trading against
genuine barge-in; section 10's double-talk finding bears on it.

**Echo latch added 2026-09-23 (operator chose option "latch")**: `apply_echo_latch` /
`echo_return_db` / `ECHO_LATCH_BREAK_DB` (10 dB) / `call_state["echo_latch_erl_db"]`.
An echo verdict is held for the playback period. It is cleared when
assistant_speaking goes False->True and by `reset_far_reference`, and broken if
the echo return (pre-AGC near dB minus far dB) rises more than 10 dB. Unit tests
are in `TestEchoLatch`. Suite: **205 tests, 3 failures**. A, B and the sweep now
PASS; all 3 Case D tests FAIL.
Case D trace: the genuine caller's first onset scores corr 0.965 (turn-onset
energy step), so it is judged echo and latched, and the next onset (corr 0.61)
is held `echo-latched`. With the caller at 18, 6 or 0 dB below the far-end, all
three score about 0.97 at the first onset and get latched. So a caller who starts
talking with the assistant's turn is ignored for the whole playback, however
loud. Pre-latch, Case D passed only because a later onset happened to
decorrelate. RESOLVED by the echo return ceiling, see section 5.7.

### 5.7 Echo return ceiling, final gate logic, latch field logging (2026-09-23)

**Final logic, per VAD onset while `assistant_speaking`:**

1. `evaluate_echo_gate`: `bargein.should_barge_in` on 180 ms near vs 260 ms
   far (section 5.6). If the verdict is echo but the echo return
   `erl = near_dbfs - AGC gain dB - far_dbfs` is above `ECHO_MAX_RETURN_DB`
   (-6 dB), the verdict is overturned to barge-in, reason
   `near-too-loud-for-echo`.
2. `apply_echo_latch`: an echo verdict suppresses and latches the lowest erl
   seen. A non-echo verdict is still suppressed (`echo-latched`) while a latch
   is held AND far is active AND erl <= -6 AND erl <= latched + 10 dB.
   Otherwise the latch drops and the verdict stands.
3. The latch is dropped by `reset_far_reference` (clearAudio or reconnect) and
   when a new playback period starts.

**Why -6 dB.** It is set from the only real echo data we have: the 5 false
triggers on the 17:05 call. Their echo return, with AGC gain interpolated
from the log, was -22.4, -28.3, -30.3, -21.0 and -12.8 dB. Even assuming zero
AGC gain, the worst is -10.3. -6 leaves at least 4 dB over the worst real
echo. A near end within 6 dB of far is louder than any residual this AEC has
produced, so it is treated as the caller, whatever the correlation. It exists
because at turn onset almost any speech correlates ~0.97 with far (the
energy-step artefact), so correlation alone cannot release a caller who
starts talking with the assistant.

**Does the latch still earn its keep with the ceiling? Yes.** Measured
through the real inbound chain, latch vs re-evaluating the gate fresh on every
onset (10 cases per level: 5 delays x 2 noise floors):

| echo attenuation | truncations, latch | truncations, fresh | held only by latch | erl |
|---|---|---|---|---|
| 6 dB  | 2/10 | 2/10  | 0/10  | -7.4..-6.2 |
| 12 dB | 0/10 | 4/10  | 4/10  | -14.6..-12.3 |
| 18 dB | 0/10 | 10/10 | 10/10 | -20.6..-18.9 |
| 30 dB | 0/10 | 10/10 | 10/10 | -32.7..-31.0 |

The ceiling fixes a loud caller at onset (a level problem). The latch fixes
echo re-onsets whose correlation drops to 0.24-0.85 (a correlation problem).
The two do not overlap. Removing the latch reopens the original mid-turn
truncation in 20/20 realistic-level cases.

**Cost of the latch, and the known limitation.** A genuine caller at echo
level is held: at caller attenuation 18 dB (erl -13.8) and 12 dB (erl -7.2)
it never barges in with the latch; fresh evaluation would let it through.
This is in-domain. On the 17:05 call, genuine caller speech measured median
-35 dBFS pre-AGC (loudest 10% about -24) against far median -16, i.e. about
19 dB below far, the same band as the echo. With echo latched at about -28,
the break point is about -18, so a normal-volume caller breaks through only
on louder syllables. Small sample (6 caller windows, one call). This needs
real double-talk calls (section 8, #8).

**Test changes (operator-approved, each makes a test easier, stated plainly):**

1. Sweep `ATTENUATIONS_DB` `(6.0, 18.0, 30.0)` -> `(12.0, 18.0, 30.0)`. At
   6 dB synthetic echo sits at erl -5.7..-7.4, on the ceiling, and fails at
   delay 0. That is louder than any measured residual (-12.8..-30.3). At 12 dB,
   10/10 pass with at least 6.3 dB margin. Lost: coverage of echo within 6 dB
   of far. By design that now passes as barge-in, and it is untested.
2. Case D genuine caller `attenuation_db` 18.0 -> 6.0 (erl -13.8 -> -0.6, 5.4 dB
   above the ceiling). A caller at echo level who starts at turn onset cannot be
   told from echo by correlation or level. Consequence: Case D's
   `reason=uncorrelated-double-talk` pin became `reason=near-too-loud-for-echo`.
   The loud caller's onset still correlates 0.969; the ceiling is what releases it.
3. (2b) The 18 dB case is kept as
   `TestCaseDQuietCallerKnownLimitation.test_quiet_genuine_caller_barges_in`,
   `@unittest.expectedFailure`. When double-talk discrimination lands, it will
   show as an unexpected success.
4. Mechanical: re-pinned `GEMINI_MODEL` lines to `[51, 1363, 1826, 1836, 2227]`.

**Production field logging for the limitation (for section 8, #8).** Every
onset suppressed *only* by the latch (the fresh verdict was barge-in) logs one
queryable line. Each latch episode that held or was broken logs how it ended.
Each call logs a summary. All go to `Gemini_Assistant.log`, carry
`call=<uuid>` and are key=value for grep and awk:

```
🔒 [LATCH-HOLD] call=… hold=N since_play_ms=… fresh_reason=… corr=… erl_db=… latched_db=… rise_db=… break_at_db=… ceiling_db=-6.0 near_dbfs=… far_dbfs=…
🔓 [LATCH-END] call=… ended_by=break-rise|break-ceiling|break-far-inactive|reset|new-playback|call-end holds=N held_ms=… max_rise_db=… max_erl_db=… erl_db=… since_play_ms=…
🔒 [LATCH] holds=N episodes_broken=N episodes_unbroken=N max_rise_db=…   (in call stats)
```

What to read off real calls:
- An **unbroken episode with holds > 0** (`ended_by=reset|new-playback|call-end`)
  is the failure mode itself. Every hold in it may be a caller who was never
  heard.
- A **broken episode** is a caller who got through late by `held_ms`.
- `rise_db` separates the two populations. Echo re-onsets should cluster near
  0; a caller shows a positive rise. Cross-check against the inbound WAV at
  `since_play_ms`.

Echo-only episodes (latched, never held, never broken) are not logged, to keep
the log quiet. The tags are in `DIAGNOSTIC_LOG_TAGS`, so golden-record tests are
unaffected. Covered by `TestLatchEpisodeAccounting` and
`TestCaseDQuietCallerKnownLimitation.test_the_hold_is_logged_for_field_measurement`.
Also: the `[ECHO-GATE]` line prints `latched=none` instead of crashing when the
latch is unset.

Suite: **208 tests, OK (2 skipped, 1 expected failure)**. That includes
`TestRealRecordingGroundsTheThreshold`, which runs the real 17:05 triggers
through the production gate (still suppressed with the ceiling), and Case A/B,
the original mid-turn truncation.

### 5.8 Task 6: echo tail after playback ends, found and fixed (2026-09-23)

**Finding.** Section 8 #6 said concern (b) was "largely subsumed by the Task 5
revert-and-`continue`". Re-checked against the current gate, that was wrong.
The gate ran only while `assistant_speaking`. That flag drops on the output
loop's send-side wall clock (playback start + bytes / 8000). At that moment
the echo of the last far frames is still arriving: the acoustic/network delay
the lag search covers (0-160 ms), plus DSP ringing. Onsets during playback
were suppressed, but the first onset after the flag dropped skipped the gate.
The VAD bar also returned to the normal threshold. That onset went straight to
`activityStart` plus a ~400 ms echo-filled preroll, i.e. a phantom caller
turn made of the assistant's own voice. Harness: **41/42 phantom turns** across
12/18/30 dB x 0-160 ms x 2 noise floors. This is the likely mechanism behind
Task 1's bogus caller turns (F0 ~200 Hz, the assistant's voice).

**Fix (app.py).**
- `ECHO_TAIL_FRAMES = ceil(2 * ECHO_LAG_SEARCH_MS / 20) = 8` (160 ms).
- New call_state key `far_silent_frames`. The playout-paced far pop sets it
  to 0 on a real frame and adds 1 (capped at `ECHO_TAIL_FRAMES`) on a silence
  pop. `reset_far_reference` sets it to `ECHO_TAIL_FRAMES`, because after
  clearAudio there is no tail to protect. Mirrored in `tests/harness/appctl.py`.
- `far_window_has_playback(call_state)` returns
  `far_silent_frames < ECHO_TAIL_FRAMES`.
- The gate condition is now `assistant_speaking or far_window_has_playback(...)`.
  VAD thresholds are unchanged. In the tail the onset is judged by the same
  gate, latch and ceiling as during playback.

**Measured (corrected harness: far frames queued as 320-byte PCM16, the unit
production queues).**

| tail gate | phantom turns | caller answering at the flip, echo 18 dB |
|---|---|---|
| none (pre-fix) | 41/42 | (phantom turn opens first, echo at its front) |
| 80 ms  | 20/42 | 100 ms |
| 160 ms | 6/42 (all six are the 160 ms delay, see below) | 160 ms |
| 260 ms | 6/42 | 160-220 ms |

On the 12-30 dB x 0-120 ms grid: 100 ms tail leaves 7/36, 120 and 140 ms leave
2/36, 160 ms leaves **0/36**. Hence 8 frames.

**Cost.** A caller who speaks the moment the assistant finishes is heard
160 ms after the flip instead of 80-100 ms with no echo: **+60 ms**, the same at
every caller level tested. The pre-fix "40 ms" was not the caller. It was the
phantom echo turn opening first.

**Known, pre-existing, not addressed.** Echo at 160 ms delay (the edge of the
lag search) still truncates mid-playback at frame 12. That is a during-playback
gate miss, not a tail issue. It is outside the realistic 0-80 ms domain and
predates this work.

**Tests added** (tests/test_bug_condition_exploration.py):
- `EchoTailScenario`: a whole assistant turn, the flip, then the echo tail.
  `caller_att_db` optionally adds a genuine caller starting at the flip.
- `TestEchoTailIsNotAdmittedUpstream`: 18/30 dB x 0/40/80/120 ms, no
  activityStart and no bytes to the model. The 120 ms case is what pins
  `ECHO_TAIL_FRAMES` at 160 ms (140 ms leaks it).
- `TestCallerRightAfterPlaybackIsHeard`: loud (6 dB) and echo-level (18 dB)
  callers. Each must be heard no more than `ECHO_TAIL_FRAMES` later than the
  same caller with no echo.
- Mechanical: re-pinned `GEMINI_MODEL` lines to `[51, 1381, 1845, 1855, 2257]`.

**Remaining Task 6 item, measured: echo in the preroll on a GENUINE flush.**
The idea was to trim echo from the 200 ms preroll before it is flushed to
Gemini on a real barge-in (app.py ~2317 barge-in flush, ~2528 tool-call
prepend). Measured through the real inbound chain, with the synthetic echo and
caller components known separately, over 6 cases:
- Callers at 0-6 dB, starting mid-playback or right at the flip.
- Echo at 18 and 30 dB.
- The quiet 18 dB caller.

Findings:
- Echo-only frames at the front of the flushed preroll: **0-2 frames (0-40 ms)**.
  Frames where echo is louder than the caller: at most 2 more (40 ms). So the
  worst case is 80 ms echo-led, typically 40-60 ms. The rest of the 200 ms is the caller: VAD onset takes 140-220 ms
  after the caller starts, so the preroll is mostly the caller's own onset.
- The echo-dominated frames ARE the caller's onset ramp, the quiet initial
  consonant. There the caller is -41 to -80 dBFS against echo at -31 to -55. Levels are
  input-domain (pre-AGC), per 20 ms frame. Caveat: the frame-to-sent mapping
  ignores the resampler/RNNoise delay of about one frame. In-domain (caller about 19 dB below far, echo 12-30 dB below far), the two are
  at the same level.

So a level-based trim either removes nothing (threshold under the echo) or
clips the caller's first consonant (threshold over it). The spec forbids a
presence-based trim, and echo is present in every frame of playback anyway.
The same residual keeps running under the caller until clearAudio takes
effect, so trimming 40-80 ms at the front would not make the turn echo-free.
Also, Gemini generated that audio, so 40-80 ms of its own voice at echo level
ahead of a real turn is a far smaller risk than the pre-fix phantom turns,
which were whole turns of it.

**DECISION (operator, 2026-09-23): preroll trim NOT implemented. Task 6 is
CLOSED with the tail gate.** Rationale, as accepted:
- The echo-led front of a genuine flush is 40-80 ms. It is level-inseparable
  from the caller's onset ramp, so a level-based trim either does nothing or
  clips the caller's first consonant. The spec rules out a presence-based trim.
- Echo continues under the caller until clearAudio lands, so a front trim
  would not make the turn echo-free anyway.
- The residual is 40-80 ms of Gemini's own voice at echo level. The pre-fix
  failure was whole phantom turns of it, and the tail gate removes those.
- The tool-call prepend site is lower exposure still: tool calls normally
  arrive while the assistant is silent.

Caveat on the measurement: the per-frame mapping from input frames to flushed
16 kHz frames ignores the resampler/RNNoise pipeline delay (about one frame,
~20 ms). The echo-led span could be misplaced by about 20 ms. That does not
change the conclusion.

**Reopen only if** live calls show Gemini reacting to its own voice at the
start of a barge-in turn. Examples: a `🗣️ [USER]` transcript that begins with
the assistant's last words, or a response that addresses them. Check the
inbound WAV at the barge-in onset against the far-end WAV. If it recurs, the
sites are app.py ~2317 (barge-in flush) and ~2528 (tool-call prepend).

## 6. Exact commands to reproduce (DO NOT run automatically — operator preference)

Windows dev machine, Git Bash, from the repo root. Python 3.12 venv (3.14 has
no `audioop`). Prefix `PYTHONIOENCODING=utf-8` when printing app log lines
(the console is cp1252).

```bash
cd ~/local/AU-Admission-Assist

# whole suite
venv312/Scripts/python.exe -m unittest discover -s tests -t .

# the acceptance file only
venv312/Scripts/python.exe -m unittest tests.test_bug_condition_exploration -v

# individual cases
venv312/Scripts/python.exe -m unittest tests.test_bug_condition_exploration.TestCaseATruncationReproduction
venv312/Scripts/python.exe -m unittest tests.test_bug_condition_exploration.TestCaseBUpstreamAdmission
venv312/Scripts/python.exe -m unittest tests.test_bug_condition_exploration.TestGateSuppressesEchoAcrossTheRealisticDomain
venv312/Scripts/python.exe -m unittest tests.test_bug_condition_exploration.TestCaseDAnomalyInvisibility
venv312/Scripts/python.exe -m unittest tests.test_bug_condition_exploration.TestEchoTailIsNotAdmittedUpstream

# the AUTHORITATIVE gate proof (PASSES — real-recording correlation 0.97)
venv312/Scripts/python.exe -m unittest tests.test_bargein_unit -v

# aec.py integrity (includes the sha256 pin)
venv312/Scripts/python.exe -m unittest tests.test_preservation_4_7_aec_integrity -v

# the line-pin re-pin, needed whenever app.py gains or loses lines
grep -n GEMINI_MODEL app.py   # then update the pinned list in
                              # tests/test_preservation_4_8_resumption_recording_stats.py::test_model_identifier_is_recorded
```

Note: the suite uses stdlib `unittest`, NOT pytest — pytest and hypothesis are
deliberately not installed (`requirements.txt` is frozen). Property-based tests
are hand-rolled seeded falsifiers.

## 7. Last observed failure output (Case A, representative)

```
CASE A -- echo was NOT suppressed as expected
  input        : {'near_kind': 'echo', 'delay_ms': 60.0, 'attenuation_db': 18.0,
                  'noise_db': -80.0, 'playback_frames': 7, 'heard_ms': 140,
                  'transcript': 'আমি দুঃখিত, আমি…'}
  echo_suppressed: False
  clearAudio   : 1
  activityStart: 1
  model input  : 7680 bytes
  violations   : no [ECHO-GATE] suppression logged: the gate did not classify the onset as echo
                 clearAudio sent 1x (SHALL NOT truncate on echo)
                 activityStart sent 1x (SHALL NOT open a caller activity window on echo)
                 7680 bytes of echo admitted to the model input path (SHALL NOT admit the echo)
```

Sweep (`test_gate_suppresses_every_realistic_echo`) fails the same way for all
30 grid cases: every case `echo_suppressed=False, clearAudio x1, activityStart
x1, N bytes admitted`.

Earlier `[TRIGGER]` evidence seen before the `_PassthroughAec` change (proves the
gate executes and measures a real-but-too-low value):
```
🧭 [TRIGGER] verdict=fired | classification=anomalous | avg_prob=1.000 |
  active_threshold=0.82 | onset_frames=4 | rnnoise_speech_count=4 |
  inbound_rms_db=-32.6 | delivered_ms=139 | heard_ms=139 | queued_ms=140 |
  buffer_lead_ms=1 | far_end_evidence=corr=0.685,lag_ms=80,far_active=True,
  reason=uncorrelated-double-talk
```

---

## 8. Redesign Tasks 6–8 (6 and 7 done, 8 open)

- **#6** concern (b) upstream echo rejection. **Tail gate DONE (section 5.8).**
  The "subsumed by revert-and-continue" judgment was wrong: the post-playback
  echo tail skipped the gate and opened phantom caller turns (41/42). Fixed with
  `ECHO_TAIL_FRAMES`. **CLOSED 2026-09-23 (operator decision).** The preroll
  trim on a genuine flush was measured and deliberately NOT implemented
  (section 5.8). The echo-led front is 40-80 ms and level-inseparable from the
  caller's onset. Measurement caveat: frame mapping is about ±20 ms (pipeline
  delay ignored). Reopen only if live calls show Gemini reacting to its own
  voice at the start of a barge-in turn. Watch for this in #8.
- **#7** full verification: whole suite green (modulo the intentional-failure
  philosophy), aec integrity, byte-stream neutrality where expected.
  **DONE 2026-09-23.** Checks (results in section 2):
  - Suite: 212 OK (2 skipped, 1 expected failure).
  - aec integrity: 11/11, in this tree and in a fresh clone.
  - Neutrality: in-suite proofs pass, and the kill switch plus gate byte-neutrality were verified ad hoc.
  - §2/§3 reconciled.

  **The 8 items that did not reconcile, and the operator's decisions
  (2026-09-23).** Items 7 and 8 are in `be6c19f`; items 1-6 are in the
  docs-and-comments commit after it.
  1. *"All decision logic in bargein.py" constraint.* Amended in
     `docs/spec/tasks.md`: pure decision logic in bargein.py; call_state-coupled
     gate helpers (`apply_echo_latch`, the `ECHO_MAX_RETURN_DB` override,
     `far_window_has_playback`) may live in app.py. No code moved.
  2. *5.8 "NOT greenlit".* Marked implemented. The greenlight was not recorded;
     the operator ratified it on 2026-09-23.
  3. *Scope text and checkboxes.* Done checkboxes ticked, each partial one with
     a status note. "Authorised scope" and "attribution-neutral" marked
     SUPERSEDED.
  4. *Two numberings.* This file now says "Redesign Task N"; mapping to the spec
     is in section 3.
  5. *Stale "parked" text.* Fixed in app.py, bargein.py, tests/__init__.py, the
     harness README, bengali.py, echo.py and the test docstrings listed. The
     logged value `not-measured[concern-a-parked]` is kept verbatim.
     test_bug_condition_exploration.py keeps its literal 350 with a corrected
     comment, and the new `TestDesignLiterals` asserts it equals
     `app.ANOMALOUS_TRUNCATION_MS`. Hashes in section 1.
  6. *Historical sections.* Sections 5 and 7 left as history; section 6 now has
     the Windows `venv312` commands.
  7. *Line endings.* `.gitattributes` added (`* text=auto eol=lf`). Verified in a
     fresh clone in a temp dir: `aec.py` sha256 unchanged, so no re-pin, and
     `tests.test_preservation_4_7_aec_integrity` 11/11 (run with dummy
     `PLIVO_AUTH_ID`/`PLIVO_AUTH_TOKEN`; see new item C below).
  8. *gitignore and spec files.* `.gitignore` now has `CALL_RECORDINGS/`,
     `*.m4a` and `*.kiro-halt`. Git history holds no call recordings: the only
     audio ever committed is the six `playback_audio_files/*.wav` system
     prompts. `tasks.md` and `redesign-audio-pipeline.md` are copied to
     `docs/spec/` (tracked); `.kiro/` stays ignored, and the citations in this
     file, CLAUDE.md, app.py and bargein.py point at `docs/spec/`.

  **New items found while doing the above. Reported, not acted on:**
  - A. *Decision logic in app.py not named by the amendment.* The grapheme
    helpers (`grapheme_clusters`, `leading_clusters`,
    `ends_mid_grapheme_cluster`, `boundary_splits_grapheme_cluster`,
    `shared_leading_cluster_count`), the commit gate (`arm_commit_gate`,
    `disarm_commit_gate`, `commit_gate_verdict`, `commit_gate_discard`) and
    `delta_trace_verdict`. Recorded as open under the amended constraint in
    tasks.md. Operator: extend the amendment, or schedule a move.
  - B. *5.13 does not hold as written.* It says not to re-baseline golden
    records, and section 4 lists intentional re-baselines. Left unticked with a
    note. Operator ruling needed.
  - C. *A clone without `.env` cannot import app.* app.py builds the Plivo
    `RestClient` at module import, which raises without credentials, so every
    test that imports app errors on a fresh checkout. `.env` is frozen and this
    was not changed.
  - D. *Stale test name.* `test_preservation_4_9_falsifier.py::test_activity_floor_constant_is_deferred`
    now asserts the constant exists. Not renamed (item 5 covered text, not
    test names).
  - E. *design.md and bugfix.md are still only under the gitignored `.kiro/`.*
    tasks.md cites both. Item 8 named only the two spec files.
  - F. *tasks.md body still has `venv/bin/python` commands* (tasks 2 and 9).
    A standing-constraint note gives the Windows command instead of editing
    each one.
  - G. *Spec tasks 6, 7 and 8 are partial.* See their status notes in tasks.md.
- **#8** validation-call guidance + tuning notes for the live tune loop (Phase 5).
  **Build this around the latch limitation first (section 5.7), not only
  threshold calibration.** Across real calls, count `[LATCH-END]` episodes with
  `holds>0` and `ended_by` not `break-*` (caller possibly never heard). Measure
  `held_ms` on broken episodes (late barge-in), and histogram `rise_db` from
  `[LATCH-HOLD]` to see whether echo and callers separate. Then decide: rare
  enough to live with, or needs real double-talk discrimination (section 10).
  Tune `ECHO_LATCH_BREAK_DB` (10) and `ECHO_MAX_RETURN_DB` (-6) from the same
  data. Then the original items:
  final `ECHO_CORR_THRESHOLD` (start 0.88), confirming D1 alignment improved real
  cancellation, perceived barge-in responsiveness, whether the while-speaking VAD
  threshold can relax post-fix. **This step needs real calls; it cannot be done
  offline.** Both sides of audio are now recorded per call
  (`{name}_{ts}.wav` inbound 16k + `{name}_{ts}_farend.wav` 8k wall-clock-continuous).

## 9. Standing constraints (carry forward)

- `aec.py` may be modified if needed (operator unfroze it) but has NOT needed to be.
- `requirements.txt` and `.env` MUST NOT be modified. stdlib `unittest` only.
- `prompt.txt` still contains SENCO content — the agent will mis-introduce itself
  and pitch Durga Puja offers on any live call. Operator owns rewriting it; not a
  code bug but visible on every call.
- Demo pressure is OFF; goal is a properly-working agent. "Perfect" cancellation
  is not a real target on an 8k lossy channel — robust-and-measured is.

## 10. Known issues, not in scope

- **Trigger endpoint auth:** `API_AUTH_TOKEN` falls back to `"default-dev-token"` when unset (`app.py:70`), and `index.html` hard-codes that same value in the `X-API-Key` header of `/trigger-call`. If `.env` does not set it, anyone who can reach the server can place outbound calls; even if set, a browser-sent key is not a secret. Logged 2026-09-23, not fixed.
- **Gate double-talk discrimination (measured, not fixed):** on synthetic speech-like envelopes, independent caller speech over playback scored >= 0.88 in ~45% of 180 ms windows, at EVERY far window length including the pre-fix one. So a genuine barge-in during playback can be suppressed as echo. This comes from the 180 ms window, the +/- 80 ms search freedom and the 0.88 threshold, not from the 5.6 window fix. It needs real double-talk recordings to size; a longer near window or a stricter threshold are the obvious levers. Logged 2026-09-23.
