# Audio Pipeline Redesign — post-measurement

Supersedes the PARKED concern (a)/(b) sections in `design.md`. Those were written
before we had a real call with the far-end recorded. Two reproduction calls
(2026-09-22, 16:32 and 17:05) gave measured ground truth, and it contradicts the
original design in three specific places. This note records the corrected design
and drives the implementation. It is grounded, not aspirational: "perfect"
cancellation is not a target on an 8 kHz lossy channel talking to a black-box
model — robust-and-measured is.

## Deliverables (operator-agreed)

1. Echo no longer trips false barge-ins.
2. Genuine interruptions still cut in fast (responsiveness preserved).
3. The agent stops talking to its own echo (no garbage caller turns).
4. Robust on speakerphone, not just handset.

## What the measurements proved

From the affected calls, with the far-end signal recorded for the first time:

- **The leak is at ≈0 lag.** The assistant's outbound audio appears in the
  inbound stream near-instantly, not after an acoustic round trip (>150 ms on a
  mobile leg). Measured best-match lag clustered tightly once the far-end
  recording was wall-clock continuous. → the correlation search must be centred
  on 0, not on an acoustic-delay window. (Original design searched the wrong
  place and would have missed the echo entirely.)
- **Envelope correlation is the right discriminator; waveform is useless.**
  Envelope-domain correlation was 0.976–0.993 across every trigger; waveform
  correlation flipped sign per trigger (−0.47 … +0.58) because RNNoise/AEC
  phase-distort the residual. → correlate energy envelopes, never raw samples.
- **Amplitude, not presence, separates echo from speech.** The assistant's
  leaked audio is present at the onset of *every* turn, including turns nobody
  interrupted. Only its level differs. → a presence test cannot work; the gate
  keys on correlation strength above a threshold.
- **`ECHO_CORR_THRESHOLD ≈ 0.85–0.9`** cleanly separates the echo triggers
  (0.98+) from the little genuine speech in the data. Starting value 0.88;
  final value is a live-tune (Phase 5).
- **`buffer_lead_ms` climbs 208 → 558 ms** across a run of triggers, exceeding
  the canceller's own 480 ms modelled window (`_PFDKF` N·M = 3840 samples at
  8 kHz). This is the far-end reference being fed at send-time while Plivo plays
  at real time — the deepest root cause, and it is in how `app.py` drives the
  AEC, not in `aec.py`.

## Root-cause model (two coupled defects)

**D1 — far-end reference misalignment (degrades cancellation at the source).**
`emit_chunk` calls `aec.add_far_end(ulaw_to_pcm(chunk))` the instant a frame is
handed to Plivo. The send loop has no pacing, so a whole turn's frames are dumped
into the AEC's far-end FIFO far ahead of when the caller actually hears them.
When the echo of what is *currently* playing arrives at the mic, the AEC's
reference pointer is hundreds of ms ahead of it, past its modelled window →
cancellation is poor → residual echo is large enough to cross the VAD gate.

**D2 — the barge-in gate cannot tell echo from speech.** The gate asks only
"is there speech-like energy above threshold for N frames?". Residual echo (from
D1) answers yes. So playback is truncated (the stutter), and the echo-bearing
preroll is flushed to Gemini as a caller turn (the garbage turns), which makes
the model regenerate the same prefix → loop.

D1 makes the residual large; D2 acts on it. Fixing D1 shrinks the residual at
the source; fixing D2 rejects whatever residual remains. Both are needed: D1
alone leaves a thinner but real leak, D2 alone leaves the AEC degraded and leans
entirely on the correlation threshold. Do both.

## The design

### Part 1 — far-end reference alignment (fixes D1)

Feed the AEC far-end reference (and a parallel *shadow* buffer for the
correlation gate) on a **playout-paced schedule** instead of at send time, so the
reference tracks what the caller is actually hearing.

Decision: **pace the reference feed, not the Plivo send.** Keep the Plivo `send`
loop exactly as it is (fast drain — the preservation tests pin this, and Plivo's
own jitter buffer plays at real time regardless of how fast we hand it frames).
Introduce a small **playout model** in `send_plivo_audio`: each emitted frame is
timestamped, and `add_far_end` + shadow-append happen against a monotonic
playout clock advanced 20 ms per frame from the moment the utterance started
playing. Concretely, the reference for a frame is committed when
`now >= utterance_start + frame_index * 20 ms`, not when the frame is sent.

Why not pace the actual send: pacing the send changes outbound timing that the
commit gate and ~9 preservation tests are built on, for no cancellation benefit
(Plivo buffers either way). Pacing only the *reference feed* is the surgical
change that fixes alignment without touching what the caller hears.

