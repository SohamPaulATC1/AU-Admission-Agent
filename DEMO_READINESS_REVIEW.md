Review of `app_aec1.py` — 18 September 2026
==================================================

**Recommendation: fix the interruption, playback/AEC, greeting, task-cleanup, and access-control issues before the client demo.** The current implementation has useful building blocks, but several reproducible defects affect whether the caller is heard and whether playback stops correctly.

This review covers the current working file, including its existing uncommitted switch to `gemini-3.1-flash-live-preview`, plus `aec.py`, `prompt1.txt`, the dashboard, installed dependencies, playback WAV metadata, and existing logs. Application files were not changed. Offline probes used selected functions from the current source with fake transports; no live calls or paid Gemini requests were made.

The isolated reproduction harness is `/tmp/au_demo_review_checks.py`. It does not import/start the application or load API credentials. Run from the project directory with:

```bash
PYTHONDONTWRITEBYTECODE=1 venv/bin/python /tmp/au_demo_review_checks.py
```

Findings, in priority order
--------------------------

1. **[P1] Barge-in uses the wrong Plivo field name.**

   References: `app_aec1.py:1571`, `app_aec1.py:1864`, `app_aec1.py:1913`.

   All three `clearAudio` messages use `stream_id`. Plivo's current protocol requires `streamId`. The app logs that it cleared playback immediately after sending, without checking a `clearedAudio` acknowledgement. Draining the Python queue cannot remove audio already buffered at Plivo, so the assistant can keep talking over the caller.

   Reproduction: driving the actual input function with sustained speech produced `{"event": "clearAudio", "stream_id": "test-stream"}`.

   Fix: use `streamId`, consume the acknowledgement, and invalidate interrupted audio consistently across the local queue, sender buffer, and echo-reference state. Verify on an actual call that buffered speech stops.

2. **[P1] The echo reference is not synchronized with playback and loses audio during bursts.**

   References: `app_aec1.py:2131`, `app_aec1.py:2149`; `aec.py:142`, `aec.py:162`.

   The sender emits every available frame as fast as WebSocket sends complete. It also feeds each frame to the AEC immediately. Plivo plays buffered audio in real time, while the AEC consumes its reference according to inbound frames. The AEC FIFO retains only 64 frames, dropping the oldest reference samples when it fills.

   Reproduction: with a nonblocking fake transport, two seconds of audio were sent in about **4.4 ms**, leaving only **1.28 seconds** in the reference FIFO. This demonstrates the missing pacing/backlog control; the 4.4 ms figure is not a measured Plivo network speed.

   A caller may still be hearing the beginning of a response while the canceller has discarded that portion. Speakerphone echo can then survive, trigger VAD, and interrupt the agent. The current clear-audio paths also leave the AEC reference FIFO intact.

   Fix: either pace output against a monotonic playback schedule with a small bounded lead, or maintain an independent timestamped reference schedule that represents actual playback. Reset/invalidate references on clears and reconnects. Increasing the FIFO limit alone does not solve timing alignment. Validate with delayed echo and simultaneous caller/assistant speech.

3. **[P1] A caller who begins answering during the greeting can lose the entire utterance.**

   References: `app_aec1.py:1456`, `app_aec1.py:1475`, `app_aec1.py:2191`.

   VAD changes `is_speaking` before the `greeting_completed` check discards input. If onset occurs while the greeting gate is closed, the one-time `speech_started` event is discarded. When the gate opens, sustained speech does not produce another onset, so no Gemini activity opens. The caller must stop and start again.

   Reproduction: four speech frames before greeting completion, ten speech frames after completion, and ten silence frames resulted in **zero Gemini messages**.

   Fix: preserve the onset/audio across the greeting boundary, or explicitly open an activity when the gate opens and speech is already active. Decide whether greeting interruption is allowed, then test both early language answers and speech crossing the end of the greeting.

