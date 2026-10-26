"""Runaway guard: EMPTY_TURN_LIMIT empty model turns in a row end the call (operator, 2026-10-26)."""

from __future__ import annotations

import asyncio
import unittest

from tests.harness.appctl import LogCapture, app, live_call_state
from tests.harness.fakes import (
    FakeClock,
    FakePlivoClient,
    FakePlivoWS,
    FakeSession,
    resp_interrupted,
    resp_output_transcription,
    resp_turn_complete,
    run_gemini_output,
)

EMPTY = "<no speech>{pause}"


def run(script, **state):
    async def scenario():
        clock = FakeClock()
        client = FakePlivoClient()
        call_state = live_call_state(with_denoiser=False, call_uuid="call-1", **state)
        with clock.install(), LogCapture() as log:
            await run_gemini_output(FakeSession(script), FakePlivoWS(clock), call_state, client)
        return call_state, client, log

    return asyncio.run(scenario())


def turn(text):
    return [resp_output_transcription(text), resp_turn_complete()]


class TestIsEmptyModelTurn(unittest.TestCase):
    def test_only_the_markers_count(self):
        self.assertTrue(app.is_empty_model_turn(EMPTY))
        self.assertTrue(app.is_empty_model_turn("<no speech>"))
        self.assertTrue(app.is_empty_model_turn(" {pause} <no speech> "))
        self.assertFalse(app.is_empty_model_turn(""))
        self.assertFalse(app.is_empty_model_turn("Hello, I am Neha."))
        self.assertFalse(app.is_empty_model_turn("{pause} Hello"))


class TestGuard(unittest.TestCase):
    def test_two_empty_turns_in_a_row_hang_up(self):
        call_state, client, log = run(turn(EMPTY) + turn(EMPTY))
        self.assertEqual(client.deleted, ["call-1"])
        self.assertTrue(log.contains("model sent 2 empty turns in a row"))

    def test_one_empty_turn_does_not(self):
        call_state, client, _ = run(turn(EMPTY))
        self.assertEqual(client.deleted, [])
        self.assertEqual(call_state["empty_model_turns"], 1)

    def test_a_real_turn_between_resets_the_count(self):
        call_state, client, _ = run(turn(EMPTY) + turn("Which program?") + turn(EMPTY))
        self.assertEqual(client.deleted, [])
        self.assertEqual(call_state["empty_model_turns"], 1)

    def test_an_interrupted_real_turn_resets_the_count(self):
        call_state, client, _ = run(turn(EMPTY) + [resp_output_transcription("Which prog"), resp_interrupted()]
                                    + turn(EMPTY))
        self.assertEqual(client.deleted, [])

    def test_a_turn_with_no_text_is_ignored(self):
        # a tool-call turn has no transcript: it neither counts nor resets
        call_state, client, _ = run(turn(EMPTY) + [resp_turn_complete()] + turn(EMPTY))
        self.assertEqual(client.deleted, ["call-1"])


if __name__ == "__main__":
    unittest.main()
