"""call_analysis.py: the Haiku 5.5 request and its reply (docs/spec/call-analysis-design.md §3)."""

from __future__ import annotations

import inspect
import json
import os
import subprocess
import sys
import types
import unittest

import call_analysis
import call_store
import course_catalog
from tests.test_call_store import PROGRAM, SAMPLE_FIELDS

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def make_response(text=None, stop="end_turn", tokens=(3000, 500), category=None, blocks=None):
    """A stand-in for an anthropic Message: content blocks, stop reason, usage."""
    if blocks is None:
        blocks = [types.SimpleNamespace(type="thinking", thinking="")]
        if text is not None:
            blocks.append(types.SimpleNamespace(type="text", text=text))
    usage = None if tokens is None else types.SimpleNamespace(input_tokens=tokens[0], output_tokens=tokens[1])
    details = None if category is None else types.SimpleNamespace(category=category)
    return types.SimpleNamespace(content=blocks, stop_reason=stop, usage=usage, stop_details=details)


CONTEXT = {"name": "Asha Das", "end_reason": "Parent agreed to visit on Saturday.", "duration_sec": 95,
           "legs": 1, "programs_looked_up": [], "b_legs": []}
TRANSCRIPT = "call_uuid: u-1\n\n[00:00] Neha: নমস্কার\n[00:05] User: হ্যাঁ বলুন\n"


class TestConfig(unittest.TestCase):
    def _run(self, code, **env):
        clean = {k: v for k, v in os.environ.items()
                 if not k.startswith(("ANALYSIS_", "NLP_PRICE_"))}
        clean.update(env)
        return subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, env=clean,
                              capture_output=True, text=True)

    def test_defaults(self):
        result = self._run("import call_analysis as c; print(c.ANALYSIS_ENABLED, c.ANALYSIS_MODEL, "
                           "c.ANALYSIS_CONCURRENCY, c.NLP_PRICE_INPUT, c.NLP_PRICE_OUTPUT)")
        self.assertEqual(result.stdout.split(), ["True", "us.anthropic.claude-haiku-5-5", "2", "0.11", "0.55"])

    def test_bad_switch_fails_at_import(self):
        result = self._run("import call_analysis", ANALYSIS_ENABLED="yes")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ANALYSIS_ENABLED must be", result.stderr)


class TestSchema(unittest.TestCase):
    def test_fields_and_strictness(self):
        schema = call_analysis.SCHEMA
        self.assertEqual(set(schema["properties"]), set(call_store.ANALYSIS_FIELDS))
        self.assertEqual(schema["required"], list(call_store.ANALYSIS_FIELDS))
        self.assertIs(schema["additionalProperties"], False)
        json.dumps(schema)

    def test_choices(self):
        props = call_analysis.SCHEMA["properties"]
        self.assertEqual(props["lead_status"]["enum"],
                         ["hot", "warm", "cold", "not_interested", "callback_requested", "wrong_number",
                          "no_conversation"])
        self.assertEqual(props["interest_score"]["enum"], list(range(1, 11)))
        names = [r["name"] for r in course_catalog.RECORDS]
        self.assertEqual(props["primary_program"]["enum"], names + [None])
        self.assertEqual(props["programs_of_interest"]["items"]["enum"], names)
        self.assertEqual(len(names), 87)


class TestRequest(unittest.TestCase):
    def test_request_shape(self):
        request = call_analysis.build_request(CONTEXT, TRANSCRIPT)
        self.assertEqual(request["model"], "us.anthropic.claude-haiku-5-5")
        self.assertEqual(request["max_tokens"], 4000)
        self.assertEqual(request["system"], call_analysis.SYSTEM_PROMPT)
        self.assertEqual(request["output_config"],
                         {"effort": "low", "format": {"type": "json_schema", "schema": call_analysis.SCHEMA}})
        for banned in ("temperature", "top_p", "top_k"):
            self.assertNotIn(banned, request)
        [message] = request["messages"]
        self.assertEqual(message["role"], "user")
        self.assertEqual(call_analysis.build_request(CONTEXT, TRANSCRIPT, model="m")["model"], "m")

    def test_user_message(self):
        text = call_analysis.user_message(CONTEXT, TRANSCRIPT)
        self.assertIn("Contact name: Asha Das\n", text)
        self.assertIn("Call length: 95 seconds\n", text)
        self.assertIn("Agent's end-of-call note: Parent agreed to visit on Saturday.\n", text)
        for name in call_analysis.PROGRAM_NAMES:
            self.assertIn(f"- {name}\n", text)
        self.assertTrue(text.endswith("Transcript:\n" + TRANSCRIPT.strip()))

    def test_user_message_drops_old_cost_lines(self):
        old = TRANSCRIPT + "\ntranscription_cost_usd: 0.0011 (audio_in=1 text_in=2 output=3 tokens, model=m)\n"
        text = call_analysis.user_message(CONTEXT, old)
        self.assertNotIn("transcription_cost_usd", text)
        self.assertTrue(text.endswith("Transcript:\n" + TRANSCRIPT.strip()))

    def test_user_message_unknowns(self):
        text = call_analysis.user_message({}, "")
        self.assertIn("Contact name: unknown\n", text)
        self.assertIn("Call length: unknown seconds\n", text)
        self.assertIn("Agent's end-of-call note: none\n", text)
        self.assertTrue(text.endswith("Transcript:\n(empty)"))

    def test_system_prompt_defines_every_choice(self):
        for status in call_analysis.LEAD_STATUSES:
            self.assertIn(f"- {status}:", call_analysis.SYSTEM_PROMPT)
        for phrase in ("1 (none) to 10", "English", "null", "Never guess", "User (fallback)"):
            self.assertIn(phrase, call_analysis.SYSTEM_PROMPT)


