# Post-call transcript file: design

Status: approved in brainstorming 2026-10-12 (operator). Spike results in §9.

## 1. Goal

After every call, write one plain-text file `TRANSCRIPTS/<call_uuid>.txt` with the whole
conversation turn by turn (Neha, caller, Neha, caller, ...) for a downstream NLP task.

- Neha's lines: Gemini Live `output_transcription` (operator: accurate, trusted as is).
- Caller's lines: a separate speech-to-text pass after the call, because Gemini Live's
  `input_transcription` is unreliable for Bengali and Hindi (TEST8: it logged Neha's own
  sentences as `[USER]` lines and misheard "তিন মিনিট আছে" as "তিনি নেই কাছে").
- Not live. The file may arrive seconds (worst case about two minutes) after hangup.
- Must not affect the call: during the call only in-memory appends; all network and file
  work happens after teardown.

Out of scope: dashboard display, DB storage, pushing the file anywhere, transcribing Neha.

## 2. Decisions (operator)

| Topic | Decision |
|---|---|
| Output | One `.txt` per call on local disk, read by an operator script |
| Caller script | Native script (Bengali / Devanagari), English terms in Latin |
| Engine | `gemini-3.5-transcribe` family on Vertex AI, REST via `httpx` (google-genai 1.66 has no transcription config fields; requirements.txt frozen) |
| Request shape | Option A: one request per call, all caller turns joined, word timestamps, split back into turns by time |
| Vocabulary | `customVocabulary` sent (Vertex accepts it with word timestamps) |
| Cost | Last line of the file, USD from `usageMetadata` |
| Barge-in order | By turn start time; Neha's cut-off line first, marked `(interrupted)` |
| STT failure | Fall back to Gemini Live's caller text for that line, marked `(fallback)` |

## 3. Capture during the call (app.py)

`call_state["transcript"]` holds a `call_transcript.CallTranscript` when
`TRANSCRIPT_ENABLED=1`, else `None`. It is created right after `wait_for_plivo_start`
(`app.py:1983`), which fixes `t0 = time.monotonic()` and `started_at` (IST wall clock).
Every hook in app.py is one line: `if call_state["transcript"]: call_state["transcript"].<method>(...)`.
The key is mirrored in `tests/harness/appctl.py`.

| Hook | app.py site | Method |
|---|---|---|
| Caller activity starts | `activityStart` sends (`2672`, deferred `2890`) | `caller_start()` |
| Caller audio sent to Gemini | every `send_realtime_input(audio=...)` (`2693`, `2702`, `2903`) | `caller_audio(pcm16k)` |
| Caller activity ends | `activityEnd` send (`2710`) | `caller_end()` |
| Gemini's caller text flushed | `user_text_buffer` flush (`2993`) | `caller_gemini_text(text)` |
| First Neha transcription piece of a turn | `output_transcription` while `ai_text_buffer` is empty (`2924`) | `agent_started()` |
| Neha turn complete | `ai_text_buffer` flush on `turn_complete` (`2971`) | `agent_text(text, interrupted=False)` |
| Neha interrupted | `ai_text_buffer` flush on `interrupted` (`2936`) | `agent_text(text, interrupted=True)` |

Rules inside `CallTranscript` (pure, no I/O):

- Tapping the sends means the STT hears exactly what Gemini heard: 16 kHz mono PCM16 after
  AEC, guard, denoise and AGC, Neha's echo already removed. Audio dropped during a tool
  call (never sent) is not captured either.
- `caller_start()` opens a turn `{start_s, pcm, gemini_text}`; it first closes any turn still
  open (the endCall / transferCall / reconnect paths reset `user_activity_open` without an
  `activityEnd`). `caller_audio` outside an open turn is ignored. A turn with no audio is
  discarded on close.
- `caller_gemini_text` attaches to the most recent closed caller turn (Gemini flushes it
  after `activityEnd`, possibly while the caller has already started the next turn); with
  no closed caller turn it is dropped. A second flush for the same turn is appended with a space.
- `agent_started()` records the start of the pending Neha turn; `agent_text` closes it.
  Without a recorded start, the flush time is used.
