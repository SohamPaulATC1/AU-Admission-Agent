# Bengali Grapheme Stutter Fix — Bugfix Design

## Overview

The caller hears the leading partial grapheme **দুঃ** repeated several times instead of the word **দুঃখিত**. The word is never re-synthesised incorrectly; the same *first ~130 ms of a new turn* is played over and over because each new turn is cut off almost the instant it starts.

The fix treats this as a **false barge-in loop driven by residual echo of the assistant's own outbound audio**, not as a speech-synthesis or grapheme-rendering defect. The loop has four links, and the design breaks all four:

| Link | Mechanism | Fix concern |
|---|---|---|
| 1 | Echo of the assistant's voice trips the while-speaking VAD gate after ~80 ms | (a) Corroborated barge-in gate |
| 2 | The ~200 ms preroll (containing that echo) is flushed upstream as caller speech | (b) Echo not admitted upstream |
| 3 | The model, hearing an apology-like caller turn, regenerates the same "আমি দুঃখিত" prefix | (c) Leading-fragment commit gate |
| 4 | The new turn is cut at the same point, delivering দুঃ again | (a) + (c) |

Plus a fifth, non-behavioural concern: the system currently cannot record *why* a barge-in fired, *which* model served the call, or *what* the model streamed delta-by-delta. Two of the requirement clauses (1.7/2.7, 1.8/2.8) exist purely to close those blind spots, and one of them — upstream-vs-downstream duplication — is the single question this design cannot answer from existing evidence. Concern (d) is the instrumentation that settles it on the next deployment.

`দুঃ` is not special to the model. It is the onset of the assistant's standard refusal opener "আমি দুঃখিত", and ~130 ms of delivered audio happens to land inside that first grapheme cluster. Any refusal-heavy call in any language would stutter the same way on whatever its opener's first cluster happens to be; Bengali just makes it maximally audible, because a consonant + matra + visarga cluster is one indivisible perceptual unit.

**Scope note.** This design changes `app.py` only. `aec.py` is frozen (requirement 3.10) and is not modified, not re-placed in the chain, and not re-tuned — it is only *called* through its existing public methods from `app.py`. `requirements.txt` is also frozen; everything specified here uses packages already installed (`regex`, `numpy`, `scipy`) or the standard library.

## Glossary

- **Bug_Condition (C)**: A playback-stopping barge-in trigger fires while the assistant is speaking, within the first few hundred milliseconds of a turn, and the near-end audio that triggered it is residual echo of the assistant's own outbound audio rather than caller speech.
- **Property (P)**: For inputs satisfying C, playback is not truncated; the leading grapheme cluster of the utterance is delivered once, whole, and continuously; the echo is not admitted upstream as caller speech.
- **Preservation**: Genuine barge-in responsiveness, μ-law framing and pacing, tool-call deferral, silence watchdog, session resumption, debug recording, and turn-level logging all behave exactly as today.
- **Far-end**: The outbound audio the assistant is playing to the caller. Fed to the echo canceller as its reference signal via `aec.add_far_end()` in `send_plivo_audio` (app.py ~1942).
- **Near-end**: Inbound audio from the caller's handset, post-AEC, post-RNNoise, post-AGC, as seen by the VAD in `stream_plivo_to_gemini`.
- **While-speaking gate**: The raised-threshold VAD path taken when `assistant_speaking and not is_speaking` — `VAD_THRESHOLD_WHILE_SPEAKING = 0.82` with `VAD_SPEECH_ONSET_FRAMES_WHILE_SPEAKING = 4` (app.py 80–81). Four 20 ms RNNoise frames = **80 ms** of sustained "speech" to barge in.
- **Preroll**: `preroll_pcm16`, a rolling ~200 ms buffer of clean 16 kHz inbound audio (`PREROLL_MAX_BYTES_PCM16 = 6400`) maintained while no caller activity window is open, flushed upstream on `speech_started` so the onset of a genuine utterance is not clipped (app.py 1348–1353, 1408–1412).
- **Grapheme cluster**: A UAX #29 extended grapheme cluster — the smallest unit a reader perceives as one character. `দুঃ` is one cluster of three code points: `দ` U+09A6 + `ু` U+09C1 (vowel sign U, category Mn) + `ঃ` U+0983 (visarga, category Mc).
- **Anomalous truncation**: Playback stopped after delivering less than `ANOMALOUS_TRUNCATION_MS` of audio for a turn — short enough that the caller heard a sub-lexical fragment rather than a word.
- **Armed state**: The window immediately following an anomalous truncation, during which the leading-fragment commit gate is active.
- **Upstream duplication**: The model stream itself contains the same audio/text twice within one turn.
- **Downstream re-delivery**: The same audio is queued or sent to Plivo more than once by local buffering.

## Bug Details

### Bug Condition

The bug manifests when the assistant starts a new turn and, within roughly the first 100–350 ms of playback, the while-speaking VAD gate fires on near-end audio that is residual echo of the assistant's own voice rather than caller speech. The barge-in path then (i) drains `plivo_output_queue`, (ii) sends `clearAudio` to Plivo, and (iii) opens a caller activity window and flushes the echo-bearing preroll upstream — after only the leading partial grapheme has reached the caller.

**Formal Specification:**
```
FUNCTION isBugCondition(input)
  INPUT: input of type BargeInDecisionInput
           { near_end_frames    : sequence of 20 ms post-DSP frames
           , far_end_reference  : outbound audio being played, aligned to playback time
           , assistant_speaking : boolean
           , delivered_ms       : ms of the current turn already sent to Plivo
           , leading_text       : accumulated output transcription for the current turn }
  OUTPUT: boolean

  RETURN input.assistant_speaking
         AND vadGateWouldFire(input.near_end_frames,
                              VAD_THRESHOLD_WHILE_SPEAKING,
                              VAD_SPEECH_ONSET_FRAMES_WHILE_SPEAKING)
         AND input.delivered_ms < ANOMALOUS_TRUNCATION_MS
         AND nearEndIsExplainedByFarEnd(input.near_end_frames,
                                         input.far_end_reference)
END FUNCTION
```

where `nearEndIsExplainedByFarEnd` is true when the far-end is active and the near-end energy envelope correlates with the far-end envelope above `ECHO_CORR_THRESHOLD` at the tracked acoustic round-trip lag — i.e. the "speech" the VAD heard is the assistant's own audio coming back.

The complementary condition `NOT isBugCondition(input)` covers, and must be preserved unchanged: far-end inactive (assistant not playing), near-end uncorrelated with far-end (genuine caller speech, including double-talk), and triggers arriving after `ANOMALOUS_TRUNCATION_MS` of delivered playback.

### Examples

All excerpts below are from `Gemini_Assistant.log`, call UUID `dbcd514d-3fe8-4af9-bb1a-f430634c89fd`, caller [number redacted].