4. **[P1] Cancelling the WebSocket handler can orphan its call tasks.**

   References: `app_aec1.py:780`, `app_aec1.py:1271`, `app_aec1.py:1318`.

   `coordinate_call_tasks()` cancels sibling tasks only after `asyncio.wait()` returns. If its parent is cancelled while awaiting that wait, cleanup is skipped. The handler's outer `finally` does not cancel the four tasks either. They can retain sockets, continue supervision, or use a Gemini session that has already closed.

   Reproduction: cancelling the coordinator left **all four child tasks unfinished**.

   Fix: own all call tasks in a `try/finally`, always cancel unfinished tasks, and await every task during exit. A structured task group is another option, but preserve the current “one task exits, stop siblings” behavior explicitly.

5. **[P1 when publicly reachable] The dashboard exposes the call API credential; the media socket and event feed have no authentication.**

   References: `app_aec1.py:68`, `app_aec1.py:418`, `app_aec1.py:464`, `app_aec1.py:908`, `app_aec1.py:2237`; `index.html:979`.

   The public HTML contains `default-dev-token`, which is also the backend fallback credential. An unauthenticated request to the dashboard returned HTTP 200 with that token in the offline route check. With the fallback configuration, a visitor can submit outbound call requests. If a different backend token is configured, the dashboard's hardcoded token instead causes its requests to fail.

   Independently, `/call-events` exposes caller information/transcripts to unauthenticated subscribers. `/media-stream` accepts arbitrary clients and trusts their start payload before opening Gemini. The externally configured reverse proxy was not reviewed; these findings describe the application's own controls.

   Fix: require an authenticated operator session, remove the development fallback in deployed environments, and validate Plivo signatures or a properly bound short-lived stream credential before accepting media. Protect the event feed and enforce call limits. Do not place a replacement shared server secret in publicly served JavaScript.

6. **[P2] Playback completion is estimated locally, so terminal actions can run before remote playback finishes.**

   References: `app_aec1.py:2030`, `app_aec1.py:2175`, `app_aec1.py:2201`.

   `first_send_time + total_audio_duration` assumes continuous playback with no delivery or generation gaps. It does not establish what the caller has actually heard. The app uses that estimate for `assistant_speaking`, greeting completion, silence follow-ups, and hangup/transfer readiness. A later chunk after a stall can still be playing when the estimate says the response ended.

   Plivo provides `checkpoint` and `playedStream` for playback completion, but this app sends/handles neither. A final buffer shorter than 160 bytes also remains unsent: an isolated 159-byte response produced no transport messages.

   Fix: finish framing, pad/send the final partial frame, enqueue a uniquely identified checkpoint after the completed response, and base terminal actions on its acknowledgement with a bounded fallback. Distinguish a cleared/interrupted response from one that finished playing.

7. **[P2] The limiter distorts negative peaks, and output downsampling has no anti-alias filtering.**

   References: `app_aec1.py:1423`, `app_aec1.py:1905`.

   The limiter adds a positive saturation term to both positive and negative peaks. It is not symmetric and becomes non-monotonic on the negative branch: inputs `[-2, -1, 1, 2]` produced approximately `[-0.800, -0.824, 0.976, 1.000]`. This can distort louder speech after AGC.

   Separately, the default `audioop.ratecv` 24 kHz → 8 kHz path does not apply an appropriate anti-alias low-pass filter. In an offline tone test, 6 kHz became 2 kHz with an output/input RMS ratio of approximately 1.0. This can introduce audible artifacts in assistant speech; the tone test is not a speech-quality score.

   Fix the limiter's sign application:

   ```python
   np.sign(x) * (
       threshold
       + (1.0 - threshold)
       * np.tanh((np.abs(x) - threshold) / (1.0 - threshold))
   )
   ```

   Apply that expression only outside the threshold. Use a streaming resampler with anti-alias filtering for the output path, preserving filter state and accounting for its small buffering delay.

