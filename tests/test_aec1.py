"""aec1.py -- the path-B canceller, selected with AEC_IMPL=aec1 (2026-09-28).

aec.py stays byte-identical (test_preservation_4_7_aec_integrity) and remains
the default. aec1.py is the same PFDKF canceller with adaptation gated to
far-active, non-double-talk, non-silent blocks, a step-size ceiling, leakage,
and a two-path (background learns, foreground outputs) divergence reset. The
TEST2 defect it fixes: on a handset line that already cancels echo, stock
aec.py drifts and injects a filtered copy of the agent's voice.

Offline comparison on the TEST2-5 recordings: TEST_FILES/_aec_compare.py.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import subprocess
import sys
import unittest

import numpy as np

import aec
import aec1
from tests.harness import echo, scenarios
from tests.harness.appctl import REPO_ROOT, LogCapture, app, live_call_state
from tests.harness.fakes import FakeClock, FakePlivoWS, FakeSession, InboundDriver

FRAME = app.PLIVO_ULAW_CHUNK_SIZE          # 160 samples
STEP = FRAME * 2                           # bytes of PCM16 per 20 ms frame
FIXTURES = ("recorded.wav", "transfer.wav", "no_agent.wav", "reconnecting.wav")


def _far(min_seconds: float = 12.0) -> bytes:
    """Agent-side speech: the repo's playback fixtures, repeated to length."""
    one = b"".join(echo.load_pcm8k(name) for name in FIXTURES)
    reps = int(np.ceil(min_seconds * 8000 * 2 / len(one)))
    pcm = (one * reps)[: int(min_seconds * 8000) * 2]
    return pcm[: len(pcm) // STEP * STEP]


def _f64(pcm: bytes) -> np.ndarray:
    return np.frombuffer(pcm, dtype=np.int16).astype(np.float64) / 32768.0


def _pcm(sig: np.ndarray) -> bytes:
    return (np.clip(sig, -1.0, 32767 / 32768) * 32768).astype(np.int16).tobytes()


def _noise(samples: int, level_db: float, seed: int) -> np.ndarray:
    return np.random.default_rng(seed).normal(0.0, 10 ** (level_db / 20), samples)


def _run(canceller, far: bytes, near: bytes) -> bytes:
    out = bytearray()
    for i in range(len(far) // STEP):
        canceller.add_far_end(far[i * STEP:(i + 1) * STEP])
        out += canceller.process(near[i * STEP:(i + 1) * STEP])
    return bytes(out)


def _frame_energy(sig: np.ndarray) -> np.ndarray:
    n = sig.size // FRAME
    return np.sum(sig[: n * FRAME].reshape(n, FRAME) ** 2, axis=1)


def _db_ratio(num: np.ndarray, den: np.ndarray) -> float:
    return 10 * np.log10(np.sum(num ** 2) / (np.sum(den ** 2) + 1e-20))


class TestPublicSurfaceMatchesAec(unittest.TestCase):
    def test_same_methods_and_constructor(self):
        for name in ("add_far_end", "reset_far_end", "process"):
            self.assertTrue(callable(getattr(aec1.AcousticEchoCanceller, name, None)), name)
            self.assertEqual(
                inspect.signature(getattr(aec.AcousticEchoCanceller, name)),
                inspect.signature(getattr(aec1.AcousticEchoCanceller, name)), name)
        self.assertEqual(inspect.signature(aec.AcousticEchoCanceller.__init__),
                         inspect.signature(aec1.AcousticEchoCanceller.__init__))

    def test_stats_counters(self):
        canceller = aec1.AcousticEchoCanceller(frame_size=FRAME)
        self.assertEqual(set(canceller.stats), {
            "blocks", "far_active_blocks", "adapted_blocks", "doubletalk_blocks",
            "near_silent_blocks", "promotions", "foreground_resets", "background_resets"})
        self.assertTrue(all(value == 0 for value in canceller.stats.values()))

    def test_runs_in_the_real_inbound_loop(self):
        async def scenario():
            clock = FakeClock()
            ws = FakePlivoWS(clock)
            call_state = live_call_state(aec=aec1.AcousticEchoCanceller(frame_size=FRAME))
            with clock.install():
                async with InboundDriver(call_state, ws, FakeSession()) as driver:
                    for frame in scenarios.caller_speech_frames(10):
                        await driver.feed_pcm8(frame)
            return call_state

        call_state = asyncio.run(scenario())
        self.assertEqual(call_state["chunk_count"], 10)
        self.assertEqual(call_state["aec"].stats["blocks"], 10)


class TestHandsetLineIsPassedThrough(unittest.TestCase):
    """The phone already cancelled the echo: the line is -70 dBFS noise while
    the agent plays. There is nothing to cancel, so the output must be the
    input (within the int16 rescale, one LSB)."""

    def setUp(self):
        self.far = _far()
        self.near = _pcm(_noise(len(self.far) // 2, -70.0, seed=11))

    def test_aec1_output_is_the_input(self):
        canceller = aec1.AcousticEchoCanceller(frame_size=FRAME)
        out = _run(canceller, self.far, self.near)
        diff = np.abs(np.frombuffer(out, np.int16).astype(int)
                      - np.frombuffer(self.near, np.int16).astype(int))
        self.assertLessEqual(int(diff.max()), 1)
        self.assertEqual(canceller.stats["promotions"], 0)
        self.assertGreater(canceller.stats["far_active_blocks"], 0)

    def test_no_frame_is_louder_than_its_input(self):
        out = _run(aec1.AcousticEchoCanceller(frame_size=FRAME), self.far, self.near)
        louder = _frame_energy(_f64(out)) > _frame_energy(_f64(self.near))
        self.assertEqual(int(louder.sum()), 0)


class TestSpeakerphoneEchoIsCancelled(unittest.TestCase):
    """A real acoustic echo (100 ms, -10 dB) of the agent's speech."""

    def test_echo_is_reduced_after_convergence(self):
        far = _far()
        near = echo.synth_echo(far, delay_ms=100, attenuation_db=10, noise_db=-70,
                               length_samples=len(far) // 2)
        canceller = aec1.AcousticEchoCanceller(frame_size=FRAME)
        out = _f64(_run(canceller, far, near))
        half = out.size // 2
        erle = _db_ratio(_f64(near)[half:], out[half:])
        self.assertGreater(erle, 15.0, f"ERLE {erle:.1f} dB")  # measured 22.8
        self.assertGreaterEqual(canceller.stats["promotions"], 1)
        self.assertEqual(canceller.stats["foreground_resets"], 0)

    def test_caller_speech_survives_double_talk(self):
        """After convergence the caller talks over the agent. What reaches
        Gemini must be closer to the caller's voice than with no AEC at all."""
        far = _far(16.0)
        n = len(far) // 2
        echo_sig = _f64(echo.synth_echo(far, delay_ms=100, attenuation_db=10, noise_db=-70,
                                        length_samples=n))
        caller = np.zeros(n)
        voice = _f64(echo.load_pcm8k("transfer.wav"))
        start = 10 * 8000
        length = min(voice.size, n - start)
        caller[start:start + length] = voice[:length]
        near = echo_sig + caller
        canceller = aec1.AcousticEchoCanceller(frame_size=FRAME)
        out = _f64(_run(canceller, far, _pcm(near)))
        talk = slice(start, start + length)
        with_aec = _db_ratio(caller[talk], out[talk] - caller[talk])
        without = _db_ratio(caller[talk], echo_sig[talk])
        self.assertGreater(with_aec, without + 6.0,
                           f"signal-to-residual {with_aec:.1f} dB vs {without:.1f} dB without AEC")
        self.assertEqual(canceller.stats["foreground_resets"], 0)


class TestForegroundDivergenceReset(unittest.TestCase):
    def test_a_wrong_foreground_is_reset_to_pass_through(self):
        far = _far()
        near = echo.synth_echo(far, delay_ms=100, attenuation_db=10, noise_db=-70,
                               length_samples=len(far) // 2)
        canceller = aec1.AcousticEchoCanceller(frame_size=FRAME)
        split = (len(far) // STEP) // 2 * STEP
        _run(canceller, far[:split], near[:split])
        self.assertGreaterEqual(canceller.stats["promotions"], 1)
        # Corrupt the foreground: it now adds three times the echo it should remove.
        canceller._filter.H_fg = -3.0 * canceller._filter.H_fg
        canceller._filter.H = canceller._filter.H_fg.copy()
        out = _f64(_run(canceller, far[split:], near[split:]))
        self.assertGreaterEqual(canceller.stats["foreground_resets"], 1)
        # Well after the reset (1 s in) the output is no louder than the input.
        tail_in = _f64(near[split:])[8000:]
        tail_out = out[8000:]
        self.assertLessEqual(np.sum(tail_out ** 2), np.sum(tail_in ** 2))


class TestAppSelectsTheCanceller(unittest.TestCase):
    def _import_app(self, value):
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        env.pop("AEC_IMPL", None)
        if value is not None:
            env["AEC_IMPL"] = value
        code = ("from tests.harness.appctl import app; "
                "print(app.AEC_IMPL, app.AcousticEchoCanceller.__module__)")
        return subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, env=env,
                              capture_output=True, text=True, timeout=300)

    def test_default_is_aec1(self):
        """Path B trial live (2026-09-29): default flipped so `python app.py`
        with no env var runs aec1. Still overridable both ways."""
        result = self._import_app(None)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split()[-2:], ["aec1", "aec1"])

    def test_stock_aec_is_selectable(self):
        result = self._import_app(" AEC ")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split()[-2:], ["aec", "aec"])

    def test_unknown_value_fails_at_startup(self):
        result = self._import_app("aec2")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("AEC_IMPL must be 'aec' or 'aec1', got 'aec2'", result.stderr)

    def test_call_stats_report_the_canceller(self):
        with LogCapture() as log:
            app.log_call_stats(live_call_state(with_denoiser=False, with_aec=False))
        self.assertEqual([line for line in log.lines if "[AEC]" in line],
                         [f"🎛️ [AEC] impl={app.AEC_IMPL}"])

        canceller = aec1.AcousticEchoCanceller(frame_size=FRAME)
        canceller.stats["promotions"] = 3
        with LogCapture() as log:
            app.log_call_stats(live_call_state(with_denoiser=False, aec=canceller))
        self.assertEqual([line for line in log.lines if "[AEC]" in line], [
            f"🎛️ [AEC] impl={app.AEC_IMPL} blocks=0 far_active_blocks=0 adapted_blocks=0 "
            "doubletalk_blocks=0 near_silent_blocks=0 promotions=3 foreground_resets=0 "
            "background_resets=0"])


if __name__ == "__main__":
    unittest.main()
