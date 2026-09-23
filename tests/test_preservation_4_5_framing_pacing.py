"""Task 4.5 -- framing, pacing and byte conservation.

OBSERVATION-FIRST. Requirement 3.9.

Golden records observed on unfixed code:

* every ``playAudio`` payload decodes to exactly ``PLIVO_ULAW_CHUNK_SIZE`` = 160
  ??-law bytes, ``contentType`` ``audio/x-mulaw``, ``sampleRate`` 8000;
* the outbound stream is exactly the queued stream truncated to whole 160-byte
  frames -- same bytes, same order, nothing inserted, nothing repeated;
* a chunk shorter than one frame emits nothing and is carried in ``out_buffer``
  until 160 bytes have accumulated;
* while ``interrupting`` or ``user_activity_open`` is set, ``out_buffer`` is
  cleared and those bytes are never sent, so ``sent <= queued`` strictly.

CORRECTION to the spec, and it changes what 3.9's "existing 20 ms outbound
pacing" can mean: **``send_plivo_audio`` contains no pacing sleep.** It blocks on
``asyncio.wait_for(plivo_output_queue.get(), timeout=PLIVO_SEND_POLL_TIMEOUT)``
and then sends every whole 160-byte frame back to back with no delay
(app.py 1925-1966). The only explicit 20 ms pacing in the file is in
``play_disclaimer`` (app.py 766-769), which this spec must not modify. Outbound
spacing during a model turn is therefore governed by the arrival rate of
``inline_data`` chunks from the model, not by the sender.

Measured: with a 1 s turn delivered as 1000-byte queue items, all 50 frames were
sent at the same virtual instant -- inter-frame gaps of 0.0 ms, not 20.0 ms. That
is the baseline. A future commit gate (task 5.10) that "resumes normal 20 ms
pacing" would therefore be *introducing* pacing, not restoring it, and that is a
behavioural change to 3.9 which needs to be called out in design rather than
assumed away.
"""

from __future__ import annotations

import asyncio
import hashlib
import unittest

import numpy as np

from tests.harness import scenarios
from tests.harness.appctl import LogCapture, app, live_call_state
from tests.harness.falsifier import run_property, uniform_int
from tests.harness.fakes import (
    FakeClock,
    FakePlivoClient,
    FakePlivoWS,
    FakeSession,
    SenderHarness,
    drain_queue,
)

# --- golden records, observed on unfixed code --------------------------------
OBSERVED_INTER_FRAME_GAP_MS = 0.0
FRAME_BYTES = 160


class TestFramingAndPacing(unittest.TestCase):
    def setUp(self):
        async def scenario():
            clock = FakeClock()
            ws = FakePlivoWS(clock)
            session = FakeSession()
            client = FakePlivoClient()
            call_state = live_call_state(with_denoiser=False)
            ulaw = scenarios.model_audio_to_ulaw(scenarios.model_audio_24k(1000))
            items = [ulaw[offset:offset + 1000] for offset in range(0, len(ulaw), 1000)]
            with clock.install(), LogCapture() as log:
                async with SenderHarness(call_state, clock, ws, session, client) as sender:
                    await sender.enqueue(*items)
                    await sender.settle()
            return call_state, ws, log, ulaw

        self.call_state, self.ws, self.log, self.ulaw = asyncio.run(scenario())

    def test_every_payload_is_exactly_160_ulaw_bytes(self):
        sizes = {len(payload) for payload in self.ws.play_audio_payloads}
        self.assertEqual(sizes, {app.PLIVO_ULAW_CHUNK_SIZE})
        self.assertEqual(app.PLIVO_ULAW_CHUNK_SIZE, FRAME_BYTES)

    def test_frame_envelope_fields(self):
        for frame in self.ws.play_audio_frames:
            self.assertEqual(frame["content_type"], "audio/x-mulaw")
            self.assertEqual(frame["sample_rate"], 8000)
            self.assertEqual(set(frame["frame"].keys()), {"event", "media"})
            self.assertEqual(set(frame["frame"]["media"].keys()),
                             {"contentType", "sampleRate", "payload"})

    def test_observed_pacing_is_back_to_back_not_20ms(self):
        gaps = self.ws.inter_frame_gaps_ms
        self.assertGreater(len(gaps), 10)
        self.assertEqual(set(gaps), {OBSERVED_INTER_FRAME_GAP_MS})

    def test_no_pacing_sleep_exists_in_the_sender(self):
        """Source-level companion to the measurement above."""
        from tests.harness.appctl import APP_PY

        with open(APP_PY, "r", encoding="utf-8") as handle:
            source = handle.read()
        sender = source[source.index("async def send_plivo_audio"):
                        source.index("@app.route(\"/trigger-call\"")]
        self.assertNotIn("asyncio.sleep(0.02", sender)
        self.assertIn("asyncio.sleep(0.2)", source)  # only the terminal-action drain sleep

    def test_utterance_byte_accounting(self):
        self.assertEqual(self.call_state["current_utterance_bytes"],
                         len(self.ws.outbound_ulaw))
        self.assertEqual(self.call_state["current_utterance_bytes"] % FRAME_BYTES, 0)


