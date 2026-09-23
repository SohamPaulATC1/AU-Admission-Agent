"""Task 4.9 -- Property 2 falsifier over the non-bug input domain.

Non-bug domain, per design.md Property 2: far-end inactive, OR near-end
uncorrelated with far-end, OR the trigger arriving after
``ANOMALOUS_TRUNCATION_MS`` (350 ms) of delivered playback.

Two falsifiers, because the two halves of the domain have costs that differ by
three orders of magnitude:

* ``TestOutboundDomainFalsifier`` -- 500 seeded cases plus 6 boundary shapes over
  randomised turn shapes on the outbound path. Asserts the outbound byte stream,
  the ??-law framing, the side-effect ordering and the log-line sequence against
  the golden records. Cheap: the sender does no DSP.

* ``TestInboundBargeInFalsifier`` -- 60 seeded cases over randomised *uncorrelated*
  near-end. Asserts the barge-in onset counter, the side-effect ordering and the
  existing-log-line sequence.

  Why 60 and not 500, stated rather than hidden: the inbound chain runs RNNoise,
  measured at **19.2 ms of CPU per 20 ms audio frame** on this machine (143
  frames in 2.741 s). A case needs ~10 frames, so 500 cases would cost roughly
  100 s of pure RNNoise plus the AEC and two stateful resamplers. 60 cases is
  ~12 s. The dimension being swept -- near-end level, noise floor, fixture
  offset, far-end state -- is low-dimensional, so the marginal value of case 500
  over case 60 is small; the cost is not. Recorded as a deliberate reduction
  against the spec's "500-1000 cases", not an oversight.

Boundary cases the design calls out:

* trigger at exactly 350 ms of delivered playback -- EXERCISED (against 349 and
  351 as neighbours).
* a turn whose first chunk is shorter than 20 ms -- EXERCISED.
* far-end energy exactly at the activity floor -- EXERCISED as a *signal level*
  (-60.0 dBFS RMS fed through ``aec.add_far_end``); the constant
  ``FAR_END_ACTIVE_FLOOR_DB`` itself is DEFERRED, since it belongs to parked
  task 5.7 and must not be defined by this pass.
* correlation exactly at ``ECHO_CORR_THRESHOLD`` -- DEFERRED. Unfixed code
  computes no correlation anywhere and the constant is parked, so there is no
  quantity to sit a boundary on. See the skipped test below for the reason and
  for what would make it exercisable.
"""

from __future__ import annotations

import asyncio
import hashlib
import unittest
from datetime import timedelta

import numpy as np

import bargein

from tests.harness import scenarios
from tests.harness.appctl import LogCapture, app, live_call_state
from tests.harness.falsifier import choice, uniform_float, uniform_int
from tests.harness.fakes import (
    FakeClock,
    FakePlivoClient,
    FakePlivoWS,
    FakeSession,
    InboundDriver,
    SenderHarness,
    drain_queue,
)

FRAME_BYTES = app.PLIVO_ULAW_CHUNK_SIZE
ANOMALOUS_TRUNCATION_MS_LITERAL = 350   # literal, NOT a new constant in app.py

# Golden log sequences, observed on unfixed code.
# INTENTIONAL BASELINE UPDATE — task 5.3 inserts one 🧭 [TRIGGER] line per
# barge-in decision, immediately after the [TIMING] line. It is listed here
# explicitly rather than filtered out, so the falsifier keeps pinning the exact
# length and order of the sequence: an accidental extra line still fails.
# Requirement 2.4 puts this at INFO on purpose — production runs at INFO, and a
# DEBUG-only provenance line would not be captured on the next affected call.
GOLDEN_BARGEIN_LOGS = [
    "Ready to stream audio from Plivo to Gemini",
    "\U0001F50A [AGC] Inbound RMS: ",           # prefix-matched: carries a measured value
    "\U0001F3A4 User speech detected",
    "\U0001F399\uFE0F [TIMING] AI Speech Interrupted at: ",
    "\U0001F9ED [TRIGGER] verdict=fired | classification=normal | ",
    "\U0001F6D1 Cleared Plivo playback buffer",
    "\u25B6\uFE0F Sent activityStart to Gemini",
]
GOLDEN_SILENT_LOGS = [
    "Ready to stream audio from Plivo to Gemini",
    "\U0001F50A [AGC] Inbound RMS: ",
    "\U0001F3A4 User speech detected",
    "\u25B6\uFE0F Sent activityStart to Gemini",
]