On `clearAudio` / barge-in: the frames dumped-but-not-yet-played are discarded by
Plivo, so the playout model resets and `aec.reset_far_end()` fires (already done,
task 5.8). The shadow buffer clears in lockstep.

`aec.py`: still not modified. The reset/add API is sufficient. Re-open only if
live tuning shows the modelled window (N=24) is too short for the residual path,
which the data does not currently suggest.

### Part 2 — corroborated barge-in gate (concern a, fixes D2 truncation)

In `stream_plivo_to_gemini`, when the while-speaking VAD gate is about to fire
(`speech_started and assistant_speaking`), corroborate before truncating:

1. Take the recent near-end envelope (the post-processing `clean_pcm_16k` window
   the VAD just judged — proven to carry the residual at 0.98 correlation).
2. Take the aligned far-end shadow window for the same wall-clock interval.
3. `echo_correlation = max normalised envelope cross-correlation over a small
   lag search centred on 0` (± a few frames, not ± acoustic-delay).
4. If far-end was **inactive** in that window → not echo, real speech → barge in
   now, at today's latency (no change to genuine barge-in).
5. If far-end active and `echo_correlation >= ECHO_CORR_THRESHOLD` → it is echo →
   **do not truncate**, do not open activity, log a suppressed decision.
6. If far-end active and correlation below threshold → genuine double-talk →
   barge in.

Responsiveness (deliverable 2): the corroboration is a cheap envelope correlation
over a ~200 ms window, computed only at the moment of a candidate onset while the
assistant is speaking — not per frame, not when the assistant is silent. Genuine
interruptions (far-end inactive, or uncorrelated) take the existing path at the
existing 80 ms latency. Only the echo case is newly suppressed.

### Part 3 — echo not admitted upstream (concern b, fixes D2 loop)

The preroll flush to Gemini (`activityStart` + preroll bytes) currently happens
unconditionally on barge-in. Gate it on the same corroboration: if the trigger
was classified echo, the preroll is **not** flushed and no `activityStart` is
sent. The model never receives its own echo as a caller turn, so it never
regenerates the apology prefix → the loop cannot close. Genuine speech flushes
the preroll exactly as today.

### Part 4 — VAD, left as-is for now

Manual VAD (RNNoise) with local barge-in is kept — correct for a telephony agent
that must clean audio before the model sees it and control barge-in latency
locally. `automatic_activity_detection` stays disabled. With D1 fixed and the
correlation gate rejecting echo, the while-speaking threshold (0.82 / 4 frames)
can potentially be relaxed toward the normal path for snappier genuine barge-in —
but that is a Phase 5 tuning decision, made against live data, not now.

## Affected code

- `app.py` `send_plivo_audio` — playout model + shadow buffer feed (Part 1).
- `app.py` `stream_plivo_to_gemini` — corroborated gate at the barge-in point
  (Part 2) and the preroll/activity flush gate (Part 3).
- `bargein.py` (new) — pure functions: `envelope`, `envelope_correlation`,
  `lag_search`, `far_end_active`, `should_barge_in`. Pure so they unit-test
  against the on-disk recordings with no phone call.
- `app.py` constants — define `ECHO_CORR_THRESHOLD` (0.88), `ECHO_CORR_WINDOW_MS`
  (200), `ECHO_LAG_SEARCH_MS` (±40), `FAR_END_ACTIVE_FLOOR_DB` (−60).
- Tests — `bargein` unit tests; Case A/B/loop become the acceptance targets;
  all `test_preservation_4_*` must still pass.
- `aec.py` — unchanged unless live tuning proves otherwise.

## Correctness properties (acceptance)

- **P1 (echo suppressed):** for a while-speaking onset whose near-end envelope
  correlates with the aligned active far-end above threshold, the system does not
  truncate, does not `clearAudio`, does not `activityStart`, and logs a suppressed
  decision with the measured correlation. (Case A + B go green.)
- **P2 (genuine barge-in preserved):** for an onset with far-end inactive or
  near-end uncorrelated, behaviour is byte-for-byte today's — same 80 ms latency,
  same clearAudio/activityStart/preroll flush ordering. (Preservation tests stay
  green.)
- **P3 (no self-talk):** echo-classified triggers never reach the model input;
  no garbage caller turn is produced. (Concern b.)

## What still needs a live call (Phase 5, cannot be done offline)

- Final `ECHO_CORR_THRESHOLD` value (start 0.88).
- Confirming D1 alignment improved real cancellation (buffer_lead bounded,
  residual smaller) on a live acoustic path.
- That genuine barge-in still *feels* responsive to a human, not just in frames.
- Whether the while-speaking VAD threshold can be relaxed post-fix.
