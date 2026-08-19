# Backend Lifecycle Changes Report

Date: 2026-08-18

## Scope

This report documents the backend reliability changes made to the Senco voice agent after reviewing the Plivo, Gemini Live, audio-stream, call-ending, and human-transfer paths.

## Modified Files

- `app.py`
- `prompt.txt`
- `.gitignore`

## Implemented Changes

### 1. Reliable End Call and Transfer Actions

The previous implementation required Gemini to send new audio after invoking `endCall` or `transferCall`. If Gemini had already spoken its farewell or transfer message before invoking the tool, the call could remain open indefinitely.

The new flow:

- Waits for any queued Plivo audio to finish.
- Uses the normal model turn-complete state when available.
- Uses a terminal-action timeout as a fallback.
- Hangs up or transfers without requiring a second post-tool audio response.
- Terminates the call if a transfer operation fails.

## 2. Plivo and Gemini Failure Handling

The Plivo `start` event is now read before attempting the Gemini Live connection. This captures the Plivo call UUID early enough to explicitly terminate the phone call if Gemini setup fails.

The application now:

- Times out while waiting for Plivo to send `start`.
- Times out while establishing the Gemini Live connection.
- Hangs up the Plivo call when Gemini initialization or the runtime lifecycle fails.
- Handles Gemini Live policy-close errors by ending the Plivo call.

## 3. Coordinated Async Task Shutdown

The call previously waited on `asyncio.gather()` even after the Plivo stream stopped, which could leave the Gemini receiver and audio sender running.

The updated lifecycle:

- Waits for the first call task to finish.
- Sets the shared termination flag.
- Cancels the remaining tasks.
- Awaits cancelled tasks to avoid orphaned coroutines.

## 4. Bounded Call and Silence Behavior

The following limits are now configurable through environment variables:

| Variable | Default | Purpose |
| --- | ---: | --- |
| `PLIVO_START_TIMEOUT_SECONDS` | `10` | Maximum wait for the Plivo WebSocket start event. |
| `GEMINI_CONNECT_TIMEOUT_SECONDS` | `20` | Maximum wait for Gemini Live connection setup. |
| `MODEL_RESPONSE_TIMEOUT_SECONDS` | `30` | Maximum wait for Gemini after a user turn. |
| `MAX_CALL_DURATION_SECONDS` | `900` | Maximum total call length. |
| `SILENCE_FOLLOWUP_SECONDS` | `8` | Delay before a silence re-engagement prompt. |
| `MAX_SILENCE_FOLLOWUPS` | `2` | Maximum automatic re-engagement attempts. |
| `TERMINAL_ACTION_TIMEOUT_SECONDS` | `8` | Fallback wait before executing end/transfer actions. |

After the silence limit or response timeout is reached, the call is terminated instead of continuing indefinitely.

## 5. Nonblocking Outbound Call Creation

`plivo_client.calls.create()` now runs in a worker thread with `asyncio.to_thread()`. This prevents a slow outbound-call API request from pausing audio processing for active calls on the Quart event loop.

## 6. Persistent Human Transfer Context

Human-transfer summaries are now stored in a local SQLite database rather than a process-memory dictionary.

- Default database path: `./transfer_context.sqlite3`
- Default retention: `86400` seconds
- Optional configuration:
  - `TRANSFER_CONTEXT_DB_PATH`
  - `TRANSFER_CONTEXT_TTL_SECONDS`

The transfer callback now passes only a transfer context ID to the replacement WebSocket connection. This avoids placing the full call summary in the URL and preserves the context across application restarts on the same host.

The re-entry URL now consistently uses `phone_number`, fixing the previous parameter mismatch that caused returning callers to appear as `Unknown`.

## 7. End Call Tool Schema Alignment

The Gemini `endCall` tool now declares the required `summary_of_whole_call` field expected by the backend. The prompt was updated so Gemini is instructed to provide that summary when invoking the tool.

## Validation Performed

The following local checks passed:

- `app.py` Python syntax compilation.
- Git whitespace validation with `git diff --check`.
- Gemini end-call tool schema inspection.
- SQLite transfer-context save and load round trip.
- Plivo start-event preflight handling.
- Plivo stop-event coordinated cancellation.
- End-call fallback when no post-tool audio is produced.
- Gemini response-timeout hangup.
- Local route smoke checks for `/` and `/trigger-call`.
- Event-loop responsiveness while simulating a slow Plivo call-create request.

No real Plivo call or Gemini Live session was placed during validation.

## Remaining Production Recommendations

These were outside the requested implementation scope:

- Authenticate and rate-limit `/trigger-call`.
- Validate Plivo webhook signatures.
- Correct the cumulative token usage and cost calculation.
- Add the missing recording callback endpoint.
- Add automated integration tests for Plivo and Gemini Live using test doubles.