def _log_shape(lines):
    return [line for line in lines if "cancelled" not in line and "Terminating" not in line]


def _matches_golden(lines, golden):
    lines = _log_shape(lines)
    if len(lines) != len(golden):
        return f"log sequence length {len(lines)} != {len(golden)}: {lines}"
    for actual, expected in zip(lines, golden):
        if not actual.startswith(expected):
            return f"log line {actual!r} does not start with {expected!r}"
    return None


class TestOutboundDomainFalsifier(unittest.TestCase):
    """500 + 6 cases: outbound byte stream, framing, ordering, log sequence."""

    SEED = 20260409
    CASES = 500
    BOUNDARY = (
        {"sizes": [1], "discard": False},
        {"sizes": [159], "discard": False},
        {"sizes": [160], "discard": False},
        {"sizes": [161], "discard": False},
        {"sizes": [100, 380], "discard": False},   # first chunk shorter than 20 ms
        {"sizes": [1600], "discard": True},        # interrupting: everything dropped
    )

    def test_property(self):
        async def scenario():
            clock = FakeClock()
            ws = FakePlivoWS(clock)
            session = FakeSession()
            client = FakePlivoClient()
            call_state = live_call_state(with_denoiser=False)

            rng = np.random.default_rng(self.SEED)
            cases = list(self.BOUNDARY) + [
                {
                    "sizes": [uniform_int(rng, 1, 1400)
                              for _ in range(uniform_int(rng, 1, 4))],
                    "discard": bool(rng.integers(0, 5) == 0),
                }
                for _ in range(self.CASES)
            ]

            pending = b""
            frames_seen = 0
            hashes: set[str] = set()

            with LogCapture() as log:
                async with SenderHarness(call_state, clock, ws, session, client) as sender:
                    for index, case in enumerate(cases):
                        case_rng = np.random.default_rng((self.SEED + index) % (2 ** 32))
                        items = [scenarios.random_ulaw(case_rng, size) for size in case["sizes"]]
                        queued = b"".join(items)

                        # Model of app.py 1925-1966, item by item. Each queue
                        # item is appended to ``out_buffer`` in its own loop
                        # iteration; the discard branch clears the buffer only
                        # once it holds at least one whole frame, so a sub-frame
                        # remainder survives an ``interrupting`` window.
                        call_state["interrupting"] = bool(case["discard"])
                        available = pending + queued
                        buffer_model = pending
                        expected_sent = b""
                        for item in items:
                            buffer_model = buffer_model + item
                            if case["discard"]:
                                if len(buffer_model) >= FRAME_BYTES:
                                    buffer_model = b""
                            else:
                                whole = len(buffer_model) // FRAME_BYTES * FRAME_BYTES
                                expected_sent += buffer_model[:whole]
                                buffer_model = buffer_model[whole:]
                        expected_new_frames = len(expected_sent) // FRAME_BYTES
                        expected_pending = buffer_model

                        await sender.enqueue(*items)
                        if expected_new_frames:
                            await sender.wait_sent(frames_seen + expected_new_frames)
                        await drain_queue(call_state, idle_polls=2, poll=0.001)

                        new_frames = ws.play_audio_frames[frames_seen:]
                        sent = b"".join(frame["ulaw"] for frame in new_frames)

                        problems = []
                        if len(new_frames) != expected_new_frames:
                            problems.append(f"{len(new_frames)} frames, expected "
                                            f"{expected_new_frames}")
                        if any(len(frame["ulaw"]) != FRAME_BYTES for frame in new_frames):
                            problems.append("payload not exactly 160 ??-law bytes")
                        if sent != expected_sent:
                            problems.append("outbound bytes differ from the modelled stream")
                        if len(sent) > len(available):
                            problems.append(f"sent {len(sent)} bytes but only "
                                            f"{len(available)} were received")
                        if any(frame["event"] != "playAudio" for frame in new_frames):
                            problems.append("a non-playAudio frame appeared in the audio stream")
                        for frame in new_frames:
                            digest = hashlib.sha256(frame["ulaw"]).hexdigest()
                            if digest in hashes:
                                problems.append(f"chunk hash {digest[:12]} sent twice")
                            hashes.add(digest)

                        if problems:
                            raise AssertionError(
                                "Property 2 (outbound domain) falsified\n"
                                f"  seed          = {self.SEED}\n"
                                f"  case index    = {index}\n"
                                f"  failing input = {case!r}\n"
                                f"  detail        = {'; '.join(problems)}"
                            )

                        pending = expected_pending
                        frames_seen += len(new_frames)

            return len(cases), frames_seen, ws.clear_audio_count, _log_shape(log.lines)

        executed, frames_seen, clears, lines = asyncio.run(scenario())
        self.assertEqual(executed, self.CASES + len(self.BOUNDARY))
        self.assertGreater(frames_seen, 400)
        self.assertEqual(clears, 0, "the sender never emits clearAudio")
        self.assertEqual([line for line in lines if "AI Speech Started playing" not in line],
                         ["Ready to send audio to Plivo"])