**Example 1 — the canonical stutter, 131 ms of delivered audio.** Log lines 2040–2047:

```
14:50:03,972 🗣️ [USER]: I'm sorry. I'm sorry.
14:50:03,995 🎙️ [TIMING] AI Speech Started playing at: 14:50:03.995
14:50:04,126 🎤 User speech detected
14:50:04,126 🎙️ [TIMING] AI Speech Interrupted at: 14:50:04.126
14:50:04,127 🛑 Cleared Plivo playback buffer
14:50:04,127 ▶️ Sent activityStart to Gemini
14:50:04,195 🛑 Gemini confirmed interruption
14:50:04,195 🤖 [GEMINI] (Interrupted): আমি দুঃখিত, আমি
```

Expected: the caller hears "আমি দুঃখিত, আমি…" continuing to a full sentence. Actual: 131 ms of audio reaches the caller — `আমি দুঃ` at most — then playback is cleared. The logged transcript reads as clean Bengali, so nothing in the transcript reveals that the caller heard a fragment (defect 1.3).

**Example 2 — the echo is visible in the transcript.** The caller turn logged at 14:50:03,972, 23 ms *before* the assistant's next turn begins playing, is `I'm sorry. I'm sorry.` — an English transcription of the assistant's own "আমি দুঃখিত" from the turn that was just truncated. The caller did not say this. Other bogus caller turns in the same minute: `¿Qué?`, `¿Qué tal?` (14:50:14,157), `Amén.` (14:50:05,780), `A` (14:50:15,455), `जी`, `Ata`. Expected: only genuine caller speech becomes a caller turn (2.5). Actual: the assistant's own audio is admitted upstream and prompts a fresh apology.

**Example 3 — three truncations in ten seconds, then a clean turn.** Interrupt-to-playback-start deltas of 131 ms (14:50:04.126), 340 ms (14:50:11.487) and 128 ms (14:50:14.286), each producing a `আমি দুঃখিত…` interrupted transcript — while the turn at 14:50:05.799 runs 3.00 s to completion (`AI speech playback ended at 14:50:08.813 (calculated duration: 3.00s)`). Expected: consistent delivery. Actual: the loop runs while echo conditions hold and breaks when they do not, which is exactly the "resumes normally" signature in defect 1.3.

**Example 4 — the latency distribution is bimodal and the short mode is machine-tight.** Across the call, 45 truncations have a measurable playback-start → interrupt delta. Twenty fall under 200 ms:

```
32, 103, 106, 106, 110, 117, 127, 128, 130, 134, 135, 138, 139, 139, 141, 145, 146, 161, 165, 193 ms
```

and the remaining twenty are human-plausible:

```
1238, 1314, 1573, 1595, 1639, 1855, 2136, 2571, 2997, 3204,
3566, 3656, 3738, 4315, 5514, 5605, 5831, 5917, 6598, 11803 ms
```

with only three values (231, 340, plus the 193 above) between. Expected: barge-in latency reflects human reaction time, which has no reason to cluster inside a 62 ms band. Actual: the sub-200 ms mode sits directly on the configured gate — 4 frames × 20 ms = 80 ms, plus the measured RNNoise/DSP cost of 3–12 ms per chunk and queue/transport latency.

**Example 5 — edge case, correct behaviour to preserve.** The 11803 ms trigger, and the whole >1 s mode, are genuine caller barge-ins. These must continue to cut playback promptly (3.1). The fix must not widen the gate for them.

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**
- Genuine sustained caller speech over the assistant continues to barge in, drain `plivo_output_queue`, send `clearAudio`, and open a caller activity window promptly (3.1).
- Bengali without a consonant + matra + visarga cluster renders byte-identically to today (3.2).
- English and Hindi render byte-identically to today (3.3).
- An uninterrupted turn still delivers in full, logs `🤖 [GEMINI]: …`, and reports `AI speech playback ended at … (calculated duration: N.NNs)` from `current_utterance_bytes / 8000.0` (3.4).
- An interrupted turn still logs `🤖 [GEMINI] (Interrupted): …` and `🎙️ [TIMING] AI Speech Interrupted at: HH:MM:SS.mmm` in the existing format (3.5).
- Session resumption keeps the existing handle, `MAX_GEMINI_RECONNECTS` limit, and context restoration (3.6).
- `tool_call_in_progress` still defers caller activity signalling and still prepends `preroll_pcm16` ahead of `gemini_input_buffer` on completion (3.7).
- Silence follow-up (`SILENCE_FOLLOWUP_SECONDS`, `MAX_SILENCE_FOLLOWUPS`) and `VAD_SILENCE_OFFSET_FRAMES` end-of-turn detection are unchanged (3.8).
- μ-law 8 kHz both directions, `PLIVO_ULAW_CHUNK_SIZE = 160` at 20 ms pacing, no added end-to-end latency beyond baseline (3.9).
- `aec.py` is used **unmodified**, still called before RNNoise in the inbound chain (3.10).
- Debug WAV recording and `log_call_stats` end-of-call statistics unchanged (3.11).

**Scope:**
All inputs that do NOT satisfy the bug condition must be completely unaffected. Concretely:
- Every inbound frame while the assistant is silent — the far-end is inactive, so the new decision logic short-circuits to today's code path before any correlation is computed.
- Genuine caller speech during assistant playback whose envelope does not correlate with the far-end, which keeps today's 80 ms barge-in latency.
- Every outbound byte of a turn that is not truncated inside `ANOMALOUS_TRUNCATION_MS`.
- All non-audio paths: tool calls, transfer, end-call, usage metering, analytics.

The correct behaviour for buggy inputs is specified in Correctness Properties, Property 1.

## Hypothesized Root Cause

**Verdict: implementation-side, not model-side.** The repeated `দুঃ` is the same *newly generated* turn prefix being truncated at the same point over and over, not one audio buffer replayed. Stated plainly, so the confidence level is auditable:

**Proven from the log.**
1. The truncations are machine-timed, not human-timed. Twenty of 45 cluster in 103–165 ms against a configured 80 ms gate (Example 4). Human barge-in does not do this.
2. The assistant's own speech is being admitted upstream as caller speech. `🗣️ [USER]: I'm sorry. I'm sorry.` at 14:50:03,972 is the assistant's just-truncated "আমি দুঃখিত" (Example 2), alongside `¿Qué tal?`, `Amén.`, `A`. There is no plausible caller behaviour that produces those turns.
3. The loop is closed. Each spurious caller turn is followed within ~25 ms by a new assistant turn opening with the same apology prefix, which is then truncated again (Examples 1, 3).
4. Reconnect/replay is not implicated. Only one `🔑 Session resumption handle updated`, at 14:46:55,568 — four seconds after the session opened at 14:46:51,660. The next one is 14:52:56,229, the `1000 OK` after the call ended. No mid-call reconnect occurred, so the reconnect path cannot be re-sending audio.
5. A truncation is logged identically to a legitimate interruption. `🎙️ [TIMING] AI Speech Interrupted at:` carries no delivered-duration, no trigger source, no anomaly marker (defect 1.6) — which is why this took a recording to find.

