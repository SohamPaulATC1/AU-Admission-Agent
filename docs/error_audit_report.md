# Error audit report

**Audited:** branch `dev`, HEAD `0b0a912` ("Changes made to folder structure") **plus the uncommitted
working tree** (the tick boxes, the event-forward retry and the R&D cost trace, all deployed;
`git status` shows `app.py`, `campaigns.py`, `web_auth.py`, `web/campaigns.html`, `CLAUDE.md`, the
step 3 report and four test files modified or new). Date 2026-10-13. EC2 agent.

**What was done:** read-and-report only. No app code, test, prompt, config or database was changed.
Nothing was committed and no service was restarted. Throwaway reproductions ran from `/tmp/error_audit/`
against temp databases, fakes and the test client, and were deleted at the end. No PII appears below.

**Execution note:** the subagent type I used for steps 2-4 is not available in this session, so every
area was audited inline, one after another. The order was live call path, audio, background work,
web and data, pages, then startup.

## Evidence used

- **Suite:** 1139 run, FAILED (failures=5, skipped=4, expected failures=1): the 4 known failures
  plus `tests.test_cost_trace.TestSwitch.test_off_by_default_and_bad_values_switch_it_off` (finding E11).
- **journalctl** for the four real units: 2026-10-11 to 2026-10-13. **0 tracebacks.**
- **`Gemini_Assistant.log`** (2026-09-19 to 2026-10-13, 4 MB): 36 tracebacks, all from 2026-09-19 to
  09-25:
  - 12 × "Task exception was never retrieved" (`websockets.ConnectionClosedOK` inside the
    `stream_gemini_to_plivo` task);
  - 12 × `google.genai.errors.APIError` (1000) followed by `GeminiSessionDisconnected`.

  These predate the 2026-09-25 CancelledError / orphan-task fix and nothing like them has happened
  since, so they are not reported again. They do show that a resumable Gemini close happens on real
  calls, which is relevant to E1 and E2.
- **nginx access logs** (2026-10-01 to 10-13, 6,111 requests):
  - 0 responses with status 500;
  - 39 × 404 on `POST /recording-callback` from Plivo (E9);
  - 502s only during restarts and from scanners.
- **Database:** read-only queries that print counts only.

## Confirmed findings (most severe first)

### E1 — critical — Gemini close codes other than 1000 / 1008 hang up the call even with a resumption handle
- **Where:** `app.py:3463-3489`, `stream_gemini_to_plivo`, the `except Exception` block. It only
  turns an `APIError` into `GeminiSessionDisconnected` (reconnect) when `"1000"` or `"1008"` is in its
  text. Everything else is re-raised, reaches `handle_media_stream`'s outer `except Exception`
  (app.py:2609, "Live call lifecycle failed"), and the Plivo call is deleted.
- **Trigger:** the Live socket closes with 1001 (going away), 1006 (abnormal closure, i.e. a network
  drop; the SDK maps a close with no frame to 1006, `google/genai/live.py:538-545`), 1011 (server
  internal error) or 1013 (try again later).
- **Result:** the caller is hung up mid-conversation, although `session_resumption_handle` is
  present and the reconnect loop would have resumed the session.
