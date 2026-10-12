"""
Bedrock Transcript Summarizer
==============================
Invokes Claude 3.5 Haiku on Amazon Bedrock to summarize a call transcript.

Expects the following environment variables to be set BEFORE running:
  - AWS_ACCESS_KEY_ID
  - AWS_SECRET_ACCESS_KEY
  - AWS_DEFAULT_REGION

Usage:
    python bedrock_summary.py <path_to_transcript_file>

Example:
    python bedrock_summary.py TEST_FILES\\_transcripts\\TEST8.txt
"""

import sys
import os
import json
import boto3

# Ensure UTF-8 output on Windows consoles (handles ₹, Bengali chars, etc.)
sys.stdout.reconfigure(encoding="utf-8")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
MODEL_ID = "us.anthropic.claude-haiku-5-5"  # Claude Haiku 5.5 (cross-region inference profile)
MAX_TOKENS = 2048

# The summarization prompt (embedded in the script as requested)
SYSTEM_PROMPT = (
    "You are an expert call-transcript analyst. "
    "Given a raw transcript of a phone call between a university admission agent and a prospective student, "
    "produce a concise, well-structured summary that includes:\n"
    "1. **Caller Details** – name, current qualification, CGPA/percentage mentioned.\n"
    "2. **Program of Interest** – what program(s) the caller asked about.\n"
    "3. **Key Discussion Points** – scholarship details, campus visit, distance learning, etc.\n"
    "4. **Outcome / Next Steps** – what was agreed upon or left pending.\n"
    "5. **Issues / Observations** – any friction points, repeated questions, or areas where the agent could improve.\n\n"
    "Keep the summary under 300 words. Use bullet points for clarity. "
    "If the transcript is in a language other than English, still write the summary in English."
)


def load_transcript(file_path: str) -> str:
    """Read the transcript file and return its contents as a string."""
    if not os.path.isfile(file_path):
        print(f"ERROR: File not found – {file_path}")
        sys.exit(1)

    with open(file_path, "r", encoding="utf-8") as f:
        return f.read()


def invoke_bedrock(transcript_text: str) -> str:
    """Send the transcript to Claude 3.5 Haiku via Amazon Bedrock and return the summary."""

    # boto3 automatically picks up AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY,
    # and AWS_DEFAULT_REGION from the environment – no .env parsing needed.
    client = boto3.client("bedrock-runtime")

    body = json.dumps({
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": MAX_TOKENS,
        "system": SYSTEM_PROMPT,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Here is the call transcript to summarise:\n\n"
                            "---\n"
                            f"{transcript_text}\n"
                            "---\n\n"
                            "Please provide a structured summary."
                        ),
                    }
                ],
            }
        ],
    })

    print(f"[INFO] Invoking model: {MODEL_ID}")
    print(f"[INFO] Region:         {os.environ.get('AWS_DEFAULT_REGION', '(not set)')}")
    print(f"[INFO] Transcript len: {len(transcript_text)} chars\n")

    response = client.invoke_model(
        modelId=MODEL_ID,
        contentType="application/json",
        accept="application/json",
        body=body,
    )

    response_body = json.loads(response["body"].read())

    # Claude Haiku 5.5 may return thinking blocks before the text block.
    # Extract only the text-type content blocks.
    text_parts = [
        block["text"]
        for block in response_body.get("content", [])
        if block.get("type") == "text"
    ]
    if not text_parts:
        print("[DEBUG] Full response body:")
        print(json.dumps(response_body, indent=2, ensure_ascii=False))
        raise RuntimeError("No text content found in the model response.")

    summary = "\n".join(text_parts)
    return summary


def main():
    # Default to the TEST8 transcript if no argument is supplied
    if len(sys.argv) < 2:
        default_path = os.path.join(
            os.path.dirname(__file__),
            "TEST_FILES", "_transcripts", "TEST8.txt",
        )
        file_path = default_path
        print(f"[INFO] No file argument given – using default: {file_path}\n")
    else:
        file_path = sys.argv[1]

    transcript = load_transcript(file_path)
    summary = invoke_bedrock(transcript)

    print("=" * 60)
    print("TRANSCRIPT SUMMARY")
    print("=" * 60)
    print(summary)
    print("=" * 60)


if __name__ == "__main__":
    main()