**Inferred, consistent, not proven.** That *echo specifically* is what trips the gate. The direct measurement does not exist, because nothing records the trigger's provenance (defect 1.4). Supporting circumstantial evidence: the AGC/VAD samples nearest each trigger are systematically weaker for short triggers than for long ones — median inbound RMS −41.8 dB vs −32.4 dB, median VAD probability 0.85 vs 0.99, with the 14:50:04 trigger at −42.6 dB / 0.84 against a 0.82 threshold. That is the profile of attenuated echo scraping over the bar rather than a caller speaking. It is suggestive only: `🔊 [AGC]` and `🎧 [RNNoise]` lines emit every `LOG_EVERY_N_CHUNKS = 50` chunks (~1 Hz), so these are readings up to a second *after* the trigger, not the triggering frames. Requirement 2.4 exists to convert this inference into a measurement.

**Cannot currently be proven or disproven: model-side duplicate deltas.** `app.py` accumulates `output_transcription.text` into `ai_text_buffer` (line 1646) and only logs at `interrupted` (1651–1653) or `turn_complete` (1680–1688). The per-chunk line is commented out (`#logger.info("⚡ Gemini Audio Chunk Received!")`, line 1692). No per-delta record of arrival time, byte count or text boundary exists (defect 1.7). Two things are worth stating about this gap:
- It does not weaken the verdict. Upstream duplication cannot explain 20 truncations landing on an 80 ms gate, and cannot explain the assistant's own words arriving as caller turns. Those two facts are sufficient to establish an implementation-side false-barge-in loop regardless of what the model stream contains.
- It does mean a *second, independent* duplication mechanism cannot be ruled out. Concern (d) closes this, and concern (c) is deliberately designed to hold whether the repeat originates upstream or downstream.

**Contributing mechanism found by reading the code — AEC far-end desynchronisation.** This is why residual echo is strong enough to matter at all, and it is a defect in how `app.py` *drives* the canceller, not in `aec.py` itself:

1. **Reference is timestamped at send, not at playout.** `aec.add_far_end(ulaw_to_pcm(chunk))` (app.py ~1942) runs the instant a chunk is handed to the WebSocket. The caller hears it one network hop plus one Plivo jitter buffer plus handset latency later. `_PFDKF` is built with `N=24, M=160` → it models `24 × 160 = 3840` samples = **480 ms** of echo path at 8 kHz. Fixed misalignment consumes that budget directly; the comment at the call site claims the reference is "aligned to real playback time", and it is not.
2. **`clearAudio` desynchronises the reference permanently.** On barge-in, `app.py` drains `plivo_output_queue` and sends `clearAudio` — Plivo discards audio that was already pushed into the AEC's far-end FIFO. The sender also does `out_buffer.clear()` on `interrupting`. The canceller is then referenced against audio the caller never heard, and the offset persists for the remaining backlog. This call fired `🛑 Cleared Plivo playback buffer` **45 times** in six minutes, with **52** confirmed interruptions. `aec.reset_far_end()` — which exists and does exactly the right thing — is called only on Gemini reconnect (app.py 1110), never on `clearAudio`.
3. **Consequence.** Each false barge-in degrades cancellation, raising residual echo, making the next false barge-in more likely. The loop is self-reinforcing, which matches the observed clustering of truncations rather than a uniform sprinkle.

Ranked, the causes are: (1) the while-speaking gate is too permissive at 80 ms and has no echo corroboration; (2) the preroll flush admits echo upstream and closes the loop; (3) AEC far-end desynchronisation across `clearAudio` inflates residual echo; (4) no anomaly detection, so the whole thing is invisible; (5) unverified — possible model-side delta duplication, which concern (d) will settle.

**The offline provenance analysis is a hard gate, not a recommendation.** "Residual echo tripped the gate" is inferred, so the offline measurement against `debug_recordings/Abhishek_20260921_144651.wav` (Exploratory Bug Condition Checking, test case 1) governs whether the echo-dependent work may be built at all:

- Concerns **(a)** and **(b)** MUST NOT be implemented until that analysis returns **CONFIRMED**. No code for either concern is written before then.
- A **REFUTED** or **INCONCLUSIVE** result sends (a) and (b) **back to the design phase** — the gate is not satisfied by a partial or ambiguous answer, and re-hypothesising is the required next step, not proceeding with the current design. Refuted specifically means the short-trigger windows contain genuine caller speech, which moves the root cause to VAD/AGC sensitivity and invalidates the corroborated-gate premise.
- Concerns **(c)** and **(d)** are **unaffected by the outcome and may proceed regardless**. (c) is designed to hold whether the repeat originates upstream or downstream and whether or not echo is the trigger; (d) is the instrumentation that makes any of these questions answerable, and is more valuable, not less, if the hypothesis is refuted.

This is the same gate recorded as **AR-2** in bugfix.md.

**Model attribution is established, and the fix does not depend on it.** The affected call ran on **`gemini-3.1-flash-live-preview`**. This is proven, not inferred, and the evidence chain is short enough to state in full: the call ended 14:52:56 IST and the log's final entry is 14:57:58 IST (09:27:58 UTC); `app.py` was modified at 11:35:50 UTC (17:05:50 IST), 2 h 13 min later, by the assistant in an earlier session of this conversation; in that session the model string was changed by a find-and-replace whose match target was the literal `GEMINI_MODEL = "gemini-3.1-flash-live-preview"`, with a `grep` immediately beforehand confirming that line. A find-and-replace only succeeds if its match target is present, so `app.py` provably held `gemini-3.1-flash-live-preview` right up until 11:35:50 UTC — after the affected call had already ended. See **Affected Configuration** in bugfix.md.

**Superseded reasoning.** An earlier version of this section argued attribution from `app.py`'s mtime alone — file edited ~2 h after the call, therefore the call predates the current model string — and concluded only that the call "most likely" ran on 3.1. That argument is **withdrawn as contaminated**: the mtime records an assistant edit made during a working session, so on its own it dates *an* edit, not the migration, and cannot exclude the model string having already been `gemini-3.8-live` beforehand. The find-and-replace match target is what closes the gap. The conclusion is unchanged; the evidence for it is not. This paragraph is retained so the change is auditable.

**Configuration delta.** The affected call ran **with `thinking_config=types.ThinkingConfig(thinking_level="low")` set** on `LiveConnectConfig`. That config was removed in the same session, because `gemini-3.8-live` does not support `thinkingLevel`, so the current code runs with **no thinking config at all**. This matters to the defect rather than being bookkeeping: thinking level changes turn-generation latency, which changes how much audio is in flight when a spurious trigger lands, and therefore how the defect would present on 3.8.