class TestByteConservation(unittest.TestCase):
    """The downstream half of the upstream/downstream discriminator, asserted as
    an invariant rather than only logged."""

    def test_sent_stream_is_the_whole_frame_prefix_of_the_queued_stream(self):
        async def scenario():
            clock = FakeClock()
            ws = FakePlivoWS(clock)
            session = FakeSession()
            client = FakePlivoClient()
            call_state = live_call_state(with_denoiser=False)
            rng = np.random.default_rng(11)
            items = [scenarios.random_ulaw(rng, size) for size in (37, 160, 401, 999, 13)]
            with clock.install():
                async with SenderHarness(call_state, clock, ws, session, client) as sender:
                    await sender.enqueue(*items)
                    await sender.settle()
            return ws, b"".join(items)

        ws, queued = asyncio.run(scenario())
        whole = len(queued) // FRAME_BYTES * FRAME_BYTES
        self.assertEqual(ws.outbound_ulaw, queued[:whole])
        self.assertLessEqual(len(ws.outbound_ulaw), len(queued))

    def test_short_first_chunk_emits_nothing_until_a_frame_accumulates(self):
        """Boundary case: a turn whose first chunk is shorter than 20 ms."""
        async def scenario():
            clock = FakeClock()
            ws = FakePlivoWS(clock)
            session = FakeSession()
            client = FakePlivoClient()
            call_state = live_call_state(with_denoiser=False)
            rng = np.random.default_rng(3)
            short = scenarios.random_ulaw(rng, 100)     # 12.5 ms
            rest = scenarios.random_ulaw(rng, 380)
            with clock.install():
                async with SenderHarness(call_state, clock, ws, session, client) as sender:
                    await sender.enqueue(short)
                    await sender.settle()
                    after_short = len(ws.play_audio_frames)
                    await sender.enqueue(rest)
                    await sender.settle()
            return after_short, ws, short + rest

        after_short, ws, queued = asyncio.run(scenario())
        self.assertEqual(after_short, 0)
        self.assertEqual(len(ws.play_audio_frames), 3)
        self.assertEqual(ws.outbound_ulaw, queued[:480])

    def test_interrupting_discards_rather_than_re_sends(self):
        async def scenario():
            clock = FakeClock()
            ws = FakePlivoWS(clock)
            session = FakeSession()
            client = FakePlivoClient()
            call_state = live_call_state(with_denoiser=False, interrupting=True)
            rng = np.random.default_rng(5)
            discarded = scenarios.random_ulaw(rng, 1600)
            kept = scenarios.random_ulaw(rng, 1600)
            with clock.install():
                async with SenderHarness(call_state, clock, ws, session, client) as sender:
                    await sender.enqueue(discarded)
                    await sender.settle()
                    during_interrupt = len(ws.play_audio_frames)
                    call_state["interrupting"] = False
                    await sender.enqueue(kept)
                    await sender.settle()
            return during_interrupt, ws, discarded, kept

        during_interrupt, ws, discarded, kept = asyncio.run(scenario())
        self.assertEqual(during_interrupt, 0)
        self.assertEqual(ws.outbound_ulaw, kept)
        self.assertNotIn(discarded[:160], ws.play_audio_payloads)

    def test_no_chunk_hash_is_ever_sent_twice(self):
        """Hash uniqueness over distinct-content frames.

        Deliberately over randomised bytes, not over real audio: real audio
        contains runs of identical silence frames, so identical frame *content*
        occurs legitimately and would falsify a naive uniqueness claim for
        reasons unrelated to re-delivery. Randomised content makes a repeated
        hash mean a repeated buffer.
        """
        async def scenario():
            clock = FakeClock()
            ws = FakePlivoWS(clock)
            session = FakeSession()
            client = FakePlivoClient()
            call_state = live_call_state(with_denoiser=False)
            rng = np.random.default_rng(23)
            items = [scenarios.random_ulaw(rng, 1600) for _ in range(10)]
            with clock.install():
                async with SenderHarness(call_state, clock, ws, session, client) as sender:
                    await sender.enqueue(*items)
                    await sender.settle()
            return ws

        ws = asyncio.run(scenario())
        hashes = [hashlib.sha256(payload).hexdigest() for payload in ws.play_audio_payloads]
        self.assertEqual(len(hashes), 100)
        self.assertEqual(len(set(hashes)), len(hashes))


