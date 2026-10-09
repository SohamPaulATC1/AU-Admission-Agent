"""Task 4.7 -- ``aec.py`` integrity and placement.

Requirement 3.10.

The content hash below was computed against ``aec.py`` as it stands at the start
of this pass and is pinned. If it changes, requirement 3.10 has been violated.

    sha256 = 495a82ca41a97460a9885aa1f62b9c856e8feee158fe4f8785223be3d20edd29
    bytes  = 7599
    lines  = 196
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import unittest

from tests.harness import scenarios
from tests.harness.appctl import AEC_PY, APP_PY, app, live_call_state
from tests.harness.fakes import FakeClock, FakePlivoWS, FakeSession, InboundDriver

AEC_SHA256 = "495a82ca41a97460a9885aa1f62b9c856e8feee158fe4f8785223be3d20edd29"
AEC_BYTE_COUNT = 7599
AEC_LINE_COUNT = 196

# Public surface of ``aec.AcousticEchoCanceller`` at the pinned hash.
AEC_PUBLIC_METHODS = {"add_far_end", "reset_far_end", "process"}


class TestAecPyIsByteIdentical(unittest.TestCase):
    def test_content_hash(self):
        with open(AEC_PY, "rb") as handle:
            data = handle.read()
        self.assertEqual(hashlib.sha256(data).hexdigest(), AEC_SHA256,
                         "aec.py was modified -- requirement 3.10 forbids it")
        self.assertEqual(len(data), AEC_BYTE_COUNT)
        self.assertEqual(data.count(b"\n"), AEC_LINE_COUNT)


class TestAppOnlyCallsExistingPublicMethods(unittest.TestCase):
    def test_source_level_call_sites(self):
        with open(APP_PY, "r", encoding="utf-8") as handle:
            source = handle.read()
        called = set(re.findall(r'call_state\["aec"\]\.(\w+)', source))
        self.assertIn('"aec": AcousticEchoCanceller(frame_size=PLIVO_ULAW_CHUNK_SIZE)', source,
                      "the canceller is constructed with its existing signature")
        self.assertTrue(called <= AEC_PUBLIC_METHODS,
                        f"app.py calls non-public or new AEC methods: {called - AEC_PUBLIC_METHODS}")
        self.assertEqual(called, {"reset_far_end", "process", "add_far_end"})

    def test_no_private_attribute_access_from_app(self):
        with open(APP_PY, "r", encoding="utf-8") as handle:
            source = handle.read()
        self.assertEqual(re.findall(r'call_state\["aec"\]\._\w+', source), [])

    def test_public_methods_exist_on_the_module(self):
        from aec import AcousticEchoCanceller

        for name in AEC_PUBLIC_METHODS:
            self.assertTrue(callable(getattr(AcousticEchoCanceller, name, None)), name)

    def test_add_far_end_has_exactly_one_call_site(self):
        """BASELINE UPDATE — audio-pipeline redesign (Part 1) MOVED this call.

        Requirement 3.10 protects the invariant that there is still **exactly
        one** ``add_far_end`` call site. The redesign moved it OUT of the send
        path (``emit_chunk``) and INTO the inbound loop, fed one frame per
        processed block from ``farend_ref_queue``. This is the whole point of
        Part 1: the AEC reference is now paced to playout (inbound realtime)
        instead of loaded in the send burst, which is what the measured
        buffer_lead (208 -> 558 ms, past the 480 ms modelled window) required.
        The send path now only ENQUEUES the frame; it no longer feeds the AEC.
        """
        with open(APP_PY, "r", encoding="utf-8") as handle:
            source = handle.read()
        lines = source.splitlines()
        sites = [index + 1 for index, line in enumerate(lines) if "add_far_end(" in line]
        self.assertEqual(len(sites), 1, f"add_far_end gained a call site: {sites}")
        # Now fed from the playout-paced queue in the inbound loop.
        self.assertIn('call_state["aec"].add_far_end(far_ref)', source)
        # The send path enqueues rather than feeding the AEC directly.
        self.assertIn('call_state["farend_ref_queue"].append(far_pcm8)', source)
        self.assertIn("far_pcm8 = ulaw_to_pcm(chunk)", source)
        # Still exactly one decode of the sent chunk (shared: queue + recorder).
        self.assertEqual(source.count("ulaw_to_pcm(chunk)"), 1)

    def test_clear_audio_send_sites(self):
        """The three ``clearAudio`` sites named by the design, verified.

        ``play_disclaimer`` is not among them -- it sends ``playAudio`` only --
        which is consistent with the design's own correction.

        BASELINE UPDATE — audio-pipeline redesign. Task 5.8 first added a bare
        ``reset_far_end()`` beside each ``clearAudio``. The redesign wraps that
        in ``reset_far_reference(call_state)``, which resets the AEC FIFO AND
        clears the playout-paced ``farend_ref_queue`` and correlation
        ``far_shadow`` in lockstep (frames discarded by ``clearAudio`` must not
        keep feeding the AEC or the gate). So the pairing is now: every
        ``clearAudio`` is followed by a ``reset_far_reference(call_state)`` call,
        and the only bare ``reset_far_end()`` left is inside that helper.
        Invariant: exactly three ``clearAudio`` sites, each paired with a reset,
        plus the reconnect-path reset — four ``reset_far_reference`` calls total.
        """
        with open(APP_PY, "r", encoding="utf-8") as handle:
            source = handle.read()
        lines = source.splitlines()
        sites = [index + 1 for index, line in enumerate(lines) if '"event": "clearAudio"' in line]
        self.assertEqual(len(sites), 3, f"clearAudio send sites changed count: {sites}")

        # reset_far_end() is now called in exactly one place: the helper body.
        bare_resets = [
            index for index, line in enumerate(lines)
            if re.search(r'^\s*call_state\["aec"\]\.reset_far_end\(\)\s*$', line)
        ]
        self.assertEqual(len(bare_resets), 1,
                         "reset_far_end() should be called only inside "
                         "reset_far_reference()")

        # reset_far_reference: 3 clearAudio sites + 1 reconnect path = 4 calls.
        ref_positions = [
            index for index, line in enumerate(lines)
            if re.search(r'^\s*reset_far_reference\(call_state\)\s*$', line)
        ]
        self.assertEqual(len(ref_positions), 4,
                         f"expected 3 clearAudio-site resets + 1 reconnect reset, found at "
                         f"{[i + 1 for i in ref_positions]}")

        clear_positions = [
            index for index, line in enumerate(lines) if '"event": "clearAudio"' in line
        ]
        # Every clearAudio site pairs with exactly one reset_far_reference within
        # the next 20 lines -- paired, not merely present. The reconnect reset is
        # far from all three and must not satisfy this for any of them.
        for clear_line in clear_positions:
            paired = [r for r in ref_positions if clear_line < r <= clear_line + 20]
            self.assertEqual(len(paired), 1,
                             f"clearAudio at line {clear_line + 1} has no paired "
                             f"reset_far_reference() within 20 lines")


class _AecSpy:
    """Transparent proxy over the real canceller that records call order."""

    def __init__(self, inner):
        self._inner = inner
        self.calls: list[str] = []

    def process(self, pcm16):
        self.calls.append("aec.process")
        return self._inner.process(pcm16)

    def add_far_end(self, pcm16):
        self.calls.append("aec.add_far_end")
        return self._inner.add_far_end(pcm16)

    def reset_far_end(self):
        self.calls.append("aec.reset_far_end")
        return self._inner.reset_far_end()


class _DenoiserSpy:
    def __init__(self, inner, sink):
        self._inner = inner
        self._sink = sink

    def denoise_chunk(self, chunk):
        self._sink.append("rnnoise.denoise_chunk")
        return self._inner.denoise_chunk(chunk)


class TestAecRunsAheadOfRnnoise(unittest.TestCase):
    def test_runtime_call_order_is_aec_then_rnnoise_per_frame(self):
        async def scenario():
            clock = FakeClock()
            ws = FakePlivoWS(clock)
            session = FakeSession()
            call_state = live_call_state()
            spy = _AecSpy(call_state["aec"])
            call_state["aec"] = spy
            call_state["denoiser"] = _DenoiserSpy(call_state["denoiser"], spy.calls)
            with clock.install():
                async with InboundDriver(call_state, ws, session) as driver:
                    for frame in scenarios.caller_speech_frames(4):
                        await driver.feed_pcm8(frame)
            return spy.calls

        calls = asyncio.run(scenario())
        # BASELINE UPDATE — audio-pipeline redesign. The far-end reference is now
        # fed one frame per inbound block (playout pacing), so add_far_end
        # precedes process on every frame, then RNNoise. Still AEC-before-RNNoise.
        self.assertEqual(
            calls,
            ["aec.add_far_end", "aec.process", "rnnoise.denoise_chunk"] * 4)

    def test_source_order_within_the_media_branch(self):
        with open(APP_PY, "r", encoding="utf-8") as handle:
            source = handle.read()
        branch = source[source.index("elif data['event'] == 'media':"):
                        source.index("elif data['event'] == 'stop':")]
        # Playout-paced feed then cancel then denoise.
        self.assertLess(branch.index('call_state["aec"].add_far_end'),
                        branch.index('call_state["aec"].process'))
        self.assertLess(branch.index('call_state["aec"].process'),
                        branch.index('denoiser"].denoise_chunk'))
        self.assertLess(branch.index('call_state["aec"].process'),
                        branch.index("audioop.ratecv"))


class TestTask58ResetsFarEndOnClearAudio(unittest.TestCase):
    """Task 5.8, at runtime rather than by source inspection.

    Drives a real barge-in through ``app.stream_plivo_to_gemini`` (the same
    path and golden fed-frame indices as test_preservation_4_1_bargein) and
    asserts ``reset_far_end`` is called on the real ``AcousticEchoCanceller``
    immediately after ``clearAudio`` is sent -- not merely present somewhere in
    the source. ``_AecSpy`` already exists above for the AEC-before-RNNoise
    ordering test; reused here for the reset ordering.
    """

    def test_reset_far_end_follows_the_barge_in_clear_audio(self):
        from datetime import timedelta

        async def scenario():
            clock = FakeClock()
            ws = FakePlivoWS(clock)
            session = FakeSession()
            call_state = live_call_state()
            spy = _AecSpy(call_state["aec"])
            call_state["aec"] = spy
            call_state["assistant_speaking"] = True
            call_state["ai_playback_start_time"] = (
                clock.now(app.ist_tz) - timedelta(milliseconds=200)
            )
            call_state["current_utterance_bytes"] = int(200 / 1000 * 8000)
            for index in range(4):
                call_state["plivo_output_queue"].put_nowait(
                    bytes([index]) * app.PLIVO_ULAW_CHUNK_SIZE)
            with clock.install():
                async with InboundDriver(call_state, ws, session) as driver:
                    for frame in scenarios.caller_speech_frames(14):
                        await driver.feed_pcm8(frame)
            return spy.calls, ws.events

        calls, events = asyncio.run(scenario())
        self.assertIn("aec.reset_far_end", calls,
                      "clearAudio fired but reset_far_end was never called on the "
                      "real canceller -- task 5.8 did not actually run")
        self.assertEqual(calls.count("aec.reset_far_end"), 1,
                         f"expected exactly one reset for one barge-in, got: {calls}")
        # Ordering: the reset must follow the LAST process() call that preceded
        # clearAudio -- i.e. it happens as part of handling this barge-in, not
        # before it (which would reset a reference that hasn't gone stale yet)
        # and not so late it misses the next incoming frame.
        reset_index = calls.index("aec.reset_far_end")
        process_calls_before = calls[:reset_index].count("aec.process")
        process_calls_after = calls[reset_index:].count("aec.process")
        self.assertGreater(process_calls_before, 0,
                           "reset fired before any inbound frame was processed at all")
        self.assertGreater(process_calls_after, 0,
                           "no inbound frame was processed after the reset -- "
                           "the call would appear to hang from the caller's side")
        self.assertEqual(events.count("clearAudio"), 1)


class TestFrozenDependencyFiles(unittest.TestCase):
    """Standing constraints: ``requirements.txt`` and ``.env`` are not modified,
    and the two test-only dependencies are still absent."""

    def test_pytest_and_hypothesis_are_not_installed(self):
        import importlib.util

        for name in ("pytest", "hypothesis"):
            self.assertIsNone(importlib.util.find_spec(name),
                              f"{name} became installed; the spec forbids adding it")

    def test_regex_is_pinned_in_requirements(self):
        from tests.harness.appctl import REPO_ROOT
        import os

        with open(os.path.join(REPO_ROOT, "requirements.txt"), "r", encoding="utf-8") as handle:
            lines = handle.read().splitlines()
        self.assertTrue(any(line.startswith("regex==") for line in lines))


if __name__ == "__main__":
    unittest.main()
