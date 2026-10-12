"""One real Haiku 5.5 analysis on an existing transcript (docs/spec/call-analysis-design.md §4).

Not a unit test: it calls Amazon Bedrock (about $0.001). Run on EC2 with the AWS keys loaded:
    set -a; . /etc/au-app/common.env; set +a
    venv/bin/python tests/analysis_spike.py TRANSCRIPTS/<call_uuid>.txt
Prints the stop reason, tokens, cost and the parsed fields. A schema error from Bedrock shows here
first: fix call_analysis.SCHEMA before deploying.
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import call_analysis  # noqa: E402


async def main(path):
    with open(path, encoding="utf-8") as handle:
        transcript = handle.read()
    client = call_analysis._default_client()
    request = call_analysis.build_request({"name": None, "end_reason": None, "duration_sec": None}, transcript)
    response = await client.messages.create(**request)
    fields, problem, tin, tout = call_analysis.parse_reply(response)
    print(f"model: {request['model']}")
    print(f"stop_reason: {response.stop_reason}  problem: {problem}")
    cache_write, cache_read = call_analysis.cache_tokens(response)
    cost = call_analysis.nlp_cost(tin, tout, cache_write, cache_read)
    print(f"tokens: in={tin} cache_write={cache_write} cache_read={cache_read} out={tout}  cost: ${cost:.5f}")
    print("(run it twice within 5 minutes: the second run should show cache_read > 0)")
    print(json.dumps(fields, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: analysis_spike.py TRANSCRIPTS/<call_uuid>.txt")
    asyncio.run(main(sys.argv[1]))
