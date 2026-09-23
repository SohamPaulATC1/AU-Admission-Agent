"""Unit tests for bargein.py -- the pure echo-corroboration logic.

Three kinds of case:

1. Envelope / activity primitives -- pin the arithmetic.

2. Synthetic decision cases -- controlled, aligned signals with a VARYING
   envelope (amplitude-modulated, never a pure tone). A pure sine has a flat
   energy envelope and therefore ZERO envelope variance, so its envelope
   correlation is undefined/0 -- that is a property of energy-envelope
   correlation, not a bug, and real speech/echo always carries envelope
   structure. The synthetic cases model that.

3. Real recording, PROPERLY ALIGNED -- the far-end + inbound pair captured on the
   reproduction call 2026-09-22 17:05. This is the empirical ground truth the
   whole redesign rests on. The two WAVs cannot be aligned by a single global
   offset (the far-end recorder is wall-clock continuous via zero-padding, but
   the inbound recorder skips frames during the disclaimer/closing phases, so
   the inbound file has compressed gaps). Alignment must be done per trigger from
   the log anchors, exactly as the offline analysis did -- and exactly as the
   LIVE shadow buffer will, since it is built inline against the playout clock.
   With that alignment the residual echo correlates ~0.97 at ~50-60 ms lag.

No phone call, no live model: everything runs off files already in the repo.
"""

from __future__ import annotations

import ast
import collections
import datetime
import os
import re
import unittest
import wave

import numpy as np

import bargein

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REC_DIR = os.path.join(REPO_ROOT, "debug_recordings")
INBOUND = os.path.join(REC_DIR, "Soham_Paul_20260922_170459.wav")
FAREND = os.path.join(REC_DIR, "Soham_Paul_20260922_170459_farend.wav")
LOG = os.path.join(REPO_ROOT, "Gemini_Assistant.log")
APP_PY = os.path.join(REPO_ROOT, "app.py")

DEFAULT_THRESHOLD = 0.88


def _mod_tone(freq, mod_freq, ms, rate, amp=0.5, phase=0.0):
    """Amplitude-modulated tone: a carrier whose envelope varies at mod_freq.

    The modulation is what gives the energy envelope structure to correlate;
    ``phase`` shifts the whole waveform in time for the delayed-copy case.
    """
    n = int(rate * ms / 1000.0)
    t = (np.arange(n) + phase) / rate
    carrier = np.sin(2 * np.pi * freq * t)
    mod = 0.5 * (1.0 + np.sin(2 * np.pi * mod_freq * t))
    return (carrier * mod * amp * 32767).astype(np.int16).tobytes()


def _noise(ms, rate, amp=0.3, seed=0):
    n = int(rate * ms / 1000.0)
    rng = np.random.default_rng(seed)
    return (rng.normal(0, amp, n) * 32767).clip(-32768, 32767).astype(np.int16).tobytes()


def _silence(ms, rate):
    return b"\x00\x00" * int(rate * ms / 1000.0)


# Names app.evaluate_echo_gate reads, directly or through the constants it uses.
_GATE_NAMES = (
    "PLIVO_SAMPLE_RATE", "GEMINI_INPUT_RATE", "PLIVO_ULAW_CHUNK_SIZE",
    "ECHO_CORR_THRESHOLD", "ECHO_CORR_WINDOW_MS", "ECHO_LAG_SEARCH_MS",
    "FAR_END_ACTIVE_FLOOR_DB", "FAR_SHADOW_FRAMES", "ECHO_NEAR_BYTES_16K",
    "ECHO_FAR_MS", "ECHO_FAR_FRAMES", "ECHO_FAR_BYTES_8K", "PREROLL_MAX_BYTES_PCM16",
    "ECHO_LATCH_BREAK_DB", "ECHO_MAX_RETURN_DB",
)
_GATE_FUNCTIONS = ("evaluate_echo_gate", "echo_return_db", "apply_echo_latch")


