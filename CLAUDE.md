# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

An outbound voice agent ("Neha") for **Adamas University** (Kolkata) admissions. It places phone calls via **Plivo**, streams the caller's audio bidirectionally over a WebSocket through a DSP chain, and pipes it to the **Google Gemini Live API** for speech-to-speech responses. Gemini's audio is streamed back to the caller. The agent informs prospective applicants about School of Engineering & Technology (SOET) programs, qualifies them as leads, and can hand off to a human counselor.

The project was cloned from a Senco Gold jewelry voice agent and reshaped. **`app_aec1.py` is the live/authoritative server** (deployed on EC2). `app.py` is an older leftover — do not edit it expecting effect. Several other files (`chromadb_setup.py`, `groqLLM.py`, `pinsearcher.py`, `phone_modifier.py`, `senco_llm4.json`, `senco_chroma_db/`, `prompt.txt`, `details.txt`) are Senco-era leftovers **no longer imported by `app_aec1.py`**; `requirements.txt` still pins `chromadb` and `groq` for the same reason, but they are unused at runtime now.

## Working constraints

- The sandbox usually lacks `plivo` / `google-genai` / `pyrnnoise` / native audio libs, so importing `app_aec1.py` fails there. Use `python -m py_compile app_aec1.py` for a syntax check. Live behavior can only be verified on the server with a real call.
- Only two files ship to the EC2 server for prototype changes: **`app_aec1.py`** and **`prompt1.txt`**.

## Commands

```bash
# Run the server. Hypercorn on 0.0.0.0:8008. Needs a public HTTPS tunnel
# (PUBLIC_BASE_URL) so Plivo can reach the webhooks / WSS.
python app_aec1.py

# Syntax check without importing heavy runtime deps
python -m py_compile app_aec1.py

# Full runtime deps
pip install -r requirements.txt
```

There is no test suite and no linter configured in this repo.

## Required environment (.env)

`GOOGLE_API_KEY`, `PUBLIC_BASE_URL` (public https base, no trailing slash), `FROM_NUMBER` (Plivo caller ID), plus Plivo auth used by the SDK. Many behavioral knobs are env-overridable — see the constants block near the top of `app_aec1.py` (`GEMINI_MODEL`, `VAD_*`, `SILENCE_FOLLOWUP_SECONDS`, `MAX_CALL_DURATION_SECONDS`, `LEADS_DIR`, etc.). `HUMAN_TRANSFER_NUMBER` is currently a hardcoded constant.

## Architecture

### Call lifecycle (one WebSocket = one call)
1. `POST /trigger-call` (from the dashboard) originates an outbound Plivo call; answer URL → `/outbound-webhook`. `user_name` and `phone_number` flow in as query args and are the source of a lead's identity.
2. `/outbound-webhook` returns Plivo XML with a bidirectional `<Stream>` (μ-law 8 kHz) pointing at `wss://…/media-stream`.
3. `@app.websocket('/media-stream')` (`handle_media_stream`) is the heart. It waits for Plivo's `start` event (`apply_plivo_start`), plays a legal disclaimer (`play_disclaimer`, from `playback_audio_files/recorded1.wav`), opens the Gemini Live session, then runs **four concurrent asyncio tasks** coordinated by `coordinate_call_tasks` (first task to finish tears the call down):
   - `stream_plivo_to_gemini` — inbound audio: μ-law→PCM, DSP chain, VAD, forward to Gemini.
   - `stream_gemini_to_plivo` — Gemini events: audio, transcripts, **tool calls**, usage/cost.
   - `send_plivo_audio` — paces Gemini's 24 kHz audio out to Plivo as 8 kHz μ-law 20 ms frames.
   - `supervise_call` — watchdogs: max call duration, model-response deadline.

### Audio / DSP pipeline
- Rates: Plivo 8 kHz μ-law ↔ Gemini 16 kHz PCM in / 24 kHz PCM out. Resampling via `audioop.ratecv`; μ-law via `audioop`. `aec.py` (`AcousticEchoCanceller`) removes the AI's own voice echoed back through a speakerphone before RNNoise/VAD.
- **Manual VAD** drives turn-taking. `silence_watchdog` re-engages a silent-but-connected caller after ~8s ("still on line?") and, after `MAX_SILENCE_FOLLOWUPS`, says a farewell and ends. This nudge is intentional — do not remove it.

### Gemini tools
Declared in the tool block and dispatched inside `stream_gemini_to_plivo`. Only two tools are active:
- `endCall(summary_of_whole_call)` — the model speaks its farewell, then this fires. The summary must be a full admission summary; it is the source of the lead record.
- `transferCall(call_summary, language)` — human handoff. `execute_pending_terminal_action` stores context (SQLite `transfer_context.sqlite3`), serves `/transfer.xml` to `<Dial>` a human, and on dial-end `/dial-action` re-enters `/media-stream` (`is_transfer_return=1`) to resume the AI.

Both terminal actions defer until closing audio has drained (`execute_pending_terminal_action`), so the agent never gets cut off mid-sentence.

### Knowledge base (prototype)
SOET course/eligibility/fee data is embedded directly in `prompt1.txt` under `## KNOWLEDGE BASE`, kept as a delimited block. The plan is to later replace it with a real database served to the agent as a tool (e.g. `getCourseDetails`); keep the block self-contained so that swap stays clean. `prompt1.txt` is loaded once at startup into `RAW_SYSTEM_PROMPT` and `.format()`-substituted with `{user_name}` / `{phone_number}` per call — avoid stray `{`/`}` in the prompt or `.format()` breaks.

### Lead capture
`save_lead(call_state, outcome, summary)` writes `leads/{phone}_{name}.json`. It fires at three points: `endCall` (`outcome:"completed"`), successful `transferCall` (`transferred`), and the `handle_media_stream` teardown `finally` as a fallback (`user_hangup`) when the caller drops before any tool ran. **The summary always comes from the model** (the endCall/transfer summary), never from the STT transcript buffers (`user_text_buffer`/`ai_text_buffer`) — those are unreliable for Hindi/Bengali speech. `call_state["lead_saved"]` makes it idempotent per call.

### Live dashboard
`GET /` serves `index.html`; `GET /call-events` is an SSE stream fed by `emit_call_event()` (call_ringing, tool_called, ai_transcript, transfer_started, call_ended, …).

## Deployment note
`app_aec1.py` runs under **Hypercorn, single process** on port 8008. DSP runs inline in the asyncio event loop (GIL-bound, CPU-heavy), so one call largely saturates one core. Module-level state (`call_event_subscribers` SSE set) does not cross processes, so multi-worker is not drop-in safe.

## Reference material
`call_transcript/*.json` are structured turn arrays (`{"transcript": [{"speaker", "text"}, …]}`) captured from the existing production admission agent — used to diagnose failure modes (ignoring parent-vs-student, ignoring program corrections, punting every fee question), which the current redesign targets.
