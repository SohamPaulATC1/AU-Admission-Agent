"""Task 4.4 -- tool-call deferral and the silence watchdog.

OBSERVATION-FIRST. Requirements 3.7, 3.8.

Golden records observed on unfixed code:

* While ``tool_call_in_progress`` is set, a speech onset still flips
  ``is_speaking`` and still logs "User speech detected", but the
  ``activityStart`` is suppressed (app.py 1381) and the audio keeps accumulating
  in ``preroll_pcm16`` up to ``PREROLL_MAX_BYTES_PCM16`` (6400). Nothing is sent
  to the model.
* On tool-call completion the deferred branch (app.py 1600-1622) sends
  ``activityStart`` and then ONE audio blob that is exactly
  ``preroll_pcm16 + gemini_input_buffer``, in that order, and clears both.
* Silence watchdog: follow-up 1, follow-up 2, then the farewell. The counter
  stops at ``MAX_SILENCE_FOLLOWUPS`` (2) and the farewell sets
  ``closing_audio_phase``.

CORRECTION, and it matters for whether 5.9 can preserve 3.7: the deferred
prepend at app.py 1620-1622 is **unreachable through either declared tool**.
Both the ``endCall`` and ``transferCall`` branches set ``is_speaking = False``
and ``user_activity_open = False`` before the ``finally`` block runs (app.py
1509-1510 and 1546-1547), and the ``finally`` gate is
``if call_state.get("is_speaking") and not call_state.get("user_activity_open")``.
The only ways in are (a) a tool name matching neither branch -- now the
unknown-tool ``else``, which answers with an error tool_response first, and also
any course catalog tool, which leaves ``is_speaking`` alone -- or (b) the inbound
task setting
``is_speaking`` True during the ``await session.send_tool_response(...)`` inside
the ``finally``, which is a race and not deterministically reproducible offline.
This test exercises route (a) and records why: a golden record of dead code is
still the baseline that must not change, and the deferral logic itself is what
requirement 3.7 protects.
"""

from __future__ import annotations

import asyncio
import unittest

from tests.harness import scenarios
from tests.harness.appctl import LogCapture, app, live_call_state
from tests.harness.fakes import (
    FakeClock,
    FakePlivoClient,
    FakePlivoWS,
    FakeSession,
    InboundDriver,
    resp_tool_call,
    run_gemini_output,
)

PREROLL_MARKER = b"\xAA\xBB" * 160        # 320 bytes
INPUT_BUFFER_MARKER = b"\xCC\xDD" * 8     # 16 bytes


class TestSpeechDuringToolCallLandsInPreroll(unittest.TestCase):
    def setUp(self):
        async def scenario():
            clock = FakeClock()
            ws = FakePlivoWS(clock)
            session = FakeSession()
            call_state = live_call_state(tool_call_in_progress=True)
            with clock.install(), LogCapture() as log:
                async with InboundDriver(call_state, ws, session) as driver:
                    for frame in scenarios.caller_speech_frames(10):
                        await driver.feed_pcm8(frame)
            return call_state, ws, session, log, driver

        self.call_state, self.ws, self.session, self.log, self.driver = asyncio.run(scenario())

    def test_speech_is_detected_but_no_activity_is_opened(self):
        self.assertTrue(self.call_state["is_speaking"])
        self.assertFalse(self.call_state["user_activity_open"])
        self.assertTrue(self.log.contains("User speech detected"))
        self.assertFalse(self.log.contains("Sent activityStart"))

    def test_nothing_reaches_the_model_or_plivo(self):
        self.assertEqual(self.session.sent, [])
        self.assertEqual(self.ws.frames, [])

    def test_audio_accumulates_in_preroll_within_the_bound(self):
        self.assertEqual(len(self.call_state["preroll_pcm16"]), 6080)
        self.assertLessEqual(len(self.call_state["preroll_pcm16"]), app.PREROLL_MAX_BYTES_PCM16)
        self.assertEqual(len(self.call_state["gemini_input_buffer"]), 0)

    def test_preroll_is_monotonically_growing_until_the_bound(self):
        sizes = [snap["preroll_bytes"] for snap in self.driver.snapshots]
        self.assertEqual(sizes, sorted(sizes))

    def test_interrupting_flag_is_untouched(self):
        self.assertFalse(self.call_state["interrupting"])


