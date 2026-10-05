"""One-off Vertex AI connect test (no caller audio sent).

1. Load the service-account key and mint an access token.
2. Open a Live session with the app's own client factory, connect wrapper and
   config (prompt, tools, voice, VAD off, compression).
3. Send the app's greeting trigger (send_client_content), time the first audio
   chunk, collect the output transcript until turn_complete, then close.

Run: PYTHONIOENCODING=utf-8 venv312/Scripts/python.exe TEST_FILES/_vertex_smoke.py
Env: GEMINI_BACKEND / VERTEX_LOCATION as for app.py.
"""

import asyncio
import os
import sys
import time
import wave

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.harness.appctl import app  # noqa: E402

types = app.types
OUT_WAV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_vertex_smoke_greeting.wav")
TURN_TIMEOUT_S = 30


def build_config(system_instruction_text):
    # Same as handle_media_stream's first-connect config.
    return types.LiveConnectConfig(
        system_instruction=types.Content(parts=[types.Part.from_text(text=system_instruction_text)]),
        tools=app.LOCAL_GEMINI_TOOLS,
        temperature=0.2,
        session_resumption=types.SessionResumptionConfig(handle=None),
        response_modalities=["AUDIO"],
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name="Kore")
            )
        ),
        realtime_input_config=types.RealtimeInputConfig(
            automatic_activity_detection=types.AutomaticActivityDetection(disabled=True),
            activity_handling=types.ActivityHandling.START_OF_ACTIVITY_INTERRUPTS,
        ),
        context_window_compression=types.ContextWindowCompressionConfig(
            sliding_window=types.SlidingWindow(),
        ),
    )


async def main():
    print(f"[1] {app.describe_gemini_backend()} model={app.GEMINI_MODEL}")
    user_name, phone_number = "Test Caller", "+910000000000"
    system_instruction_text = (
        f"Current Date and Time (India IST): {app.get_indian_time()}\n\n"
        + app.RAW_SYSTEM_PROMPT.replace("{user_name}", user_name).replace("{phone_number}", phone_number)
    )
    initial_prompt = f"""
            System Event: The outbound call to {user_name} has just connected.
            * **Greeting:** Greet {user_name} and introduce yourself per your persona instructions in the system prompt, then ask whether they'd like to continue in English or switch to another supported language.
            * **STOP HERE:** After asking the language question, END YOUR TURN and stay silent. Do NOT mention any offers, programs, or further details yet. Wait for the user to state their language preference. Only AFTER the user replies do you continue — in their chosen language — with the rest of the flow.
            """

    t0 = time.monotonic()
    if app.GEMINI_BACKEND == "vertex":
        await app.ensure_vertex_token()
        print(f"[1] access token OK ({time.monotonic() - t0:.2f}s)")

    client = app.create_gemini_client()
    t1 = time.monotonic()
    async with app.connect_live_with_timeout(client, app.GEMINI_MODEL, build_config(system_instruction_text)) as session:
        print(f"[2] Live session open ({time.monotonic() - t1:.2f}s)")

        t2 = time.monotonic()
        await session.send_client_content(
            turns=types.Content(role="user", parts=[types.Part.from_text(text=initial_prompt)]),
            turn_complete=True,
        )
        print("[3] greeting trigger sent (send_client_content)")

        first_audio_s = None
        audio = bytearray()
        transcript = []
        usage = None
        turn_complete = False

        async def consume():
            nonlocal first_audio_s, usage, turn_complete
            async for msg in session.receive():
                if msg.usage_metadata:
                    usage = msg.usage_metadata
                sc = msg.server_content
                if sc is None:
                    continue
                if sc.model_turn:
                    for part in sc.model_turn.parts or []:
                        if part.inline_data and part.inline_data.data:
                            if first_audio_s is None:
                                first_audio_s = time.monotonic() - t2
                            audio.extend(part.inline_data.data)
                if sc.output_transcription and sc.output_transcription.text:
                    transcript.append(sc.output_transcription.text)
                if sc.turn_complete:
                    turn_complete = True
                    return

        try:
            await asyncio.wait_for(consume(), timeout=TURN_TIMEOUT_S)
        except asyncio.TimeoutError:
            print(f"[3] no turn_complete within {TURN_TIMEOUT_S}s")

    if first_audio_s is None:
        print("[3] FAIL: no audio received")
    else:
        print(f"[3] first audio after {first_audio_s:.2f}s; "
              f"{len(audio) / 48000:.1f}s of audio; turn_complete={turn_complete}")
        with wave.open(OUT_WAV, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(24000)
            w.writeframes(bytes(audio))
        print(f"[3] saved {OUT_WAV}")
    print(f"[3] transcript: {''.join(transcript).strip()!r}")
    if usage is not None:
        print(f"[3] usage: prompt={usage.prompt_token_count} response={usage.response_token_count} "
              f"total={usage.total_token_count}")


if __name__ == "__main__":
    asyncio.run(main())
