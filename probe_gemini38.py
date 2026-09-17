"""
THROWAWAY migration probe — DELETE after use. Not part of the app.

Purpose: verify the 4 residual risks of migrating app_aec1.py from
`gemini-3.1-flash-live-preview` to `gemini-3.8-live-extended-thinking`
BEFORE editing the production server:

  1. Does the model accept our EXACT production config?
     (manual VAD disabled=True + thinking_level="low" + NON_BLOCKING tools
      + in/out audio transcription + session resumption + sliding-window
      context compression + speech voice "Kore")
  2. What google-genai version is installed here?
  3. Does `types.Behavior.NON_BLOCKING` exist in this SDK?
  4. Where does `interaction_status` live on a received message
     (response.* vs response.server_content.*) and what values appear?

Run on the EC2 box (same venv as the server) with GOOGLE_API_KEY set:

    python probe_gemini38.py

It only makes ONE short text-driven turn. No telephony, no audio device.
Reads nothing from and writes nothing to the app's runtime state.
"""

import asyncio
import os
import sys

MODEL = os.getenv("PROBE_MODEL", "gemini-3.8-live-extended-thinking")
API_KEY = os.getenv("GOOGLE_API_KEY")
RECEIVE_TIMEOUT_SECONDS = 30.0


def hr(title):
    print("\n" + "=" * 70)
    print(title)
    print("=" * 70)


def report_sdk_version():
    hr("1. SDK VERSION")
    try:
        from importlib.metadata import version
        print(f"google-genai == {version('google-genai')}")
    except Exception as e:
        print(f"could not read google-genai version: {e!r}")
    print(f"python == {sys.version.split()[0]}")


def report_enum_support(types):
    hr("2. SDK CAPABILITY CHECK (does not need network)")
    checks = {
        "types.Behavior": lambda: types.Behavior,
        "types.Behavior.NON_BLOCKING": lambda: types.Behavior.NON_BLOCKING,
        "types.FunctionDeclaration(behavior=...)": lambda: types.FunctionDeclaration(
            name="probe", description="x", behavior=types.Behavior.NON_BLOCKING
        ),
        "types.ThinkingConfig(thinking_level=)": lambda: types.ThinkingConfig(
            thinking_level="low"
        ),
        "types.ActivityStart": lambda: types.ActivityStart(),
        "types.ActivityEnd": lambda: types.ActivityEnd(),
        "types.AutomaticActivityDetection(disabled=True)": lambda: types.AutomaticActivityDetection(
            disabled=True
        ),
    }
    for label, fn in checks.items():
        try:
            fn()
            print(f"  OK    {label}")
        except Exception as e:
            print(f"  FAIL  {label}  -> {type(e).__name__}: {e}")

    # interaction_status enum, if the SDK models it as a type
    candidates = [n for n in dir(types) if "interaction" in n.lower() or "InteractionStatus" in n]
    print(f"  types.* names mentioning 'interaction': {candidates or 'none found'}")


def build_production_config(types):
    """Mirror app_aec1.py's LiveConnectConfig (lines ~1097-1130), plus the two
    migration deltas: NON_BLOCKING tools and (kept) thinking_level='low'."""
    end_call_tool = types.FunctionDeclaration(
        name="endCall",
        description="Ends the call when the user says goodbye.",
        behavior=types.Behavior.NON_BLOCKING,
        parameters={
            "type": "OBJECT",
            "properties": {"summary_of_whole_call": {"type": "STRING"}},
            "required": ["summary_of_whole_call"],
        },
    )
    transfer_call_tool = types.FunctionDeclaration(
        name="transferCall",
        description="Hands the call to a human counselor.",
        behavior=types.Behavior.NON_BLOCKING,
        parameters={
            "type": "OBJECT",
            "properties": {"call_summary": {"type": "STRING"}, "language": {"type": "STRING"}},
            "required": ["call_summary", "language"],
        },
    )
    tools = [{"function_declarations": [end_call_tool, transfer_call_tool]}]

    return types.LiveConnectConfig(
        system_instruction=types.Content(
            parts=[types.Part.from_text(text="You are a probe. Reply in one short sentence.")]
        ),
        tools=tools,
        temperature=0.2,
        session_resumption=types.SessionResumptionConfig(handle=None),
        response_modalities=["AUDIO"],
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        thinking_config=types.ThinkingConfig(thinking_level="low"),
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
            sliding_window=types.SlidingWindow()
        ),
    )


