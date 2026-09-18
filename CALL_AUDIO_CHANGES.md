Live-call fixes — 18 September 2026
===================================

`app_aec1.py` now uses `call_runtime.py` for paced playback, streaming output resampling, and response ownership. `aec.py` clears abandoned reference history while preserving learned filter weights. The SDK is now pinned to `google-genai==2.24.0` with `google-auth==2.56.0` in both requirements files, to preserve the interaction status used by Gemini Extended Thinking. These versions were tested in isolation before installation into the project virtual environment.

Behavior
--------

- One Plivo reader stays active through startup and Gemini reconnects. Caller audio during the disclaimer and greeting is discarded, including partial frames crossing the greeting boundary. Listening opens only after Plivo acknowledges greeting playback.
- A single sender paces 20 ms audio frames on a monotonic clock and feeds the AEC reference as frames are sent. Clear requests use `streamId`; interrupted local audio, checkpoints, and references are invalidated together. New playback waits for `clearedAudio`.
- Final partial output frames are padded. Unique checkpoints and `playedStream` acknowledgements determine playback completion and terminal-action readiness. Missing acknowledgements or blocked sends have bounded timeouts; the call closes rather than pretending playback succeeded.
- Root call tasks, per-session tasks, and silence timers are cancelled and awaited on exit. The Gemini client is closed during cleanup.
- Transient connection/send/receive failures retry with bounded backoff. GoAway allows an idle handover before its deadline. Idle sessions can resume; interrupted turns or rejected handles use a fresh session with recent confirmed transcript context and ask the caller to repeat. Accepted hangup/transfer actions drain their final playback instead of reconnecting.
- Response deadlines belong to a session and turn. Interrupted turns remain tracked until their completion boundary, so late completion cannot clear a newer deadline. An empty completed response gets one recovery prompt; a second empty response ends the call.
- Intermediate Gemini completions marked `IN_PROGRESS` retain response ownership, resampler history, and the stalled-response timer. They do not finish playback or trigger an empty-response retry. An `IDLE` completion finalizes the response; classic models without this status keep their existing completion behavior. Late boundaries for interrupted turns cannot rearm a newer turn's deadline.
- Manual VAD requires both RNNoise confidence and pre-AGC energy above an adaptive noise floor. Idle speech starts after 80 ms of qualifying frames; interruption requires 240 ms at a stricter confidence and energy threshold. A 320 ms preroll preserves the beginning of confirmed speech. Short noise bursts and quiet high-confidence noise cannot clear playback. Once speech starts, lower continuation thresholds and 400 ms of silence avoid cutting a sentence at short pauses. AGC no longer increases gain for frames that lack speech evidence.
- The limiter is symmetric. Output uses a stateful 97-tap low-pass FIR before 24 kHz → 8 kHz decimation, with approximately 2 ms group delay and a flushed filter tail.
- The SSE response has no application timeout and sends a heartbeat every 15 seconds.

Demo settings
-------------

Manual VAD remains active. Plivo noise cancellation remains enabled at its existing level; RNNoise supplies speech probabilities without sending its denoised waveform to Gemini. Both Plivo call recording and local input WAV recording are disabled. Existing `HYBRID_VAD` and `RECORD_GEMINI_INPUT` environment flags do not enable those features in this version. The existing disclaimer asset and audio content are preserved. Authentication and the user-selected `gemini-3.8-live-extended-thinking` default with medium thinking were retained. No additional waveform denoiser or hybrid VAD was enabled. The legacy `RECORD_GEMINI_INPUT` constant is unused; no recording path is invoked.

| Setting | Default | Purpose |
| --- | --- | --- |
| `VAD_ONSET_MS` | 80 | Speech confirmation when playback is idle |
| `VAD_BARGE_IN_MS` | 240 | Speech confirmation during playback |
| `VAD_THRESHOLD` | 0.75 | RNNoise onset confidence while idle |
| `VAD_THRESHOLD_WHILE_SPEAKING` | 0.92 | RNNoise onset confidence during playback |
| `VAD_MIN_RMS_DB` | -50 | Minimum pre-AGC onset level while idle, in dBFS |
| `VAD_BARGE_MIN_RMS_DB` | -42 | Minimum pre-AGC onset level during playback, in dBFS |
| `VAD_SILENCE_OFFSET_MS` | 400 | Silence required to end an established user turn |
| `PLAYBACK_ACK_TIMEOUT_SECONDS` | 5 | Playback/clear acknowledgement and WebSocket-send timeout |
| `MODEL_RESPONSE_TIMEOUT_SECONDS` | 30 | Response or stalled-generation timeout |
| `MAX_GEMINI_RECONNECTS` | 5 | Maximum reconnects after the first connection attempt |

The gate also requires 6 dB above the learned noise floor while idle and 10 dB during playback. These defaults add 160 ms of interruption confirmation and 200 ms of endpoint silence compared with the previous settings. This trades some response speed for fewer accidental interruptions and sentence splits. Nearby human speech can still qualify; the gate cannot identify the intended caller. Existing environment overrides still apply.

The input queue is capped at 500 ms; stale/overflowing input triggers recovery rather than replaying a growing backlog. Unsent outbound audio is capped at 60 seconds.

Validation
----------

```bash
PYTHONDONTWRITEBYTECODE=1 venv/bin/python -m unittest discover -s tests -v
```

The 45-test regression suite covers transient and steady noise rejection, sustained interruptions with preroll, short greeting replies, pause hysteresis, multi-segment and empty intermediate completions, SDK status decoding, DSP, paced echo references, clear races, partial output frames, missing acknowledgements, greeting muting, late turn events, empty responses, recoverable/permanent failures, GoAway, bounded retries, terminal actions, SSE, and the actual Quart WebSocket handler using mocked providers. Tests isolate credentials and file writes; they do not call Plivo or Gemini.

The installed RNNoise path also processed synthetic audio successfully. Synthetic filter checks preserved 1 kHz content and attenuated 6 kHz before decimation. These checks do not establish phone-call recognition accuracy or end-to-end latency.

Restart the running server with the updated virtual environment before testing (`venv/bin/python app_aec1.py`). The server was not restarted and no live calls were placed during these changes. Before the demo, validate earpiece calls in quiet and with background noise, deliberate interruptions, early greeting answers, English/Hindi/Bengali replies, pause handling, complete goodbye playback, and successful/failed human transfer. Logs now identify user activity end, first Gemini audio, first Plivo send, and confirmed playback with call/stream and turn ownership. VAD decisions include confidence, pre-AGC level, learned noise floor, required level, and rejected candidate count. Clear requests include their cause, and send gaps above 60 ms are logged. These distinguish local false interruption from a gap in model output or transport pacing. First-send timestamps are not measurements of when the caller actually hears audio.

The earlier `DEMO_READINESS_REVIEW.md` is the pre-change audit; its line numbers and defect descriptions describe the previous implementation.