class TestByteConservationFalsifier(unittest.TestCase):
    """500 seeded cases over randomised turn shapes and chunk sizes.

    All cases run inside one ``send_plivo_audio`` lifetime; each case is a "turn"
    whose byte accounting is reset before it starts. That keeps the wall-clock
    cost of 500 cases at a few seconds, since the only real-time cost in the
    sender is the 100 ms poll timeout, which only fires when the queue is idle.
    """

    SEED = 20260301
    CASES = 500
    BOUNDARY_SHAPES = ([1], [159], [160], [161], [1, 1, 1], [1200, 1])

    def test_property(self):
        async def scenario():
            clock = FakeClock()
            ws = FakePlivoWS(clock)
            session = FakeSession()
            client = FakePlivoClient()
            call_state = live_call_state(with_denoiser=False)

            rng = np.random.default_rng(self.SEED)
            shapes = list(self.BOUNDARY_SHAPES) + [
                [uniform_int(rng, 1, 1200) for _ in range(uniform_int(rng, 1, 5))]
                for _ in range(self.CASES)
            ]

            # ``carry`` is the model of ``out_buffer``: bytes accepted by the
            # sender but not yet a whole frame. ``pending`` is the exact byte
            # sequence those bytes should be, so the prefix assertion is on
            # content and order, not just on counts.
            state = {"frames": 0, "pending": b""}
            seen_hashes: set[str] = set()

            async with SenderHarness(call_state, clock, ws, session, client) as sender:
                for index, sizes in enumerate(shapes):
                    seed_for_case = (self.SEED + index) % (2 ** 32)
                    case_rng = np.random.default_rng(seed_for_case)
                    items = [scenarios.random_ulaw(case_rng, size) for size in sizes]
                    queued = b"".join(items)
                    available = state["pending"] + queued

                    await sender.enqueue(*items)
                    expected_frames = len(available) // FRAME_BYTES
                    await sender.wait_sent(state["frames"] + expected_frames)
                    await drain_queue(call_state, idle_polls=2, poll=0.001)

                    new_frames = ws.play_audio_frames[state["frames"]:]
                    sent = b"".join(frame["ulaw"] for frame in new_frames)

                    problems = []
                    if any(len(frame["ulaw"]) != FRAME_BYTES for frame in new_frames):
                        problems.append("a payload was not exactly 160 bytes")
                    if len(sent) > len(available):
                        problems.append(f"sent {len(sent)} bytes but only {len(available)} "
                                        "had been received")
                    if sent != available[:len(sent)]:
                        problems.append("sent bytes are not the received stream's prefix")
                    if len(new_frames) != expected_frames:
                        problems.append(f"expected {expected_frames} frames, "
                                        f"got {len(new_frames)}")
                    for frame in new_frames:
                        digest = hashlib.sha256(frame["ulaw"]).hexdigest()
                        if digest in seen_hashes:
                            problems.append(f"chunk hash {digest[:12]} sent twice")
                        seen_hashes.add(digest)

                    if problems:
                        raise AssertionError(
                            "byte conservation falsified\n"
                            f"  seed          = {self.SEED} (case rng seed {seed_for_case})\n"
                            f"  case index    = {index}\n"
                            f"  failing input = chunk sizes {sizes!r}\n"
                            f"  detail        = {'; '.join(problems)}"
                        )

                    state["pending"] = available[len(sent):]
                    state["frames"] += len(new_frames)

            return len(shapes), state["frames"]

        executed, frames_sent = asyncio.run(scenario())
        self.assertEqual(executed, self.CASES + len(self.BOUNDARY_SHAPES))
        self.assertGreater(frames_sent, 500)


class TestFalsifierHelperIsUsedElsewhere(unittest.TestCase):
    """The shared falsifier helper is exercised on the pure ??-law codec too,
    which is cheap enough for a wide sweep."""

    def test_ulaw_roundtrip_framing_property(self):
        def generate(rng, index):
            return uniform_int(rng, 1, 400)

        def check(sample_count):
            pcm = np.random.default_rng(sample_count).integers(
                -32768, 32767, sample_count, dtype=np.int16).tobytes()
            ulaw = app.pcm_to_ulaw(pcm)
            if len(ulaw) != sample_count:
                return f"pcm_to_ulaw produced {len(ulaw)} bytes for {sample_count} samples"
            back = app.ulaw_to_pcm(ulaw)
            if len(back) != sample_count * 2:
                return f"ulaw_to_pcm produced {len(back)} bytes"
            return None

        executed = run_property("ulaw framing", generate, check, cases=600, seed=4242,
                                boundary_cases=(1, 159, 160, 161, 320))
        self.assertEqual(executed, 605)


if __name__ == "__main__":
    unittest.main()
