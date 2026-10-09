"""Task 2 -- test harness scaffolding, self-verified.

These tests exist to prove the harness itself works before anything depends on
it. They assert:

* discovery runs under ``python -m unittest discover -s tests -t .`` (here:
  ``venv312/Scripts/python.exe``);
* importing ``app`` is side-effect-contained (no production log writes);
* the mirrored ``call_state`` key set still matches app.py's literal;
* ``FakePlivoWS`` / ``FakeSession`` / ``FakeClock`` behave as documented;
* the synthetic echo path produces a near-end that really is a delayed,
  attenuated copy of the far-end;
* the seeded falsifier reports the seed, case index and failing input;
* and the four Bengali expectations named in task 2 hold on the *installed*
  ``regex`` interpreter.
"""

from __future__ import annotations

import asyncio
import json
import os
import unittest

import numpy as np
import regex

from tests.harness import bengali, echo, falsifier
from tests.harness.appctl import (
    APP_PY,
    REPO_ROOT,
    app,
    call_state_keys_from_app_source,
    live_call_state,
    new_call_state,
)
from tests.harness.fakes import FakeClock, FakePlivoWS, FakeSession, resp_audio, resp_turn_complete


class TestAppImportContainment(unittest.TestCase):
    def test_root_logger_has_no_production_file_handler(self):
        """``logging.basicConfig`` in app.py must have been neutralised.

        If this fails the suite is appending to ``Gemini_Assistant.log`` -- the
        log task 1's whole measurement was derived from.
        """
        paths = [
            os.path.basename(getattr(handler, "baseFilename", ""))
            for handler in __import__("logging").getLogger().handlers
        ]
        self.assertNotIn("Gemini_Assistant.log", paths)

    def test_transfer_context_db_is_redirected(self):
        self.assertNotEqual(
            os.path.abspath(app.TRANSFER_CONTEXT_DB_PATH),
            os.path.join(REPO_ROOT, "transfer_context.sqlite3"),
            "tests must not touch the production transfer-context store",
        )
        self.assertIn("bgsf_offline_tests", app.TRANSFER_CONTEXT_DB_PATH)

    def test_frozen_constants_untouched(self):
        """Pinned values. The idle onset count was raised 3 -> 6 on 2026-10-09 (operator, TEST8 blips). The while-speaking count stays 4: the echo gate is tuned around it."""
        self.assertEqual(app.VAD_THRESHOLD_WHILE_SPEAKING, 0.82)
        self.assertEqual(app.VAD_SPEECH_ONSET_FRAMES_WHILE_SPEAKING, 4)
        self.assertEqual(app.VAD_THRESHOLD, 0.75)
        self.assertEqual(app.VAD_SPEECH_ONSET_FRAMES, 6)
        self.assertEqual(app.PLIVO_ULAW_CHUNK_SIZE, 160)

    def test_corroborated_gate_constants(self):
        """The redesign defines the corroborated-gate constants; only the
        unused two-tier constant stays absent.

        BASELINE UPDATE (audio-pipeline redesign). Concern (a) is no longer
        parked: ``ECHO_CORR_THRESHOLD`` (0.88) and ``FAR_END_ACTIVE_FLOOR_DB``
        (-60) are now defined and drive the corroborated barge-in gate. The
        redesign uses a SINGLE-threshold gate (far-inactive => barge in;
        correlated >= threshold => echo; else barge in), NOT the original
        two-tier "ambiguous onset frames" design, so
        ``ECHO_AMBIGUOUS_ONSET_FRAMES`` is intentionally never defined. This test
        pins that choice: the two used constants exist, the unused one does not.
        """
        self.assertTrue(hasattr(app, "ECHO_CORR_THRESHOLD"))
        self.assertTrue(hasattr(app, "FAR_END_ACTIVE_FLOOR_DB"))
        self.assertFalse(hasattr(app, "ECHO_AMBIGUOUS_ONSET_FRAMES"),
                         "the single-threshold gate must not resurrect the two-tier "
                         "ambiguous-onset constant")

    def test_authorised_constants_present_with_their_designed_values(self):
        self.assertEqual(app.ANOMALOUS_TRUNCATION_MS, 350)
        self.assertEqual(app.GRAPHEME_COMMIT_MS, 240)
        self.assertEqual(app.GRAPHEME_COMMIT_BYTES, 1920)
        self.assertEqual(app.GRAPHEME_COMMIT_BYTES,
                         app.GRAPHEME_COMMIT_MS * app.PLIVO_SAMPLE_RATE // 1000)

    def test_bargein_module_exists_with_its_pure_surface(self):
        """``bargein.py`` is now created (redesign, task #3) and exposes the pure
        echo-corroboration surface the barge-in gate depends on. This guard used
        to assert its ABSENCE while task 5.5 was parked; the redesign unparks it,
        so the guard flips to asserting the surface exists and stays pure."""
        self.assertTrue(os.path.exists(os.path.join(REPO_ROOT, "bargein.py")))
        import bargein
        for name in ("envelope", "far_end_active", "echo_correlation",
                     "should_barge_in", "pcm16_to_float", "rms_dbfs"):
            self.assertTrue(hasattr(bargein, name), f"bargein.{name} missing")


class TestCallStateMirror(unittest.TestCase):
    def test_mirrored_keys_match_app_source(self):
        from_source = call_state_keys_from_app_source()
        mirrored = list(new_call_state(with_denoiser=False, with_aec=False).keys())
        self.assertEqual(
            sorted(from_source), sorted(mirrored),
            "tests.harness.appctl.new_call_state has drifted from app.py's call_state literal:\n"
            f"  only in app.py : {sorted(set(from_source) - set(mirrored))}\n"
            f"  only in harness: {sorted(set(mirrored) - set(from_source))}",
        )

    def test_is_ringing_is_read_but_never_initialised(self):
        """Recorded finding, not a defect this spec fixes.

        app.py reads ``call_state.get("is_ringing")`` at line 1717 and writes it
        at 1718, but the ``call_state`` literal never initialises it. The
        synthetic-ringback ``clearAudio`` at 1721 is therefore dead on any call
        started through ``handle_media_stream``. Pinned here so a future change
        that starts initialising it is noticed.
        """
        self.assertNotIn("is_ringing", call_state_keys_from_app_source())
        with open(APP_PY, "r", encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn('call_state.get("is_ringing")', source)


class TestFakeClock(unittest.TestCase):
    def test_advance_moves_both_time_sources(self):
        clock = FakeClock()
        before_mono, before_wall = clock.monotonic(), clock.now(None)
        clock.advance_ms(130)
        self.assertAlmostEqual(clock.monotonic() - before_mono, 0.130, places=9)
        self.assertAlmostEqual((clock.now(None) - before_wall).total_seconds(), 0.130, places=9)

    def test_install_rebinds_apps_time_and_datetime(self):
        clock = FakeClock(monotonic_start=500.0)
        with clock.install():
            self.assertEqual(app.time.monotonic(), 500.0)
            self.assertEqual(app.datetime.now(app.ist_tz), clock.now(app.ist_tz))
            clock.advance(2.5)
            self.assertEqual(app.time.monotonic(), 502.5)
        self.assertNotEqual(app.time.monotonic(), 502.5)


class TestFakePlivoWS(unittest.TestCase):
    def test_records_play_audio_with_decoded_payload_and_send_time(self):
        async def scenario():
            clock = FakeClock()
            ws = FakePlivoWS(clock)
            payload = bytes(range(160))
            await ws.send(json.dumps({
                "event": "playAudio",
                "media": {"contentType": "audio/x-mulaw", "sampleRate": 8000,
                          "payload": __import__("base64").b64encode(payload).decode()},
            }))
            clock.advance_ms(20)
            await ws.send(json.dumps({"event": "clearAudio", "stream_id": "s"}))
            return ws

        ws = asyncio.run(scenario())
        self.assertEqual(ws.events, ["playAudio", "clearAudio"])
        self.assertEqual(ws.play_audio_payloads, [bytes(range(160))])
        self.assertEqual(len(ws.play_audio_payloads[0]), app.PLIVO_ULAW_CHUNK_SIZE)
        self.assertEqual(ws.clear_audio_count, 1)
        self.assertAlmostEqual(ws.clear_audio_times[0] - ws.send_times[0], 0.020, places=9)

    def test_inter_frame_gaps_are_measured_from_the_fake_clock(self):
        async def scenario():
            clock = FakeClock()
            ws = FakePlivoWS(clock)
            for _ in range(3):
                await ws.send(json.dumps({
                    "event": "playAudio",
                    "media": {"contentType": "audio/x-mulaw", "sampleRate": 8000,
                              "payload": __import__("base64").b64encode(b"\xff" * 160).decode()},
                }))
                clock.advance_ms(20)
            return ws

        ws = asyncio.run(scenario())
        self.assertEqual(ws.inter_frame_gaps_ms, [20.0, 20.0])

    def test_receive_replays_queued_events_then_signals_disconnect(self):
        async def scenario():
            ws = FakePlivoWS(FakeClock())
            ws.queue_start()
            ws.queue_media_ulaw(b"\x7f" * 160)
            received = [await ws.receive(), await ws.receive(), await ws.receive()]
            return received

        first, second, third = asyncio.run(scenario())
        self.assertEqual(json.loads(first)["event"], "start")
        self.assertEqual(json.loads(second)["event"], "media")
        self.assertIsNone(third)

    def test_checkpoint_is_recordable_even_though_app_never_sends_one(self):
        """Task 2 asks for checkpoint recording. It is generic, so it works --
        but app.py emits only playAudio and clearAudio, so any checkpoint
        assertion against unfixed code would be vacuous."""
        async def scenario():
            ws = FakePlivoWS(FakeClock())
            await ws.send(json.dumps({"event": "checkpoint", "name": "x"}))
            return ws

        ws = asyncio.run(scenario())
        self.assertEqual(len(ws.checkpoint_frames), 1)
        with open(APP_PY, "r", encoding="utf-8") as handle:
            self.assertNotIn("checkpoint", handle.read())


class TestFakeSession(unittest.TestCase):
    def test_script_is_yielded_then_receive_parks(self):
        async def scenario():
            session = FakeSession([resp_audio(b"\x00\x00" * 480), resp_turn_complete()])
            seen = [item async for item in session.receive()]
            parked = asyncio.ensure_future(_consume(session))
            await asyncio.sleep(0.01)
            still_running = not parked.done()
            parked.cancel()
            return seen, still_running

        async def _consume(session):
            async for _ in session.receive():
                pass

        seen, still_running = asyncio.run(scenario())
        self.assertEqual(len(seen), 2)
        self.assertTrue(still_running, "exhausted FakeSession must park, not spin")

    def test_records_activity_and_audio_sends_in_order(self):
        from google.genai import types

        async def scenario():
            session = FakeSession()
            await session.send_realtime_input(activity_start=types.ActivityStart())
            await session.send_realtime_input(
                audio=types.Blob(data=b"\x01\x02", mime_type="audio/pcm;rate=16000"))
            await session.send_realtime_input(activity_end=types.ActivityEnd())
            return session

        session = asyncio.run(scenario())
        self.assertEqual(session.sent_kinds, ["activityStart", "audio", "activityEnd"])
        self.assertEqual(session.audio_bytes_sent, b"\x01\x02")


class TestSyntheticEchoPath(unittest.TestCase):
    def test_fixtures_are_readable(self):
        self.assertIn("recorded.wav", echo.available_fixtures())
        pcm8 = echo.load_pcm8k("recorded.wav")
        self.assertGreater(len(pcm8), 8000)
        self.assertEqual(len(pcm8) % 2, 0)

    def test_echo_is_a_delayed_attenuated_copy(self):
        far = echo.load_pcm8k("recorded.wav")
        near = echo.synth_echo(far, delay_ms=60, attenuation_db=18)
        far_db = echo.rms_db(far)
        near_db = echo.rms_db(near[60 * 8 * 2:])
        self.assertLess(near_db, far_db - 12.0)
        self.assertGreater(near_db, far_db - 24.0)
        # the delay shows up as a lag in the envelope correlation
        at_lag = echo.normalised_envelope_correlation(near, far, lag_bins=6)
        at_zero = echo.normalised_envelope_correlation(near, far, lag_bins=0)
        self.assertGreater(at_lag, 0.9)
        self.assertGreater(at_lag, at_zero)

    def test_delay_and_attenuation_span_the_designed_ranges(self):
        far = echo.load_pcm8k("recorded.wav")
        for delay_ms in (20, 200, 400):
            for attenuation_db in (6, 18, 30):
                near = echo.synth_echo(far, delay_ms=delay_ms, attenuation_db=attenuation_db,
                                       noise_db=-70, rng=np.random.default_rng(1))
                self.assertEqual(len(near) % 2, 0)
                head = near[:int(delay_ms / 1000 * 8000) * 2]
                self.assertLess(echo.rms_db(head), -55.0, "pre-delay head should be near-silent")

    def test_frames_are_whole_20ms_plivo_frames(self):
        frames = echo.frames_pcm8(echo.load_pcm8k("recorded.wav"))
        self.assertTrue(all(len(f) == 320 for f in frames))
        self.assertTrue(all(len(app.pcm_to_ulaw(f)) == 160 for f in frames[:5]))


class TestFalsifier(unittest.TestCase):
    def test_passing_property_runs_every_case_and_boundary(self):
        seen = []
        executed = falsifier.run_property(
            "sum is non-negative",
            lambda rng, i: int(rng.integers(0, 10)),
            lambda case: seen.append(case) or None,
            cases=25, seed=7, boundary_cases=(0, 9),
        )
        self.assertEqual(executed, 27)
        self.assertEqual(seen[:2], [0, 9])

    def test_failure_reports_seed_case_index_and_input(self):
        with self.assertRaises(falsifier.PropertyFailure) as raised:
            falsifier.run_property(
                "always below 5",
                lambda rng, i: i,
                lambda case: None if case < 5 else f"case {case} >= 5",
                cases=10, seed=99,
            )
        failure = raised.exception
        self.assertEqual(failure.seed, 99)
        self.assertEqual(failure.index, 5)
        self.assertEqual(failure.case, 5)
        self.assertIn("seed        = 99", str(failure))
        self.assertIn("failing input = 5", str(failure))

    def test_generation_is_reproducible_for_a_fixed_seed(self):
        def collect():
            drawn = []
            falsifier.run_property(
                "collect", lambda rng, i: float(rng.uniform(0, 1)),
                lambda case: drawn.append(case) or None, cases=20, seed=4242)
            return drawn

        self.assertEqual(collect(), collect())


class TestBengaliFixtureTable(unittest.TestCase):
    """The four expectations are VERIFIED here, never adjusted to match the spec."""

    def test_the_four_named_expectations_hold_on_the_installed_regex(self):
        mismatches = []
        for word, expected in bengali.VERIFIED_SEGMENTATIONS.items():
            actual = regex.findall(r"\X", word)
            if actual != expected:
                mismatches.append((word, expected, actual))
        self.assertEqual(mismatches, [], f"regex {regex.__version__} disagrees with the spec")

    def test_regex_version_is_the_one_the_spec_verified_against(self):
        self.assertEqual(regex.__version__, "2026.2.28")

    def test_table_covers_base_x_matra_x_sign_x_conjunct(self):
        table = bengali.cluster_table()
        self.assertEqual(len(table), 10 * 8 * 4 + 4 * 8 * 4)
        signs = {case.label.rsplit("+", 1)[1] for case in table}
        self.assertEqual(signs, {"none", "visarga", "anusvara", "candrabindu"})
        self.assertTrue(any(case.conjunct for case in table))

    def test_every_table_entry_is_exactly_one_cluster(self):
        offenders = [
            (case.label, case.text, bengali.clusters(case.text))
            for case in bengali.cluster_table()
            if len(bengali.clusters(case.text)) != 1
        ]
        self.assertEqual(offenders, [])

    def test_leading_cluster_of_the_reported_word(self):
        self.assertEqual(bengali.clusters(bengali.APOLOGY_TURN_1)[:4],
                         ["\u0986", "\u09AE\u09BF", " ", bengali.LEADING_CLUSTER])


class TestHarnessRunsGreenWithZeroTests(unittest.TestCase):
    """Task 2's last bullet: the harness must be green before anything depends on it.

    Importing every harness module and constructing every fake, with no
    production behaviour exercised, is the zero-test green run.
    """

    def test_all_harness_modules_import_and_construct(self):
        from tests.harness import appctl, fakes  # noqa: F401
        clock = FakeClock()
        ws = FakePlivoWS(clock)
        session = FakeSession()
        state = live_call_state(with_denoiser=False, with_aec=False)
        self.assertEqual(ws.frames, [])
        self.assertEqual(session.sent, [])
        self.assertTrue(state["greeting_completed"])
        self.assertFalse(state["playing_disclaimer"])
        self.assertEqual(len(bengali.cluster_table()), 448)
        self.assertGreater(len(echo.available_fixtures()), 0)


if __name__ == "__main__":
    unittest.main()