def load_production_gate():
    """The REAL ``evaluate_echo_gate`` and its constants, lifted from app.py.

    Importing app pulls in quart, plivo, pyrnnoise and google.genai; this file
    stays runnable on numpy alone. So the module-level assignments the gate
    depends on and the function itself are compiled from app.py's own source
    and executed against the real ``bargein``. What runs is production code,
    not a hand copy of its slicing -- which is exactly what a hand copy got
    wrong before.
    """
    with open(APP_PY, "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=APP_PY)
    body = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id in _GATE_NAMES for t in node.targets):
            body.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name in _GATE_FUNCTIONS:
            body.append(node)
    module = ast.Module(body=body, type_ignores=[])
    namespace = {"os": os, "bargein": bargein, "np": np}
    exec(compile(module, APP_PY, "exec"), namespace)
    return namespace


def gate_call_state(near16, far8, now_s, *, preroll_bytes, shadow_frames):
    """Buffers exactly as the inbound loop leaves them at time ``now_s``.

    ``preroll_pcm16`` is the tail of the 16 kHz near-end, ``far_shadow`` a deque
    of 20 ms 8 kHz PCM16 frames, both ending at the same instant -- the inbound
    loop appends one of each per processed frame.
    """
    end8 = int(now_s * 8000) * 2
    end8 -= end8 % 320
    return gate_call_state_at(near16, end8 * 2, far8, end8,
                              preroll_bytes=preroll_bytes, shadow_frames=shadow_frames)


def gate_call_state_at(near16, near_end, far8, far_end, *, preroll_bytes, shadow_frames,
                       agc_gain_lin=1.0):
    """As ``gate_call_state`` but with independent byte offsets for "now" in each
    stream -- the recorded inbound and far-end WAVs do not share a timeline.

    ``agc_gain_lin`` defaults to unity (0 dB). AGC gain is never below 0 dB, so
    unity overstates the pre-AGC near level: the conservative direction for the
    echo return ceiling (it can only make echo look louder, never quieter)."""
    frame = 320  # 20 ms of 8 kHz PCM16
    shadow = collections.deque(maxlen=shadow_frames)
    for start in range(max(0, far_end - shadow_frames * frame), far_end - frame + 1, frame):
        shadow.append(far8[start:start + frame])
    return {
        "preroll_pcm16": bytearray(near16[max(0, near_end - preroll_bytes):near_end]),
        "far_shadow": shadow,
        "agc_current_gain_lin": agc_gain_lin,
    }


def _speechlike_envelope(t):
    """Gap-free, non-periodic-looking syllabic envelope (always > 0)."""
    return (1.5 + 0.6 * np.sin(2 * np.pi * 3.1 * t)
            + 0.4 * np.sin(2 * np.pi * 7.3 * t + 1.0)
            + 0.3 * np.sin(2 * np.pi * 11.7 * t + 2.0))


def delayed_echo_pair(delay_ms, seconds=3.5, atten_db=18.0):
    """Far-end at 8 kHz and its residual echo at 16 kHz, delayed by ``delay_ms``.

    Carriers are independent noise: the DSP chain destroys waveform phase, so
    only the energy envelope survives into the near-end -- the premise the
    envelope correlator rests on.
    """
    rng = np.random.default_rng(7)
    t8 = np.arange(int(seconds * 8000)) / 8000.0
    t16 = np.arange(int(seconds * 16000)) / 16000.0
    far = 0.12 * _speechlike_envelope(t8) * rng.standard_normal(t8.size)
    gain = 10 ** (-atten_db / 20.0)
    near = (0.12 * gain * _speechlike_envelope(t16 - delay_ms / 1000.0)
            * rng.standard_normal(t16.size))
    to_pcm = lambda x: (np.clip(x, -1, 1) * 32767).astype(np.int16).tobytes()
    return to_pcm(near), to_pcm(far)


class TestEnvelopeAndActivity(unittest.TestCase):
    def test_envelope_of_silence_is_near_zero(self):
        env = bargein.envelope(bargein.pcm16_to_float(_silence(200, 8000)), rate=8000)
        self.assertTrue(env.size > 0)
        self.assertLess(float(env.max()), 1e-3)

    def test_short_input_yields_empty_envelope(self):
        env = bargein.envelope(bargein.pcm16_to_float(b"\x00\x00" * 10), rate=8000)
        self.assertEqual(env.size, 0)

    def test_far_end_active_floor(self):
        loud = bargein.pcm16_to_float(_mod_tone(300, 5, 200, 8000, amp=0.5))
        quiet = bargein.pcm16_to_float(_silence(200, 8000))
        self.assertTrue(bargein.far_end_active(loud, floor_dbfs=-60.0))
        self.assertFalse(bargein.far_end_active(quiet, floor_dbfs=-60.0))

    def test_rms_dbfs_silence_is_very_low(self):
        self.assertLess(bargein.rms_dbfs(bargein.pcm16_to_float(_silence(50, 8000))), -100.0)