- `start_s` is seconds since `t0`.
- Memory: 32 KB per second of caller speech; worst case (900 s call cap, about 5 min of
  caller speech) about 10 MB, freed when the job finishes.

## 4. Post-call job (call_transcript.py)

Started from the `finally` of `handle_media_stream`, after `log_call_stats(call_state)`
(`app.py:2330`), when `call_state["transcript"]` has at least one turn of either kind:

```python
call_transcript.start_job(call_state["transcript"], call_uuid, get_token)
```

`start_job` calls `asyncio.create_task(write_transcript(...))` and keeps the task in a
module-level set until it finishes (no garbage collection mid-run). The task inherits the
`call_log_tag` context, so its log lines carry the call's `[uuid8]` tag. Teardown never
awaits it.

`get_token` is an async callable supplied by app.py: `await ensure_vertex_token()` then
return `load_vertex_credentials().token`. The module never imports app.py and never reads
the key file itself.

Steps of `write_transcript`:

1. **No caller turns:** skip STT; `stt_status: skipped (no caller audio)`, cost 0.
2. **Language** from Neha's lines: count Bengali (U+0980-U+09FF) vs Devanagari
   (U+0900-U+097F) letters; the larger, if non-zero, gives `bn-IN` / `hi-IN`, else `en-IN`.
3. **Join:** caller turns in order, each followed by 1.5 s of zeros, into one 16 kHz mono
   WAV in memory. Record each turn's span `(begin_s, end_s)` on the joined timeline.
4. **Request:** `POST https://aiplatform.googleapis.com/v1/projects/{VERTEX_PROJECT}/locations/{STT_LOCATION}/publishers/google/models/{STT_MODEL}:generateContent`
   (for `STT_LOCATION` other than `global`: host `aiplatform.{loc}.rep.googleapis.com` for
   `us` / `eu`, `{loc}-aiplatform.googleapis.com` for a region), `Authorization: Bearer`,
   `httpx.AsyncClient`, timeout 120 s, body:
   ```json
   {"contents": [{"role": "user", "parts": [{"inlineData": {"mimeType": "audio/wav", "data": "<base64>"}}]}],
    "generationConfig": {"audioTranscriptionConfig": {
        "languageCodes": ["bn-IN"], "customVocabulary": [...], "wordTimestamp": true}}}
   ```
5. **Retries:** on 429, 5xx, timeout or connection error, sleep 2, 4, 8, 16, 32, 60 s and
   retry (7 attempts, about 2 min). Any other status fails at once. A token error fails at once.
6. **Words:** `candidates[0].content.parts[*].audioTranscription.words[*]`, each
   `{"word", "startOffset": "3.300s", "endOffset"}` (shape seen in the spike). A 200 response
   with no words is a failure, `no words in response`. Each word goes to the span containing
   its start, else the nearest span.
7. **Latin terms** per word (§6), then each turn's words joined with single spaces.
8. **Lines:** a caller turn with words is `User:`; with no words but `gemini_text` it is
   `User (fallback):` with that text; with neither it is dropped. On STT failure every
   caller turn takes the fallback rule.
9. **Cost** (§5) and **write** (§5), then one log line (§7).

The whole of steps 2-9 runs inside `try/except Exception`. Any exception after the request
(split, render) is logged and the file is rebuilt from the raw turns with
`stt_status: failed (internal error: <ExceptionType>)`. If the write itself fails, one error
line is logged, the `.tmp` file is removed if present, and nothing is raised.

Event-loop cost: joining and base64 of a few MB plus a few hundred words, a few
milliseconds; the rest is network wait.

## 5. Output file

Path `TRANSCRIPT_DIR/<call_uuid>.txt` (created with `os.makedirs(exist_ok=True)`), UTF-8,
`\n` line ends. Written to `<call_uuid>.txt.tmp`, then `os.replace`, so a reader never sees
a partial file. `TRANSCRIPTS/` is added to `.gitignore` (caller PII). Each process writes
its own calls; names are unique per `call_uuid`.

