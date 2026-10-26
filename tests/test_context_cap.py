"""Context cap: GEMINI_TRIGGER_TOKENS / GEMINI_TARGET_TOKENS in .env (operator, 2026-10-26)."""

from __future__ import annotations

import inspect
import os
import unittest
from unittest import mock

from tests.harness.appctl import LogCapture, app


def cap(trigger=None, target=None):
    env = {k: v for k, v in os.environ.items() if k not in ("GEMINI_TRIGGER_TOKENS", "GEMINI_TARGET_TOKENS")}
    if trigger is not None:
        env["GEMINI_TRIGGER_TOKENS"] = trigger
    if target is not None:
        env["GEMINI_TARGET_TOKENS"] = target
    with mock.patch.dict(os.environ, env, clear=True), LogCapture() as log:
        return app._context_cap(), log.lines


class TestContextCapSetting(unittest.TestCase):
    def test_off_when_missing(self):
        self.assertEqual(cap(), (None, []))

    def test_a_good_pair_is_used(self):
        self.assertEqual(cap("14000", "9000")[0], (14000, 9000))

    def test_bad_values_switch_it_off_with_a_warning(self):
        for trigger, target in [("abc", "9000"), ("14000", None), ("4000", "2000"), ("200000", "9000"),
                                ("14000", "14000"), ("14000", "0"), ("14000", "-5")]:
            with self.subTest(trigger=trigger, target=target):
                value, lines = cap(trigger, target)
                self.assertIsNone(value)
                self.assertTrue(any("context cap" in line for line in lines), lines)


class TestCompressionConfig(unittest.TestCase):
    def test_default_is_geminis_own_window(self):
        with mock.patch.object(app, "CONTEXT_CAP", None):
            config = app.context_compression_config()
        self.assertIsNone(config.trigger_tokens)
        self.assertIsNone(config.sliding_window.target_tokens)

    def test_cap_sets_trigger_and_target(self):
        with mock.patch.object(app, "CONTEXT_CAP", (14000, 9000)):
            config = app.context_compression_config()
        self.assertEqual(config.trigger_tokens, 14000)
        self.assertEqual(config.sliding_window.target_tokens, 9000)

    def test_every_gemini_session_uses_it(self):
        self.assertIn("context_window_compression=context_compression_config()",
                      inspect.getsource(app.handle_media_stream))


class FakeSession:
    def __init__(self):
        self.sent = []

    async def send_client_content(self, turns, turn_complete):
        self.sent.append((turns.parts[0].text, turn_complete))


class TestFactsNote(unittest.TestCase):
    def state(self):
        return {"facts": {}, "catalog_found": []}

    def test_no_facts_no_note(self):
        state = self.state()
        app.note_facts(state, {"keyword": "robotics"})
        self.assertIsNone(app.facts_note(state))

    def test_note_holds_marks_subjects_and_programs_and_later_calls_overwrite(self):
        state = self.state()
        app.note_facts(state, {"aggregate_pct": 58, "subjects": ["Physics", "Biology"], "degree": ""})
        app.note_facts(state, {"aggregate_pct": 62})
        state["catalog_found"] = ["B.Tech CSE"]
        note = app.facts_note(state)
        self.assertIn("aggregate pct 62", note)
        self.assertIn("subjects Physics, Biology", note)
        self.assertIn("programs looked up: B.Tech CSE", note)
        self.assertNotIn("58", note)
        self.assertNotIn("degree", note)
        self.assertIn("do not reply to it", note)

    def test_note_is_sent_without_ending_the_turn(self):
        import asyncio
        state, session = self.state(), FakeSession()
        app.note_facts(state, {"qualifying_pct": 72})
        with LogCapture():
            asyncio.run(app.send_facts_note(session, state))
        self.assertEqual(len(session.sent), 1)
        self.assertIn("qualifying pct 72", session.sent[0][0])
        self.assertFalse(session.sent[0][1])

    def test_nothing_sent_without_facts(self):
        import asyncio
        session = FakeSession()
        asyncio.run(app.send_facts_note(session, self.state()))
        self.assertEqual(session.sent, [])

    def test_wiring(self):
        self.assertIn("note_facts(call_state, args_dict)", inspect.getsource(app.stream_gemini_to_plivo))
        self.assertIn("await send_facts_note(session, call_state)", inspect.getsource(app.stream_gemini_to_plivo))


if __name__ == "__main__":
    unittest.main()
