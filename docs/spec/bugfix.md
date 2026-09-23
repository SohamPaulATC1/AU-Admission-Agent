# Bugfix Requirements Document

## Introduction

During a live Bengali call, the assistant was about to say **দুঃখিত** ("sorry"). Instead of speaking the word cleanly, the caller heard the leading partial grapheme **দুঃ** repeated several times — "দুঃ …. দুঃ ….. দুঃ …." — after which the assistant continued and answered the question normally.

The defect is narrow and specific:

- It is **not** a full-sentence repeat.
- It is **not** silence or a dropout.
- It is a **partial-grapheme loop** at the **দুঃ** boundary, where দুঃ is the grapheme cluster দ + ু (u-matra) + ঃ (visarga) — consonant + matra + visarga.
- The utterance resumes normally once the loop ends, so the turn-level conversation transcript looks clean and the defect leaves no trace in it.

Affected call: caller [number redacted], call UUID `dbcd514d-3fe8-4af9-bb1a-f430634c89fd`, 14:46:51 to 14:52:56 IST, with the matching debug recording `debug_recordings/Abhishek_20260921_144651.wav` (364.57 s). The immediately preceding call at 14:45:41 serves as a control.

Impact: on a customer-facing outbound sales call the assistant sounds broken at exactly the moments it is declining a request or apologising — the highest-sensitivity moments in the conversation. Because the apology phrase "আমি দুঃখিত" is the assistant's standard opener for refusals, the defect recurs many times per call. It is also invisible to existing monitoring, since the logged transcript for each affected turn reads as normal Bengali text.

Two secondary problems block diagnosis and are therefore in scope as part of this bugfix: the system does not record whether a playback-stopping trigger came from genuine caller speech or from residual echo of the assistant's own audio, and it does not record streaming-delta boundaries or the model identity for a call. Without these, no call can be attributed to a model version from its own log, and upstream (model-side) duplicate deltas cannot be distinguished from downstream (implementation-side) re-delivery from the evidence alone. The model serving *this* call has since been established from external evidence rather than from the log (see Affected Configuration below); defect 1.8 stands unchanged, because the next affected call would be just as unattributable from its log alone.

## Affected Configuration

The model and configuration that served the affected call are established fact, not an open question.

**Timeline:**

- The affected call ran 14:46:51 → 14:52:56 IST on 2026-09-21. The log file's final entry is `2026-09-21 14:57:58,282 [INFO] Plivo -> Gemini stream cancelled`, i.e. 09:27:58 UTC.
- `app.py` was modified at 2026-09-21 11:35:50 UTC (17:05:50 IST) — 2 h 13 min after that final log entry. That edit was made by the assistant in an earlier session of this same conversation, not by the operator.
- In that session the model string was changed by a find-and-replace whose match target was the literal `GEMINI_MODEL = "gemini-3.1-flash-live-preview"`, replaced with `GEMINI_MODEL = "gemini-3.8-live"`. The replace succeeded, and a `grep` immediately beforehand confirmed line 48 read `gemini-3.1-flash-live-preview`. (The definition sits at line 46 today; the same session removed the thinking-config lines above it.)
- A find-and-replace can only succeed if its match target is present. `app.py` therefore provably contained `gemini-3.1-flash-live-preview` right up until 11:35:50 UTC, which is after the affected call had already ended.

**Established:** the affected call ran on **`gemini-3.1-flash-live-preview`**.

**Configuration difference:** in the same session, `thinking_config=types.ThinkingConfig(thinking_level="low")` was removed from `LiveConnectConfig`, because `gemini-3.8-live` does not support `thinkingLevel`. The affected call therefore ran **with `thinking_level="low"` set**; the current code runs with **no thinking config at all**.

**Current state, verified by inspection:** `app.py` line 46 reads `GEMINI_MODEL = "gemini-3.8-live"`. `GEMINI_MODEL` is referenced in exactly two places — its definition (line 46) and the session connect call (line 1113, `connect_live_with_timeout(client, GEMINI_MODEL, config)`). There is no model-conditional branching anywhere in the audio, AEC, barge-in, preroll or `clearAudio` code paths.

**Superseded reasoning:** an earlier version of this spec argued model identity from `app.py`'s mtime alone — file modified ~2 h after the call, therefore the call predates the current model string. That argument is withdrawn. The mtime records an assistant edit made during a working session, so on its own it dates *an* edit, not the migration, and cannot exclude the possibility that the model string was already `gemini-3.8-live` beforehand. The find-and-replace match target is what closes that gap: it proves what the line contained immediately before the edit. The conclusion is unchanged; only the evidence supporting it is.

The operator's recollection was that the call ran on 3.8 and that `app.py` was unchanged after the call. Both are contradicted by the record above. This document follows the evidence.

