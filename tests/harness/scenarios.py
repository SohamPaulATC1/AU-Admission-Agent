"""Scenario builders shared by the preservation and exploration tests."""

from __future__ import annotations

import audioop

import numpy as np

from tests.harness import echo
from tests.harness.appctl import app

SPEECH_FIXTURE = "recorded.wav"


def caller_speech_frames(count: int | None = None, fixture: str = SPEECH_FIXTURE) -> list[bytes]:
    """20 ms 8 kHz PCM16 frames of real speech, for driving the inbound VAD path.

    ``recorded.wav`` is the disclaimer recording already in the repo; as a clean
    speech signal it is what an uncorrelated near-end (a genuinely speaking
    caller) looks like to RNNoise.
    """
    frames = echo.frames_pcm8(echo.load_pcm8k(fixture))
    return frames if count is None else frames[:count]


def silence_frames(count: int) -> list[bytes]:
    return [echo.silence_pcm8(1) for _ in range(count)]


def model_audio_24k(duration_ms: float, fixture: str = SPEECH_FIXTURE) -> bytes:
    """A slice of 24 kHz PCM16, standing in for one ``inline_data`` chunk."""
    pcm24 = echo.load_pcm24k(fixture)
    samples = int(round(duration_ms / 1000.0 * 24000))
    return pcm24[: samples * 2]


def model_audio_to_ulaw(pcm24: bytes) -> bytes:
    """Mirror of app.py 1706-1710: ratecv 24k->8k then ``pcm_to_ulaw``.

    Used to predict the outbound byte stream for a scripted turn. It carries no
    ``ratecv`` state, so it is only exact for a single-chunk turn -- which is how
    the tests use it.
    """
    pcm8, _ = audioop.ratecv(pcm24, 2, 1, app.GEMINI_OUTPUT_RATE, app.PLIVO_SAMPLE_RATE, None)
    return app.pcm_to_ulaw(pcm8)


def random_ulaw(rng: np.random.Generator, length: int) -> bytes:
    """Random ??-law bytes.

    Randomised content matters for the chunk-hash uniqueness half of the
    byte-conservation invariant: real audio contains runs of identical silence
    frames, so identical frame *content* occurs legitimately and a naive
    "no hash twice" assertion over real audio would be false for reasons that
    have nothing to do with re-delivery.
    """
    return bytes(rng.integers(0, 256, length, dtype=np.uint8))


def ulaw_ms(byte_count: int) -> float:
    """??-law 8 kHz: 8000 bytes per second."""
    return byte_count / 8000.0 * 1000.0