class TestPrerollPrependOnToolCompletion(unittest.TestCase):
    def setUp(self):
        async def scenario():
            clock = FakeClock()
            ws = FakePlivoWS(clock)
            client = FakePlivoClient()
            call_state = live_call_state(
                with_denoiser=False,
                is_speaking=True,
                preroll_pcm16=bytearray(PREROLL_MARKER),
                gemini_input_buffer=bytearray(INPUT_BUFFER_MARKER),
            )
            # Route (a): a tool name matching neither declared branch, so nothing
            # resets ``is_speaking`` before the ``finally`` gate is evaluated.
            session = FakeSession([resp_tool_call("someUnknownTool", {"x": 1})])
            with clock.install(), LogCapture() as log:
                await run_gemini_output(session, ws, call_state, client)
            return call_state, ws, session, log

        self.call_state, self.ws, self.session, self.log = asyncio.run(scenario())

    def test_ordering_is_activity_start_then_one_prepended_blob(self):
        # INTENTIONAL BASELINE UPDATE — course catalog tools: an unknown tool
        # name now gets an error tool_response (it used to get none, leaving
        # Gemini waiting). The deferred activityStart and the one prepended
        # blob still follow, in the same order.
        self.assertEqual(self.session.sent_kinds, ["tool_response", "activityStart", "audio"])
        self.assertEqual(self.session.audio_bytes_sent, PREROLL_MARKER + INPUT_BUFFER_MARKER)

    def test_both_buffers_are_cleared(self):
        self.assertEqual(len(self.call_state["preroll_pcm16"]), 0)
        self.assertEqual(len(self.call_state["gemini_input_buffer"]), 0)

    def test_state_flags_and_log_line(self):
        self.assertTrue(self.call_state["user_activity_open"])
        self.assertFalse(self.call_state["tool_call_in_progress"])
        self.assertFalse(self.call_state["turn_complete"])
        self.assertFalse(self.call_state["awaiting_model"])
        self.assertIsNone(self.call_state["model_response_deadline"])
        self.assertEqual(
            self.log.matching("deferred activityStart"),
            ["\u25B6\uFE0F Sent deferred activityStart to Gemini (tool call finished)"],
        )

    def test_declared_tools_cannot_reach_this_branch(self):
        """Pins the unreachability finding above, for both declared tools."""
        for tool_name, args in (
            ("endCall", {"summary_of_whole_call": "done"}),
            ("transferCall", {"call_summary": "s", "language": "bengali"}),
        ):
            async def scenario(tool_name=tool_name, args=args):
                clock = FakeClock()
                ws = FakePlivoWS(clock)
                client = FakePlivoClient()
                call_state = live_call_state(with_denoiser=False, is_speaking=True,
                                            preroll_pcm16=bytearray(PREROLL_MARKER))
                session = FakeSession([resp_tool_call(tool_name, args)])
                with clock.install(), LogCapture() as log:
                    await run_gemini_output(session, ws, call_state, client)
                return call_state, session, log

            call_state, session, log = asyncio.run(scenario())
            self.assertEqual(session.sent_kinds, ["tool_response"], tool_name)
            self.assertFalse(log.contains("deferred activityStart"), tool_name)
            self.assertFalse(call_state["is_speaking"], tool_name)
            self.assertEqual(len(call_state["preroll_pcm16"]), len(PREROLL_MARKER), tool_name)


