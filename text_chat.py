"""Text-based chat with Gemini using the AU course catalog tools.

This is the "text-only Gemini test" from HANDOFF2 §8 item 4.  It uses the
same prompt_au.txt and the same course_catalog.handle_tool_call as app.py,
so tool responses are identical to what the Live API would receive.

Uses GOOGLE_API_KEY from .env (Google AI Studio backend, not Vertex).

Usage:
    venv312/Scripts/python.exe TEST_FILES/text_chat.py
"""

import json
import os
import sys
import time

# Allow imports from the repo root.
ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from dotenv import load_dotenv
load_dotenv(os.path.join(ROOT, ".env"))

from google import genai
from google.genai import types
import course_catalog

# ── configuration ──────────────────────────────────────────────────────────

MODEL = os.getenv("TEXT_CHAT_MODEL", "gemini-3.6-flash")
PRICE_TEXT_INPUT = float(os.getenv("PRICE_TEXT_INPUT", "0.75"))
PRICE_TEXT_OUTPUT = float(os.getenv("PRICE_TEXT_OUTPUT", "4.50"))

PROMPT_FILE = os.path.join(ROOT, "prompt_au.txt")
USER_NAME = "Paul Abhishek"
PHONE_NUMBER = "+91-0000000000"

# ── load prompt ────────────────────────────────────────────────────────────

with open(PROMPT_FILE, encoding="utf-8") as f:
    system_prompt = f.read().format(user_name=USER_NAME, phone_number=PHONE_NUMBER)

# ── build tool declarations ────────────────────────────────────────────────

catalog_tools = [types.FunctionDeclaration(**spec) for spec in course_catalog.TOOL_SPECS]
tools = types.Tool(function_declarations=catalog_tools)

# ── create client ──────────────────────────────────────────────────────────

project = os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("VERTEX_PROJECT")
location = os.getenv("VERTEX_LOCATION", "eu").strip().lower()

if not project:
    print("ERROR: GOOGLE_CLOUD_PROJECT or VERTEX_PROJECT not found in .env")
    sys.exit(1)

base_url = f"https://aiplatform.{location}.rep.googleapis.com/" if location in ("us", "eu") else None
http_options = types.HttpOptions(base_url=base_url) if base_url else None
client = genai.Client(vertexai=True, project=project, location=location, http_options=http_options)

# ── chat loop ──────────────────────────────────────────────────────────────

GREY = "\033[90m"
CYAN = "\033[96m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
RESET = "\033[0m"
BOLD = "\033[1m"

def print_banner():
    print(f"""
{BOLD}{'═' * 70}
  AU Admission Agent — Text Chat (catalog tools connected)
  Model : {MODEL}
  Prompt: prompt_au.txt ({len(system_prompt)} chars)
  Tools : {', '.join(spec['name'] for spec in course_catalog.TOOL_SPECS)}
{'═' * 70}{RESET}
  Type your message and press Enter.  Empty line or Ctrl+C to quit.
""")


def run():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    print_banner()

    # Build the chat with system instruction and tools.
    chat = client.chats.create(
        model=MODEL,
        config=types.GenerateContentConfig(
            system_instruction=system_prompt,
            tools=[tools],
            automatic_function_calling=types.AutomaticFunctionCallingConfig(
                disable=True,
            ),
            temperature=0.7,
        ),
    )

    turn_count = 0
    total_tool_calls = 0
    total_tool_chars = 0
    session_cost = 0.0

    while True:
        # ── get user input ─────────────────────────────────────────────
        try:
            user_input = input(f"\n{BOLD}You >{RESET} ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not user_input:
            break

        turn_count += 1
        start = time.perf_counter()
        turn_cost = 0.0

        def update_cost(resp):
            nonlocal turn_cost, session_cost
            if hasattr(resp, "usage_metadata") and resp.usage_metadata:
                in_tok = resp.usage_metadata.prompt_token_count or 0
                out_tok = resp.usage_metadata.candidates_token_count or 0
                cost = (in_tok * PRICE_TEXT_INPUT + out_tok * PRICE_TEXT_OUTPUT) / 1_000_000
                turn_cost += cost
                session_cost += cost

        # ── send to Gemini and handle tool calls in a loop ─────────────
        response = chat.send_message(user_input)
        update_cost(response)

        # The model may request tool calls before giving a final text reply.
        # We loop: execute each tool call, feed the results back, repeat
        # until the model returns text (or gives up).
        tool_round = 0
        while response.candidates and response.candidates[0].content.parts:
            # Check if any part is a function call.
            fn_calls = [
                part for part in response.candidates[0].content.parts
                if part.function_call
            ]
            if not fn_calls:
                break  # no tool calls — the model produced a text reply

            tool_round += 1
            function_responses = []

            for part in fn_calls:
                fc = part.function_call
                args = dict(fc.args) if fc.args else {}
                tool_start = time.perf_counter()
                result = course_catalog.handle_tool_call(fc.name, args)
                tool_ms = (time.perf_counter() - tool_start) * 1000
                chars = course_catalog.response_chars(result)
                status = course_catalog.result_status(fc.name, result)

                total_tool_calls += 1
                total_tool_chars += chars

                print(
                    f"  {YELLOW}🔎 [TOOL round {tool_round}] "
                    f"{fc.name}({json.dumps(args, ensure_ascii=False)}) "
                    f"→ status={status}  chars={chars}  {tool_ms:.1f}ms{RESET}"
                )
                # Optionally show the full result (compact).
                compact = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
                if len(compact) <= 500:
                    print(f"  {GREY}{compact}{RESET}")
                else:
                    print(f"  {GREY}{compact[:500]}…{RESET}")

                function_responses.append(
                    types.Part.from_function_response(
                        name=fc.name,
                        response=result,
                    )
                )

            # Send the tool results back to the model.
            response = chat.send_message(function_responses)
            update_cost(response)

        # ── print the model's final text reply ─────────────────────────
        elapsed = time.perf_counter() - start
        text = response.text if response.text else "(no text response)"

        print(f"\n{CYAN}{BOLD}Neha >{RESET} {GREEN}{text}{RESET}")
        print(f"  {GREY}[turn {turn_count} | {elapsed:.2f}s | "
              f"tools this turn: {tool_round} | cost: ${turn_cost:.5f}]{RESET}")

    # ── summary ────────────────────────────────────────────────────────
    print(f"\n{BOLD}Session summary{RESET}")
    print(f"  Turns      : {turn_count}")
    print(f"  Tool calls : {total_tool_calls}")
    print(f"  Tool chars : {total_tool_chars}")
    print(f"  Total cost : ${session_cost:.5f}")
    print()


if __name__ == "__main__":
    run()