**Model-independence claim, stated precisely.** `GEMINI_MODEL` is referenced in exactly two places — its definition (line 46) and the session connect call (line 1113). There is no model-conditional branching in the AEC far-end handling, the `clearAudio` path, the while-speaking VAD gate, the preroll capture/flush, or the outbound pacing. The **structural defect is therefore identical across model strings**: the far-end reference is timestamped at send rather than at playout, and `reset_far_end()` is never called on `clearAudio`, regardless of which model serves the call. What is **not** claimed: that the observable *rate and severity* reproduce on `gemini-3.8-live`. Those are unconfirmed, and no occurrence has been observed under the current configuration. That risk is accepted and recorded as **AR-1** in bugfix.md; the requirement-2.8 model logging and requirement-2.6 anomaly logging are its mitigation, converting the next occurrence from an ambiguity into a measurement.

## Correctness Properties

Property 1: Bug Condition - Echo-Driven Truncation Never Cuts a Grapheme Cluster

_For any_ input where the bug condition holds (`isBugCondition` returns true) — that is, a while-speaking VAD trigger within `ANOMALOUS_TRUNCATION_MS` of turn onset whose near-end energy is explained by the far-end reference — the fixed system SHALL NOT truncate playback, SHALL NOT send `clearAudio`, SHALL NOT open a caller activity window, and SHALL NOT admit the triggering frames or the echo-tagged portion of the preroll into the model input path; the turn's leading grapheme cluster SHALL be delivered exactly once, whole and contiguous, and the trigger SHALL be recorded as a suppressed anomalous truncation with its delivered duration, leading text fragment, and echo-correlation evidence.

**Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5, 2.6**

Property 2: Preservation - Non-Echo Inputs Behave Identically

_For any_ input where the bug condition does NOT hold (`isBugCondition` returns false) — far-end inactive, near-end uncorrelated with far-end, or the trigger arriving after `ANOMALOUS_TRUNCATION_MS` of delivered playback — the fixed system SHALL produce the same result as the original system, preserving barge-in timing and side-effect ordering, the outbound μ-law byte stream and its 20 ms pacing, the preroll flush contents, the tool-call deferral sequence, the silence watchdog schedule, the session resumption path, the debug recording, and every existing log line's format.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9, 3.10, 3.11**

## Fix Implementation

### Changes Required

Assuming the root cause analysis is correct. All new decision logic lands in **pure, side-effect-free functions in a new module `bargein.py`**, called from `app.py`. This is deliberate: it is the only way to make the logic testable without a live phone call (see Testing Strategy). `aec.py` is untouched.

**New file**: `bargein.py` — pure helpers: envelope extraction, lag tracking, echo correlation, grapheme segmentation, anomaly classification, prefix comparison, commit-gate arithmetic.

**Modified file**: `app.py` — wiring, state fields, and logging only.

#### (a) Corroborated barge-in gate — prevents echo from truncating playback

Requirements 2.1, 2.4. Regression risk owner for 3.1.

1. **Far-end shadow buffer in `app.py`.** Add `far_shadow` — a bounded ring of 8 kHz PCM16 samples holding the most recent ~600 ms — appended at the same site as `aec.add_far_end()` in `send_plivo_audio`, from the same `ulaw_to_pcm(chunk)` result. `aec.py`'s own FIFO is consumed by `_pop_far` and cannot be read without modifying the module; the shadow is how `app.py` gets its own view of the reference without touching the frozen file. Cost is ~10 KB and one `numpy` copy per 20 ms chunk.