```
call_uuid: 3f2a9c1e-7b4d-4e8a-9f10-2c5d6e7a8b90
started_at: 2026-10-11 18:42:07 IST
duration_sec: 214
language: bn-IN
stt_model: gemini-3.5-transcribe-preview
stt_status: ok

[00:00] Neha: Hello, this is Neha from Adamas University ...
[00:05] User: হ্যাঁ বলুন
[00:40] Neha (interrupted): B.Tech এর fees প্রতি semester এ ...
[00:42] User: MBA টা বলতে পারেন
[00:52] User (fallback): ঠিক আছে, আমি বাবার সাথে কথা বলে নেব।

transcription_cost_usd: 0.0067 (audio_in=2074 text_in=311 output=158 tokens, model=gemini-3.5-transcribe-preview)
```

- `[mm:ss]` is `start_s` formatted `{m:02d}:{s:02d}` (the 900 s call cap keeps minutes
  under 16). Lines sorted by `start_s`; ties keep capture order.
- `duration_sec` is whole seconds from `t0` to job start.
- `stt_status`: `ok`, `failed (<reason>)`, or `skipped (no caller audio)`. Reasons:
  `http <status>`, `timeout`, `connection error`, `token error`, `no words in response`,
  `internal error: <type>`.
- Header carries no phone number or name; `call_uuid` joins to `call_history.db`.
- STT lines have no punctuation (timestamp mode); fallback lines keep Gemini's.
- **Cost line**, always last:
  `cost = (audio_in * STT_PRICE_AUDIO_INPUT + text_in * STT_PRICE_TEXT_INPUT + output * STT_PRICE_OUTPUT) / 1e6`,
  printed with 4 decimals. `audio_in` / `text_in` come from `usageMetadata.promptTokensDetails`
  by modality, `output` from `candidatesTokenCount`. Failed or skipped:
  `transcription_cost_usd: 0.0000 (stt failed: <reason>)` / `(stt skipped: no caller audio)`.
  429 attempts are not billed and not counted.

## 6. Vocabulary and Latin terms

`VOCABULARY` (module constant, sent as `customVocabulary`; keep it short and specific, a
loose list over-biases, see §9):
Adamas, Adamas University, Neha, B.Tech, M.Tech, BBA, MBA, BCA, MCA, B.Sc, M.Sc, LLB, LL.M,
B.Pharm, AUAT, AUJET, WBJEE, JEE Main, CAT, PCM, PCB, CGPA, Barasat, Kolkata, CSE,
Computer Science, Data Science.

`LATIN_TERMS` (module constant): Bengali / Devanagari spelling to Latin, applied per word
(the STT words carry no punctuation). A word matches if it equals a key, or a Bengali key
followed by one of the classifier particles `টা টি টার টাই টাও`; the particle is kept,
separated by a space: `এলএলএমটা` → `LL.M টা`. Endings that start with a vowel sign
(`বিটেকে`, `বিটেকের`) stay native, since splitting them leaves a dangling sign. Devanagari
keys match exactly only. Initial entries:

| Latin | Spellings |
|---|---|
| MBA | এমবিএ, एमबीए |
| BBA | বিবিএ, बीबीए |
| BCA | বিসিএ, बीसीए |
| MCA | এমসিএ, एमसीए |
| B.Tech | বিটেক, বি.টেক, बीटेक, बी.टेक |
| M.Tech | এমটেক, এম.টেক, एमटेक |
| LLB | এলএলবি, एलएलबी |
| LL.M | এলএলএম, एलएलएम |
| CGPA | সিজিপিএ, सीजीपीए |
| PCM | পিসিএম, पीसीएम |
| PCB | পিসিবি, पीसीबी |
| AI | এআই, एआई |

Unknown English words stay in native script. The table is extended by hand from live
transcripts.

## 7. Config and logging

| Env var | Default | Meaning |
|---|---|---|
| `TRANSCRIPT_ENABLED` | `1` | `0` = no capture, no job. Only `0` / `1`, else `ValueError` at import (like `DEBUG_RECORDING`) |
| `TRANSCRIPT_DIR` | `TRANSCRIPTS` | Output folder, relative to the working directory |
| `STT_MODEL` | `gemini-3.5-transcribe-preview` | GA `gemini-3.5-transcribe` returned 404 for the project on 2026-10-11 |
| `STT_LOCATION` | `global` | Only location that served the model in the spike |
| `STT_PRICE_AUDIO_INPUT` | `2.00` | USD per 1M tokens |
| `STT_PRICE_TEXT_INPUT` | `2.00` | USD per 1M tokens (assumed; Vertex price not confirmed) |
| `STT_PRICE_OUTPUT` | `12.00` | USD per 1M tokens |

