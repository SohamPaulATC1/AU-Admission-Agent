"""Pure barge-in / echo-corroboration logic for the audio pipeline redesign.

Every function here is pure: no I/O, no logging, no timers, no mutable module
state. State is passed in and results are returned. That is deliberate -- it is
what lets the barge-in decision be unit-tested against the on-disk call
recordings with no phone call and no live model (see
`docs/spec/redesign-audio-pipeline.md`).

Why the specific choices here, all from measured data on the two reproduction
calls (2026-09-22 16:32 and 17:05), not guesses:

- ENVELOPE domain, not waveform. Waveform cross-correlation between the residual
  echo (post-AEC/RNNoise) and the far-end flipped sign per trigger
  (-0.47 .. +0.58) because the DSP chain phase-distorts the residual. The
  short-term ENERGY envelope correlated 0.976-0.993 on the same triggers. So we
  correlate energy envelopes.

- LAG SEARCH CENTRED ON 0. The leak reaches the inbound stream near lag 0, not
  at an acoustic round-trip delay (>150 ms on a mobile leg). Measured best-lag on
  the reproduction call was 50-60 ms, well short of an acoustic path and mostly
  attributable to the residual DSP/buffering offset. Searching an acoustic-delay
  window would look in the wrong place; we search +/- 80 ms around 0, which
  comfortably captures the measured 50-60 ms plus alignment jitter without being
  wide enough to overfit a 180 ms window.

- AMPLITUDE (correlation strength), not presence. The assistant's audio is
  present at the onset of every turn, including turns nobody interrupted. Only
  its correlation strength distinguishes an echo trigger (>=0.88) from genuine
  double-talk (well below). So the decision keys on a threshold, never on
  "is there any far-end".
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

_INT16_SCALE = 32768.0


def pcm16_to_float(pcm16: bytes) -> np.ndarray:
    """Decode little-endian PCM16 bytes to float in [-1, 1]."""
    if not pcm16:
        return np.zeros(0, dtype=np.float64)
    return np.frombuffer(pcm16, dtype=np.int16).astype(np.float64) / _INT16_SCALE


def envelope(samples: np.ndarray, *, rate: int, bin_ms: float = 10.0) -> np.ndarray:
    """Short-term RMS energy envelope in `bin_ms` bins.

    A short tail that does not fill a whole bin is dropped. Returns an empty
    array if there is not even one full bin, which the callers treat as
    "no usable signal".
    """
    if samples.size == 0:
        return np.zeros(0, dtype=np.float64)
    bin_samples = max(1, int(round(bin_ms / 1000.0 * rate)))
    usable = samples.size - (samples.size % bin_samples)
    if usable < bin_samples:
        return np.zeros(0, dtype=np.float64)
    binned = samples[:usable].reshape(-1, bin_samples)
    return np.sqrt(np.mean(binned ** 2, axis=1) + 1e-12)


def rms_dbfs(samples: np.ndarray) -> float:
    """Full-scale RMS in dB. -inf-safe; digital silence returns a large negative."""
    if samples.size == 0:
        return -120.0
    rms = float(np.sqrt(np.mean(samples ** 2) + 1e-12))
    return 20.0 * float(np.log10(rms + 1e-12))


def far_end_active(far_samples: np.ndarray, *, floor_dbfs: float) -> bool:
    """True if the far-end window carries real signal (assistant is playing).

    This is the short-circuit that keeps genuine barge-in untouched: when the
    assistant is silent there is nothing to correlate against and the caller's
    speech must always win, so callers skip corroboration entirely when this is
    False.
    """
    return rms_dbfs(far_samples) > floor_dbfs


def _pearson(a: np.ndarray, b: np.ndarray) -> float:
    """Zero-mean normalised correlation of two equal-length vectors."""
    span = min(a.size, b.size)
    if span < 2:
        return 0.0
    a = a[:span] - a[:span].mean()
    b = b[:span] - b[:span].mean()
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    if denom == 0.0:
        return 0.0
    return float(np.dot(a, b) / denom)


@dataclass(frozen=True)
class CorrelationResult:
    """Outcome of the envelope cross-correlation lag search."""
    correlation: float      # peak Pearson correlation over the searched lags
    lag_bins: int           # lag (in envelope bins) at the peak; + => near lags far
    far_active: bool        # was the far-end window above the activity floor
    near_dbfs: float
    far_dbfs: float


def echo_correlation(
    near_pcm16: bytes,
    far_pcm16: bytes,
    *,
    near_rate: int,
    far_rate: int,
    bin_ms: float = 10.0,
    lag_search_ms: float = 80.0,
    floor_dbfs: float = -60.0,
) -> CorrelationResult:
    """Peak envelope cross-correlation of near-end against far-end near lag 0.

    near_pcm16 / far_pcm16 may be at different sample rates (near-end is the
    16 kHz post-processing signal the VAD judged; far-end shadow is 8 kHz), which
    is fine: both are reduced to fixed-`bin_ms` energy envelopes before
    correlating, so the rate difference is absorbed by the binning.

    The lag search runs +/- `lag_search_ms` around 0 (converted to whole bins),
    and returns the peak. Centring on 0 is the measured fact from the two calls;
    the small window only absorbs frame-alignment jitter.
    """
    near = pcm16_to_float(near_pcm16)
    far = pcm16_to_float(far_pcm16)
    near_env = envelope(near, rate=near_rate, bin_ms=bin_ms)
    far_env = envelope(far, rate=far_rate, bin_ms=bin_ms)

    result_far_active = far_end_active(far, floor_dbfs=floor_dbfs)
    near_db = rms_dbfs(near)
    far_db = rms_dbfs(far)

    if near_env.size < 2 or far_env.size < 2:
        return CorrelationResult(0.0, 0, result_far_active, near_db, far_db)

    max_lag = max(0, int(round(lag_search_ms / bin_ms)))
    best_corr = 0.0
    best_lag = 0
    for lag in range(-max_lag, max_lag + 1):
        if lag >= 0:
            a = near_env[lag:]
            b = far_env[: a.size]
        else:
            b = far_env[-lag:]
            a = near_env[: b.size]
        corr = _pearson(a, b)
        if corr > best_corr:
            best_corr = corr
            best_lag = lag
    return CorrelationResult(best_corr, best_lag, result_far_active, near_db, far_db)


@dataclass(frozen=True)
class BargeInDecision:
    """Result of `should_barge_in`, carrying its own evidence for logging."""
    barge_in: bool          # True => truncate playback / treat as real speech
    is_echo: bool           # True => suppressed as the assistant's own echo
    reason: str             # short machine-readable reason
    correlation: float
    lag_bins: int
    far_active: bool
    near_dbfs: float
    far_dbfs: float


def should_barge_in(
    near_pcm16: bytes,
    far_pcm16: bytes,
    *,
    near_rate: int,
    far_rate: int,
    threshold: float,
    bin_ms: float = 10.0,
    lag_search_ms: float = 80.0,
    floor_dbfs: float = -60.0,
) -> BargeInDecision:
    """Decide whether a while-speaking VAD onset is a genuine interruption.

    Called only at a VAD onset while the far window still holds playback: while
    the assistant is speaking, or in the post-playback echo tail that app.py's
    ``far_window_has_playback`` tracks -- never per frame, never once the
    assistant has been silent for the whole far window. Three outcomes:

    * far-end inactive  -> genuine speech (nothing to echo)   -> barge in
    * correlated >= threshold -> the assistant's own echo     -> suppress
    * correlated <  threshold -> genuine double-talk          -> barge in

    Preserves responsiveness: the two barge-in outcomes are the existing
    behaviour; only the echo outcome is newly suppressed.
    """
    result = echo_correlation(
        near_pcm16, far_pcm16,
        near_rate=near_rate, far_rate=far_rate,
        bin_ms=bin_ms, lag_search_ms=lag_search_ms, floor_dbfs=floor_dbfs,
    )

    if not result.far_active:
        return BargeInDecision(
            barge_in=True, is_echo=False, reason="far-end-inactive",
            correlation=result.correlation, lag_bins=result.lag_bins,
            far_active=False, near_dbfs=result.near_dbfs, far_dbfs=result.far_dbfs,
        )

    if result.correlation >= threshold:
        return BargeInDecision(
            barge_in=False, is_echo=True, reason="echo-correlated",
            correlation=result.correlation, lag_bins=result.lag_bins,
            far_active=True, near_dbfs=result.near_dbfs, far_dbfs=result.far_dbfs,
        )

    return BargeInDecision(
        barge_in=True, is_echo=False, reason="uncorrelated-double-talk",
        correlation=result.correlation, lag_bins=result.lag_bins,
        far_active=True, near_dbfs=result.near_dbfs, far_dbfs=result.far_dbfs,
    )