class TestParseReply(unittest.TestCase):
    def test_good_reply(self):
        fields, problem, tin, tout = call_analysis.parse_reply(make_response(json.dumps(SAMPLE_FIELDS)))
        self.assertEqual((fields, problem, tin, tout), (SAMPLE_FIELDS, None, 3000, 500))

    def test_refusal(self):
        self.assertEqual(call_analysis.parse_reply(make_response(None, stop="refusal", category="cyber"))[1],
                         "refusal: cyber")
        self.assertEqual(call_analysis.parse_reply(make_response(None, stop="refusal"))[1], "refusal: unknown")

    def test_cut_off_or_empty(self):
        self.assertEqual(call_analysis.parse_reply(make_response("{", stop="max_tokens"))[1], "max_tokens")
        self.assertEqual(call_analysis.parse_reply(make_response(None))[1], "no text in reply")

    def test_bad_json(self):
        self.assertEqual(call_analysis.parse_reply(make_response("not json"))[1], "bad json")
        self.assertEqual(call_analysis.parse_reply(make_response("[1, 2]"))[1], "bad json")

    def test_missing_field(self):
        partial = {k: v for k, v in SAMPLE_FIELDS.items() if k not in ("summary", "language")}
        self.assertEqual(call_analysis.parse_reply(make_response(json.dumps(partial)))[1],
                         "missing fields: language, summary")

    def test_no_usage(self):
        _, _, tin, tout = call_analysis.parse_reply(make_response(json.dumps(SAMPLE_FIELDS), tokens=None))
        self.assertEqual((tin, tout), (0, 0))


class TestSchoolAndCost(unittest.TestCase):
    def test_school_from_primary_program(self):
        school = course_catalog.RECORDS[0]["school"]
        self.assertEqual(call_analysis.school_for(PROGRAM, []), (school["code"], school["name"], "haiku"))

    def test_school_from_the_lookups(self):
        last = course_catalog.RECORDS[-1]
        self.assertEqual(call_analysis.school_for(None, ["not a program", last["name"]]),
                         (last["school"]["code"], last["school"]["name"], "lookup"))

    def test_no_school(self):
        self.assertEqual(call_analysis.school_for(None, []), (None, None, "none"))
        self.assertEqual(call_analysis.school_for("not a program", ["also not"]), (None, None, "none"))

    def test_cost(self):
        # (3000 * 0.11 + 500 * 0.55) / 1e6 = 0.000605
        self.assertAlmostEqual(call_analysis.nlp_cost(3000, 500), 0.000605, places=12)


class TestTranscriptHelpers(unittest.TestCase):
    def test_has_caller_lines(self):
        self.assertTrue(call_analysis.has_caller_lines(TRANSCRIPT))
        self.assertTrue(call_analysis.has_caller_lines("[00:12] User (fallback): ঠিক আছে।\n"))
        self.assertTrue(call_analysis.has_caller_lines("[105:02] User: x\n"))
        self.assertFalse(call_analysis.has_caller_lines("[00:00] Neha: Hello\n"))
        self.assertFalse(call_analysis.has_caller_lines(""))

    def test_old_cost_lines(self):
        line = "transcription_cost_usd: {} (audio_in=1 text_in=2 output=3 tokens, model=m)"
        one = "call_uuid: u\n\n[00:00] Neha: hi\n\n" + line.format("0.0011") + "\n"
        self.assertAlmostEqual(call_analysis.old_stt_cost(one), 0.0011)
        two = one + "\n" + "=" * 60 + "\n\n" + one.replace("0.0011", "0.0020")
        self.assertAlmostEqual(call_analysis.old_stt_cost(two), 0.0031)
        self.assertIsNone(call_analysis.old_stt_cost("call_uuid: u\n\n[00:00] Neha: hi\n"))

    def test_anthropic_is_imported_only_inside_the_client_factory(self):
        with open(call_analysis.__file__, encoding="utf-8") as handle:
            source = handle.read()
        self.assertEqual(source.count("import anthropic"), 1)
        self.assertIn("import anthropic", inspect.getsource(call_analysis._default_client))


if __name__ == "__main__":
    unittest.main()