class TestEchoCorrelationSynthetic(unittest.TestCase):
    def test_identical_modulated_signal_correlates_near_one_at_lag_zero(self):
        far = _mod_tone(250, 6, 300, 8000, amp=0.5)
        near = _mod_tone(250, 6, 300, 16000, amp=0.5)   # same envelope, diff rate
        r = bargein.echo_correlation(near, far, near_rate=16000, far_rate=8000)
        self.assertGreater(r.correlation, 0.9)
        self.assertTrue(r.far_active)
        self.assertLessEqual(abs(r.lag_bins), 2)

    def test_uncorrelated_noise_is_low(self):
        far = _noise(300, 8000, seed=1)
        near = _noise(300, 16000, seed=2)
        r = bargein.echo_correlation(near, far, near_rate=16000, far_rate=8000)
        self.assertLess(r.correlation, 0.5)

    def test_far_silent_reports_inactive(self):
        far = _silence(300, 8000)
        near = _mod_tone(250, 6, 300, 16000, amp=0.5)
        r = bargein.echo_correlation(near, far, near_rate=16000, far_rate=8000)
        self.assertFalse(r.far_active)

    def test_delayed_copy_peaks_within_the_search_window(self):
        # near-end is the far-end delayed by 50 ms -- the measured live lag.
        far = _mod_tone(300, 7, 400, 8000, amp=0.5)
        delay = int(8000 * 0.05)
        far_arr = np.frombuffer(far, dtype=np.int16)
        near_arr = np.concatenate([np.zeros(delay, dtype=np.int16), far_arr])[: far_arr.size]
        r = bargein.echo_correlation(near_arr.tobytes(), far, near_rate=8000, far_rate=8000)
        self.assertGreater(r.correlation, 0.8)
        self.assertGreaterEqual(r.lag_bins, 3)   # ~50 ms => ~5 bins, captured by +/-80 ms


class TestShouldBargeIn(unittest.TestCase):
    def test_far_inactive_always_barges_in(self):
        far = _silence(300, 8000)
        near = _mod_tone(250, 6, 300, 16000, amp=0.5)
        d = bargein.should_barge_in(near, far, near_rate=16000, far_rate=8000,
                                    threshold=DEFAULT_THRESHOLD)
        self.assertTrue(d.barge_in)
        self.assertFalse(d.is_echo)
        self.assertEqual(d.reason, "far-end-inactive")

    def test_correlated_is_suppressed_as_echo(self):
        far = _mod_tone(250, 6, 300, 8000, amp=0.5)
        near = _mod_tone(250, 6, 300, 16000, amp=0.5)
        d = bargein.should_barge_in(near, far, near_rate=16000, far_rate=8000,
                                    threshold=DEFAULT_THRESHOLD)
        self.assertFalse(d.barge_in)
        self.assertTrue(d.is_echo)
        self.assertEqual(d.reason, "echo-correlated")

    def test_uncorrelated_double_talk_barges_in(self):
        far = _noise(300, 8000, seed=3)
        near = _noise(300, 16000, seed=4)
        d = bargein.should_barge_in(near, far, near_rate=16000, far_rate=8000,
                                    threshold=DEFAULT_THRESHOLD)
        self.assertTrue(d.barge_in)
        self.assertFalse(d.is_echo)
        self.assertEqual(d.reason, "uncorrelated-double-talk")


