"""R&D cost trace (operator, 2026-10-13): the Gemini cost so far, logged every COST_TRACE_SECONDS."""

from __future__ import annotations

import asyncio
import inspect
import os
import subprocess
import sys
import unittest
from unittest import mock

from tests.harness.appctl import REPO_ROOT, LogCapture, app, live_call_state


def counters(text_in=0, audio_in=0, text_out=0, audio_out=0, tx_est=0):
    state = live_call_state(with_denoiser=False, with_aec=False)
    state.update(tokens_text_in=text_in, tokens_audio_in=audio_in, tokens_text_out=text_out,
                 tokens_audio_out=audio_out, tokens_input_transcription_est=tx_est)
    return state


class TestGeminiCost(unittest.TestCase):
    def test_the_formula(self):
        state = counters(text_in=1_000_000, audio_in=1_000_000, text_out=1_000_000, audio_out=1_000_000,
                         tx_est=1_000_000)
        expected = (app.PRICE_TEXT_INPUT + app.PRICE_AUDIO_INPUT + app.PRICE_TEXT_OUTPUT + app.PRICE_AUDIO_OUTPUT
                    + app.PRICE_TEXT_OUTPUT)
        self.assertAlmostEqual(app.gemini_cost(state)["total"], expected)

    def test_the_end_of_call_stats_use_the_same_numbers(self):
        state = counters(text_in=123_456, audio_in=654_321, text_out=11_111, audio_out=22_222, tx_est=3_333)
        with mock.patch.object(app, "emit_call_event") as emit, LogCapture():
            app.log_call_stats(state)
        self.assertAlmostEqual(emit.call_args.args[2]["cost_usd"], round(app.gemini_cost(state)["total"], 6))


class TestRunCostTrace(unittest.TestCase):
    def test_one_line_per_interval_and_a_last_line_at_the_end(self):
        state = counters(text_in=1000)
        now = [0.0]

        async def sleep(seconds):
            now[0] += seconds
            state["tokens_audio_in"] += 5000   # the call goes on between samples
            await asyncio.sleep(0)

        async def go(log):
            task = asyncio.create_task(app.run_cost_trace(state, 60, sleep=sleep, clock=lambda: now[0]))
            for _ in range(200):   # until three samples are logged (a 3-minute call)
                await asyncio.sleep(0)
                if sum("sample=" in line for line in log.lines) >= 3:
                    break
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        with LogCapture() as log:
            asyncio.run(go(log))
        lines = [line for line in log.lines if "[COST-TRACE]" in line]
        self.assertGreaterEqual(len(lines), 4)
        self.assertIn("sample=1 t=60s", lines[0])
        self.assertIn("sample=2 t=120s", lines[1])
        self.assertIn("end t=", lines[-1])
        costs = [float(line.split("gemini=$")[1].split()[0]) for line in lines]
        self.assertEqual(costs, sorted(costs))   # it only grows
        self.assertIn("audio_in=5000", lines[0])

    def test_a_failing_sample_is_logged_and_the_trace_goes_on(self):
        calls = []

        async def sleep(seconds):
            calls.append(seconds)
            if len(calls) > 2:
                raise asyncio.CancelledError
            await asyncio.sleep(0)

        async def go():
            with mock.patch.object(app, "cost_trace_line", side_effect=[KeyError("x"), "ok line", "end line"]):
                with self.assertRaises(asyncio.CancelledError):
                    await app.run_cost_trace({}, 60, sleep=sleep, clock=lambda: 0.0)

        with LogCapture() as log:
            asyncio.run(go())
        self.assertTrue(any("[COST-TRACE] sample failed: KeyError" in line for line in log.lines), log.lines)
        self.assertEqual(len(calls), 3)

    def test_no_name_or_number_in_the_line(self):
        state = counters()
        state.update(from_number="9876543210", user_name="Asha")
        line = app.cost_trace_line(state, 60, "sample=1")
        self.assertNotIn("9876543210", line)
        self.assertNotIn("Asha", line)


class TestSwitch(unittest.TestCase):
    def setting(self, value):
        env = dict(os.environ)
        env.pop("COST_TRACE_SECONDS", None)
        if value is not None:
            env["COST_TRACE_SECONDS"] = value
        code = "from tests.harness.appctl import app; print(app.COST_TRACE_SECONDS)"
        result = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, env=env, capture_output=True,
                                text=True, timeout=300)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        return result.stdout.split()[-1]

    def test_off_by_default_and_bad_values_switch_it_off(self):
        self.assertEqual(self.setting(None), "0")
        self.assertEqual(self.setting("60"), "60")
        self.assertEqual(self.setting("abc"), "0")   # a typo must not stop the voice agent from starting
        self.assertEqual(self.setting("-5"), "0")

    def test_the_call_starts_and_stops_it(self):
        source = inspect.getsource(app.handle_media_stream)
        self.assertIn("if COST_TRACE_SECONDS:", source)
        self.assertIn("cost_trace_task = asyncio.create_task(run_cost_trace(call_state, COST_TRACE_SECONDS))", source)
        self.assertLess(source.index("cost_trace_task.cancel()"), source.index("log_call_stats(call_state)"))


if __name__ == "__main__":
    unittest.main()