8. **[P2] Several recoverable disconnects bypass the reconnection loop.**

   References: `app_aec1.py:1246`, `app_aec1.py:1306`, `app_aec1.py:1989`.

   The retry loop catches only `GeminiSessionDisconnected`. The receive function converts selected 1000/1008 errors to that exception, but other failures, including a simulated 1011 internal error with a resumption handle, propagate as `APIError` and end the call. Connection-attempt errors also escape the retry loop.

   Resumption immediately cancels the Plivo reader/sender on `GoAway`, even though the message provides remaining connection time. Audio already removed into the sender's private buffer can be lost, while queued audio and playback flags survive into the new connection. With no resumption handle, the code opens a fresh session without restoring conversational context or sending a greeting/recovery prompt.

   Fix: classify transient failures separately from invalid configuration/authentication errors, bound retries across connection and receive failures, and explicitly define the audio/context policy during reconnect. Keep the Plivo receive lifecycle independent where practical. Test a disconnect during listening, speaking, and a terminal action.

9. **[P2] A completed model turn without audio can still lead to a 30-second timeout and hangup.**

   References: `app_aec1.py:735`, `app_aec1.py:1870`, `app_aec1.py:1886`.

   A response's `turn_complete` flag does not clear `awaiting_model` or its deadline. Only a model-turn payload or tool call does that. If Gemini finishes without audio, the supervisor still considers it unresponsive.

   Reproduction: receiving a response containing `turn_complete=True` with no model-turn payload left `awaiting_model=True` and the deadline active.

   Fix: handle a completed-but-empty response explicitly. Clear the completed request's deadline and use a bounded retry/re-engagement policy appropriate to the call. Associate response timers with the relevant turn so late interruption/completion messages cannot alter a newer request's state.

10. **[P2] The dashboard event stream inherits Quart's 60-second response timeout.**

    Reference: `app_aec1.py:418`.

    The SSE response never overrides its timeout or sends heartbeats. The installed Quart version uses a 60-second response timeout; the isolated response inherited it. EventSource may reconnect, but there is no replay, so status/transcript events can be lost between connections.

    Fix: set `response.timeout = None` for this streaming response, emit periodic heartbeat comments, and keep disconnect cleanup. Proxy timeouts must also be checked in the deployment.

Latency and recognition tuning
------------------------------

- **Use `thinking_level="minimal"` as the first measured comparison for this particular 3.1 model.** `app_aec1.py:1193` currently selects `"medium"`. Google's current capabilities reference says 3.1 supports minimal and defaults to it for lowest latency. Compare actual response latency and admissions-answer accuracy before choosing it for the demo; no speedup has been measured here. Do not transfer this configuration blindly to another Live model, whose supported settings can differ.
- **Do not reduce the 200 ms VAD offset without testing natural pauses.** The same probability threshold determines speech onset and ongoing speech. A short low-confidence region can close a turn, then a restart can interrupt the answer just requested. Test brief English/Hindi/Bengali replies, quiet speech, program acronyms, names, and hesitation within sentences. Separate onset/offset behavior and use audio duration rather than assuming every media message represents one 20 ms frame.
- **RNNoise is currently a VAD source, not the denoiser for model input.** The processed RNNoise waveform is discarded at `app_aec1.py:1400`; this avoids adding that particular suppression to the model audio. Plivo noise cancellation at level 85 is still enabled. Its effect on speech recognition needs an A/B call test.
- **The AGC “noise gate” does not mute noise.** Below the threshold, target gain becomes 1.0 rather than zero, and existing smoothed gain decays gradually. `AGC_MAX_GAIN_DB` is also not the value used by the gain clamp, which is hardcoded to 15 dB. Comments and configuration should reflect the actual signal path.
- **Measure caller-perceived latency.** Record a call ID and turn ID with monotonic timestamps for the last speech frame, activity end, first Gemini audio received, first audio sent to Plivo, and playback acknowledgement. The current “AI Speech Started playing” log is a local send timestamp, not confirmation that the caller heard it. Report median and p95, alongside missed utterances and false interruptions.
- The extra 8 kHz → 48 kHz → 16 kHz input resampling and synchronous debug WAV/log writes are optimization candidates after the correctness fixes. The existing measurements do not support treating these as the main cause of multi-second waits.

Additional functional gaps
--------------------------

