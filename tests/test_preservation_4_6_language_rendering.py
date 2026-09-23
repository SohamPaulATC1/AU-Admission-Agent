"""Task 4.6 -- non-Bengali and cluster-free Bengali rendering, byte-exact.

OBSERVATION-FIRST. Requirements 3.2, 3.3.

Golden record observed on unfixed code, and it is worth stating plainly because
it bounds what this sub-item can prove: **the outbound byte stream is not a
function of the transcript at all.** Audio arrives as ``inline_data`` and is
resampled and encoded (app.py 1706-1710) with no reference to
``output_transcription``. So English, Hindi, cluster-free Bengali and
visarga-bearing Bengali with the *same* audio script produce byte-identical
outbound streams.

That is the real preservation record for 3.2 and 3.3: rendering is
text-independent downstream of the model, so a fix that touches only the
barge-in decision and the sender cannot change rendering for one language and
not another. What each language case pins individually is the transcript log
line and the ``conversation_log`` entry.
"""

from __future__ import annotations

import asyncio
import hashlib
import unittest

from tests.harness import bengali, scenarios
from tests.harness.appctl import LogCapture, live_call_state
from tests.harness.fakes import (
    FakeClock,
    FakePlivoClient,
    FakePlivoWS,
    FakeSession,
    resp_audio,
    resp_output_transcription,
    resp_turn_complete,
    run_gemini_output,
)

VISARGA = bengali.VISARGA
MATRAS_WITH_GLYPH = [matra for name, matra in bengali.MATRAS.items() if matra]


def _run_turn(transcript: str, duration_ms: float = 400):
    async def scenario():
        clock = FakeClock()
        ws = FakePlivoWS(clock)
        client = FakePlivoClient()
        call_state = live_call_state(with_denoiser=False)
        session = FakeSession([
            resp_output_transcription(transcript),
            resp_audio(scenarios.model_audio_24k(duration_ms)),
            resp_turn_complete(),
        ])
        with clock.install(), LogCapture() as log:
            await run_gemini_output(session, ws, call_state, client)
            queued = []
            while not call_state["plivo_output_queue"].empty():
                queued.append(call_state["plivo_output_queue"].get_nowait())
        return call_state, log, b"".join(queued)

    return asyncio.run(scenario())


class TestRenderingIsByteIdenticalAcrossScripts(unittest.TestCase):
    def test_same_audio_different_transcript_gives_identical_bytes(self):
        digests = {}
        for label, transcript in bengali.NON_BENGALI_SAMPLES.items():
            _call_state, _log, ulaw = _run_turn(transcript)
            digests[label] = hashlib.sha256(ulaw).hexdigest()
        # plus the visarga-bearing Bengali that the bug report is about
        _call_state, _log, ulaw = _run_turn(bengali.APOLOGY_TURN_1)
        digests["bengali_with_visarga_cluster"] = hashlib.sha256(ulaw).hexdigest()

        self.assertEqual(len(set(digests.values())), 1, digests)

    def test_bytes_equal_the_resampled_model_audio(self):
        expected = scenarios.model_audio_to_ulaw(scenarios.model_audio_24k(400))
        for transcript in bengali.NON_BENGALI_SAMPLES.values():
            _call_state, _log, ulaw = _run_turn(transcript)
            self.assertEqual(ulaw, expected)


class TestTranscriptLoggingPerScript(unittest.TestCase):
    def test_english(self):
        call_state, log, _ulaw = _run_turn(bengali.NON_BENGALI_SAMPLES["english"])
        self.assertEqual(log.matching("[GEMINI]:"),
                         ["\U0001F916 [GEMINI]: I am sorry, I cannot help with that."])
        self.assertEqual(call_state["conversation_log"],
                         [{"role": "agent", "text": "I am sorry, I cannot help with that."}])

    def test_hindi(self):
        transcript = bengali.NON_BENGALI_SAMPLES["hindi"]
        call_state, log, _ulaw = _run_turn(transcript)
        self.assertEqual(log.matching("[GEMINI]:"), [f"\U0001F916 [GEMINI]: {transcript}"])

    def test_bengali_without_a_visarga_cluster(self):
        transcript = bengali.NON_BENGALI_SAMPLES["bengali_no_visarga_cluster"]
        clusters = bengali.clusters(transcript)
        self.assertNotIn(VISARGA, transcript)
        self.assertTrue(all(VISARGA not in cluster for cluster in clusters))
        call_state, log, _ulaw = _run_turn(transcript)
        self.assertEqual(log.matching("[GEMINI]:"), [f"\U0001F916 [GEMINI]: {transcript}"])

    def test_mixed_script(self):
        transcript = bengali.NON_BENGALI_SAMPLES["mixed"]
        call_state, log, _ulaw = _run_turn(transcript)
        self.assertEqual(log.matching("[GEMINI]:"), [f"\U0001F916 [GEMINI]: {transcript}"])

    def test_empty_transcript_logs_nothing_and_still_renders_audio(self):
        call_state, log, ulaw = _run_turn("")
        self.assertEqual(log.matching("[GEMINI]:"), [])
        self.assertEqual(call_state["conversation_log"], [])
        self.assertGreater(len(ulaw), 0)

    def test_lone_combining_mark_transcript(self):
        call_state, log, _ulaw = _run_turn(VISARGA)
        self.assertEqual(log.matching("[GEMINI]:"), [f"\U0001F916 [GEMINI]: {VISARGA}"])


class TestClusterFreeBengaliIsDistinguishableFromClusterBearing(unittest.TestCase):
    """Cheap segmentation guard so "contains no consonant + matra + visarga
    cluster" in 3.2 is an assertable predicate rather than a description."""

    @staticmethod
    def has_cv_visarga_cluster(text: str) -> bool:
        return any(
            VISARGA in cluster and any(matra in cluster for matra in MATRAS_WITH_GLYPH)
            for cluster in bengali.clusters(text)
        )

    def test_predicate_on_the_reported_word_and_on_the_clean_one(self):
        self.assertTrue(self.has_cv_visarga_cluster(bengali.APOLOGY_TURN_1))
        self.assertFalse(self.has_cv_visarga_cluster(
            bengali.NON_BENGALI_SAMPLES["bengali_no_visarga_cluster"]))
        self.assertFalse(self.has_cv_visarga_cluster(bengali.NON_BENGALI_SAMPLES["english"]))
        # পুনঃ is visarga on a bare consonant -- no matra -- so the predicate is False
        self.assertFalse(self.has_cv_visarga_cluster("\u09AA\u09C1\u09A8\u0983"))


if __name__ == "__main__":
    unittest.main()
