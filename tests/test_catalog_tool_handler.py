"""Course catalog tools wired into app.py's tool-call branch (spec section 7).

A catalog call is answered in the same turn and leaves the call running: no
terminal flags, no VAD reset. An unknown tool name now gets an error response
instead of no response at all.
"""

from __future__ import annotations

import asyncio
import unittest

import course_catalog
from tests.harness.appctl import LogCapture, app, live_call_state
from tests.harness.fakes import (
    FakeClock,
    FakePlivoClient,
    FakePlivoWS,
    FakeSession,
    resp_tool_call,
    run_gemini_output,
)


def run_tool_calls(*calls, **state):
    async def scenario():
        clock = FakeClock()
        ws = FakePlivoWS(clock)
        client = FakePlivoClient()
        call_state = live_call_state(with_denoiser=False, **state)
        session = FakeSession([resp_tool_call(name, args, call_id=f"call-{i}")
                               for i, (name, args) in enumerate(calls, 1)])
        with clock.install(), LogCapture() as log:
            await run_gemini_output(session, ws, call_state, client)
        return call_state, session, log

    return asyncio.run(scenario())


def responses(session):
    return [fr for kind, payload in session.sent if kind == "tool_response"
            for fr in payload["function_responses"]]


class TestCatalogDeclarations(unittest.TestCase):
    def test_catalog_tools_are_declared_after_the_call_control_tools(self):
        names = [d.name for d in app.LOCAL_GEMINI_TOOLS[0]["function_declarations"]]
        self.assertEqual(names, ["endCall", "transferCall", "list_programs", "get_program_details"])


class TestCatalogToolCall(unittest.TestCase):
    ARGS = {"programs": ["cse"], "fields": ["fees"]}

    def setUp(self):
        self.call_state, self.session, self.log = run_tool_calls(("get_program_details", self.ARGS))

    def test_response_is_the_catalog_result(self):
        self.assertEqual(self.session.sent_kinds, ["tool_response"])
        [fr] = responses(self.session)
        self.assertEqual((fr.id, fr.name), ("call-1", "get_program_details"))
        self.assertEqual(fr.response, course_catalog.handle_tool_call("get_program_details", self.ARGS))

    def test_counters_and_log_line(self):
        expected = course_catalog.handle_tool_call("get_program_details", self.ARGS)
        chars = course_catalog.response_chars(expected)
        self.assertEqual(self.call_state["catalog_calls"], 1)
        self.assertEqual(self.call_state["catalog_chars"], chars)
        self.assertEqual(self.log.matching("[CATALOG]"),
                         [f"🔎 [CATALOG] tool=get_program_details status=found chars={chars} ms=0.0"])

    def test_call_keeps_running(self):
        for flag in ("pending_end_call", "pending_transfer_call", "closing_audio_phase",
                     "terminate_session", "tool_call_in_progress"):
            self.assertFalse(self.call_state[flag], flag)
        self.assertFalse(any("[CATALOG]" in line for line in self.log.existing_lines))

    def test_bad_arguments_are_answered_with_the_error(self):
        call_state, session, log = run_tool_calls(("list_programs", {"degree": "MBBS"}))
        [fr] = responses(session)
        self.assertIn("unknown degree 'MBBS'", fr.response["error"])
        self.assertEqual(call_state["catalog_calls"], 1)
        self.assertIn("status=error", log.matching("[CATALOG]")[0])

    def test_each_call_is_answered_and_counted(self):
        call_state, session, _ = run_tool_calls(("list_programs", {}),
                                                ("get_program_details", {"programs": ["mba"]}))
        self.assertEqual(session.sent_kinds, ["tool_response", "tool_response"])
        self.assertEqual([fr.id for fr in responses(session)], ["call-1", "call-2"])
        self.assertEqual(call_state["catalog_calls"], 2)

    def test_speech_during_lookup_opens_the_deferred_activity(self):
        call_state, session, log = run_tool_calls(("list_programs", {"degree": "MBA"}),
                                                  is_speaking=True)
        self.assertEqual(session.sent_kinds[:2], ["tool_response", "activityStart"])
        self.assertTrue(call_state["user_activity_open"])
        self.assertTrue(log.contains("deferred activityStart"))


class TestResponseWatchdogAfterLookup(unittest.TestCase):
    """After a non-terminal tool reply, Gemini owes the caller an answer: the
    response deadline is re-armed so a silent model still ends the call."""

    def run_calls(self, *calls, **state):
        async def scenario():
            clock = FakeClock()
            call_state = live_call_state(with_denoiser=False, **state)
            session = FakeSession([resp_tool_call(name, args, call_id=f"call-{i}")
                                   for i, (name, args) in enumerate(calls, 1)])
            with clock.install(), LogCapture():
                await run_gemini_output(session, FakePlivoWS(clock), call_state, FakePlivoClient())
            return call_state, clock.monotonic()

        return asyncio.run(scenario())

    def assertArmed(self, call_state, now):
        self.assertTrue(call_state["awaiting_model"])
        self.assertEqual(call_state["model_response_deadline"], now + app.MODEL_RESPONSE_TIMEOUT_SECONDS)

    def test_catalog_reply_rearms_the_deadline(self):
        self.assertArmed(*self.run_calls(("list_programs", {"degree": "MBA"})))

    def test_unknown_tool_reply_rearms_the_deadline(self):
        self.assertArmed(*self.run_calls(("bookSeat", {})))

    def test_end_call_is_left_to_the_terminal_deadline(self):
        call_state, _ = self.run_calls(("list_programs", {}),
                                       ("endCall", {"summary_of_whole_call": "s"}))
        self.assertFalse(call_state["awaiting_model"])
        self.assertIsNone(call_state["model_response_deadline"])

    def test_speech_during_lookup_leaves_it_to_the_caller_turn(self):
        call_state, _ = self.run_calls(("list_programs", {"degree": "MBA"}), is_speaking=True)
        self.assertTrue(call_state["user_activity_open"])
        self.assertFalse(call_state["awaiting_model"])
        self.assertIsNone(call_state["model_response_deadline"])


class TestUnknownTool(unittest.TestCase):
    def test_unknown_tool_gets_an_error_response(self):
        call_state, session, log = run_tool_calls(("bookSeat", {"x": 1}))
        [fr] = responses(session)
        self.assertEqual(fr.response, {"error": "unknown tool bookSeat"})
        self.assertEqual(call_state["catalog_calls"], 0)
        self.assertEqual(log.matching("Unknown tool"), ["[⚠️ Unknown tool requested by Gemini: bookSeat]"])


class TestCatalogStatsLine(unittest.TestCase):
    def test_stats_line_reports_the_totals(self):
        call_state = live_call_state(with_denoiser=False, with_aec=False,
                                     catalog_calls=2, catalog_chars=1500)
        with LogCapture() as log:
            app.log_call_stats(call_state)
        self.assertEqual(log.matching("[CATALOG]"), ["🔎 [CATALOG] calls=2 chars=1500"])


if __name__ == "__main__":
    unittest.main()