class TestProductionGateCatchesRealisticEchoDelays(unittest.TestCase):
    """The windowing the live gate actually uses, on a known delayed echo.

    The synthetic cases above hand ``echo_correlation`` equal-length windows.
    Production does not: it slices the near window and the far window out of
    two different buffers in ``evaluate_echo_gate``. If those slices are not the
    same duration ending at the same instant, the lag search is displaced and
    covers the wrong range of echo delays. This drives the real function with
    the real constants at the delays the reproduction calls measured (50-60 ms)
    and the ones on either side of them.

    Deliberately NOT asserted here: that independent double-talk barges in.
    A synthetic sweep (smooth and burst-shaped speech envelopes) false-flagged
    roughly 45% of double-talk windows as echo at every far window length,
    the pre-fix one included, so that is a property of the 180 ms window and
    0.88 threshold, not of this windowing -- see docs/HANDOFF.md section 10.
    """

    NOW_POSITIONS_S = (1.0, 1.37, 1.71, 2.05, 2.43, 2.9)

    @classmethod
    def setUpClass(cls):
        cls.gate = load_production_gate()

    def _decisions(self, delay_ms):
        near16, far8 = delayed_echo_pair(delay_ms)
        return [
            self.gate["evaluate_echo_gate"](gate_call_state(
                near16, far8, now,
                preroll_bytes=self.gate["PREROLL_MAX_BYTES_PCM16"],
                shadow_frames=self.gate["FAR_SHADOW_FRAMES"]))
            for now in self.NOW_POSITIONS_S
        ]

    def test_delayed_echo_is_suppressed_at_every_realistic_delay(self):
        for delay_ms in (0, 30, 50, 60, 80, 120):
            with self.subTest(delay_ms=delay_ms):
                decisions = self._decisions(delay_ms)
                missed = [(now, round(d.correlation, 3), d.lag_bins)
                          for now, d in zip(self.NOW_POSITIONS_S, decisions)
                          if not d.is_echo]
                self.assertEqual(missed, [],
                                 f"echo delayed {delay_ms} ms leaked through the "
                                 f"production gate at (now_s, corr, lag): {missed}")

    def test_far_window_ends_now_and_leads_by_the_lag_room(self):
        """Geometry the delay coverage depends on: the far window ends at the
        same frame as the near window and is exactly window + lag long."""
        g = dict(self.gate)
        seen = {}

        class _Spy:
            @staticmethod
            def should_barge_in(near, far, **kwargs):
                seen.update(near=near, far=far, **kwargs)
                return bargein.BargeInDecision(False, False, "spy", 0.0, 0, True, -40.0, -20.0)

        _Spy.BargeInDecision = bargein.BargeInDecision
        g["bargein"] = _Spy
        exec(compile(ast.Module(body=[self._gate_def()], type_ignores=[]),
                     APP_PY, "exec"), g)
        frames = [bytes([i]) * 320 for i in range(g["FAR_SHADOW_FRAMES"])]
        g["evaluate_echo_gate"]({
            "preroll_pcm16": bytearray(b"\x01\x00" * 3200),
            "far_shadow": collections.deque(frames, maxlen=g["FAR_SHADOW_FRAMES"]),
        })
        window_ms = g["ECHO_CORR_WINDOW_MS"]
        self.assertEqual(len(seen["near"]), window_ms * 16000 // 1000 * 2)
        self.assertEqual(len(seen["far"]),
                         (window_ms + g["ECHO_LAG_SEARCH_MS"]) * 8000 // 1000 * 2)
        self.assertTrue(seen["far"].endswith(frames[-1]), "far window must end now")
        self.assertEqual(seen["lag_search_ms"], g["ECHO_LAG_SEARCH_MS"])

    @staticmethod
    def _gate_def():
        with open(APP_PY, "r", encoding="utf-8") as handle:
            tree = ast.parse(handle.read(), filename=APP_PY)
        return next(n for n in tree.body
                    if isinstance(n, ast.FunctionDef) and n.name == "evaluate_echo_gate")

def _read_pcm16_wav(path, rate):
    """Mono PCM16 WAV as int16 -- stdlib ``wave``, so no soundfile dependency."""
    with wave.open(path, "rb") as reader:
        assert (reader.getnchannels(), reader.getsampwidth(), reader.getframerate())             == (1, 2, rate), path
        return np.frombuffer(reader.readframes(reader.getnframes()), dtype=np.int16)


class TestEchoLatch(unittest.TestCase):
    """The real ``apply_echo_latch``: one echo verdict holds for the playback
    period, and only a clear rise in echo return (a caller talking over the
    assistant) breaks it."""

    @classmethod
    def setUpClass(cls):
        cls.gate = load_production_gate()

    @staticmethod
    def _decision(is_echo, near_dbfs, far_dbfs=-15.0, far_active=True):
        reason = ("echo-correlated" if is_echo else
                  "uncorrelated-double-talk" if far_active else "far-end-inactive")
        return bargein.BargeInDecision(
            barge_in=not is_echo, is_echo=is_echo, reason=reason,
            correlation=0.95 if is_echo else 0.8, lag_bins=0,
            far_active=far_active, near_dbfs=near_dbfs, far_dbfs=far_dbfs)

    def _state(self, gain_db=0.0):
        return {"echo_latch_erl_db": None, "agc_current_gain_lin": 10 ** (gain_db / 20.0)}

    def test_no_latch_leaves_a_non_echo_verdict_alone(self):
        state = self._state()
        self.assertEqual(self.gate["apply_echo_latch"](state, self._decision(False, -30.0)),
                         (False, "uncorrelated-double-talk"))
        self.assertIsNone(state["echo_latch_erl_db"])

    def test_echo_verdict_latches_and_holds_the_next_onset(self):
        state = self._state()
        latch = self.gate["apply_echo_latch"]
        self.assertEqual(latch(state, self._decision(True, -35.0)), (True, "echo-correlated"))
        self.assertAlmostEqual(state["echo_latch_erl_db"], -20.0, places=3)
        # Re-onset: correlation dipped below threshold, level unchanged -> held.
        self.assertEqual(latch(state, self._decision(False, -33.0)), (True, "echo-latched"))

    def test_agc_catching_up_does_not_break_the_latch(self):
        """Post-AGC near level rises as AGC converges on the echo; with the gain
        removed the echo return is unchanged, so the latch must hold."""
        state = self._state(gain_db=0.0)
        latch = self.gate["apply_echo_latch"]
        latch(state, self._decision(True, -35.0))
        state["agc_current_gain_lin"] = 10 ** (15.0 / 20.0)
        self.assertEqual(latch(state, self._decision(False, -20.0)), (True, "echo-latched"))

    def test_caller_over_playback_breaks_the_latch(self):
        state = self._state()
        latch = self.gate["apply_echo_latch"]
        latch(state, self._decision(True, -35.0))
        rise = self.gate["ECHO_LATCH_BREAK_DB"] + 2.0
        self.assertEqual(latch(state, self._decision(False, -35.0 + rise)),
                         (False, "uncorrelated-double-talk"))
        self.assertIsNone(state["echo_latch_erl_db"], "a broken latch must not linger")

    def test_far_end_inactive_is_never_held(self):
        state = self._state()
        latch = self.gate["apply_echo_latch"]
        latch(state, self._decision(True, -35.0))
        self.assertEqual(latch(state, self._decision(False, -35.0, far_dbfs=-90.0,
                                                     far_active=False)),
                         (False, "far-end-inactive"))


def _log_slice_for_call():
    """Lines of the 17:05 reproduction call, or None if the log has rotated."""
    if not os.path.exists(LOG):
        return None
    with open(LOG, encoding="utf-8") as handle:
        lines = [l.rstrip("\n") for l in handle]
    starts = [i for i, l in enumerate(lines)
              if "Outbound call answered by: 8335027643" in l and "17:04:59" in l]
    if not starts:
        return None
    return lines[starts[0]:]


class TestRealRecordingGroundsTheThreshold(unittest.TestCase):
    """The premise: real residual echo correlates with the real far-end, at ~0
    lag, above threshold -- once aligned the way the live shadow buffer aligns.

    Both tests go through the REAL ``evaluate_echo_gate`` (lifted from app.py),
    fed a 16 kHz preroll and an 8 kHz far shadow built as the inbound loop builds
    them. They used to hand ``echo_correlation`` two equal 180 ms windows, which
    is not what production slices, so they passed while the live gate's window
    geometry missed the very delays they measured.

    Skips (does not fail) if the recordings or the matching log slice are absent,
    so log rotation cannot break CI. When present, this is the strongest evidence
    the gate's threshold is real and not invented.
    """

    def setUp(self):
        if not (os.path.exists(INBOUND) and os.path.exists(FAREND)):
            self.skipTest("reproduction-call recordings not present")
        self.L = _log_slice_for_call()
        if self.L is None:
            self.skipTest("matching log slice for the 17:05 call not found")

        self.far = _read_pcm16_wav(FAREND, 8000)
        self.inb16 = _read_pcm16_wav(INBOUND, 16000)
        self.gate = load_production_gate()

        def ts(line):
            m = re.match(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3})", line)
            return (datetime.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S,%f")
                    if m else None)
        self._ts = ts
        t0_line = next(l for l in self.L if "Debug recording started" in l)
        self.T0 = ts(t0_line)

        far_lines = [l for l in self.L if "[FAR-END] chunk=" in l]
        self.fw = [(ts(l) - self.T0).total_seconds() for l in far_lines]
        self.ftm = [float(re.search(r"t_mono=([\d.]+)", l).group(1)) for l in far_lines]
        self.t0m = float(re.search(r"t0_mono=([\d.]+)", far_lines[0]).group(1))

        self.iw, self.isamp = [], []
        n = 0
        for l in self.L:
            if "[AGC] Inbound RMS" in l:
                self.iw.append((ts(l) - self.T0).total_seconds())
                self.isamp.append((50 * n * 320 - 160) / 2.0)  # 16k sample -> /2 for 8k index
                n += 1
        self.triggers = [ts(l) for l in self.L if "AI Speech Interrupted at" in l]

    def _rel(self, t):
        return (t - self.T0).total_seconds()

    def _gate_decisions(self):
        """Real-gate decisions at each logged false trigger that fits the files."""
        g = self.gate
        far_bytes = self.far.tobytes()
        inb_bytes = self.inb16.tobytes()
        decisions = []
        for t in self.triggers:
            t_mono = np.interp(self._rel(t), self.fw, self.ftm)
            fi = int((t_mono - self.t0m) * 8000)          # far sample index, 8 kHz
            ii = int(np.interp(self._rel(t), self.iw, self.isamp)) * 2  # 16 kHz
            fi -= fi % 160                                 # shadow is whole frames
            if (ii * 2 < g["PREROLL_MAX_BYTES_PCM16"] or fi < g["FAR_SHADOW_FRAMES"] * 160
                    or fi > len(self.far) or ii > len(self.inb16)):
                continue
            decisions.append(g["evaluate_echo_gate"](gate_call_state_at(
                inb_bytes, ii * 2, far_bytes, fi * 2,
                preroll_bytes=g["PREROLL_MAX_BYTES_PCM16"],
                shadow_frames=g["FAR_SHADOW_FRAMES"])))
        return decisions

    def test_residual_echo_correlates_above_threshold_at_the_real_triggers(self):
        decisions = self._gate_decisions()
        for d in decisions:
            self.assertTrue(d.far_active, "far-end should be active at a truncation")
        corrs = [d.correlation for d in decisions]
        self.assertGreaterEqual(len(corrs), 4, "expected several aligned triggers")
        median = float(np.median(corrs))
        self.assertGreater(median, DEFAULT_THRESHOLD,
                           f"median residual-echo correlation {median:.3f} at real "
                           f"triggers is below the gate threshold {DEFAULT_THRESHOLD} "
                           "-- the gate's premise no longer holds")
        # Echo delay = lag room + 10 ms * lag_bins (far window leads by the lag
        # room). Measured 50-60 ms; it must sit inside the searched 0-160 ms.
        delays = [self.gate["ECHO_LAG_SEARCH_MS"] + 10.0 * d.lag_bins for d in decisions]
        self.assertLessEqual(float(np.median(delays)), 2 * self.gate["ECHO_LAG_SEARCH_MS"])
        self.assertGreaterEqual(float(np.median(delays)), 0.0)

    def test_the_gate_would_have_suppressed_these_triggers(self):
        """End-to-end on real audio: the production gate classifies the real
        false-trigger windows as echo, i.e. the fix would have stopped them."""
        decisions = self._gate_decisions()
        total = len(decisions)
        suppressed = sum(d.is_echo for d in decisions)
        self.assertGreaterEqual(total, 4)
        self.assertGreaterEqual(suppressed, total - 1,
                                f"gate suppressed only {suppressed}/{total} real "
                                "false triggers")

if __name__ == "__main__":
    unittest.main()