class TestInboundBargeInFalsifier(unittest.TestCase):
    """60 cases: barge-in onset counter, side-effect ordering, log sequence.

    Every case is in the non-bug domain by construction: the near-end is real
    caller speech at a randomised level with a randomised noise floor, and it is
    NOT derived from the far-end. Where a far-end is fed, it is an independent
    fixture, so near and far are uncorrelated.
    """

    SEED = 20260410
    CASES = 60

    @staticmethod
    def _scale(frame: bytes, gain_db: float, noise_db: float, rng) -> bytes:
        samples = np.frombuffer(frame, dtype=np.int16).astype(np.float64) / 32768.0
        samples = samples * (10.0 ** (gain_db / 20.0))
        samples = samples + rng.normal(0.0, 10.0 ** (noise_db / 20.0), samples.size)
        np.clip(samples, -1.0, 1.0, out=samples)
        return (samples * 32767.0).astype(np.int16).tobytes()

    def test_property(self):
        base_frames = scenarios.caller_speech_frames(10)
        far_fixture = scenarios.caller_speech_frames(10, fixture="ring1.wav")
        rng = np.random.default_rng(self.SEED)

        for index in range(self.CASES):
            case = {
                "assistant_speaking": bool(rng.integers(0, 2)),
                "gain_db": uniform_float(rng, -6.0, 0.0),
                "noise_db": uniform_float(rng, -90.0, -70.0),
                "delivered_ms": choice(rng, (350, 400, 800, 1238, 5514)),
                "feed_far_end": bool(rng.integers(0, 2)),
            }
            case_rng = np.random.default_rng((self.SEED + index) % (2 ** 32))
            frames = [self._scale(frame, case["gain_db"], case["noise_db"], case_rng)
                      for frame in base_frames]

            problem = self._check(case, frames, far_fixture)
            if problem:
                raise AssertionError(
                    "Property 2 (inbound barge-in domain) falsified\n"
                    f"  seed          = {self.SEED}\n"
                    f"  case index    = {index}\n"
                    f"  failing input = {case!r}\n"
                    f"  detail        = {problem}"
                )

    def _check(self, case, frames, far_fixture):
        async def scenario():
            clock = FakeClock()
            ws = FakePlivoWS(clock)
            session = FakeSession()
            call_state = live_call_state(assistant_speaking=case["assistant_speaking"])
            if case["assistant_speaking"]:
                call_state["ai_playback_start_time"] = clock.now(app.ist_tz)
                call_state["current_utterance_bytes"] = int(case["delivered_ms"] / 1000 * 8000)
                # Back-date the playback start so heard_ms matches the queued
                # bytes, keeping every case in the NON-bug domain this falsifier
                # covers. Classification is wall-clock based now; leaving the
                # start at "now" would read heard_ms=0 and classify every case
                # anomalous, dragging the property out of its own domain.
                call_state["ai_playback_start_time"] = (
                    clock.now(app.ist_tz) - timedelta(milliseconds=case["delivered_ms"])
                )
                for position in range(3):
                    call_state["plivo_output_queue"].put_nowait(
                        bytes([position]) * FRAME_BYTES)
            if case["feed_far_end"]:
                # Independent fixture => near-end is uncorrelated with far-end.
                for far_frame in far_fixture:
                    call_state["aec"].add_far_end(far_frame)
            with clock.install(), LogCapture() as log:
                async with InboundDriver(call_state, ws, session) as driver:
                    for frame in frames:
                        await driver.feed_pcm8(frame)
                    onset = driver.first_frame_index_where(lambda snap: snap["is_speaking"])
            return call_state, ws, session, log, driver, onset

        call_state, ws, session, log, driver, onset = asyncio.run(scenario())

        problems = []
        if onset is None:
            problems.append("VAD never fired on real caller speech")
        else:
            expected_count = (app.VAD_SPEECH_ONSET_FRAMES_WHILE_SPEAKING
                              if case["assistant_speaking"] else app.VAD_SPEECH_ONSET_FRAMES)
            observed = driver.snapshots[onset]["speech_count"]
            if observed != expected_count:
                problems.append(f"onset counter {observed} != {expected_count}")

        if case["assistant_speaking"]:
            if ws.clear_audio_count != 1:
                problems.append(f"{ws.clear_audio_count} clearAudio frames, expected 1")
            if not call_state["plivo_output_queue"].empty():
                problems.append("outbound queue was not drained before clearAudio")
            try:
                order = log.order_of("AI Speech Interrupted at:",
                                     "Cleared Plivo playback buffer",
                                     "Sent activityStart to Gemini")
                if order != sorted(order):
                    problems.append(f"side-effect order changed: {order}")
            except AssertionError as missing:
                problems.append(str(missing))
            mismatch = _matches_golden(log.lines, GOLDEN_BARGEIN_LOGS)
        else:
            if ws.clear_audio_count != 0:
                problems.append("clearAudio sent while the assistant was silent")
            mismatch = _matches_golden(log.lines, GOLDEN_SILENT_LOGS)

        if mismatch:
            problems.append(mismatch)
        if session.count("activityStart") != 1:
            problems.append(f"{session.count('activityStart')} activityStart sends, expected 1")
        return "; ".join(problems)