class TestTerminalToolCallStateAndLogs(unittest.TestCase):
    def test_end_call_tool_golden_record(self):
        async def scenario():
            clock = FakeClock()
            ws = FakePlivoWS(clock)
            client = FakePlivoClient()
            call_state = live_call_state(with_denoiser=False)
            session = FakeSession([resp_tool_call("endCall", {"summary_of_whole_call": "all done"})])
            with clock.install(), LogCapture() as log:
                await run_gemini_output(session, ws, call_state, client)
            return call_state, session, log

        call_state, session, log = asyncio.run(scenario())
        self.assertTrue(call_state["pending_end_call"])
        self.assertTrue(call_state["closing_audio_phase"])
        self.assertFalse(call_state["end_call_tool_executed"])
        self.assertFalse(call_state["terminate_session"])
        self.assertEqual(call_state["end_call_summary"], "all done")
        self.assertEqual([line for line in log.lines if "cancelled" not in line], [
            "Ready to stream audio from Gemini to Plivo",
            "\n[\u2699\uFE0F Gemini requested tool execution: endCall]",
            "Arguments: {'summary_of_whole_call': 'all done'}",
            "\U0001F4CB End-call summary captured from Gemini: all done",
            "\U0001F4F4 Call ending requested by Gemini; MCP endCall will execute after closing"
            " audio playback finishes",
        ])

    def test_transfer_call_tool_golden_record(self):
        async def scenario():
            clock = FakeClock()
            ws = FakePlivoWS(clock)
            client = FakePlivoClient()
            call_state = live_call_state(with_denoiser=False)
            session = FakeSession([
                resp_tool_call("transferCall", {"call_summary": "s", "language": "bengali"})
            ])
            with clock.install(), LogCapture() as log:
                await run_gemini_output(session, ws, call_state, client)
            return call_state, log

        call_state, log = asyncio.run(scenario())
        self.assertTrue(call_state["pending_transfer_call"])
        self.assertEqual(call_state["transfer_summary"],
                         '{"call_summary": "s", "language": "bengali"}')
        self.assertTrue(log.contains("Call transfer requested by Gemini"))


class TestSilenceWatchdogSchedule(unittest.TestCase):
    def setUp(self):
        async def scenario():
            clock = FakeClock()
            session = FakeSession()
            call_state = live_call_state(with_denoiser=False, with_aec=False)
            states = []
            with clock.install(), LogCapture() as log:
                for _ in range(3):
                    await app.silence_watchdog(session, call_state, delay=0.0)
                    states.append({
                        "count": call_state["silence_followup_count"],
                        "closing": call_state["closing_audio_phase"],
                        "awaiting": call_state["awaiting_model"],
                        "deadline": call_state["model_response_deadline"],
                    })
            return call_state, session, log, states, clock

        self.call_state, self.session, self.log, self.states, self.clock = asyncio.run(scenario())

    def test_constants(self):
        self.assertEqual(app.MAX_SILENCE_FOLLOWUPS, 2)
        self.assertEqual(app.SILENCE_FOLLOWUP_SECONDS, 8.0)

    def test_followup_then_followup_then_farewell(self):
        self.assertEqual([state["count"] for state in self.states], [1, 2, 2])
        self.assertEqual([state["closing"] for state in self.states], [False, False, True])
        self.assertEqual(self.session.sent_kinds, ["text", "text", "text"])
        self.assertEqual(self.session.sent[0][1], app.SILENCE_FOLLOWUP_PROMPT)
        self.assertEqual(self.session.sent[1][1], app.SILENCE_FOLLOWUP_PROMPT)
        self.assertEqual(self.session.sent[2][1], app.SILENCE_FAREWELL_PROMPT)

    def test_log_sequence(self):
        self.assertEqual(self.log.lines, [
            "Prompting AI to re-engage (1/2).",
            "Prompting AI to re-engage (2/2).",
            "Maximum silence follow-ups reached. Sending farewell prompt before ending call.",
        ])

    def test_deadlines_are_set_from_the_monotonic_clock(self):
        expected = self.clock.monotonic() + app.MODEL_RESPONSE_TIMEOUT_SECONDS
        self.assertAlmostEqual(self.states[-1]["deadline"], expected, places=6)
        self.assertIsNotNone(self.call_state["terminal_action_deadline"])

    def test_watchdog_is_a_no_op_while_activity_is_open_or_terminating(self):
        for flag in ("terminate_session", "user_activity_open"):
            async def scenario(flag=flag):
                session = FakeSession()
                call_state = live_call_state(with_denoiser=False, with_aec=False, **{flag: True})
                with LogCapture() as log:
                    await app.silence_watchdog(session, call_state, delay=0.0)
                return call_state, session, log

            call_state, session, log = asyncio.run(scenario())
            self.assertEqual(session.sent, [], flag)
            self.assertEqual(log.lines, [], flag)
            self.assertEqual(call_state["silence_followup_count"], 0, flag)


if __name__ == "__main__":
    unittest.main()