- `HYBRID_VAD` defaults off. If enabled, the silence watchdog still checks `user_activity_open`, which that mode does not set. A simulated locally detected speaking state still allowed a silence follow-up. The deferred tool-completion path can also emit manual activity markers without checking the mode. Validate that mode separately before enabling it.
- The `<Record>` action points to `/recording-callback`, but the working file declares no matching route. Plivo recording callbacks cannot be persisted by this app.
- `save_lead()` uses only phone/name in its filename. Repeat calls overwrite earlier records. Transfer return also omits `user_name`, so a completed lead can be stored under `Unknown` rather than updating the original person's record.
- The prompt contains the entire static admissions knowledge base. This review checked its integration, not the university's approval of 2027 fees, eligibility, and admissions claims. Obtain a client-approved answer set and evaluate responses against it.
- The default assistant introduction waits for the 2.87-second prerecorded disclaimer to finish before requesting Gemini's greeting. This is a deliberate startup delay, separate from response latency during conversation.

Validation performed
--------------------

- Python syntax parsing passed for `app_aec1.py` and `aec.py`.
- Installed runtime inspected: Python 3.12.3, google-genai 1.66.0, Plivo 4.59.6, pyrnnoise 0.4.3, NumPy 2.2.6, Quart 0.20.0, Hypercorn 0.18.0.
- `recorded1.wav` is already 8 kHz, mono, 16-bit PCM; its current loader's format assumption matches that file.
- Offline reproductions covered burst output/AEC truncation, cancellation cleanup, greeting overlap, clear-audio payloads, partial output frames, hybrid silence handling, a 1011 disconnect, empty model turns, limiter symmetry, output aliasing, public dashboard content, and SSE timeout inheritance.
- Existing logs span 16–17 September and contain 20 call-stat endings, 6 response-timeout terminations, and 15 “Task exception was never retrieved” entries. These span previous revisions and potentially different model configurations; they are not a failure-rate estimate for the exact current file.
- Across 3,155 logged DSP timing samples, median was **2.88 ms**, p95 **3.87 ms**, and maximum **26.98 ms**. This timer excludes the preceding AEC step and samples only every 50 chunks; it is not total pipeline latency.

Minimum demo acceptance checks
------------------------------

| Scenario | Evidence to collect |
| --- | --- |
| Answer language question before greeting ends | Answer reaches Gemini once, without requiring repetition |
| Interrupt a long answer on handset and speakerphone | Plivo confirms clear; old response stays stopped; caller's full question is heard |
| Quiet speech, brief replies, pauses, and background noise in all three languages | Correct turn boundaries and accurate program/fee answers |
| Ask a fee question, correct a program, ask an unknown question | Matches the approved KB; accepts corrections; escalates unknown details |
| Goodbye and successful/failed human transfer | Closing audio is heard completely; call reaches the intended state |
| Disconnect/reconnect during listening and speaking | Bounded recovery without duplicate/stale audio or a silent abandoned call |
| Caller hangs up during setup, speech, and reconnect | All owned tasks and sessions close |
| Multiple simultaneous demo calls and dashboard open beyond 60 seconds | No cross-call state, excessive event-loop delay, lost operator status, or unauthorized access |

Primary references consulted on 18 September 2026
-------------------------------------------------

- Google Gemini Live capabilities — audio formats, activity detection, model-specific thinking settings, and function calling: `https://ai.google.dev/gemini-api/docs/live-guide`
- Google Gemini Live session management — session resumption and GoAway behavior: `https://ai.google.dev/gemini-api/docs/live-session`
- Plivo Audio Streaming Protocol Reference — `clearAudio`, `clearedAudio`, `checkpoint`, `playedStream`, and authentication guidance: `https://plivo.com/docs/voice-agents/audio-streaming/concepts/audio-streaming-reference`
- Plivo Stream XML — audio formats and noise-cancellation configuration: `https://plivo.com/docs/voice-agents/audio-streaming/xml/stream`
- Installed Google GenAI and Quart source were inspected for behavior specific to this environment.