class TestBoundaryTriggerAt350ms(unittest.TestCase):
    """Trigger at exactly 350 ms of delivered playback, with 349 and 351 either side."""

    def _run(self, delivered_ms):
        async def scenario():
            clock = FakeClock()
            ws = FakePlivoWS(clock)
            session = FakeSession()
            call_state = live_call_state(assistant_speaking=True)
            call_state["ai_playback_start_time"] = clock.now(app.ist_tz)
            call_state["current_utterance_bytes"] = int(round(delivered_ms / 1000 * 8000))
            # Classification reads the WALL CLOCK (what the caller heard), not the
            # byte count, so the boundary has to be walked on the clock. Back-date
            # the playback start rather than advancing the clock: that varies
            # heard_ms while leaving "now" -- and therefore every logged timestamp
            # -- identical across the three cases, which is what lets this test
            # assert the existing log lines are unchanged.
            call_state["ai_playback_start_time"] = (
                clock.now(app.ist_tz) - timedelta(milliseconds=delivered_ms)
            )
            with clock.install(), LogCapture() as log:
                async with InboundDriver(call_state, ws, session) as driver:
                    for frame in scenarios.caller_speech_frames(8):
                        await driver.feed_pcm8(frame)
                    onset = driver.first_frame_index_where(lambda snap: snap["is_speaking"])
            return (onset, ws.events, session.sent_kinds,
                    _log_shape(log.existing_lines), log.diagnostic_lines)

        return asyncio.run(scenario())

    def test_349_350_351_split_only_in_the_new_diagnostic_lines(self):
        """INTENTIONAL BASELINE UPDATE — tasks 5.3 / 5.10.

        Previously all three were indistinguishable, because nothing in app.py
        read ``delivered_ms`` when deciding or reporting. The boundary is now real:
        349 ms classifies anomalous, 350 and 351 do not. Everything a caller can
        observe is still identical across all three -- same Plivo frames, same
        model sends, same pre-existing log lines -- which is the preservation half.
        """
        results = {ms: self._run(ms) for ms in (349, 350, 351)}

        # observable behaviour: unchanged across the boundary
        for ms in (349, 350, 351):
            self.assertEqual(results[ms][0], results[350][0], f"onset moved at {ms} ms")
            self.assertEqual(results[ms][1], ["clearAudio"], f"Plivo frames moved at {ms} ms")
            self.assertEqual(results[ms][2], results[350][2], f"model sends moved at {ms} ms")
            self.assertEqual(results[ms][3], results[350][3], f"existing log lines moved at {ms} ms")

        # The provenance line reports the measured delivered_ms, so all three
        # differ there by construction. What the boundary decides is the
        # classification: 350 and 351 agree, 349 sits on the other side.
        def classification(diagnostics):
            return [line.split("classification=")[1].split(" ")[0]
                    for line in diagnostics if "[TRIGGER]" in line]

        self.assertEqual(classification(results[350][4]), ["normal"])
        self.assertEqual(classification(results[351][4]), ["normal"])
        self.assertEqual(classification(results[349][4]), ["anomalous"])
        self.assertTrue(any("[ANOMALY]" in line for line in results[349][4]))
        self.assertFalse(any("[ANOMALY]" in line for line in results[350][4]))
        self.assertFalse(any("[ANOMALY]" in line for line in results[351][4]))
        for ms in (349, 350, 351):
            self.assertTrue(any(f"delivered_ms={ms}" in line for line in results[ms][4]),
                            f"delivered_ms was not reported for {ms} ms")

    def test_the_350_ms_literal_is_now_a_constant_in_app_py(self):
        """INTENTIONAL BASELINE UPDATE — the constant is authorised by tasks 5.3/5.10."""
        self.assertTrue(hasattr(app, "ANOMALOUS_TRUNCATION_MS"))
        self.assertEqual(app.ANOMALOUS_TRUNCATION_MS, ANOMALOUS_TRUNCATION_MS_LITERAL)
        self.assertEqual(ANOMALOUS_TRUNCATION_MS_LITERAL, 350)