2. **Envelope-domain echo correlation** (`bargein.echo_correlation`). Reduce both near-end (pre-AEC 8 kHz, so the canceller's own suppression does not hide the evidence) and `far_shadow` to short-term energy envelopes in 10 ms bins, then compute the normalised correlation over a ~300 ms window at a slowly-tracked lag. Envelope domain rather than waveform domain, because the acoustic round-trip delay is unknown and drifts: waveform cross-correlation over a ±400 ms search is 3200 lags per 20 ms frame, while envelope correlation is ~30 bins against ~40 candidate lags — arithmetic small enough to be free. `numpy` suffices; `scipy.signal.correlate` (1.15.3, installed) is available if profiling calls for it.

3. **Lag tracking** (`bargein.LagTracker`). Estimate the near/far lag once per second from a wider envelope correlation during known assistant-only playback, and hold it between updates. Re-estimate after `clearAudio`.

4. **Two-tier decision** (`bargein.should_barge_in`), replacing the raw `speech_started and assistant_speaking` test at app.py 1370:

   - **Tier 1 — far-end inactive** (shadow energy below the activity floor): return today's verdict unchanged. No correlation computed. This path covers the overwhelming majority of frames, and byte-for-byte identical behaviour here is what makes Property 2 hold for 3.2, 3.3 and most of 3.1.
   - **Tier 2 — far-end active**: require corroboration.
     - `echo_correlation < ECHO_CORR_THRESHOLD` → uncorrelated, so it is the caller. Barge in at the **existing** 4-frame / 80 ms latency.
     - correlation at or above threshold → ambiguous or echo. Withhold truncation until sustained speech reaches `ECHO_AMBIGUOUS_ONSET_FRAMES`. Echo tracks the far-end and dies when the far-end dies, so it does not reach that count; a real double-talking caller does.

5. **Constants** (app.py, near lines 77–92):
   ```
   ECHO_CORR_THRESHOLD           = 0.35   # envelope correlation above this = far-end explains near-end
   ECHO_AMBIGUOUS_ONSET_FRAMES   = 9      # 180 ms sustained before trusting a correlated trigger
   FAR_END_ACTIVE_FLOOR_DB       = -60.0  # shadow-buffer activity floor
   ANOMALOUS_TRUNCATION_MS       = 350    # matches the 100-350 ms band in requirement 1.1
   GRAPHEME_COMMIT_MS            = 240    # one Bengali CV+visarga cluster, with margin
   ```
   `VAD_THRESHOLD_WHILE_SPEAKING = 0.82` and `VAD_SPEECH_ONSET_FRAMES_WHILE_SPEAKING = 4` are **left alone**. Raising them globally would be the obvious quick fix and is rejected precisely because it would trade away genuine barge-in latency on every turn, violating 3.1. All five values above are initial estimates that must be tuned against `debug_recordings/Abhishek_20260921_144651.wav` before deployment; `ECHO_CORR_THRESHOLD` in particular is a guess until the V1 offline measurement lands.

6. **Latency budget** (requirement 3.1, the primary regression risk). Explicit, per case:

   | Case | Today | After fix | Delta |
   |---|---|---|---|
   | Assistant silent (far-end inactive) | 3 frames / 60 ms | 3 frames / 60 ms | **0** |
   | Assistant speaking, near-end uncorrelated | 4 frames / 80 ms | 4 frames / 80 ms | **0** |
   | Assistant speaking, correlated or ambiguous | 4 frames / 80 ms | 9 frames / 180 ms | **+100 ms** |
   | Turn onset while armed (see (c)) | 0 | up to +240 ms, once | **+240 ms one-time** |

   Justification: the only case that pays is one where the near-end is acoustically indistinguishable from what the assistant is currently playing. 100 ms is below the ~200 ms threshold at which added barge-in delay becomes conversationally noticeable, and it buys removal of a defect that fired 20 times in six minutes. The common cases pay nothing, which is what keeps 3.9's "no added end-to-end latency beyond baseline" intact. The +240 ms case is armed only after an anomalous truncation, so it cannot occur on a healthy call.

7. **AEC reference hygiene** (uses `aec.py`'s existing public API; the module is not modified). Call `call_state["aec"].reset_far_end()` immediately after every `clearAudio` send, and clear `far_shadow` alongside. There are exactly **three** `clearAudio` sends in `app.py`, verified by inspection — **lines 1389, 1672 and 1721** — and those three are the complete set. This stops discarded audio from poisoning the reference, which is contributing mechanism 3. Also record, at the `add_far_end` site (line 1942, the only `add_far_end` call site in the file), the monotonic timestamp of each chunk so the reference's send-vs-playout skew is measurable rather than assumed.

   **Not in this concern: `play_disclaimer`.** An earlier version of this design listed "the disclaimer path" as a fourth `clearAudio` site. That is factually wrong. `play_disclaimer` (app.py ~744–777) sends `playAudio` frames directly and never sends `clearAudio`; it also never calls `aec.add_far_end()`, so it is a far-end *reference gap*, not a `clearAudio` site. Per the operator's instruction that gap is **out of scope for this bugfix** and tracked as a separate ticket — see the `## Out of Scope` section of bugfix.md. **No part of this design modifies `play_disclaimer`.**

   **Two further playback-discard points, to be decided during implementation.** Both discard already-queued outbound audio without sending `clearAudio`, so both leave the far-end reference ahead of what the caller heard, but neither is a `clearAudio` site and neither is settled here: `out_buffer.clear()` in the ending-call path (app.py ~1849) and `out_buffer.clear()` in the `interrupting or user_activity_open` branch of `send_plivo_audio` (app.py ~1933). Whether each warrants a `reset_far_end()` is an implementation decision — the ending-call path is terminal, so a reset there may be pointless, while the `send_plivo_audio` branch runs inside the barge-in flow already covered by the three sites above and may be redundant with them.

#### (b) Echo not admitted upstream — prevents the loop from closing

Requirement 2.5.

1. **Tag preroll frames at capture.** `preroll_pcm16` becomes a deque of `(pcm16_frame, far_end_active, echo_correlation)` records instead of a flat `bytearray`, with the same ~200 ms bound expressed in frames. A thin `bargein.preroll_bytes(records)` reconstructs the flat buffer so both existing flush sites keep working.
2. **Trim echo on flush.** At both flush sites — the barge-in flush (app.py 1408–1412) and the tool-call-completion prepend (app.py 1620–1622, requirement 3.7) — drop leading frames whose tag says far-end-active-and-correlated, keep everything from the first genuine frame onward. Genuine onset is still not clipped, which is the reason the preroll exists; only the echo tail that preceded it is removed.
3. **Gate the activity window on the same verdict.** `activityStart` and the `gemini_input_buffer` stream are opened only when `should_barge_in` corroborates. An uncorroborated trigger produces no `activityStart`, so the model receives no phantom caller turn — the mechanism behind `I'm sorry. I'm sorry.` and `¿Qué tal?`.
4. **Tool-call path is untouched in shape.** The `tool_call_in_progress` branch keeps its existing structure and ordering; it only consumes the trimmed preroll. This is what preserves 3.7.

#### (c) Leading-fragment commit gate — suppresses re-delivery of an already-played fragment

Requirement 2.2. Designed to hold **regardless of whether the repeat originates upstream or downstream**, because gap 1 means we cannot yet prove which.

1. **Record what was delivered.** On any truncation, capture `delivered_ms = current_utterance_bytes / 8000.0 * 1000`, the `ai_text_buffer` content, and its leading grapheme clusters.
2. **Classify.** `delivered_ms < ANOMALOUS_TRUNCATION_MS` → anomalous truncation. Enter the **armed state** and emit the anomaly log from (d).
3. **Detect the repeat.** While armed, compare the new turn's leading grapheme clusters against the truncated turn's using `bargein.shared_leading_clusters`. A match means the caller is about to hear the same partial sound again.
4. **Gate the commit.** While armed, hold the new turn's outbound audio in the sender until `GRAPHEME_COMMIT_MS` of audio has accumulated, then release it and resume normal 20 ms pacing. The point is that the fragment becomes all-or-nothing: if a spurious trigger fires again inside that window, the caller hears **nothing** rather than another `দুঃ`. "At most once" in requirement 2.2 is satisfied by never emitting a second partial cluster.
5. **Disarm** on the first turn that completes past `GRAPHEME_COMMIT_MS` without truncation. Normal calls are never armed, so steady-state latency is unchanged (3.9).

Why the alternative was rejected: trimming N milliseconds of audio off the front of the regenerated turn to "skip" the already-played fragment would be the literal reading of 2.2, and it is not safely implementable. The Live API delivers no time alignment between `output_transcription` deltas and `inline_data` audio chunks, so the byte offset corresponding to a text prefix is unknown. Guessing it would clip genuine speech. Withholding the fragment until it is whole achieves the same perceptual outcome with no guesswork.

#### (d) Diagnostic instrumentation

1. **Per-delta trace** (requirements 1.7, 2.7). At each `model_turn` part carrying `inline_data`, and at each `output_transcription` delta, append to a bounded per-turn `delta_trace`: sequence number, monotonic arrival time, byte count, cumulative bytes for the turn, a short content hash of the audio bytes, and for text the delta string plus whether it ends mid-grapheme-cluster. Replaces the commented-out line at app.py 1692. Logged at DEBUG during normal operation; the whole trace is dumped at INFO when an anomalous truncation fires, so the evidence survives without enabling global DEBUG in production.

2. **How this definitively settles upstream vs downstream.** The trace records three counters per turn: `model_audio_bytes_received`, `queued_bytes` (into `plivo_output_queue`), and `sent_bytes` (to Plivo). Combined with per-chunk hashes:
   - **Upstream duplication** ⇒ the same audio chunk hash, or the same text grapheme-cluster sequence, appears twice within one turn's delta trace, with `sent_bytes ≈ queued_bytes ≈ model_audio_bytes_received`. The duplication is present before local buffering touches it.
   - **Downstream re-delivery** ⇒ all model chunk hashes within the turn are distinct, but a hash appears twice in the sent stream, or `sent_bytes > model_audio_bytes_received`.
   - **Neither** (the prediction of this design) ⇒ hashes distinct in both directions, counters equal, and the repeated `দুঃ` appears as *separate turns* each with a short `delivered_ms` and a matching leading cluster — which is the false-barge-in signature.

   These three outcomes are mutually exclusive and exhaustive, so one deployment of the trace across one affected call closes gap 1 with no ambiguity.

3. **Trigger provenance** (requirements 1.4, 2.4). Every barge-in decision — fired or suppressed — logs `avg_prob`, `rnnoise_speech_count`, the active threshold and onset-frame count, `far_end_active`, `echo_correlation`, the tracked lag, `delivered_ms`, and the verdict. This is the measurement that turns the "inferred" part of the root cause into fact.

4. **Anomaly log** (requirements 1.6, 2.6). A distinct `⚠️ [ANOMALY] Truncation` line carrying `delivered_ms`, delivered bytes, the leading grapheme clusters of `ai_text_buffer`, the trigger source, and the correlation evidence. New line, distinct prefix — the existing `🎙️ [TIMING] AI Speech Interrupted at:` and `🤖 [GEMINI] (Interrupted):` lines keep their exact current format, preserving 3.5.

5. **Model identity** (requirements 1.8, 2.8). Log `GEMINI_MODEL` at session connect and at each resumption, with `gemini_reconnect_count`, and include it in `log_call_stats`. One line; it is the difference between an attributable recording and the unresolvable ambiguity described above.

## Testing Strategy

### Validation Approach

Two phases. First, surface counterexamples that demonstrate the bug on unfixed code — including one offline measurement that can be done today with no phone call. Then verify the fix works and preserves existing behaviour.

**Test framework — what needs setting up.** The project has no test framework. `requirements.txt` must not be modified, and neither `pytest` nor `hypothesis` is installed (verified against `venv/lib/python3.12/site-packages`). Therefore:
- Tests use the standard library `unittest` and `unittest.mock`, run as `venv/bin/python -m unittest discover -s tests -v`. No new dependency.
- Property-based tests are **hand-rolled falsifiers**: a seeded `numpy.random.default_rng(seed)` loop over 500–1000 generated inputs, asserting the property and printing the failing input, the seed, and the case index on first failure. This gives the counterexample reporting that matters without `hypothesis`.
- Already installed and used: `regex` 2026.2.28 (grapheme segmentation, also already pinned in `requirements.txt` line 177), `numpy` 2.2.6, `scipy` 1.15.3, `soundfile` 0.13.1 (reading fixtures).
- Determinism without a live call comes from three fakes: `FakePlivoWS` (records the JSON frames that would have been sent), `FakeSession` (yields a scripted sequence of `server_content` responses with `output_transcription`, `inline_data`, `interrupted`, `turn_complete`), and `FakeClock` (drives the 20 ms pacing and the timeout branch of `send_plivo_audio` without real sleeps). Real audio fixtures come from `playback_audio_files/*.wav`, already in the repo; a synthetic echo path (delay + attenuation + additive noise) turns any far-end fixture into a near-end echo signal.
- Testability depends on the `bargein.py` split from the Fix Implementation section: `should_barge_in`, `echo_correlation`, `classify_truncation`, `shared_leading_clusters`, `grapheme_clusters` and `preroll_bytes` are pure functions over plain data, so the great majority of the surface is unit-testable with no transport at all.

### Grapheme-Cluster Handling

How a partial cluster is detected, concretely — not hand-waved.

**Primary mechanism: UAX #29 extended grapheme clusters via `regex`'s `\X`.** Verified on this interpreter:

```
regex.findall(r'\X', 'দুঃখিত')      -> ['দুঃ', 'খি', 'ত']
regex.findall(r'\X', 'নিঃশব্দ')      -> ['নিঃ', 'শ', 'ব্দ']
regex.findall(r'\X', 'অন্তঃসত্ত্বা') -> ['অ', 'ন্তঃ', 'স', 'ত্ত্বা']
regex.findall(r'\X', 'পুনঃ')        -> ['পু', 'নঃ']
```

`দুঃ` is one cluster of `দ` U+09A6 + `ু` U+09C1 + `ঃ` U+0983. `\X` also keeps virama-joined conjuncts intact — `ব্দ` is U+09AC + U+09CD + U+09A6, and `ন্তঃ` is U+09A8 + U+09CD + U+09A4 + U+0983 — which matters because a conjunct plus visarga is a longer indivisible unit than the reported case.

**Code points involved**, for the fallback and for test-data construction: matras U+09BE–U+09CC (`Mc` for spacing marks such as U+09BE AA, `Mn` for non-spacing such as U+09C1 U), visarga U+0983 (`Mc`), candrabindu U+0981 and anusvara U+0982, hasanta/virama U+09CD (`Mn`), and ZWJ U+200D (`Cf`).

**Fallback** if `regex` is ever unavailable: a walker in `bargein.py` that starts a new cluster at a base character and extends it while the next code point has `unicodedata.category` in `{'Mn', 'Mc', 'Me'}`, is U+200D, or follows a U+09CD (a virama pulls the next base into the cluster). The stdlib has no UAX #29 segmentation, so this is an approximation over the Bengali block only; it is a safety net, not the design.

**Honest limitation.** Cluster boundaries are known in the **text** domain. Truncation happens in the **audio** domain, and the Live API provides no time alignment between transcription deltas and audio chunks, so there is no way to map "cut after cluster 1" onto a byte offset. Grapheme awareness therefore governs three things: classifying a delivered fragment as sub-cluster (arming the gate, and the anomaly log's leading-fragment field), comparing a regenerated turn's prefix against the truncated one, and sizing `GRAPHEME_COMMIT_MS`. The guarantee this design delivers is **"a spurious trigger never cuts inside a cluster"** — achieved by not truncating on spurious triggers (a) and by making the leading fragment all-or-nothing (c) — not "every cut lands exactly on a cluster boundary". Claiming the latter would be claiming an alignment the API does not expose.

### Exploratory Bug Condition Checking

**Goal**: Surface counterexamples on unfixed code, and confirm or refute the root cause. If refuted, re-hypothesise before writing any fix.

**Test Plan**: Two tracks. Offline, replay the existing debug recording and the log to test the echo hypothesis against data already in hand. In harness, drive the unfixed barge-in path with synthetic echo and observe truncation.

**Test Cases**:
1. **Offline echo confirmation from the debug recording** (will confirm or refute on data already in hand). `debug_recordings/Abhishek_20260921_144651.wav` is the post-DSP *inbound* stream, so if echo was leaking it is recorded there. Extract the ±500 ms windows around each sub-200 ms trigger timestamp from Example 4 and compare against windows around the >1 s triggers. Predicted: the short-trigger windows contain the assistant's Bengali voice at low level; the long-trigger windows contain the caller. This is the single highest-value test because it needs no code change and no phone call, and it either confirms the inferred half of the root cause or refutes it outright.
2. **Truncation reproduction in harness** (will fail on unfixed code). Script `FakeSession` to emit a turn transcribed `আমি দুঃখিত, আমি…`, start playback, and at t=130 ms inject near-end frames that are a delayed, attenuated copy of the far-end. Assert on unfixed code that `clearAudio` is sent and `delivered_ms < 350`. Documents the defect as an executable artefact.
3. **Upstream admission reproduction** (will fail on unfixed code). Same setup; assert that on unfixed code `activityStart` is sent and the flushed preroll contains echo-correlated frames — the mechanism behind `🗣️ [USER]: I'm sorry. I'm sorry.`
4. **Loop reproduction** (will fail on unfixed code). Chain three turns, each truncated at ~130 ms with the same `আমি দুঃখিত` prefix, and assert the delivered audio contains the leading cluster's partial audio three times.
5. **AEC desynchronisation measurement** (edge case; may fail on unfixed code). Feed far-end through `aec.add_far_end` without a matching `reset_far_end` across a simulated `clearAudio`, then measure echo-return-loss enhancement on a known echo signal before and after. Predicted: measurable degradation, quantifying contributing mechanism 3. `aec.py` is exercised read-only via its public API, never modified.

**Expected Counterexamples**:
- A trigger at ~130 ms after turn onset, with near-end that is a scaled copy of the far-end, truncates playback after one partial grapheme cluster.
- Possible causes, in the order this design ranks them: while-speaking gate too permissive with no echo corroboration; echo-bearing preroll admitted upstream; AEC far-end reference desynchronised by `clearAudio`.
- If test 1 refutes the echo hypothesis — short-trigger windows contain genuine caller speech — then the gate is firing on real audio and the root cause shifts to VAD/AGC sensitivity. That would keep concerns (c) and (d) intact and require re-designing (a) and (b).

### Fix Checking

**Goal**: Verify that for all inputs where the bug condition holds, the fixed system produces the expected behaviour.

**Pseudocode:**
```
FOR ALL input WHERE isBugCondition(input) DO
  result := handleBargeIn_fixed(input)
  ASSERT NOT result.playback_truncated
  ASSERT NOT result.clear_audio_sent
  ASSERT NOT result.activity_started
  ASSERT result.anomaly_logged
     AND result.anomaly.delivered_ms IS RECORDED
     AND result.anomaly.echo_correlation IS RECORDED
  ASSERT countOccurrences(leadingCluster(result.delivered_audio)) <= 1
END FOR
```

Generated by the seeded falsifier over: far-end fixture choice, echo delay 20–400 ms, echo attenuation 6–30 dB, additive noise level, trigger offset 20–349 ms, and utterance prefix drawn from a Bengali cluster table.

### Preservation Checking

**Goal**: Verify that for all inputs where the bug condition does NOT hold, the fixed system produces the same result as the original.

**Pseudocode:**
```
FOR ALL input WHERE NOT isBugCondition(input) DO
  ASSERT handleBargeIn_original(input) = handleBargeIn_fixed(input)
  ASSERT sentFrames_original(input)    = sentFrames_fixed(input)
  ASSERT logLines_original(input)      = logLines_fixed(input)   -- excluding new [ANOMALY]/diagnostic lines
END FOR
```

**Testing Approach**: Property-based testing is the right tool for preservation checking here because:
- It generates many inputs automatically across the far-end/near-end/timing domain, where the interesting behaviour lives at boundaries rather than at nominal values.
- It catches edge cases manual tests miss — trigger at exactly `ANOMALOUS_TRUNCATION_MS`, far-end energy exactly at the activity floor, correlation exactly at `ECHO_CORR_THRESHOLD`, a turn whose first chunk is shorter than 20 ms.
- Equality of the full outbound byte stream and log-line sequence across thousands of non-buggy inputs is a far stronger preservation guarantee than any hand-written set.

Implemented as the seeded falsifier described above, not `hypothesis`, which is not installed.

**Test Plan**: Capture the unfixed system's behaviour first — outbound byte stream, `clearAudio` timing, preroll contents, log-line sequence — as golden records for non-bug inputs, then assert the fixed system reproduces them exactly.

**Test Cases**:
1. **Genuine barge-in latency**: observe on unfixed code that sustained uncorrelated near-end during playback cuts at 4 frames / 80 ms, then verify the fixed code cuts at the same frame count with the same side-effect ordering — queue drained, `clearAudio` sent, `activityStart` sent (3.1).
2. **Assistant-silent path**: observe that inbound frames while the assistant is silent take the `VAD_THRESHOLD` / 3-frame path, then verify the fixed code short-circuits at Tier 1 with byte-identical results and no correlation computed (3.1, 3.2, 3.3).
3. **Uninterrupted turn**: observe the full outbound byte stream, the `🤖 [GEMINI]:` line, and the `calculated duration: N.NNs` value, then verify all three are unchanged (3.4).
4. **Interrupted turn log format**: observe `🎙️ [TIMING] AI Speech Interrupted at:` and `🤖 [GEMINI] (Interrupted):` exactly, then verify byte-identical format after the fix (3.5).
5. **Tool-call deferral**: observe that speech during `tool_call_in_progress` lands in preroll and is prepended to `gemini_input_buffer` on completion, then verify the trimmed-preroll change preserves the sequence for non-echo frames (3.7).
6. **Silence watchdog**: observe the `silence_watchdog` schedule and `MAX_SILENCE_FOLLOWUPS` behaviour, then verify unchanged (3.8).
7. **Framing and pacing**: observe that every `playAudio` payload decodes to exactly 160 μ-law bytes at 20 ms spacing, then verify unchanged, including while armed (3.9).
8. **Non-Bengali and cluster-free Bengali**: observe rendering for English, Hindi, and Bengali without a visarga cluster, then verify byte-identical output (3.2, 3.3).
9. **AEC module integrity**: assert `aec.py` is unmodified — a content hash pinned in the test — and that `app.py` calls only its existing public methods, with AEC still invoked before RNNoise in the inbound chain (3.10).
10. **Debug recording and stats**: observe the debug WAV byte count and `log_call_stats` output, then verify unchanged (3.11).

### Unit Tests

- **Grapheme segmentation**, table-driven: `দুঃখিত → ['দুঃ','খি','ত']`, `নিঃশব্দ → ['নিঃ','শ','ব্দ']`, `অন্তঃসত্ত্বা → ['অ','ন্তঃ','স','ত্ত্বা']`, `পুনঃ → ['পু','নঃ']`, plus English, Hindi, mixed-script, empty string, lone combining mark, and a string ending mid-cluster. Run against both the `regex` implementation and the `unicodedata` fallback, asserting they agree on the Bengali table.
- **Echo correlation**: identical signals → correlation near 1 at lag 0; delayed copy → near 1 at the true lag; uncorrelated speech → below threshold; far-end silent → activity floor short-circuit; near-end = echo + caller speech (double-talk) → below threshold, the case that protects 3.1.
- **Lag tracker**: converges to a known injected delay; holds its estimate through a silent gap; re-estimates after reset.
- **Truncation classifier**: boundary cases at `delivered_ms` of 0, 349, 350, 351; arm and disarm transitions.
- **Prefix comparison**: shared leading clusters for `আমি দুঃখিত, আমি` vs `আমি দুঃখিত, কিন্তু আমি এই ধরনের` (both real interrupted transcripts from the log) → shared prefix detected; `আমি দুঃখিত…` vs `আমি কি আপনাকে…` → no shared leading cluster beyond `আমি`.
- **Commit gate arithmetic**: releases at exactly `GRAPHEME_COMMIT_MS`; passes through unchanged when not armed; never emits a partial fragment when a trigger arrives mid-window.
- **Preroll tagging and trimming**: leading echo frames dropped, genuine onset preserved, ~200 ms bound respected, flat-bytes reconstruction matches today's `bytearray` for all-genuine input.
- **Diagnostic emitters**: the anomaly line and the delta trace contain every field requirements 2.4, 2.6, 2.7 and 2.8 enumerate, and existing log lines are untouched.

### Property-Based Tests

Seeded falsifiers, 500–1000 cases each, reporting seed and failing input.

- **Property 1 (fix)**: over randomised echo delay, attenuation, noise, trigger offset and utterance prefix — whenever the input satisfies the bug condition, playback is not truncated, no `activityStart` is sent, the anomaly is logged, and the leading cluster appears at most once in the delivered audio.
- **Property 2 (preservation)**: over randomised non-bug inputs — far-end inactive, or uncorrelated near-end, or trigger beyond 350 ms — the fixed outbound byte stream, barge-in frame count, side-effect ordering, and existing-log-line sequence are equal to the unfixed system's.
- **Cluster invariance**: over randomised Bengali strings built from a base-consonant × matra × {visarga, anusvara, candrabindu, none} × conjunct table, segmentation never splits a cluster and never merges two, and the commit gate never releases a partial cluster's worth of audio.
- **Byte conservation**: over randomised turn shapes and chunk sizes, bytes sent to Plivo never exceed bytes received from the model for a turn, and never duplicate a chunk hash — the downstream half of the gap-1 discriminator, asserted as an invariant rather than only logged.

### Integration Tests

Harness-level, no telephony, using `FakePlivoWS` + `FakeSession` + `FakeClock` over the real `stream_plivo_to_gemini`, `stream_gemini_to_plivo` and `send_plivo_audio` coroutines.

- **Requirement 2.9 — different cluster, bug class closed.** The headline regression test, deliberately not using `দুঃ`. Script a turn transcribed `আমি নিঃশব্দে বলছি` (leading word `নিঃশব্দ`, cluster `নিঃ` = ন U+09A8 + ি U+09BF + ঃ U+0983 — a *different* consonant and a *different* matra than `দুঃ`), inject an echo-correlated trigger at 130 ms, and assert: no truncation, `নিঃ` present exactly once in the delivered audio, outbound bytes equal model bytes, and the suppressed trigger logged with its correlation evidence. Repeat the same scenario for `অন্তঃ` (conjunct + visarga, the hardest case) and `পুনঃ` (visarga on a bare consonant, no matra). Determinism comes entirely from the fakes and a fixed seed — no phone call, no live model.
- **Full loop closure**: three chained apology-prefixed turns with echo triggers; assert the loop does not form, no phantom caller turn is created, and the third turn completes.
- **Genuine barge-in end to end**: uncorrelated sustained near-end during playback cuts at 80 ms, `clearAudio` is sent, `activityStart` follows, the trimmed preroll still carries the caller's onset.
- **Context switching**: tool call in progress → echo trigger → tool completes → genuine speech; assert deferral ordering and preroll prepend are unchanged (3.7).
- **Reconnect path**: simulate `GeminiSessionDisconnected` mid-turn; assert `reset_far_end`, shadow clear, lag re-estimation, and that resumption logs the model identifier with the attempt count (3.6, 2.8).
- **Upstream/downstream discrimination**: inject a deliberately duplicated model audio chunk and assert the delta trace flags it as upstream; separately inject a deliberate local re-queue and assert it is flagged downstream. Proves the gap-1 instrumentation actually discriminates before it is relied on in production.

### What Can Only Be Validated on a Live Call

Stated plainly, so the offline suite is not mistaken for full coverage.

**Offline / unit-testable**: grapheme segmentation; correlation and lag arithmetic; truncation classification and arming; commit-gate timing; preroll trimming; prefix comparison; byte conservation and chunk-hash uniqueness; log formats and field completeness for 2.4/2.6/2.7/2.8; barge-in frame-count equality for non-bug inputs; μ-law framing and 20 ms pacing invariants; `aec.py` integrity; the retrospective echo measurement against `debug_recordings/Abhishek_20260921_144651.wav`.

**Live-call only**:
- Real acoustic echo on a real handset or speakerphone, and therefore the true distribution of `echo_correlation` — which is what `ECHO_CORR_THRESHOLD = 0.35` must be tuned against. The synthetic echo path in the harness is a model, not the room.
- Actual AEC convergence with the far-end reference hygiene fix in place (3.10 interaction), including the send-vs-playout skew against the canceller's 480 ms modeled path.
- Perceived barge-in responsiveness under the +100 ms ambiguous-case budget (3.1). The frame count is unit-testable; whether a caller notices is not.
- Plivo's real `clearAudio` timing and jitter-buffer behaviour, and end-to-end latency against baseline (3.9).
- Mid-call session resumption under real network conditions (3.6) — note the affected call had no mid-call reconnect, so this path is untested by the incident itself.
- **The rate and severity of the stutter on `gemini-3.8-live`** (**AR-1**). Which model served the affected call is settled — `gemini-3.1-flash-live-preview`, with `thinking_level="low"` — so this is no longer an attribution question. What is unconfirmed is whether the defect presents at the same rate and severity under the current configuration, since that depends on the acoustic character, timing and turn structure of the model's audio, plus the turn-generation latency change from removing the thinking config. The fix is not contingent on it: every link in the loop is implementation-side and model-independent. Once the requirement-2.8 model logging is deployed, the next occurrence answers it as a measurement.
- Model-side delta behaviour, and therefore the final upstream-vs-downstream verdict (gap 1). The harness proves the instrumentation discriminates correctly; only a real affected call supplies the data to discriminate.