- **Proof** (`r1_gemini_close_codes.py`; the real function with a fake session whose `receive()`
  raises the SDK's own error):
  ```
  1000 with resumption handle -> RECONNECT (GeminiSessionDisconnected)
  1001 with resumption handle -> call ends: APIError propagates to 'Live call lifecycle failed'
  1006 with resumption handle -> call ends: APIError propagates to 'Live call lifecycle failed'
  1008 with resumption handle -> RECONNECT (GeminiSessionDisconnected)
  1011 with resumption handle -> call ends: APIError propagates to 'Live call lifecycle failed'
  1013 with resumption handle -> call ends: APIError propagates to 'Live call lifecycle failed'
  ```
- **How often:** not in the logs since 2026-09-19, where only 1000 closes appear (12 of them). A 1006
  is any network blip between EC2 and Vertex; 1011 was seen on AI Studio in the text tests
  (CLAUDE.md, TEST6). Rare, but each one drops a live call.
- **Smallest fix:** classify on the error's code (`getattr(e, "code", None)`), not on a substring.
  For codes `{1000, 1001, 1006, 1011, 1013}`, raise `GeminiSessionDisconnected` when
  `terminate_session` is not set. Keep 1008 as it is.
- **Test:** parametrise R1 over the codes: 1001, 1006, 1011 and 1013 must raise
  `GeminiSessionDisconnected`.

### E2 — critical — a Gemini close noticed while *sending* ends the call instead of resuming it
- **Where:**
  - `app.py:3071-3075`: the `except Exception` of `stream_plivo_to_gemini` re-raises unchanged.
    Every `session.send_realtime_input(...)` in that loop (activityStart, audio, activityEnd)
    raises `websockets.ConnectionClosedOK` / `ConnectionClosedError` on a closed session.
  - `coordinate_call_tasks` (app.py:1906-1933) re-raises the first finished task's exception.
  - `silence_watchdog` (app.py:476-500) catches the same error and forces an end of call
    (`pending_end_call = True`, a deadline 1 s away).
- **Trigger:** Gemini closes the socket (for example the resumable 1000 close) while the caller is
  speaking (audio is being sent), or at the moment a silence follow-up is sent.
- **Result:** the `plivo_input` task fails first with `ConnectionClosedOK`, which is not a
  `GeminiSessionDisconnected`, so the reconnect loop never sees it and the call is hung up. From the
  watchdog, the call is ended on purpose about 1 s later.
- **Proof:**
  - `r2_send_on_closed_session.py` (the real `coordinate_call_tasks`): `RESULT: ConnectionClosedOK
    reaches handle_media_stream -> 'Live call lifecycle failed', call hung up`;
    `is ConnectionClosed a GeminiSessionDisconnected? False`.
  - Live log: `Silence watchdog failed: sent 1000 (OK); then received 1000 (OK)` 4 times,
    2026-09-22 to 09-25, each followed by the forced end.
- **How often:** every resumable Gemini close that lands during caller speech or a watchdog prompt.
  12 resumable closes appear in the September logs.
- **Smallest fix:**
  1. In `stream_plivo_to_gemini`'s handler and around the watchdog send, turn
     `websockets.ConnectionClosed` (and the same `APIError` codes as E1) into
     `GeminiSessionDisconnected`. In the watchdog, return without forcing an end.
  2. In `coordinate_call_tasks`, prefer raising `GeminiSessionDisconnected` when any finished task
     raised it.
- **Test:** R2 as a unittest expecting `GeminiSessionDisconnected`, plus a watchdog test whose
  session send raises `ConnectionClosedOK` and which asserts `pending_end_call` stays False.

### E3 — high — a failed reconnect attempt hangs up instead of trying again
- **Where:** `app.py:2524` / `2593`. The reconnect loop only catches `GeminiSessionDisconnected`.
  `connect_live_with_timeout` (app.py:1871-1878) raises `TimeoutError` when `live.connect()` or the
  Vertex token refresh takes over `GEMINI_CONNECT_TIMEOUT_SECONDS`, and `APIError` / `OSError` when
  the connect itself fails.
- **Trigger:** a resumption attempt (after a GoAway or a 1000 close) whose connect is slow or refused.
- **Result:** the error goes to the outer handler (app.py:2609) and the call is hung up after the
  first failed attempt, although `MAX_GEMINI_RECONNECTS` is 5.
- **Proof** (`r3_reconnect_connect_timeout.py`, the real `connect_live_with_timeout` with a slow fake
  client): `RESULT: TimeoutError -> not caught by except GeminiSessionDisconnected (app.py:2593);
  outer handler app.py:2609 hangs the call up`.
- **How often:** only on a reconnect that meets a slow or failed connect. Not in the logs, and it
  compounds E1/E2 after they are fixed.
- **Smallest fix:** when `is_reconnect`, also catch `(asyncio.TimeoutError, OSError,
  genai_errors.APIError)` from the `async with connect_live_with_timeout(...)` line, count it as a
  reconnect attempt and `continue`.
- **Test:** a handle_media_stream-level test with a client whose second connect times out once and
  then succeeds; the call must not be terminated.

### E4 — high — a transferred call's duration and connect time are its last leg only; status flips back to "ringing"
- **Where:** `app.py:1116` and `1119`, in `persist_call_event_sync`:
  - `call_ringing` sets `status='ringing'` on any row;
  - `call_connected` overwrites `connected_at`;
  - `call_ended` computes `duration_sec` from that last `connected_at`.

  The return leg after a human transfer runs `apply_plivo_start` (→ `call_ringing`) and its greeting
  (→ `call_connected`) again with the same `call_uuid`.
- **Result:**
  - the row goes ended → ringing → connected → ended;
  - `connected_at` becomes the second leg's;
  - `duration_sec` is the second leg's time after its greeting only;
  - `call_context` hands that duration to Haiku ("Call length: N seconds").
- **Proof:**
  - **Live row:** the only transferred call (`0a5cbf03`, `legs=2`) has `duration_sec=8`, while the
    answered span from the log was about 2.5 min.
  - **`r4_transfer_duration.py`** replays the same event sequence on a temp DB:
    ```
    t= 19s call_connected  -> status=connected connected_at=20:51:47
    t=128s call_ended      -> status=ended     duration_sec=109
    t=130s call_ringing    -> status=ringing   duration_sec=109
    t=166s call_connected  -> status=connected connected_at=20:54:14
    t=174s call_ended      -> status=ended     duration_sec=8      (true answered span 155 s)
    ```
- **How often:** every call with a transfer to a human.
- **Smallest fix:**
  - `call_connected`: `connected_at=COALESCE(connected_at, ?)`;
  - `call_ringing`: `... WHERE call_uuid=? AND status IN ('queued','ringing')`;
  - optional: let `call_ended` keep the status `ended` from a previous leg.
- **Test:** R4 as a unittest asserting `connected_at` stays at leg 1, `duration_sec` is 155 and the
  status never goes back to ringing.

### E5 — medium — `/api/call-history`: bad numbers give a 500 page, a database error is shown as "no calls", the cap can be bypassed
- **Where:** `app.py:1343-1344` and `1357`: `int()` on `limit`, `offset`, `start_hour` and `end_hour`
  runs outside the `try`. `app.py:1394-1396`: any database error becomes HTTP 200 with
  `{"calls": [], "total": 0}`. `web/index.html:2372` / `2395`: `res.json()` is called without
  checking `res.ok`, and errors only go to `console.error`.
- **Trigger:**
  - `?limit=abc`, `?offset=x` or `?start_hour=nine`;
  - `?limit=-1` (SQLite treats LIMIT -1 as "no limit");
  - a locked or missing database.
- **Result:**
  - the bad numbers get a 500 HTML page;
  - `limit=-1` returns every row past the 200 cap;
  - on a database error the calls page silently shows an empty history, so the operator can't tell
    "no calls" from "failed".
- **Proof** (`r9_web_inputs.py` and `r10_*.py`, test client and temp DB):
  ```
  /api/call-history?limit=abc     -> (500, 'text/html')
  /api/call-history?start_hour=nine -> (500, 'text/html')
  /api/call-history?offset=x      -> (500, 'text/html')
  limit=-1 returns 250 rows (cap is 200)
  /api/call-history with a locked DB -> (200, {'calls': [], 'has_more': False, 'total': 0})
  ```
- **How often:** the page itself sends valid integers, so the 500 needs a hand-edited URL. The
  silent-empty case happens on any database error during a page load.
- **Smallest fix:**
  - parse the four numbers in a `try` and answer 400 JSON;
  - clamp `limit` to 1..200 and `offset` to 0 or more;
  - on a database error answer 503 `{"status":"error","message":...}`;
  - in `fetchHistory`, check `res.ok` and show the message in the list.
- **Test:** test-client cases for the bad values (400), `limit=-1` (at most 200 rows) and a patched
  `sqlite3.connect` raising (503).

### E6 — medium — an SSE client that falls 200 events behind gets a stream that stays open and silent
- **Where:** `app.py:1265-1278`. On `QueueFull`, `emit_call_event_local` removes the subscriber
  from `call_event_subscribers`, but the `/call-events` generator (app.py:1309-1318) keeps waiting on
  that same queue.
- **Trigger:** a dashboard tab that doesn't read for about 200 events: a sleeping laptop, a
  throttled background tab, or a burst of bulk calls (each call emits dozens of events).
- **Result:** once it drains its backlog, the browser receives nothing more. The connection doesn't
  error, so EventSource never reconnects, and the live feed is frozen until a reload.
- **Proof** (`r11_sse_full_queue.py`, the real `emit_call_event_local`): `subscriber still
  registered: False | queued: 200` then `new event NOT delivered: the open stream is silent (no error,
  so EventSource never reconnects)`.
- **How often:** more likely with 5-8 concurrent bulk calls and a dashboard left open. Not observable
  in the logs (nothing is logged when it happens).
- **Smallest fix:** on `QueueFull`, end that subscriber's stream: put a sentinel the generator
  returns on (clear the queue first so it fits), or keep a `closed` flag the generator checks. Then
  EventSource reconnects and the page re-reads history.
- **Test:** R11 as a unittest: after an overflow, the generator finishes.

### E7 — medium — phone numbers and student summaries are still written to the log
- **Where:**
  - `app.py:1728` `apply_plivo_start`: `From Number: <full number>`;
  - `app.py:3105` `Arguments: {args_dict}`;
  - `3111` "End-call summary captured";
  - `3147` "Summary captured from Gemini";
  - `1680` "Retrieved summary for re-entry".

  The summaries hold names, marks and so on. Step 4 masked three other lines; these were missed.
- **Result:** PII in `Gemini_Assistant.log` and the journal. The log is unrotated (a plain
  `FileHandler`, 4 MB) and shared by the four processes.
- **Proof** (counts only): `From Number:` 60 lines, all with a full number, 11 of them on 2026-10-13
  after the masking deploy. `Arguments: {'summary_of_whole_call'` 20; "End-call summary captured" 20;
  "Summary captured" 7; "Retrieved summary" 5.
- **How often:** every call (the number); every endCall and transfer (the summaries).
- **Smallest fix:** `campaigns.masked(...)` on line 1728; log only argument *names* and lengths for
  tool calls; drop the summary text from the three summary lines (it is already stored in the
  database).
- **Test:** extend `tests/test_log_masking.py` with a start payload and an endCall tool call; assert
  no 10-digit number and no summary text in the captured lines.

### E8 — medium — one malformed setting in `.env` stops all four processes, the voice agent included
- **Where:** import-time `int()` / `float()` / switch checks:
  - `campaigns.py:32` and `39` (`CAMPAIGN_*`), plus the hours check;
  - `call_analysis.py:30` and `32`;
  - `app.py` `PRICE_*`, `GEMINI_CONNECT_TIMEOUT_SECONDS`, `PORT` (app.py:129) and others;
  - the `ANALYSIS_ENABLED` / `PLIVO_COST_ENABLED` switch checks.
- **Result:** `ValueError` at import, so systemd restarts every process each second and no call can
  be made. (The two switch checks are fail-fast on purpose, from the step 2 design.)
- **Proof:**
  - `r14_bad_env.py` (subprocess, a controlled environment, `.env` not read): `CAMPAIGN_MAX_CONCURRENT`,
    `CAMPAIGN_TICK_S`, `CAMPAIGN_HOURS_END`, `ANALYSIS_CONCURRENCY`, `NLP_PRICE_INPUT`,
    `PRICE_AUDIO_INPUT`, `GEMINI_CONNECT_TIMEOUT_SECONDS`, `ANALYSIS_ENABLED` and `PLIVO_COST_ENABLED`
    each give `import fails: ValueError`. `COST_TRACE_SECONDS` falls back as designed.
  - **Live:** on 2026-10-13 the stray `au-app@800x\xa0` units crash-looped (restart counter over 340)
    on `int(os.getenv("PORT"))` at app.py:129. That was a malformed value reaching the same code.
- **How often:** only when someone edits `.env` or `common.env` wrongly, but then for everything at once.
- **Smallest fix:** one helper (like `_cost_trace_seconds`) that logs a warning and uses the default
  for every optional numeric setting. Keep fail-fast only for the security ones.
- **Test:** the `r14` loop as a unittest over the optional keys, asserting the app starts.

### E9 — low — Plivo's recording callback hits a route that doesn't exist (404 on every answered call)
- **Where:** `app.py:1574`. `/outbound-webhook` returns `<Record action="{PUBLIC_BASE_URL}/recording-callback" ...>`,
  but there is no `/recording-callback` route (and no entry in `ENDPOINT_ACCESS`).
- **Result:** Plivo's POST with the recording URL gets a 404, so the recording link is never
  stored. The call itself is unaffected.
- **Proof:** nginx logged 39 × `404 POST /recording-callback`, user agent `PlivoProxy/1.0`, between
  2026-10-05 and 10-13 (11 on 10-13).
- **How often:** every answered call.
- **Smallest fix:** either add a `PLIVO` route that stores `RecordUrl` against the call (a new
  column) or logs it masked, or remove the `action` attribute if recordings are only read from the
  Plivo console.
- **Test:** `test_access_control` already pins `ENDPOINT_ACCESS == url_map`. Add one test that
  `/recording-callback` answers 200 for a signed Plivo POST.

### E10 — low — a call-list id beyond SQLite's integer range gives a 500 page
- **Where:** the `/api/campaigns/<int:campaign_id>` routes (app.py 4153-4215) and the `ids` list of
  `/select`. Python ints pass the route converter and the `isinstance(int)` check, then sqlite3
  raises `OverflowError`, which isn't a `CampaignError`.
- **Proof** (`r12_campaign_overflow.py`): `GET /api/campaigns/1180591620717411303424 -> (500,
  'text/html')`; `POST .../action` 500; `POST /select {"ids": [2**70]}` 500. The admin routes are
  fine (400 / 200).
- **How often:** only from a hand-crafted request; the pages never send it.
- **Smallest fix:** in the routes, treat `campaign_id > 2**63-1` as 404; in `/select`, require
  `0 < id < 2**63`.
- **Test:** the three requests expect 404 / 400.

### E11 — low — a cost-trace test depends on the server's `.env`
- **Where:** `tests/test_cost_trace.py:97-99`. The "off by default" case removes
  `COST_TRACE_SECONDS` from the subprocess environment, so `load_dotenv()` in app.py fills it from
  `.env`, which currently sets it for the operator's R&D run.
- **Proof:** suite failure `AssertionError: '60' != '0'`, the only failure beyond the 4 known ones.
- **Smallest fix:** set `env["COST_TRACE_SECONDS"] = ""` for the "missing" case instead of popping
  it. `load_dotenv` doesn't override a set variable, and an empty value already means off.
- **Test:** the test itself, run with the key present in `.env`.

## Unconfirmed (could not be proven here)

| # | What | Where | What would prove it |
|---|---|---|---|
| U1 | The calls page applies an event for a call that has no card to the **last** card (`activeCalls.get([...activeCalls.keys()].pop())`). After a reload during live or bulk calls, a new call's card can show another call's Ringing/Live/Ended. | `web/index.html:1946-1956` | A browser test: open the page, queue call A, then send an event for an unknown call B; card A changes. Node isn't installed here, so it's a code read only. |
| U2 | A Plivo websocket message that isn't JSON, has no `event` key, or carries invalid base64 raises `JSONDecodeError` / `KeyError` / `binascii.Error` and ends the call. Confirmed for the decoder (`r5`: `binascii.Error`), but no evidence that Plivo ever sends such a message. | `app.py:1740`, `2684-2698` | A capture of a malformed Plivo frame; or harden with `data.get("event")` and a skipped frame. |
| U3 | `persist_call_event_sync` opens SQLite with the default 5 s timeout and only logs a failure, so a dropped event leaves a call stuck (e.g. `connected`). | `app.py:1101`, `1168` | A lock test with the dispatcher, the Analyzer and admin CSVs writing at once. Logs show 0 "Error persisting". |
| U4 | Events are persisted with `run_in_executor` and no ordering, so two events of one call can be applied out of order (e.g. a no-turn leg's `transcript_ready` before its `call_ended`). | `app.py:1256` | A stress test with a slow first persist. |
| U5 | A transcript job runs in the media process that served the call. A restart during its STT retries (up to about 2 min) loses the file, and the backfill can't recover it. | `call_transcript.py:348-374` | A restart during a job (restarts are idle-only today). |
| U6 | The calls page marks every live call "Failed / Server Disconnected" on any SSE error, including a short blip after which EventSource reconnects and the calls are still live. Designed behaviour (CLAUDE.md 2026-09-25), but misleading. | `web/index.html:2013-2030` | nginx shows 13 × 502 on `/call-events` (2026-10-01..05, during restarts); would need a blip during a live call. |
| U7 | `initialize_transfer_context_store` switches `transfer_context.sqlite3` to WAL at import, with a 5 s timeout, in all four processes. On a busy file this could raise and stop a process. | `app.py:259-275` | Starting four processes against a locked file. It's in WAL already, so only a fresh file is at risk. |
| U8 | `"1000" in str(e)` / `"1008" in str(e)` can match a different close whose message contains those digits. | `app.py:3467`, `3474`, `1891` | Fixed by E1's fix (classify on `e.code`). |

## Areas checked and found clean

| Area | Result |
|---|---|
| Per-frame audio: μ-law decode, aec1, the AEC guard, resampling, RNNoise, AGC, the echo gate | Clean on empty, 1-byte, 80-, 160-, 161- and 320-byte frames, digital silence and full scale (`r5`). aec1 stayed finite with the right frame lengths over 30,000 frames (10 min) of mixed loud, silent and noise input (`r7`). `should_barge_in` handled silence, length mismatch and empty input; only an odd byte count raises, which the app never passes (`r6`). `aec.py` was not run (observation only, as asked). |
| Catalog tool calls from Gemini | No exception for any odd argument (None, numbers, lists, dicts, 500-item lists, unknown fields) on any tool (`r10`). Unknown tools get an error reply. |
| Haiku worker, transcript job, Plivo cost fetch | Retries, the per-call lock, the backfill guard and error logging are all in place. Timeouts: Bedrock connect 5 s / read 600 s, SDK retries 4, our 3 attempts; STT 120 s; Plivo SDK 5 s. The database since the step 2 deploy has no connected call missing its STT, Haiku or Plivo cost. |
| Campaign dispatcher and hangup hook | Every tick is in a try; picks are released on error; hangups settle in the background; the backstop covers lost hangups. |
| Admin APIs | Bad ranges, pages and huge ids give 400 / 200, never 500 (`r13`). |
| Auth | Default deny holds. The one external `/internal/call-event` probe got 404 (token check). Logged out: 302 / 401. |
| Event forwarding | Retry and duplicate dropping as deployed earlier today. The only "not delivered" in the log is the 14:39 one that led to the fix. |
| Startup schema upgrades | Race-safe in four processes ("duplicate column" ignored, `IF NOT EXISTS`, timeout 30 s). `start_background_work` steps are guarded. |
| Operational gaps (not error findings) | `Gemini_Assistant.log` is unrotated (4 MB, written by four processes); disk 65 % used. |

## Suite

`venv/bin/python -m unittest discover -s tests -t .`: **Ran 1139, FAILED (failures=5, skipped=4,
expected failures=1)**. The 4 known failures, plus E11.

## Not reported again

- Already known: Vertex stalls, the post-endCall phrase, the TEST5 latch miss, echo truncation at
  160 ms, the 3 + 1 known test failures, the step 4 minors.
- The September "Task exception was never retrieved" tracebacks: fixed 2026-09-25, no recurrence.
- The lost `call_ended` of 2026-10-13: fixed the same day. Its row `f7eef765` is still `connected`,
  pending the operator's manual repair.

Nothing was changed or committed. The fixes above are suggestions for the operator to choose from.