def introspect_message(msg, seen):
    """Log the attribute paths that matter for the migration, once each."""
    # interaction_status: response-level?
    for path in ("interaction_status", "interactionStatus"):
        if hasattr(msg, path) and getattr(msg, path) is not None and f"response.{path}" not in seen:
            seen.add(f"response.{path}")
            print(f"  FOUND response.{path} = {getattr(msg, path)!r}")

    sc = getattr(msg, "server_content", None)
    if sc is not None:
        for path in ("interaction_status", "interactionStatus"):
            if hasattr(sc, path) and getattr(sc, path) is not None and f"server_content.{path}" not in seen:
                seen.add(f"server_content.{path}")
                print(f"  FOUND server_content.{path} = {getattr(sc, path)!r}")
        tc = getattr(sc, "turn_complete", None)
        if tc and "turn_complete" not in seen:
            seen.add("turn_complete")
            print(f"  server_content.turn_complete = {tc!r}")

    um = getattr(msg, "usage_metadata", None)
    if um is not None and "usage" not in seen:
        seen.add("usage")
        total = getattr(um, "total_token_count", None)
        details = getattr(um, "response_tokens_details", None)
        print(f"  usage_metadata.total_token_count = {total!r}")
        print(f"  usage_metadata.response_tokens_details type = {type(details).__name__}")


async def run_live_probe(genai, types):
    hr(f"3. LIVE CONNECT PROBE  (model={MODEL})")
    client = genai.Client(api_key=API_KEY)

    try:
        config = build_production_config(types)
    except Exception as e:
        print(f"  CONFIG BUILD FAILED (SDK too old for a field): {type(e).__name__}: {e}")
        print("  -> Likely need to bump google-genai. Stopping.")
        return

    try:
        async with client.aio.live.connect(model=MODEL, config=config) as session:
            print("  OK: connected with FULL production config (manual VAD + thinking + NON_BLOCKING tools).")
            await session.send_client_content(
                turns=types.Content(
                    role="user",
                    parts=[types.Part.from_text(text="Say hello in one short sentence.")],
                ),
                turn_complete=True,
            )
            print("  sent one text turn; listening for interaction_status / turn_complete / usage...")

            seen = set()
            got_audio = False

            async def listen():
                nonlocal got_audio
                async for msg in session.receive():
                    introspect_message(msg, seen)
                    sc = getattr(msg, "server_content", None)
                    if sc and getattr(sc, "model_turn", None):
                        for part in sc.model_turn.parts:
                            if getattr(part, "inline_data", None) and part.inline_data.data:
                                got_audio = True
                    # stop once we've observed idle or a full turn + audio
                    if "server_content.interaction_status" in seen or "response.interaction_status" in seen:
                        if got_audio:
                            return
                    if "turn_complete" in seen and got_audio:
                        return

            try:
                await asyncio.wait_for(listen(), timeout=RECEIVE_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                print(f"  (receive loop hit {RECEIVE_TIMEOUT_SECONDS}s timeout — printing what was seen)")

            print(f"  got_audio = {got_audio}")
            if not any("interaction_status" in s for s in seen):
                print("  NOTE: no interaction_status observed. Either the SDK names it differently,")
                print("        or it wasn't emitted for this short turn. Check the dump below.")
                # one-shot raw attribute dump of the last message type for manual inspection
    except Exception as e:
        print(f"  CONNECT/RUN FAILED: {type(e).__name__}: {e}")
        print("  -> This is the make-or-break result. The exception text usually names")
        print("     the rejected field (e.g. manual VAD, thinking, or NON_BLOCKING).")


async def main():
    if not API_KEY:
        print("GOOGLE_API_KEY not set. Export it and re-run.")
        sys.exit(1)

    try:
        from google import genai
        from google.genai import types
    except Exception as e:
        print(f"could not import google-genai: {e!r}")
        sys.exit(1)

    report_sdk_version()
    report_enum_support(types)
    await run_live_probe(genai, types)

    hr("DONE — paste this whole output back")


if __name__ == "__main__":
    asyncio.run(main())
