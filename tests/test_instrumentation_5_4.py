"""Task 5.4 — the concern (d) instrumentation must be shown to discriminate.

**The instrumentation is not trusted until this file passes.** Task 5.1's delta
trace exists to settle design gap 1 — upstream duplication vs downstream
re-delivery vs neither — so the three verdicts are exercised against production
code with deliberately constructed inputs rather than asserted by inspection.

This file also carries the attribution-neutrality proofs the current pass stands
or falls on, because "instrumentation-first" is only worth anything if the
instrumentation cannot itself be the explanation for whatever the next live call
does:

* task 5.4a's far-end recorder is observation-only — the outbound byte stream,
  the ``PLIVO_ULAW_CHUNK_SIZE`` framing, the inter-frame spacing and the bytes
  handed to ``aec.add_far_end`` are identical with the recorder on and off;
* task 5.10's commit gate is inert until armed — a call that never truncates
  anomalously produces a byte-identical outbound stream;
* and when it *is* armed, every transition is logged, so the gate's influence can
  be separated from what the instrumentation merely revealed.

WHAT IS DELIBERATELY NOT CLAIMED HERE. Nothing in this file measures echo. The
far-end level, envelope match and lag are concern (a), task 5.7, which was
parked when this file was written and is now implemented as redesigned; its
tests are ``test_bargein_unit`` and ``test_bug_condition_exploration``. When no
gate decision exists, the trigger-provenance line still names the evidence as
``far_end_evidence=not-measured[concern-a-parked]`` (a kept, historical log
value) and that is asserted as a placeholder, not as evidence.
"""

from __future__ import annotations

import asyncio
import audioop
import logging
import os
import tempfile
import unittest
from unittest import mock
import wave

import numpy as np

from tests.harness import bengali, echo, scenarios
from tests.harness.appctl import LogCapture, app, live_call_state
from tests.harness.fakes import (
    Callback,
    FakeClock,
    FakePlivoClient,
    FakePlivoWS,
    FakeSession,
    InboundDriver,
    SenderHarness,
    resp_audio,
    resp_output_transcription,
    resp_turn_complete,
    run_gemini_output,
)

FRAME_BYTES = app.PLIVO_ULAW_CHUNK_SIZE
TRANSCRIPT = bengali.APOLOGY_TURN_1


def _diagnostics(log, tag):
    return [line for line in log.diagnostic_lines if tag in line]


# =============================================================================
# 5.1 / 5.4 — the three verdicts
# =============================================================================

class TestUpstreamDuplicationIsFlagged(unittest.TestCase):
    """A duplicated model audio chunk inside one turn reads as UPSTREAM.

    The duplicate is injected the only way it could reach production code for
    real: as two ``inline_data`` parts carrying identical bytes in the same turn.
    """

    def _run(self, duplicate: bool):
        async def scenario():
            clock = FakeClock()
            ws = FakePlivoWS(clock)
            client = FakePlivoClient()
            call_state = live_call_state(with_denoiser=False)
            first = scenarios.model_audio_24k(120)
            second = first if duplicate else scenarios.model_audio_24k(120)[::-1]
            session = FakeSession([
                resp_output_transcription(TRANSCRIPT),
                resp_audio(first),
                resp_audio(second),
            ])
            with clock.install(), LogCapture(level=logging.DEBUG) as log:
                await run_gemini_output(session, ws, call_state, client)
                verdict = app.delta_trace_verdict(call_state)
            return call_state, verdict, log

        return asyncio.run(scenario())

    def test_duplicate_model_chunk_reads_upstream(self):
        call_state, verdict, log = self._run(duplicate=True)
        self.assertEqual(verdict["verdict"], "upstream-duplication")
        self.assertEqual(verdict["duplicate_model_chunk_hashes"], 1)
        self.assertEqual(verdict["duplicate_sent_chunk_hashes"], 0)
        # counters equal: everything the model sent was queued, nothing was sent
        # twice locally. This is the shape docs/spec/design.md (d)(2) calls upstream.
        self.assertEqual(verdict["queued_bytes"], verdict["model_ulaw_equivalent_bytes"])
        self.assertEqual(verdict["sent_bytes"], 0, "the sender is not running in this scenario")

    def test_distinct_model_chunks_read_neither(self):
        call_state, verdict, log = self._run(duplicate=False)
        self.assertEqual(verdict["verdict"], "neither")
        self.assertEqual(verdict["duplicate_model_chunk_hashes"], 0)

    def test_every_audio_part_is_traced_with_its_evidence_fields(self):
        call_state, verdict, log = self._run(duplicate=True)
        audio_entries = [e for e in call_state["delta_trace"] if e["kind"] == "audio"]
        self.assertEqual(len(audio_entries), 2)
        for position, entry in enumerate(audio_entries, start=1):
            self.assertEqual(set(entry),
                             {"seq", "kind", "t_mono", "bytes", "cumulative_bytes",
                              "hash", "queued"})
            self.assertTrue(entry["queued"])
            self.assertGreater(entry["bytes"], 0)
            self.assertEqual(len(entry["hash"]), 12)
        self.assertEqual(audio_entries[0]["hash"], audio_entries[1]["hash"])
        self.assertEqual(audio_entries[1]["cumulative_bytes"],
                         audio_entries[0]["bytes"] + audio_entries[1]["bytes"])
        self.assertLessEqual(audio_entries[0]["t_mono"], audio_entries[1]["t_mono"])
        self.assertTrue(_diagnostics(log, "[DELTA] audio"),
                        "per-part trace is emitted at DEBUG during normal operation")

    def test_the_trace_replaced_the_commented_out_per_chunk_line(self):
        from tests.harness.appctl import APP_PY

        with open(APP_PY, "r", encoding="utf-8") as handle:
            source = handle.read()
        self.assertNotIn('#logger.info("⚡ Gemini Audio Chunk Received!")', source)
        self.assertIn("record_audio_delta(", source)


