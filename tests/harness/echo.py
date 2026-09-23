"""Synthetic echo path: turn a far-end fixture into a near-end echo signal.

Delay 20-400 ms, attenuation 6-30 dB, additive noise, over the
``playback_audio_files/*.wav`` fixtures already in the repo.

Honest limitation, restated from design.md: this is a model of an echo path, not
a room. It is a pure delay-and-attenuate with optional white noise -- no room
impulse response, no handset nonlinearity, no codec. It is sufficient to build a
near-end signal that is a *known* function of the far-end (which is what the
bug-condition tests need to construct), and insufficient to say anything about
real ``echo_correlation`` distributions.
"""

from __future__ import annotations

import audioop
import os

import numpy as np
import soundfile as sf

from tests.harness.appctl import REPO_ROOT, app

PLAYBACK_DIR = os.path.join(REPO_ROOT, "playback_audio_files")

FRAME_BYTES_PCM8 = 320   # 160 samples @ 8 kHz PCM16 == 20 ms == one Plivo frame
FRAME_BYTES_ULAW = app.PLIVO_ULAW_CHUNK_SIZE


def fixture_path(name: str) -> str:
    return os.path.join(PLAYBACK_DIR, name)


def available_fixtures() -> list[str]:
    return sorted(
        name for name in os.listdir(PLAYBACK_DIR) if name.endswith(".wav")
    )


def load_pcm8k(name: str) -> bytes:
    """Read a playback fixture and resample to 8 kHz mono PCM16.

    ``audioop.ratecv`` is used rather than scipy so the resampling matches what
    app.py itself does on the outbound path (app.py 1706-1708).
    """
    data, rate = sf.read(fixture_path(name), dtype="int16", always_2d=True)
    mono = data[:, 0].tobytes()
    if rate == 8000:
        return mono
    converted, _ = audioop.ratecv(mono, 2, 1, rate, 8000, None)
    return converted


def load_pcm24k(name: str) -> bytes:
    """Read a playback fixture at 24 kHz -- the model's output rate, so a fixture
    can stand in for ``inline_data`` audio."""
    data, rate = sf.read(fixture_path(name), dtype="int16", always_2d=True)
    mono = data[:, 0].tobytes()
    if rate == 24000:
        return mono
    converted, _ = audioop.ratecv(mono, 2, 1, rate, 24000, None)
    return converted


def synth_echo(
    far_pcm8: bytes,
    *,
    delay_ms: float,
    attenuation_db: float,
    noise_db: float | None = None,
    rng: np.random.Generator | None = None,
    length_samples: int | None = None,
) -> bytes:
    """Return a near-end 8 kHz PCM16 signal that is a delayed, attenuated copy
    of ``far_pcm8``.

    ``delay_ms``        20-400 ms (the design's search window).
    ``attenuation_db``  6-30 dB, positive = quieter than the far-end.
    ``noise_db``        additive white noise level in dBFS, or ``None``.
    """
    far = np.frombuffer(far_pcm8, dtype=np.int16).astype(np.float64) / 32768.0
    delay_samples = int(round(delay_ms / 1000.0 * 8000))
    total = length_samples if length_samples is not None else far.size + delay_samples
    near = np.zeros(total, dtype=np.float64)

    gain = 10.0 ** (-abs(attenuation_db) / 20.0)
    copy_len = min(far.size, total - delay_samples)
    if copy_len > 0:
        near[delay_samples:delay_samples + copy_len] = far[:copy_len] * gain

    if noise_db is not None:
        generator = rng if rng is not None else np.random.default_rng(0)
        near += generator.normal(0.0, 10.0 ** (noise_db / 20.0), size=total)

    np.clip(near, -1.0, 1.0, out=near)
    return (near * 32767.0).astype(np.int16).tobytes()


def frames_pcm8(pcm8: bytes, frame_bytes: int = FRAME_BYTES_PCM8) -> list[bytes]:
    """Split into whole 20 ms frames; a short tail is dropped, matching the way
    ``load_disclaimer`` drops a short trailing chunk (app.py 246-250)."""
    return [
        pcm8[offset:offset + frame_bytes]
        for offset in range(0, len(pcm8) - frame_bytes + 1, frame_bytes)
    ]


def silence_pcm8(frames: int = 1) -> bytes:
    return b"\x00" * (FRAME_BYTES_PCM8 * frames)


def rms_db(pcm16: bytes) -> float:
    """Same arithmetic as ``app.calculate_rms_db``, returned as a scalar."""
    value, _ = app.calculate_rms_db(pcm16)
    return float(value)


def envelope(pcm16: bytes, bin_ms: float = 10.0, rate: int = 8000) -> np.ndarray:
    """Short-term energy envelope, for describing test inputs.

    Note this is a *test-side* helper for characterising the fixtures we build.
    It is not ``bargein.envelope`` -- ``bargein.py`` belongs to parked task 5.5
    and is not created by this pass.
    """
    samples = np.frombuffer(pcm16, dtype=np.int16).astype(np.float64) / 32768.0
    bin_samples = max(1, int(round(bin_ms / 1000.0 * rate)))
    usable = samples.size - (samples.size % bin_samples)
    if usable == 0:
        return np.zeros(0)
    binned = samples[:usable].reshape(-1, bin_samples)
    return np.sqrt(np.mean(binned ** 2, axis=1))


def normalised_envelope_correlation(
    near_pcm8: bytes, far_pcm8: bytes, lag_bins: int = 0, bin_ms: float = 10.0
) -> float:
    """Pearson correlation of the two energy envelopes at a given lag.

    Used only to *characterise the synthetic inputs this harness builds* -- to
    show that a case labelled "echo-correlated" really is. It is explicitly NOT
    a stand-in for the parked ``bargein.echo_correlation``, and no production
    code path computes anything like it today.
    """
    near_env = envelope(near_pcm8, bin_ms)
    far_env = envelope(far_pcm8, bin_ms)
    if lag_bins > 0:
        near_env = near_env[lag_bins:]
    elif lag_bins < 0:
        far_env = far_env[-lag_bins:]
    span = min(near_env.size, far_env.size)
    if span < 2:
        return 0.0
    a = near_env[:span] - near_env[:span].mean()
    b = far_env[:span] - far_env[:span].mean()
    denominator = float(np.linalg.norm(a) * np.linalg.norm(b))
    if denominator == 0.0:
        return 0.0
    return float(np.dot(a, b) / denominator)