## Bug Analysis

### Current Behavior (Defect)

What currently happens when the bug is triggered.

1.1 WHEN the assistant begins playing an utterance whose leading word starts with a Bengali consonant + matra + visarga grapheme cluster (দুঃখিত) AND a playback-stopping barge-in trigger fires within roughly the first 100–350 ms of playback THEN the system truncates the utterance after only the leading partial grapheme দুঃ has reached the caller, cutting inside the grapheme cluster rather than at a word or phrase boundary.

1.2 WHEN such a truncation occurs AND the model regenerates a turn that opens with the same lexical prefix ("আমি দুঃখিত" / "দুঃখিত") THEN the system delivers the same leading partial grapheme again, and repeats this across consecutive turns, so the caller hears "দুঃ … দুঃ … দুঃ" instead of one clean word.

1.3 WHEN the partial-grapheme loop ends THEN the system continues the utterance normally and produces no full-sentence repeat, no silence and no dropout, so the caller-perceived stutter is unrecoverable from the turn-level transcript.

1.4 WHEN a barge-in trigger fires while the assistant is speaking THEN the system attributes it to caller speech and stops playback without recording whether the triggering near-end audio was caller speech or residual echo of the assistant's own outbound audio.

1.5 WHEN residual echo of the assistant's own speech is carried into the model input path THEN the system accepts the resulting transcription as a genuine caller turn (for example the assistant's "আমি দুঃখিত" surfacing as the caller turn "I'm sorry. I'm sorry."), prompting a fresh apology-prefixed response and sustaining the loop.

1.6 WHEN playback is truncated after an abnormally short duration THEN the system records the truncation with the same severity and detail as a normal caller interruption, emitting no anomaly signal that would distinguish a legitimate barge-in from a spurious one.

1.7 WHEN streaming audio chunks and output transcription deltas arrive from the model THEN the system accumulates them into buffers without recording per-delta arrival time, byte count or text boundary, so the log cannot discriminate duplicated or overlapping upstream deltas from downstream re-delivery of already-played audio.

1.8 WHEN a call session is established THEN the system does not record the model identifier serving that call, so an affected recording cannot be attributed to a specific model version.

### Expected Behavior (Correct)

What should happen instead. Each clause corresponds to the same-numbered defect above.

2.1 WHEN the assistant begins playing an utterance whose leading word starts with a Bengali consonant + matra + visarga grapheme cluster AND a barge-in trigger fires within the first few hundred milliseconds of playback THEN the system SHALL require that trigger to be corroborated as genuine caller speech before truncating playback, so playback is never cut inside a grapheme cluster by a spurious trigger.

2.2 WHEN a turn has been truncated and the model regenerates a turn opening with the same lexical prefix THEN the system SHALL suppress re-delivery of the leading fragment that was already played, so the caller hears any given partial grapheme at most once.

2.3 WHEN the assistant speaks a Bengali utterance containing a consonant + matra + visarga cluster THEN the system SHALL deliver that cluster as one continuous, single-pass grapheme with no repetition of its leading portion.

2.4 WHEN a barge-in trigger fires while the assistant is speaking THEN the system SHALL record the evidence behind the decision — including the near-end speech confidence, the sustained-onset frame count, and an indication of whether the near-end energy correlates with the audio currently being played out — so the trigger source is attributable after the fact.

2.5 WHEN near-end audio is residual echo of the assistant's own playback THEN the system SHALL NOT admit it into the model input path as caller speech, and SHALL NOT allow it to open a caller activity window or to produce a caller turn.

2.6 WHEN playback is truncated after an abnormally short duration THEN the system SHALL log it as an anomalous truncation, including the delivered playback duration, the leading text fragment, and the trigger source, so the defect is detectable from logs without listening to a recording.

2.7 WHEN streaming audio chunks and output transcription deltas arrive from the model THEN the system SHALL record per-delta arrival time, byte count and text boundary at a diagnostic log level, sufficient to prove whether repeated content originated upstream in the model stream or downstream in local buffering and playback.

2.8 WHEN a call session is established or re-established THEN the system SHALL record the model identifier and, on reconnect, the attempt count, so any affected call can be attributed to a model version.

2.9 WHEN the fix is verified THEN the system SHALL be shown to deliver clean single-pass audio for at least one Bengali consonant + matra + visarga cluster other than দুঃ — for example নিঃ in নিঃশব্দ, or অন্তঃ / পুনঃ — so the bug class is demonstrated closed rather than the single reported word.

### Unchanged Behavior (Regression Prevention)

Existing behavior that must be preserved.

3.1 WHEN the caller genuinely speaks over the assistant with sustained speech THEN the system SHALL CONTINUE TO barge in, stop playback, clear the outbound audio buffer and open a caller activity window promptly.