class TestDownstreamReDeliveryIsFlagged(unittest.TestCase):
    """A local re-queue of already-sent audio reads as DOWNSTREAM.

    The re-queue is injected into ``plivo_output_queue`` directly — that is the
    buffer a downstream re-delivery bug would have to go through, and it is the
    one place where ``sent_bytes`` can exceed what the model supplied.
    """

    def _run(self, requeue: bool):
        async def scenario():
            clock = FakeClock()
            ws = FakePlivoWS(clock)
            session = FakeSession()
            client = FakePlivoClient()
            call_state = live_call_state(with_denoiser=False)
            rng = np.random.default_rng(5041)
            block = scenarios.random_ulaw(rng, FRAME_BYTES * 6)

            # Book the model side as if the block had arrived once, in the same
            # units app.py records: 24 kHz PCM16, six times the mu-law byte count.
            app.reset_delta_trace(call_state, "test setup")
            app.record_audio_delta(call_state, b"\x00" * (len(block) * 6), True)
            call_state["queued_bytes"] = len(block)

            with clock.install():
                async with SenderHarness(call_state, clock, ws, session, client) as sender:
                    await sender.enqueue(block)
                    await sender.wait_sent(6)
                    if requeue:
                        await sender.enqueue(block)      # the defect under test
                        await sender.wait_sent(12)
                    await sender.settle()
            return call_state, app.delta_trace_verdict(call_state), ws

        return asyncio.run(scenario())

    def test_local_requeue_reads_downstream(self):
        call_state, verdict, ws = self._run(requeue=True)
        self.assertEqual(verdict["verdict"], "downstream-re-delivery")
        self.assertEqual(verdict["duplicate_model_chunk_hashes"], 0,
                         "the model side is clean: this is ours, not theirs")
        self.assertEqual(verdict["duplicate_sent_chunk_hashes"], 6)
        self.assertGreater(verdict["sent_bytes"], verdict["model_ulaw_equivalent_bytes"])
        self.assertEqual(len(ws.play_audio_frames), 12)

    def test_without_the_requeue_the_same_scenario_reads_neither(self):
        call_state, verdict, ws = self._run(requeue=False)
        self.assertEqual(verdict["verdict"], "neither")
        self.assertEqual(verdict["sent_bytes"], verdict["model_ulaw_equivalent_bytes"])
        self.assertEqual(len(ws.play_audio_frames), 6)

    def test_the_counter_comparison_is_made_in_one_unit(self):
        """The design phrases this as ``sent_bytes > model_audio_bytes_received``,
        which is not directly comparable: the model's audio is 24 kHz PCM16 and the
        outbound stream is 8 kHz mu-law, a factor of exactly 6. Recorded here
        because a naive comparison would never fire and the verdict would be dead
        code."""
        call_state, verdict, ws = self._run(requeue=False)
        self.assertEqual(verdict["model_ulaw_equivalent_bytes"],
                         verdict["model_audio_bytes_received"] // 6)
        self.assertEqual(app.GEMINI_OUTPUT_RATE // app.PLIVO_SAMPLE_RATE * 2, 6)


class TestNeitherVerdictOnChainedShortTruncatedTurns(unittest.TestCase):
    """Three short truncated turns with matching leading clusters read NEITHER.

    This is the false-barge-in signature the design predicts: distinct audio in
    both directions, counters in agreement, and the repeat showing up as separate
    turns each with a short ``delivered_ms`` rather than as duplicated bytes.
    """

    def test_neither_with_distinct_audio_and_a_repeated_leading_cluster(self):
        async def scenario():
            clock = FakeClock()
            ws = FakePlivoWS(clock)
            client = FakePlivoClient()
            call_state = live_call_state(with_denoiser=False)
            verdicts = []
            for turn in range(3):
                # A fresh turn each time: same transcript prefix, different audio.
                session = FakeSession([
                    resp_output_transcription(TRANSCRIPT),
                    resp_audio(scenarios.model_audio_24k(140 + turn * 7)),
                ])
                with clock.install():
                    await run_gemini_output(session, ws, call_state, client)
                verdicts.append(app.delta_trace_verdict(call_state))
                app.reset_delta_trace(call_state, f"test turn {turn} boundary")
            return verdicts

        verdicts = asyncio.run(scenario())
        self.assertEqual([v["verdict"] for v in verdicts], ["neither"] * 3)
        for verdict in verdicts:
            self.assertEqual(verdict["duplicate_model_chunk_hashes"], 0)
            self.assertEqual(verdict["duplicate_sent_chunk_hashes"], 0)
            self.assertEqual(verdict["queued_bytes"], verdict["model_ulaw_equivalent_bytes"])


class TestTextDeltaClusterBoundaries(unittest.TestCase):
    """A transcript split mid-cluster is recorded as such.

    The reported word is the case that matters: ``দুঃখিত`` segments as
    ``['দুঃ','খি','ত']``, so a delta boundary between ``দু`` and ``ঃ`` falls
    *inside* the leading cluster.
    """

    def _run(self, deltas):
        async def scenario():
            clock = FakeClock()
            ws = FakePlivoWS(clock)
            client = FakePlivoClient()
            call_state = live_call_state(with_denoiser=False)
            session = FakeSession([resp_output_transcription(d) for d in deltas])
            with clock.install(), LogCapture(level=logging.DEBUG) as log:
                await run_gemini_output(session, ws, call_state, client)
            return call_state, log

        return asyncio.run(scenario())

    def test_a_split_inside_the_leading_cluster_is_flagged(self):
        # আমি দু | ঃখিত — the visarga arrives in the following delta
        call_state, log = self._run(["আমি দু", "ঃখিত"])
        entries = [e for e in call_state["delta_trace"] if e["kind"] == "text"]
        self.assertEqual(len(entries), 2)
        self.assertTrue(entries[0]["ends_mid_cluster"],
                        "the first delta stopped between দু and ঃ")
        self.assertTrue(entries[1]["continues_previous_cluster"])
        self.assertEqual(call_state["ai_text_buffer"], "আমি দুঃখিত")
        self.assertEqual(bengali.clusters(call_state["ai_text_buffer"])[3],
                         bengali.LEADING_CLUSTER)

    def test_a_split_on_a_cluster_boundary_is_not_flagged(self):
        call_state, log = self._run(["আমি ", "দুঃখিত"])
        entries = [e for e in call_state["delta_trace"] if e["kind"] == "text"]
        self.assertFalse(entries[0]["ends_mid_cluster"])
        self.assertFalse(entries[1]["continues_previous_cluster"])

    def test_a_trailing_virama_is_flagged_without_needing_the_next_delta(self):
        call_state, log = self._run(["শব্"])
        entries = [e for e in call_state["delta_trace"] if e["kind"] == "text"]
        self.assertTrue(entries[0]["ends_mid_cluster"])

    def test_english_deltas_are_not_flagged(self):
        call_state, log = self._run(["I am ", "sorry."])
        entries = [e for e in call_state["delta_trace"] if e["kind"] == "text"]
        self.assertEqual([e["ends_mid_cluster"] for e in entries], [False, False])

    def test_accumulation_is_unchanged(self):
        """Preservation 3.4 / 3.5: the trace observes, it does not interfere."""
        call_state, log = self._run(["আমি দু", "ঃখিত", ", আমি…"])
        self.assertEqual(call_state["ai_text_buffer"], TRANSCRIPT)


class TestTraceIsBoundedAndDumpedOnlyOnAnomaly(unittest.TestCase):
    def test_trace_is_bounded_and_reports_what_it_dropped(self):
        call_state = live_call_state(with_denoiser=False, with_aec=False)
        app.reset_delta_trace(call_state, "test")
        for index in range(app.DELTA_TRACE_MAX_ENTRIES + 25):
            app.record_audio_delta(call_state, index.to_bytes(4, "big"), True)
        self.assertEqual(len(call_state["delta_trace"]), app.DELTA_TRACE_MAX_ENTRIES)
        self.assertEqual(call_state["delta_trace_dropped"], 25)
        self.assertEqual(app.delta_trace_verdict(call_state)["entries_dropped"], 25)

    def test_dump_promotes_the_trace_to_info(self):
        call_state = live_call_state(with_denoiser=False, with_aec=False)
        app.reset_delta_trace(call_state, "test")
        app.record_audio_delta(call_state, b"abc", True)
        app.record_text_delta(call_state, "", TRANSCRIPT)
        with LogCapture() as log:        # INFO only, as production runs
            app.dump_delta_trace(call_state, "unit-test")
        dumped = _diagnostics(log, "[DELTA-TRACE]")
        self.assertEqual(len(dumped), 3, "one summary line plus one line per entry")
        self.assertIn("reason=unit-test", dumped[0])

    def test_nothing_is_dumped_at_info_during_normal_operation(self):
        async def scenario():
            clock = FakeClock()
            ws = FakePlivoWS(clock)
            client = FakePlivoClient()
            call_state = live_call_state(with_denoiser=False)
            session = FakeSession([
                resp_output_transcription(TRANSCRIPT),
                resp_audio(scenarios.model_audio_24k(200)),
                resp_turn_complete(),
            ])
            with clock.install(), LogCapture() as log:
                await run_gemini_output(session, ws, call_state, client)
            return log

        log = asyncio.run(scenario())
        self.assertEqual(_diagnostics(log, "[DELTA"), [],
                         "the trace must not reach INFO unless an anomaly fires")


# =============================================================================
# 5.3 / 5.4 — field completeness on the decision and anomaly lines
# =============================================================================

def _truncation_scenario(queued_ms, *, heard_ms=None, transcript=TRANSCRIPT):
    """Prime a mid-utterance playback state and barge in with real speech.

    ``queued_ms`` is how much audio has been handed to Plivo (bytes).
    ``heard_ms`` is how much the caller has actually heard, simulated by
    advancing the fake wall clock since ``ai_playback_start_time``.

    The two are separate on purpose, and that separation is the defect measured
    on call b51312c8: ``send_plivo_audio`` has no pacing, so it hands Plivo
    bytes far faster than realtime while Plivo buffers and plays out at
    realtime. Classification uses the wall clock (what the caller heard), and
    ``heard_ms`` defaults to ``queued_ms`` here so the common case behaves like
    ideal realtime playout. Passing a smaller ``heard_ms`` reproduces the real
    signature: a small heard duration against a large queued duration, whose
    difference (``buffer_lead_ms``) is how far the AEC far-end reference runs
    ahead of playout.
    """
    async def scenario():
        clock = FakeClock()
        ws = FakePlivoWS(clock)
        session = FakeSession()
        call_state = live_call_state(assistant_speaking=True)
        call_state["ai_text_buffer"] = transcript
        call_state["ai_playback_start_time"] = clock.now(app.ist_tz)
        call_state["current_utterance_bytes"] = int(round(queued_ms / 1000 * 8000))
        # Advance the wall clock to simulate playout before the barge-in. Without
        # this, heard_ms is 0 and every truncation classifies anomalous -- correct
        # for a zero-elapsed input, but not a realistic scenario.
        elapsed_ms = queued_ms if heard_ms is None else heard_ms
        clock.advance_ms(elapsed_ms)
        with clock.install(), LogCapture() as log:
            async with InboundDriver(call_state, ws, session) as driver:
                for frame in scenarios.caller_speech_frames(14):
                    await driver.feed_pcm8(frame)
        return call_state, ws, log

    return asyncio.run(scenario())


class TestTriggerProvenanceFieldCompleteness(unittest.TestCase):
    """Requirement 2.4 — every decision carries its evidence, per decision.

    Both the fired and the not-anomalous cases are checked, because the point of
    2.4 is that the record exists regardless of the outcome. Note that a
    *suppressed* verdict is unreachable today: nothing in app.py declines a
    barge-in, which is concern (a). The verdict field is therefore always
    ``fired``, and that is recorded rather than papered over.
    """

    REQUIRED = (
        "verdict=", "classification=", "avg_prob=", "active_threshold=",
        "onset_frames=", "rnnoise_speech_count=", "rnnoise_silence_frames=",
        "inbound_rms_db=", "delivered_ms=", "delivered_bytes=",
        "anomalous_below_ms=", "far_end_evidence=",
    )

    def test_short_trigger_provenance_and_anomaly(self):
        call_state, ws, log = _truncation_scenario(140)
        trigger = _diagnostics(log, "[TRIGGER]")
        self.assertEqual(len(trigger), 1, "one line per decision, not sampled")
        for field in self.REQUIRED:
            self.assertIn(field, trigger[0], f"missing {field}")
        self.assertIn("verdict=fired", trigger[0])
        self.assertIn("classification=anomalous", trigger[0])
        self.assertIn("delivered_ms=140", trigger[0])
        self.assertIn(f"anomalous_below_ms={app.ANOMALOUS_TRUNCATION_MS}", trigger[0])

        anomaly = _diagnostics(log, "[ANOMALY]")
        self.assertEqual(len(anomaly), 1)
        for field in ("delivered_ms=140", "delivered_bytes=1120", "leading_clusters=",
                      "ai_text=", "trigger=vad-while-speaking",
                      "far_end_evidence=", "model=", "gemini_reconnect_count="):
            self.assertIn(field, anomaly[0], f"missing {field}")
        self.assertIn(repr(bengali.LEADING_CLUSTER), anomaly[0],
                      "the leading grapheme cluster of ai_text_buffer is carried verbatim")
        self.assertIn(app.GEMINI_MODEL, anomaly[0])

    def test_long_trigger_is_recorded_without_an_anomaly(self):
        call_state, ws, log = _truncation_scenario(1238)
        trigger = _diagnostics(log, "[TRIGGER]")
        self.assertEqual(len(trigger), 1)
        for field in self.REQUIRED:
            self.assertIn(field, trigger[0], f"missing {field}")
        self.assertIn("classification=normal", trigger[0])
        self.assertEqual(_diagnostics(log, "[ANOMALY]"), [])
        self.assertEqual(_diagnostics(log, "[COMMIT-GATE]"), [],
                         "a legitimate barge-in must not arm the commit gate")

    def test_the_anomaly_dumps_the_delta_trace(self):
        call_state, ws, log = _truncation_scenario(140)
        dumped = _diagnostics(log, "[DELTA-TRACE]")
        self.assertTrue(dumped)
        self.assertIn("reason=anomalous-truncation", dumped[0])

    def test_existing_lines_keep_their_exact_format(self):
        call_state, ws, log = _truncation_scenario(140)
        existing = [line for line in log.existing_lines if "cancelled" not in line]
        self.assertEqual(existing[-4:], [
            "🎤 User speech detected",
            f"🎙️ [TIMING] AI Speech Interrupted at: {existing[-3][-12:]}",
            "🛑 Cleared Plivo playback buffer",
            "▶️ Sent activityStart to Gemini",
        ])
        self.assertTrue(any(line.startswith("🎙️ [TIMING] AI Speech Interrupted at: ")
                            for line in existing))

    def test_delivered_ms_is_quantised_to_twenty_milliseconds_in_practice(self):
        """Recorded finding: only whole 160-byte frames are ever sent, so a
        delivered duration of exactly 130 ms is unreachable on a real call. The
        log's 131 ms figure for the canonical trigger is a wall-clock delta
        between two [TIMING] lines, not a byte count."""
        self.assertEqual(app.PLIVO_ULAW_CHUNK_SIZE / 8.0, 20.0)
        self.assertNotEqual(130 % 20, 0)


# =============================================================================
# 5.2 — model identity
# =============================================================================

class TestModelIdentityLogging(unittest.TestCase):
    def test_log_call_stats_carries_the_model_and_the_reconnect_count(self):
        call_state = live_call_state(with_denoiser=False, with_aec=False,
                                     gemini_reconnect_count=3)
        with LogCapture() as log:
            app.log_call_stats(call_state)
        model_lines = _diagnostics(log, "[MODEL]")
        self.assertEqual(len(model_lines), 1)
        self.assertIn(app.GEMINI_MODEL, model_lines[0])
        self.assertIn("gemini_reconnect_count: 3", model_lines[0])

    def test_session_establishment_logs_the_model_on_connect_and_on_resumption(self):
        """Source-level, and the reason is recorded rather than glossed.

        ``handle_media_stream`` is a Quart websocket route that builds a
        ``genai.Client`` and drives the reconnect loop inline; the harness README
        records that there is no seam between that loop and the transport. The
        runtime half of this is therefore out of offline reach, so what is asserted
        is that the line sits inside the ``connect_live_with_timeout`` block and
        reports both the resumption flag and the attempt count.
        """
        from tests.harness.appctl import APP_PY

        with open(APP_PY, "r", encoding="utf-8") as handle:
            source = handle.read()
        block = source[source.index("async with connect_live_with_timeout("):
                       source.index("task_map = {")]
        self.assertIn("🧬 [MODEL] {GEMINI_MODEL}", block)
        self.assertIn("resumed={is_reconnect}", block)
        self.assertIn("gemini_reconnect_count={call_state['gemini_reconnect_count']}", block)


# =============================================================================
# 5.4a — far-end persistence, and its attribution-neutrality
# =============================================================================

class TestFarEndRecorder(unittest.TestCase):
    """The recorder writes the buffer that already existed at the tap site.

    ``send_plivo_audio`` is driven for real, so the bytes written are the bytes
    production code handed to ``aec.add_far_end``.
    """

    def _run(self, *, record: bool, chunks):
        async def scenario():
            clock = FakeClock()
            ws = FakePlivoWS(clock)
            session = FakeSession()
            client = FakePlivoClient()
            call_state = live_call_state(with_denoiser=False, with_aec=False)

            far_end_fed = []

            class _AecTap:
                def add_far_end(self, pcm16):
                    far_end_fed.append(bytes(pcm16))

                def process(self, pcm16):      # pragma: no cover - inbound unused
                    return pcm16

            call_state["aec"] = _AecTap()

            path = None
            if record:
                path = os.path.join(tempfile.mkdtemp(), "farend.wav")
                writer = wave.open(path, "wb")
                writer.setnchannels(1)
                writer.setsampwidth(2)
                writer.setframerate(app.PLIVO_SAMPLE_RATE)
                call_state["debug_farend_wav_writer"] = writer

            with clock.install(), LogCapture() as log:
                async with SenderHarness(call_state, clock, ws, session, client) as sender:
                    await sender.enqueue(*chunks)
                    await sender.wait_sent(sum(len(c) for c in chunks) // FRAME_BYTES)
                    await sender.settle()

            if record:
                call_state["debug_farend_wav_writer"].close()
            return call_state, ws, log, path, b"".join(far_end_fed)

        return asyncio.run(scenario())

    @staticmethod
    def _chunks():
        rng = np.random.default_rng(54_0100)
        return [scenarios.random_ulaw(rng, FRAME_BYTES * 5),
                scenarios.random_ulaw(rng, FRAME_BYTES * 3)]

    def test_written_audio_is_the_far_end_reference_at_eight_kilohertz(self):
        chunks = self._chunks()
        call_state, ws, log, path, far_end_fed = self._run(record=True, chunks=chunks)

        with wave.open(path, "rb") as handle:
            self.assertEqual(handle.getnchannels(), 1)
            self.assertEqual(handle.getsampwidth(), 2)
            self.assertEqual(handle.getframerate(), app.PLIVO_SAMPLE_RATE)
            self.assertEqual(handle.getframerate(), 8000)
            self.assertNotEqual(handle.getframerate(), app.GEMINI_INPUT_RATE)
            written = handle.readframes(handle.getnframes())

        # BASELINE UPDATE — audio-pipeline redesign. The recorder is now
        # DECOUPLED from add_far_end: the send path (emit_chunk) enqueues the
        # far frame for the playout-paced inbound feed and, separately, writes it
        # to the recorder. So the recorder persists exactly what was SENT to
        # Plivo -- ulaw_to_pcm(outbound) -- which is the send-side diagnostic we
        # want, independent of when the AEC consumes the reference.
        self.assertEqual(written, app.ulaw_to_pcm(ws.outbound_ulaw),
                         "the recorder must persist exactly the sent far-end audio")
        # add_far_end is no longer called on the send path (it moved to the
        # inbound loop), so the send-only harness feeds it nothing.
        self.assertEqual(far_end_fed, b"")
        self.assertEqual(call_state["farend_samples_written"], len(written) // 2)
        self.assertEqual(call_state["farend_chunks_written"], 8)

    def test_alignment_metadata_is_logged_so_a_future_analysis_need_not_rebuild_it(self):
        call_state, ws, log, path, far_end_fed = self._run(record=True, chunks=self._chunks())
        anchors = _diagnostics(log, "[FAR-END]")
        self.assertTrue(anchors, "at least one alignment anchor at INFO")
        for field in ("chunk=", "cumulative_samples=", "t_mono=",
                      f"rate={app.PLIVO_SAMPLE_RATE}", "bytes="):
            self.assertIn(field, anchors[0], f"missing {field}")
        # Anchored at the ~1 Hz cadence of the existing [AGC] / [RNNoise] lines,
        # so this does not multiply production log volume by 50.
        self.assertIn("chunk=1", anchors[0])

    def test_recorder_on_versus_off_is_byte_identical_on_every_observable(self):
        """ACCEPTANCE CRITERION for 5.4a: attribution-neutral."""
        chunks = self._chunks()
        on_state, on_ws, on_log, path, on_far = self._run(record=True, chunks=chunks)
        off_state, off_ws, off_log, _none, off_far = self._run(record=False, chunks=chunks)

        self.assertEqual(on_ws.outbound_ulaw, off_ws.outbound_ulaw)
        self.assertEqual([len(f["ulaw"]) for f in on_ws.play_audio_frames],
                         [len(f["ulaw"]) for f in off_ws.play_audio_frames])
        self.assertEqual(on_ws.events, off_ws.events)
        self.assertEqual(on_ws.inter_frame_gaps_ms, off_ws.inter_frame_gaps_ms)
        self.assertEqual(on_far, off_far, "the canceller receives the same reference")
        self.assertEqual(on_state["current_utterance_bytes"],
                         off_state["current_utterance_bytes"])
        self.assertEqual(on_log.existing_lines, off_log.existing_lines)
        # the only difference is the new diagnostic lines
        self.assertEqual(_diagnostics(off_log, "[FAR-END]"), [])
        self.assertTrue(_diagnostics(on_log, "[FAR-END]"))
        self.assertTrue(all(len(f["ulaw"]) == FRAME_BYTES for f in on_ws.play_audio_frames))

    def test_a_broken_writer_never_breaks_the_call(self):
        """Non-fatal on failure, matching the inbound writer's shape."""
        async def scenario():
            clock = FakeClock()
            ws = FakePlivoWS(clock)
            session = FakeSession()
            client = FakePlivoClient()
            call_state = live_call_state(with_denoiser=False)

            class _ExplodingWriter:
                def writeframes(self, _payload):
                    raise OSError("disk full")

            call_state["debug_farend_wav_writer"] = _ExplodingWriter()
            rng = np.random.default_rng(7)
            block = scenarios.random_ulaw(rng, FRAME_BYTES * 4)
            with clock.install():
                async with SenderHarness(call_state, clock, ws, session, client) as sender:
                    await sender.enqueue(block)
                    await sender.wait_sent(4)
                    await sender.settle()
            return ws, block

        ws, block = asyncio.run(scenario())
        self.assertEqual(ws.outbound_ulaw, block)


# =============================================================================
# TEST1 follow-up — aligned inbound-loop recordings (nearraw / farref / aecout)
# =============================================================================

class _ExplodingWriter:
    def writeframes(self, _payload):
        raise OSError("disk full")


class TestAlignedRecorders(unittest.TestCase):
    """The three aligned recordings are written frame-for-frame by the inbound loop.

    ``stream_plivo_to_gemini`` is driven for real, with far frames already in
    ``farend_ref_queue``, so the files hold exactly the buffers production code
    handed to the AEC: the decoded near-end, the popped far reference (silence
    once the queue runs dry) and the canceller's output.
    """

    NEAR_FRAMES = 18
    FAR_FRAMES = 5

    @staticmethod
    def _far_frames(count):
        # Amplitude-modulated tone: a real far-end shape, not digital silence.
        t = np.arange(count * FRAME_BYTES) / float(app.PLIVO_SAMPLE_RATE)
        tone = 0.3 * np.sin(2 * np.pi * 440 * t) * (0.5 + 0.5 * np.sin(2 * np.pi * 3 * t))
        pcm = (tone * 32767).astype("<i2").tobytes()
        step = FRAME_BYTES * 2
        return [pcm[i * step:(i + 1) * step] for i in range(count)]

    def _run(self, *, record: bool, writers=None):
        async def scenario():
            clock = FakeClock()
            ws = FakePlivoWS(clock)
            session = FakeSession()
            call_state = live_call_state()
            call_state["farend_ref_queue"].extend(self._far_frames(self.FAR_FRAMES))

            paths = {}
            if writers is not None:
                call_state["debug_aligned_wav_writers"] = writers
            elif record:
                directory = tempfile.mkdtemp()
                for kind in app.ALIGNED_RECORDING_KINDS:
                    paths[kind] = os.path.join(directory, f"{kind}.wav")
                    writer = wave.open(paths[kind], "wb")
                    writer.setnchannels(1)
                    writer.setsampwidth(2)
                    writer.setframerate(app.PLIVO_SAMPLE_RATE)
                    call_state["debug_aligned_wav_writers"][kind] = writer

            near = scenarios.caller_speech_frames(self.NEAR_FRAMES)
            with clock.install(), LogCapture() as log:
                async with InboundDriver(call_state, ws, session) as driver:
                    for frame in near:
                        await driver.feed_pcm8(frame)

            if record and writers is None:
                for writer in call_state["debug_aligned_wav_writers"].values():
                    writer.close()
            return call_state, ws, session, log, paths, near

        return asyncio.run(scenario())

    @staticmethod
    def _read(path):
        with wave.open(path, "rb") as handle:
            return (handle.getnchannels(), handle.getsampwidth(), handle.getframerate(),
                    handle.readframes(handle.getnframes()))

    def test_three_files_align_frame_for_frame_at_eight_kilohertz(self):
        call_state, ws, session, log, paths, near = self._run(record=True)
        files = {kind: self._read(path) for kind, path in paths.items()}
        self.assertEqual(set(files), {"nearraw", "farref", "aecout"})
        for kind, (channels, width, rate, _data) in files.items():
            self.assertEqual((channels, width, rate), (1, 2, 8000), kind)

        expected_near = b"".join(app.ulaw_to_pcm(app.pcm_to_ulaw(frame)) for frame in near)
        self.assertEqual(files["nearraw"][3], expected_near,
                         "nearraw must be the decoded Plivo audio, before the AEC")

        silent = self.NEAR_FRAMES - self.FAR_FRAMES
        expected_far = b"".join(self._far_frames(self.FAR_FRAMES)) + app._FAR_SILENCE_FRAME * silent
        self.assertEqual(files["farref"][3], expected_far,
                         "farref must be the playout-paced reference, silence once drained")

        # Same length for all three: frame N of each file is the same 20 ms.
        lengths = {kind: len(data) for kind, (_c, _w, _r, data) in files.items()}
        self.assertEqual(len(set(lengths.values())), 1, lengths)
        self.assertEqual(lengths["nearraw"], self.NEAR_FRAMES * FRAME_BYTES * 2)
        self.assertEqual(call_state["aligned_frames_written"], self.NEAR_FRAMES)
        self.assertEqual(call_state["aligned_near_samples_written"], self.NEAR_FRAMES * FRAME_BYTES)
        self.assertEqual(call_state["aligned_aec_samples_written"], self.NEAR_FRAMES * FRAME_BYTES)

        anchors = _diagnostics(log, "[ALIGNED-REC]")
        self.assertTrue(anchors, "at least one alignment anchor at INFO")
        self.assertIn("frame=1 ", anchors[0])
        # One far frame was popped for frame 1, so four were still queued.
        self.assertIn(f"ref_queue_depth={self.FAR_FRAMES - 1}", anchors[0])
        for field in ("t_mono=", "near_samples=", "aec_samples=", "far_silent_frames=",
                      "aec_guard_fallbacks="):
            self.assertIn(field, anchors[0], f"missing {field}")

    def test_recorders_on_versus_off_is_byte_identical_on_every_observable(self):
        on_state, on_ws, on_session, on_log, _paths, _near = self._run(record=True)
        off_state, off_ws, off_session, off_log, _none, _near = self._run(record=False)

        self.assertEqual(on_session.sent, off_session.sent, "Gemini receives the same stream")
        self.assertEqual(on_ws.events, off_ws.events)
        self.assertEqual(list(on_state["far_shadow"]), list(off_state["far_shadow"]))
        self.assertEqual(on_state["chunk_count"], off_state["chunk_count"])
        self.assertEqual(on_log.existing_lines, off_log.existing_lines)
        self.assertEqual(_diagnostics(off_log, "[ALIGNED-REC]"), [])
        self.assertEqual(off_state["aligned_frames_written"], 0)

    def test_a_broken_writer_never_breaks_the_call(self):
        writers = {kind: _ExplodingWriter() for kind in app.ALIGNED_RECORDING_KINDS}
        broken_state, _ws, broken_session, _log, _paths, _near = self._run(record=True, writers=writers)
        _state, _ws, off_session, _log, _none, _near = self._run(record=False)
        self.assertEqual(broken_session.sent, off_session.sent)
        self.assertEqual(broken_state["aligned_frames_written"], self.NEAR_FRAMES)


# =============================================================================
# TEST2 follow-up — AEC output guard: the canceller may only remove energy
# =============================================================================

class _ScalingAec:
    """Stand-in canceller: output = input * gain, so louder or quieter at will."""

    def __init__(self, gain):
        self.gain = gain

    def add_far_end(self, _pcm16):
        pass

    def process(self, pcm16):
        samples = np.frombuffer(pcm16, dtype="<i2").astype(np.float64) * self.gain
        return np.clip(samples, -32768, 32767).astype("<i2").tobytes()


class TestAecOutputGuard(unittest.TestCase):
    NEAR_FRAMES = 18

    @staticmethod
    def _frame(level):
        t = np.arange(FRAME_BYTES) / float(app.PLIVO_SAMPLE_RATE)
        return (level * np.sin(2 * np.pi * 300 * t) * 32767).astype("<i2").tobytes()

    def _run_loop(self, aec):
        async def scenario():
            clock = FakeClock()
            ws = FakePlivoWS(clock)
            session = FakeSession()
            call_state = live_call_state(aec=aec)
            with clock.install(), LogCapture() as log:
                async with InboundDriver(call_state, ws, session) as driver:
                    for frame in scenarios.caller_speech_frames(self.NEAR_FRAMES):
                        await driver.feed_pcm8(frame)
            return call_state, session, log

        return asyncio.run(scenario())

    def test_louder_output_is_replaced_by_the_raw_input(self):
        call_state = live_call_state(with_denoiser=False, with_aec=False)
        raw, louder = self._frame(0.01), self._frame(0.2)
        with LogCapture() as log:
            first = app.guard_aec_output(call_state, raw, louder)
            second = app.guard_aec_output(call_state, raw, louder)
        self.assertEqual(first, raw)
        self.assertEqual(second, raw)
        self.assertEqual(call_state["aec_guard_frames_checked"], 2)
        self.assertEqual(call_state["aec_guard_fallback_frames"], 2)
        # Silent in-call: the falsifier's pinned log sequences must not change.
        self.assertEqual(log.lines, [])

    def test_quieter_or_equal_output_is_kept(self):
        call_state = live_call_state(with_denoiser=False, with_aec=False)
        raw, quieter = self._frame(0.2), self._frame(0.05)
        self.assertEqual(app.guard_aec_output(call_state, raw, quieter), quieter)
        self.assertEqual(app.guard_aec_output(call_state, raw, raw), raw)
        self.assertEqual(call_state["aec_guard_frames_checked"], 2)
        self.assertEqual(call_state["aec_guard_fallback_frames"], 0)

    def test_empty_or_buffered_output_passes_unjudged(self):
        call_state = live_call_state(with_denoiser=False, with_aec=False)
        raw = self._frame(0.01)
        self.assertEqual(app.guard_aec_output(call_state, raw, b""), b"")
        short = self._frame(0.5)[:100]
        self.assertEqual(app.guard_aec_output(call_state, raw, short), short)
        self.assertEqual(call_state["aec_guard_frames_checked"], 0)

    def test_kill_switch_passes_the_aec_output_through(self):
        call_state = live_call_state(with_denoiser=False, with_aec=False)
        raw, louder = self._frame(0.01), self._frame(0.2)
        with mock.patch.object(app, "AEC_OUTPUT_GUARD_ENABLED", False):
            self.assertEqual(app.guard_aec_output(call_state, raw, louder), louder)
        self.assertEqual(call_state["aec_guard_frames_checked"], 0)

    def test_inbound_loop_sends_raw_audio_when_the_aec_adds_energy(self):
        """A canceller that amplifies must look, downstream, like no canceller."""
        injecting_state, injecting, _log = self._run_loop(_ScalingAec(4.0))
        _state, identity, _log = self._run_loop(_ScalingAec(1.0))
        self.assertTrue(any(kind == "audio" for kind, _ in identity.sent))
        self.assertEqual(injecting.sent, identity.sent)
        self.assertEqual(injecting_state["aec_guard_fallback_frames"], self.NEAR_FRAMES)

    def test_inbound_loop_keeps_an_aec_that_removes_energy(self):
        cancelling_state, cancelling, _log = self._run_loop(_ScalingAec(0.25))
        _state, identity, _log = self._run_loop(_ScalingAec(1.0))
        self.assertNotEqual(cancelling.sent, identity.sent)
        self.assertEqual(cancelling_state["aec_guard_fallback_frames"], 0)
        self.assertEqual(cancelling_state["aec_guard_frames_checked"], self.NEAR_FRAMES)

    def test_real_canceller_on_real_echo_keeps_every_frame_it_improves(self):
        """Real aec.py on a synthetic echo of real speech, frame by frame: the
        guard keeps the AEC output wherever it removed energy and passes the raw
        input only where it added some, so the guarded stream is never louder
        than either. (Measured while writing this: on this echo aec.py's output
        is louder than its input on roughly half the echo frames.)"""
        from tests.harness.appctl import AcousticEchoCanceller

        far = echo.load_pcm8k("recorded.wav")[: 8000 * 2 * 6]
        near = echo.synth_echo(far, delay_ms=60, attenuation_db=12, noise_db=-70,
                               length_samples=len(far) // 2)
        canceller = AcousticEchoCanceller(frame_size=FRAME_BYTES)
        call_state = live_call_state(with_denoiser=False, with_aec=False)
        step = FRAME_BYTES * 2
        guarded, unguarded, kept = bytearray(), bytearray(), 0
        for i in range(len(far) // step):
            canceller.add_far_end(far[i * step:(i + 1) * step])
            raw = near[i * step:(i + 1) * step]
            out = canceller.process(raw)
            passed = app.guard_aec_output(call_state, raw, out)
            if audioop.rms(out, 2) <= audioop.rms(raw, 2):
                self.assertEqual(passed, out, f"frame {i}: an improving AEC frame was dropped")
                kept += 1
            else:
                self.assertEqual(passed, raw, f"frame {i}: an energy-adding AEC frame got through")
            guarded += passed
            unguarded += out
        self.assertGreater(kept, 0, "the fixture must exercise real cancellation")
        self.assertLessEqual(echo.rms_db(bytes(guarded)), echo.rms_db(bytes(unguarded)))
        self.assertEqual(call_state["aec_guard_frames_checked"], len(far) // step)

    def test_call_stats_report_the_guard(self):
        call_state = live_call_state(with_denoiser=False, with_aec=False,
                                     aec_guard_frames_checked=200, aec_guard_fallback_frames=50)
        with LogCapture() as log:
            app.log_call_stats(call_state)
        self.assertEqual(_diagnostics(log, "[AEC-GUARD]"), [
            "🛡️ [AEC-GUARD] enabled=True fallback_frames=50 checked=200 share=25.0%"])


# =============================================================================
# 5.10 — the commit gate: inert until armed, fully logged once armed
# =============================================================================

class TestCommitGateIsInertUntilArmed(unittest.TestCase):
    def _run_turn(self, *, pre_armed_text=None):
        async def scenario():
            clock = FakeClock()
            ws = FakePlivoWS(clock)
            session = FakeSession()
            client = FakePlivoClient()
            call_state = live_call_state(with_denoiser=False)
            call_state["ai_text_buffer"] = TRANSCRIPT
            if pre_armed_text is not None:
                app.arm_commit_gate(call_state, pre_armed_text, 140.0)
            rng = np.random.default_rng(510_10)
            block = scenarios.random_ulaw(rng, FRAME_BYTES * 20)   # 400 ms
            with clock.install(), LogCapture() as log:
                async with SenderHarness(call_state, clock, ws, session, client) as sender:
                    await sender.enqueue(block)
                    await sender.wait_sent(20)
                    await sender.settle()
            return call_state, ws, log, block

        return asyncio.run(scenario())

    def test_never_armed_is_byte_identical_and_silent(self):
        never, ws_never, log_never, block = self._run_turn()
        self.assertEqual(ws_never.outbound_ulaw, block)
        self.assertEqual(len(ws_never.play_audio_frames), 20)
        self.assertEqual(_diagnostics(log_never, "[COMMIT-GATE]"), [],
                         "a healthy call must not mention the gate at all")
        self.assertEqual(len(never["commit_gate_hold"]), 0)
        self.assertFalse(never["commit_gate_armed"])

    def test_armed_with_a_matching_prefix_delivers_the_same_bytes_in_the_same_order(self):
        armed, ws_armed, log_armed, block = self._run_turn(pre_armed_text=TRANSCRIPT)
        # The gate reorders nothing and loses nothing: it delays the first
        # GRAPHEME_COMMIT_MS, then the stream is identical.
        self.assertEqual(ws_armed.outbound_ulaw, block)
        self.assertTrue(all(len(f["ulaw"]) == FRAME_BYTES
                            for f in ws_armed.play_audio_frames))
        self.assertEqual(len(ws_armed.play_audio_frames), 20)

        # Exactly ONE withhold episode per utterance. Without the ``committed``
        # latch the gate re-engages after each release and chops the turn into
        # 240 ms bursts -- found by this assertion, which the earlier version of
        # the test missed because the idle safety valve masked it.
        transitions = _diagnostics(log_armed, "[COMMIT-GATE]")
        self.assertEqual(len([l for l in transitions if "withholding" in l]), 1)
        self.assertEqual(
            len([l for l in transitions if "reason=commit-window-reached" in l]), 1)
        self.assertEqual([l for l in transitions if "reason=short-turn-complete" in l], [],
                         "the safety valve must not be needed for a turn past the window")
        self.assertTrue(armed["commit_gate_committed"])
        self.assertEqual(len(armed["commit_gate_hold"]), 0)

    def test_the_withheld_span_is_exactly_the_commit_window(self):
        armed, ws, log, block = self._run_turn(pre_armed_text=TRANSCRIPT)
        released = [l for l in _diagnostics(log, "[COMMIT-GATE]")
                    if "reason=commit-window-reached" in l]
        self.assertIn(f"withheld_bytes={app.GRAPHEME_COMMIT_BYTES}", released[0])
        self.assertIn(f"withheld_ms={app.GRAPHEME_COMMIT_MS}", released[0])

    def test_armed_with_an_unrelated_prefix_is_pass_through(self):
        armed, ws, log, block = self._run_turn(pre_armed_text="I am sorry, I cannot help")
        self.assertEqual(ws.outbound_ulaw, block)
        self.assertEqual(len(ws.play_audio_frames), 20)
        transitions = _diagnostics(log, "[COMMIT-GATE]")
        # The gate records that it was armed and chose not to engage — an armed
        # gate that does nothing must still be visible in the next call's log.
        self.assertTrue(any("decision=release-no-match" in l for l in transitions))
        self.assertFalse(any("withholding" in l for l in transitions))

    def test_no_pacing_was_introduced(self):
        """Measured, not assumed: there is no 20 ms outbound pacing in
        ``send_plivo_audio`` today (task 4.5 records inter-frame gaps of 0.0 ms),
        so the commit gate releases back to back rather than "resuming 20 ms
        pacing" as task 5.10's prose puts it. Introducing pacing would be a
        behavioural change to requirement 3.9 and is not authorised."""
        armed, ws, log, block = self._run_turn(pre_armed_text=TRANSCRIPT)
        self.assertEqual(set(ws.inter_frame_gaps_ms), {0.0})

        from tests.harness.appctl import APP_PY

        with open(APP_PY, "r", encoding="utf-8") as handle:
            source = handle.read()
        sender = source[source.index("async def send_plivo_audio"):
                        source.index("@app.route(\"/trigger-call\"")]
        self.assertNotIn("asyncio.sleep(0.02", sender)


class TestCommitGateWithholdsTheLeadingFragment(unittest.TestCase):
    """The behaviour requirement 2.2 actually asks for.

    Case C of the exploration test cannot observe this — its harness waits for
    the frames to arrive before injecting the trigger, and a withheld turn never
    produces them — so the assertion lives here instead.
    """

    def test_a_short_matching_turn_is_withheld_while_the_turn_stays_open(self):
        async def scenario():
            clock = FakeClock()
            ws = FakePlivoWS(clock)
            session = FakeSession()
            client = FakePlivoClient()
            call_state = live_call_state(with_denoiser=False, turn_complete=False,
                                         assistant_speaking=True)
            call_state["ai_text_buffer"] = TRANSCRIPT
            app.arm_commit_gate(call_state, TRANSCRIPT, 140.0)
            rng = np.random.default_rng(510_20)
            block = scenarios.random_ulaw(rng, FRAME_BYTES * 7)     # 140 ms < 240 ms
            with clock.install(), LogCapture() as log:
                async with SenderHarness(call_state, clock, ws, session, client) as sender:
                    await sender.enqueue(block)
                    await sender.settle()
            return call_state, ws, log

        call_state, ws, log = asyncio.run(scenario())
        self.assertEqual(ws.play_audio_frames, [],
                         "140 ms of a matching prefix must not reach the caller")
        self.assertEqual(len(call_state["commit_gate_hold"]), FRAME_BYTES * 7)
        self.assertEqual(call_state["current_utterance_bytes"], 0)
        self.assertTrue(call_state["assistant_speaking"],
                        "withheld audio is pending, not finished")
        held = _diagnostics(log, "[COMMIT-GATE]")
        self.assertTrue(any("withholding" in line for line in held))
        self.assertTrue(any("armed" in line for line in held))

    def test_a_short_matching_turn_is_released_once_the_model_completes_it(self):
        async def scenario():
            clock = FakeClock()
            ws = FakePlivoWS(clock)
            session = FakeSession()
            client = FakePlivoClient()
            call_state = live_call_state(with_denoiser=False, turn_complete=False)
            call_state["ai_text_buffer"] = TRANSCRIPT
            app.arm_commit_gate(call_state, TRANSCRIPT, 140.0)
            rng = np.random.default_rng(510_30)
            block = scenarios.random_ulaw(rng, FRAME_BYTES * 7)
            with clock.install(), LogCapture() as log:
                async with SenderHarness(call_state, clock, ws, session, client) as sender:
                    await sender.enqueue(block)
                    await sender.settle()
                    withheld = len(ws.play_audio_frames)
                    call_state["turn_complete"] = True     # the model finished
                    await asyncio.sleep(app.PLIVO_SEND_POLL_TIMEOUT
                                        * (app.COMMIT_GATE_IDLE_RELEASE_POLLS + 2))
            return withheld, ws, log, block

        withheld, ws, log, block = asyncio.run(scenario())
        self.assertEqual(withheld, 0)
        self.assertEqual(ws.outbound_ulaw, block, "no audio is lost by the hold")
        self.assertTrue(any("reason=short-turn-complete" in line
                            for line in _diagnostics(log, "[COMMIT-GATE]")))

    def test_a_trigger_inside_the_window_means_the_caller_hears_nothing(self):
        """Requirement 2.2's "at most once", as a behaviour rather than a count."""
        async def scenario():
            clock = FakeClock()
            ws = FakePlivoWS(clock)
            session = FakeSession()
            client = FakePlivoClient()
            call_state = live_call_state(with_denoiser=False, turn_complete=False)
            call_state["ai_text_buffer"] = TRANSCRIPT
            app.arm_commit_gate(call_state, TRANSCRIPT, 140.0)
            rng = np.random.default_rng(510_40)
            block = scenarios.random_ulaw(rng, FRAME_BYTES * 7)
            with clock.install(), LogCapture() as log:
                async with SenderHarness(call_state, clock, ws, session, client) as sender:
                    await sender.enqueue(block)
                    await sender.settle()
                    # A spurious trigger lands inside the commit window.
                    call_state["interrupting"] = True
                    await sender.enqueue(block)
                    await sender.settle()
            return ws, log, call_state

        ws, log, call_state = asyncio.run(scenario())
        self.assertEqual(ws.play_audio_frames, [])
        self.assertEqual(len(call_state["commit_gate_hold"]), 0)
        self.assertTrue(any("withheld audio discarded" in line
                            for line in _diagnostics(log, "[COMMIT-GATE]")))

    def test_the_gate_disarms_on_a_healthy_turn_past_the_commit_window(self):
        async def scenario():
            clock = FakeClock()
            ws = FakePlivoWS(clock)
            session = FakeSession()
            client = FakePlivoClient()
            call_state = live_call_state(with_denoiser=False)
            call_state["ai_text_buffer"] = TRANSCRIPT
            app.arm_commit_gate(call_state, TRANSCRIPT, 140.0)
            rng = np.random.default_rng(510_50)
            block = scenarios.random_ulaw(rng, FRAME_BYTES * 20)     # 400 ms
            with clock.install(), LogCapture() as log:
                async with SenderHarness(call_state, clock, ws, session, client) as sender:
                    await sender.enqueue(block)
                    await sender.wait_sent(20)
                    await sender.finish_utterance()
            return call_state, log

        call_state, log = asyncio.run(scenario())
        self.assertFalse(call_state["commit_gate_armed"])
        self.assertTrue(any("disarmed" in line for line in _diagnostics(log, "[COMMIT-GATE]")))

    def test_every_transition_is_logged(self):
        """Non-negotiable: 5.10 changes audible behaviour, so its influence on the
        next live call has to be separable from what the instrumentation revealed."""
        call_state = live_call_state(with_denoiser=False, with_aec=False)
        with LogCapture() as log:
            app.arm_commit_gate(call_state, TRANSCRIPT, 140.0)
            call_state["commit_gate_hold"].extend(b"\xff" * 320)
            app.commit_gate_discard(call_state, "unit-test")
            app.disarm_commit_gate(call_state, "unit-test")
        transitions = _diagnostics(log, "[COMMIT-GATE]")
        self.assertEqual(len(transitions), 3)
        self.assertIn("armed", transitions[0])
        self.assertIn("discarded", transitions[1])
        self.assertIn("disarmed", transitions[2])


class TestCommitGateArmingFromTheRealTruncationPath(unittest.TestCase):
    """Arming through the real truncation path.

    ``COMMIT_GATE_ENABLED`` now defaults to False, because arming changes what
    the caller hears: in a sustained loop it converts an audible stutter into a
    silent gap, and that trade-off has not been accepted. The diagnostics are
    unconditional; only ``arm_commit_gate`` is gated. Both behaviours are
    asserted here -- enabled arms, disabled does not -- so neither can regress
    silently.
    """

    def test_an_anomalous_truncation_arms_it_and_a_legitimate_one_does_not(self):
        with mock.patch.object(app, "COMMIT_GATE_ENABLED", True):
            short_state, _ws, short_log = _truncation_scenario(140)
            long_state, _ws2, long_log = _truncation_scenario(1238)

        self.assertTrue(short_state["commit_gate_armed"])
        self.assertEqual(short_state["commit_gate_armed_text"], TRANSCRIPT)
        self.assertEqual(short_state["commit_gate_armed_clusters"],
                         bengali.clusters(TRANSCRIPT)[:4])
        self.assertIn(bengali.LEADING_CLUSTER, short_state["commit_gate_armed_clusters"])
        self.assertTrue(any("armed" in line for line in _diagnostics(short_log, "[COMMIT-GATE]")))

        self.assertFalse(long_state["commit_gate_armed"])
        self.assertEqual(_diagnostics(long_log, "[COMMIT-GATE]"), [])

    def test_disabled_is_the_default_and_does_not_arm(self):
        """The shipped default: diagnose, do not alter what the caller hears."""
        self.assertFalse(app.COMMIT_GATE_ENABLED,
                         "the shipped default must be diagnostics-only")
        short_state, _ws, short_log = _truncation_scenario(140)

        self.assertFalse(short_state["commit_gate_armed"])
        transitions = _diagnostics(short_log, "[COMMIT-GATE]")
        self.assertTrue(any("not armed" in line and "COMMIT_GATE_ENABLED=0" in line
                            for line in transitions),
                        f"the disabled decision must still be recorded: {transitions}")
        # The anomaly itself is still fully diagnosed -- that is the whole point.
        self.assertTrue(_diagnostics(short_log, "[ANOMALY]"))

    def test_the_wallclock_defect_signature_is_visible(self):
        """Small heard_ms against large queued_ms -- the b51312c8 signature.

        On that call the byte-based measure reported 580-1020 ms and classified
        every truncation ``normal`` while the caller had heard only 103-152 ms.
        Under the wall clock the same input classifies ``anomalous``, and
        ``buffer_lead_ms`` quantifies how far the far-end reference ran ahead of
        playout -- compared here against the canceller's modeled 480 ms path.
        """
        _state, _ws, log = _truncation_scenario(720, heard_ms=111)
        line = _diagnostics(log, "[TRIGGER]")[0]
        self.assertIn("classification=anomalous", line)
        self.assertIn("heard_ms=111", line)
        self.assertIn("queued_ms=720", line)
        self.assertIn("buffer_lead_ms=609", line)
        self.assertIn("clock=wallclock", line)
        # 609 ms of reference lead against a 480 ms modeled path: the reference
        # is outside the filter's window entirely.
        self.assertGreater(609, 480)


class TestGraphemeHelpers(unittest.TestCase):
    """The minimum from ``bargein.py``'s spec surface that 5.1 and 5.10 needed.

    They were written in app.py before ``bargein.py`` existed and are still
    there; the redesign did not migrate them, and the operator decided on
    2026-09-23 that they stay (the amended constraint in docs/spec/tasks.md).
    They are tested here, against ``app``, for that reason.
    """

    def test_the_four_verified_bengali_segmentations(self):
        for word, expected in bengali.VERIFIED_SEGMENTATIONS.items():
            self.assertEqual(app.grapheme_clusters(word), expected, word)

    def test_agrees_with_the_test_side_segmenter(self):
        for text in list(bengali.NON_BENGALI_SAMPLES.values()) + [TRANSCRIPT]:
            self.assertEqual(app.grapheme_clusters(text), bengali.clusters(text), text)

    def test_empty_and_lone_combining_mark(self):
        self.assertEqual(app.grapheme_clusters(""), [])
        self.assertEqual(app.leading_clusters(""), [])
        self.assertEqual(app.grapheme_clusters(bengali.VISARGA), [bengali.VISARGA])
        self.assertFalse(app.ends_mid_grapheme_cluster(""))

    def test_shared_leading_cluster_count_on_the_real_transcripts(self):
        # both are real interrupted transcripts from Gemini_Assistant.log
        self.assertGreaterEqual(
            app.shared_leading_cluster_count(bengali.APOLOGY_TURN_1, bengali.APOLOGY_TURN_2), 5)
        clusters = app.grapheme_clusters(bengali.APOLOGY_TURN_1)
        shared = app.shared_leading_cluster_count(bengali.APOLOGY_TURN_1, bengali.APOLOGY_TURN_2)
        self.assertIn(bengali.LEADING_CLUSTER, clusters[:shared])

    def test_no_shared_leading_cluster_beyond_the_common_word(self):
        other = "আমি কি আপনাকে সাহায্য করতে পারি"
        self.assertEqual(app.shared_leading_cluster_count(bengali.APOLOGY_TURN_1, other), 3)

    def test_empty_operands_share_nothing(self):
        self.assertEqual(app.shared_leading_cluster_count("", TRANSCRIPT), 0)
        self.assertEqual(app.shared_leading_cluster_count(TRANSCRIPT, ""), 0)

    def test_cluster_table_is_never_split_or_merged(self):
        offenders = [case.label for case in bengali.cluster_table()
                     if len(app.grapheme_clusters(case.text)) != 1]
        self.assertEqual(offenders, [])

    def test_boundary_split_detection(self):
        self.assertTrue(app.boundary_splits_grapheme_cluster("আমি দু", "ঃখিত"))
        self.assertFalse(app.boundary_splits_grapheme_cluster("আমি ", "দুঃখিত"))
        self.assertFalse(app.boundary_splits_grapheme_cluster("", "দুঃখিত"))
        self.assertFalse(app.boundary_splits_grapheme_cluster("দুঃখিত", ""))

    def test_bargein_module_now_exists_after_redesign(self):
        from tests.harness.appctl import REPO_ROOT

        # bargein.py now exists (redesign, task #3). The grapheme helpers tested
        # in this class still live in app.py for now; the echo-corroboration
        # surface lives in bargein.py. Assert the module is present rather than
        # absent -- the old "still does not exist" guard is obsolete.
        self.assertTrue(os.path.exists(os.path.join(REPO_ROOT, "bargein.py")),
                        "bargein.py should exist after the audio-pipeline redesign")


if __name__ == "__main__":
    unittest.main()