`.env` is frozen; defaults live in code, EC2 overrides go in `/etc/au-app/common.env`.
STT always uses Vertex (`VERTEX_PROJECT`, the service-account file), whatever `GEMINI_BACKEND` is.

One log line per call, tag `[TRANSCRIPT]` (added to `DIAGNOSTIC_LOG_TAGS` in
`tests/harness/appctl.py`):

```
📝 [TRANSCRIPT] status=ok lang=bn-IN user_turns=23 fallback=1 words=125 audio_s=83 latency=4.8s retries=0 cost=$0.0067 file=TRANSCRIPTS/<uuid>.txt
```

On failure a `⚠️ [TRANSCRIPT]` warning with the reason and at most 200 characters of the
error body. Transcript text is never logged.

## 8. Tests (stdlib unittest, no network, no key)

`tests/test_call_transcript.py`:
- `CallTranscript` capture rules (§3): open/close, auto-close, audio outside a turn,
  empty turns, Gemini text attachment, agent start time, interrupted flag.
- Language choice, join spans and WAV header, word split (inside span, in a gap, empty
  turn, fallback, dropped), Latin terms (exact, with suffix, no partial match, Devanagari).
- Render: order, `mm:ss`, markers, header, cost line for ok / failed / skipped; cost math
  with the TEST8 counts (0.0067).
- Request via `httpx.MockTransport`: URL, body fields, base64 round trip, bearer header.
- Retries with sleep patched: 429→429→200, 500 retried, 400 not retried, timeout, token
  error, cap reached.
- Failure isolation: exception in split/render still writes a fallback file; write error
  raises nothing and leaves no `.tmp`.
- End to end: fake turns plus fake response give an exact expected file.

`tests/test_transcript_capture.py` (FakePlivoWS harness): caller turn bytes equal the
bytes sent to Gemini, Gemini text and Neha lines (both flush paths) captured, a call with
`transcript=None` sends exactly what a call with capture sends. `handle_media_stream` is not
drivable offline (harness README), so the wiring there (creation under
`TRANSCRIPT_ENABLED`, `start_job` in the `finally` after `log_call_stats`, hook counts) is
pinned by source inspection, as `test_debug_recording_switch` does. Invalid
`TRANSCRIPT_ENABLED` rejected at import.

Existing guards: `transcript` key mirrored in appctl.py (`test_harness_smoke`); 4_8
GEMINI_MODEL line pins re-pinned; aec.py hash unchanged; bargein.py untouched.

Outside the suite: a `TEST_FILES/` script runs the real module on TEST8 audio against real
Vertex for the operator to read; then one live call on EC2.

## 9. Spike evidence (2026-10-11, TEST8, Bengali, 23 caller turns, `_aecout` audio)

- Model IDs: `gemini-3.5-transcribe-preview` works on `global`; `gemini-3.5-transcribe`
  (GA) 404 on `global`, `us`, `eu` for this project.
- Per-turn requests: about 12 successful per minute, 28 retries on 429 in 69 requests;
  short clips lose sentence ends. One whole-call request: 3.5-5 s, complete sentences.
- `languageCodes` matters: auto-detect returned Marathi for a Bengali clip; `bn-IN,en-IN`
  sometimes romanized whole sentences; `bn-IN` alone was most consistent.
- Whole-call mode writes English terms in Bengali script whatever the settings (hence §6).
- `SMART` mode is rejected with `languageCodes` (not used).
- A loose vocabulary inserted "scholarship" into a turn that did not say it; the list in §6
  drops generic words.
- Word timestamp mode drops punctuation and lost one short turn and one "না" in TEST8.
- Usage for 83 s of caller audio: audio_in 2074, text_in 311 (with vocabulary), output 158.
- Scripts: `TEST_FILES/_stt_spike.py`, `_stt_concat.py`; outputs `_stt_TEST8*.txt`,
  `_stt_concat_*.json`.