3.2 WHEN the assistant speaks Bengali text containing no consonant + matra + visarga cluster THEN the system SHALL CONTINUE TO render the audio exactly as it does today.

3.3 WHEN the assistant speaks in English or Hindi THEN the system SHALL CONTINUE TO render the audio exactly as it does today.

3.4 WHEN a turn completes with no barge-in THEN the system SHALL CONTINUE TO deliver the full utterance, log the turn-level transcript and report the calculated playback duration as it does today.

3.5 WHEN the assistant is interrupted mid-turn THEN the system SHALL CONTINUE TO log the interrupted partial transcript and the interruption timestamp in the existing format.

3.6 WHEN the model session drops mid-call THEN the system SHALL CONTINUE TO reconnect using the existing session resumption handle and retry limit, and SHALL CONTINUE TO restore conversational context on resumption.

3.7 WHEN a tool call is in progress THEN the system SHALL CONTINUE TO defer caller activity signalling and flush buffered caller audio on completion exactly as it does today.

3.8 WHEN the caller is silent THEN the system SHALL CONTINUE TO apply the existing silence follow-up and end-of-turn detection behavior.

3.9 WHEN audio is exchanged with the telephony provider THEN the system SHALL CONTINUE TO use μ-law 8 kHz framing in both directions with the existing 20 ms outbound pacing, and SHALL CONTINUE TO produce no added end-to-end latency beyond the current baseline.

3.10 WHEN echo cancellation runs THEN the system SHALL CONTINUE TO use the existing echo canceller module unmodified, preserving its current placement ahead of noise suppression in the inbound chain.

3.11 WHEN a call is recorded for debugging THEN the system SHALL CONTINUE TO write the existing debug recording and end-of-call statistics unchanged.

## Accepted Risks

Risks accepted knowingly, recorded here rather than resolved.

### AR-1: The defect has not been reproduced on the current model configuration

The affected call ran on `gemini-3.1-flash-live-preview` with `thinking_level="low"`. The code now runs `gemini-3.8-live` with no thinking config. No occurrence of the defect has been observed or reproduced under the current configuration.

**What is confirmed, by static inspection.** The implicated code path is model-independent. `GEMINI_MODEL` is referenced only at its definition (line 46) and at session connect (line 1113); there is no model-conditional branching in the AEC far-end handling, the `clearAudio` path, the while-speaking VAD gate, the preroll capture/flush, or the outbound pacing. The structural defect — the far-end reference being timestamped at send rather than at playout, and `reset_far_end()` never being called on `clearAudio` — exists identically regardless of which model string is used.

**What is not confirmed.** That the defect manifests at the same *rate or severity* on `gemini-3.8-live`. The trigger depends on the acoustic character, timing and turn structure of the model's audio output, all of which may differ between 3.1 and 3.8. The removal of `thinking_level="low"` also changes turn-generation latency, which affects how much audio is in flight when a spurious trigger lands.

**Consequence if the risk materialises** (the defect does not occur on 3.8): concerns (a) and (b) would be hardening against a condition no longer triggered in practice, while concerns (c) and (d) remain valuable regardless. The fix would be over-engineered but not harmful, since every non-bug input path is specified to be behaviourally identical.

**Accepted by the operator** on the basis that the underlying code defect is real and model-independent even if its observable rate differs.

**Mitigation.** The model-identity logging (requirement 2.8) and anomalous-truncation logging (requirement 2.6) make the next occurrence on 3.8 immediately attributable, converting this from an open risk into a measurement.

### AR-2: The echo-trigger mechanism is inferred, not measured

No existing log records barge-in trigger provenance, so "residual echo tripped the gate" is an inference — drawn from the timing distribution of the short triggers and from the assistant's own words appearing in the log as caller turns. It is not a direct measurement.

**Mitigation and gate.** The offline provenance analysis is a hard gate on implementation (tasks.md task 1). If it refutes the hypothesis, concerns (a) and (b) are re-designed before any code is written; concerns (c) and (d) survive unchanged.

## Out of Scope

**The `play_disclaimer` far-end reference gap is explicitly excluded from this bugfix**, at the operator's instruction, and is tracked as a separate ticket.

The gap, stated factually so the separate ticket has context: `play_disclaimer` (`app.py` ~744–777) sends `playAudio` frames directly to Plivo but never calls `aec.add_far_end()`, so disclaimer audio is never entered into the echo canceller's reference. Inbound audio is separately discarded while `playing_disclaimer` is set (`app.py` ~1231), which is why the gap has not produced a visible failure.

This bugfix neither fixes nor depends on it. No task in this spec may modify `play_disclaimer`.