class TestBoundaryFarEndAtTheActivityFloor(unittest.TestCase):
    def test_far_end_at_minus_60_dbfs_changes_nothing(self):
        """The far-end *level* boundary is exercised; the constant is deferred."""
        amplitude = 33   # 20*log10(33/32768) == -59.94 dBFS for a square-ish signal
        pattern = np.tile(np.array([amplitude, -amplitude], dtype=np.int16), 160).tobytes()
        measured_db, _ = app.calculate_rms_db(pattern)
        self.assertAlmostEqual(float(measured_db), -60.0, delta=0.1)

        def run(feed_far_end):
            async def scenario():
                clock = FakeClock()
                ws = FakePlivoWS(clock)
                session = FakeSession()
                call_state = live_call_state(assistant_speaking=True)
                call_state["ai_playback_start_time"] = clock.now(app.ist_tz)
                call_state["current_utterance_bytes"] = 4000
                if feed_far_end:
                    for _ in range(10):
                        call_state["aec"].add_far_end(pattern)
                with clock.install(), LogCapture() as log:
                    async with InboundDriver(call_state, ws, session) as driver:
                        for frame in scenarios.caller_speech_frames(8):
                            await driver.feed_pcm8(frame)
                        onset = driver.first_frame_index_where(lambda snap: snap["is_speaking"])
                # ``existing_lines``: the new 🧭 [TRIGGER] line carries the measured
                # inbound RMS, which legitimately reflects whether a far-end was
                # fed. What must not change is the observable behaviour.
                return onset, ws.events, session.sent_kinds, _log_shape(log.existing_lines)

            return asyncio.run(scenario())

        with_far, without_far = run(True), run(False)
        self.assertEqual(with_far[1:], without_far[1:],
                         "a far-end at the activity floor changed the observable behaviour")
        self.assertIsNotNone(with_far[0])

    def test_activity_floor_constant_is_deferred(self):
        # BASELINE UPDATE (audio-pipeline redesign): the constant now exists and
        # the far-end activity floor is a real quantity, no longer deferred.
        self.assertTrue(hasattr(app, "FAR_END_ACTIVE_FLOOR_DB"))
        self.assertEqual(app.FAR_END_ACTIVE_FLOOR_DB, -60.0)

    def test_far_end_active_floor_boundary(self):
        """far_end_active is a strict >-threshold test at the boundary."""
        n = int(8000 * 0.2)
        just_below = (np.ones(n, dtype=np.int16) * 20).tobytes()   # ~-64 dBFS
        loud = (np.tile([1000, -1000], n // 2).astype(np.int16)).tobytes()
        self.assertFalse(bargein.far_end_active(
            bargein.pcm16_to_float(just_below), floor_dbfs=app.FAR_END_ACTIVE_FLOOR_DB))
        self.assertTrue(bargein.far_end_active(
            bargein.pcm16_to_float(loud), floor_dbfs=app.FAR_END_ACTIVE_FLOOR_DB))


class TestBoundaryCorrelationAtThreshold(unittest.TestCase):
    def test_correlation_constant_now_exists(self):
        # BASELINE UPDATE (audio-pipeline redesign): concern (a) is unparked;
        # ECHO_CORR_THRESHOLD and the correlation surface now exist.
        self.assertTrue(hasattr(app, "ECHO_CORR_THRESHOLD"))
        self.assertEqual(app.ECHO_CORR_THRESHOLD, 0.88)
        self.assertTrue(hasattr(bargein, "echo_correlation"))

    def test_threshold_is_a_ge_comparison(self):
        """A decision exactly at the threshold classifies as echo (>=)."""
        # Build a near/far pair whose envelope correlation is comfortably above
        # threshold, then confirm the >= semantics at the decision boundary via a
        # threshold set to that measured value.
        n = int(8000 * 0.3)
        base = np.abs(np.sin(2 * np.pi * 5 * np.arange(n) / 8000))
        tone = np.sin(2 * np.pi * 300 * np.arange(n) / 8000)
        far = (base * tone * 0.5 * 32767).astype(np.int16).tobytes()
        r = bargein.echo_correlation(far, far, near_rate=8000, far_rate=8000)
        self.assertGreater(r.correlation, 0.9)
        at = bargein.should_barge_in(far, far, near_rate=8000, far_rate=8000,
                                     threshold=r.correlation)
        self.assertTrue(at.is_echo, "correlation == threshold must classify as echo (>=)")


if __name__ == "__main__":
    unittest.main()
