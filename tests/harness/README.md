# Test harness — seam decisions

Run the suite with:

```
venv/bin/python -m unittest discover -s tests -v
```

## No production code was modified

`app.py`, `aec.py`, `requirements.txt` and `.env` are untouched by this pass. No
refactor — not even a behaviour-identical extraction — proved necessary, because
the three coroutines that carry the audio path are **already module-level
functions**, not nested inside `handle_media_stream`:

| coroutine | app.py line | driven by |
|---|---|---|
| `stream_plivo_to_gemini(plivo_ws, session, call_state)` | 1206 | `FakePlivoWS` + `FakeSession` via `InboundDriver` |
| `stream_gemini_to_plivo(session, plivo_ws, call_state, plivo_client)` | 1465 | `FakeSession` script via `run_gemini_output` |
| `send_plivo_audio(plivo_ws, call_state, session, plivo_client)` | 1918 | `SenderHarness` + `FakeClock` |
| `execute_pending_terminal_action(...)` | 1823 | called from the sender |
| `silence_watchdog(session, call_state, delay)` | 324 | called directly |
| `log_call_stats(call_state)` | 723 | called directly |
| `ulaw_to_pcm`, `pcm_to_ulaw`, `calculate_rms_db`, `format_digits`, `get_indian_time` | 214–350 | pure, called directly |

Every golden record in this suite is produced by running that production code.

## What is not reachable offline, and why

`handle_media_stream` itself (app.py 781–1204) is a Quart websocket route. It
reads `websocket.args`, constructs `genai.Client`, and drives the reconnect loop
through `client.aio.live.connect`. There is no seam between the reconnect `for`
loop and the transport, so the following are recorded as gaps rather than tested:

* `MAX_GEMINI_RECONNECTS` retry accounting and context restoration on resumption
  (task 4.8). Offline coverage stops at `GeminiSessionDisconnected` being raised
  correctly by `stream_gemini_to_plivo`. What would make it testable: extracting
  the per-attempt body into a module-level coroutine taking
  `(client, config, call_state)` — a pure move, but out of scope for this pass.
* The `call_state` dict is a literal inside that function, so `appctl.new_call_state`
  mirrors it. `tests/test_harness_smoke.py` scrapes the key names out of app.py's
  source and asserts the mirror has not drifted.

## Containment of `import app`

Importing `app` has module-scope side effects. `appctl` contains them **before**
the import:

1. A `NullHandler` is added to the root logger, which makes `logging.basicConfig`
   in app.py a no-op — otherwise every test run would append to the production
   `Gemini_Assistant.log`, the same log task 1's measurement was derived from.
2. `TRANSFER_CONTEXT_DB_PATH` is repointed at a temp file, because
   `initialize_transfer_context_store()` runs at import and issues a DELETE.
   `.env` is not modified; `load_dotenv()` does not override variables already in
   `os.environ`.

## The clock

`FakeClock.install()` rebinds `app.time` and `app.datetime`. It deliberately does
not patch the event loop clock: `asyncio.wait_for` reads `loop.time()`, and
replacing that means replacing the loop. Consequence — the 100 ms
`PLIVO_SEND_POLL_TIMEOUT` still costs 100 ms of real time per idle poll in
`send_plivo_audio`. Everything app.py *observes* is virtual, so branch selection
and every logged timestamp are deterministic.

## Cost budget

RNNoise dominates the inbound path at **19.2 ms of CPU per 20 ms audio frame**
(measured: 143 frames in 2.741 s). That is why falsifiers that drive the inbound
DSP chain run tens of cases rather than hundreds, and why the 500-case
falsifiers target the outbound path, which does no DSP. Each reduction is stated
in the test that makes it.
