"""
THROWAWAY DSP probe — DELETE after use. Not part of the app.

Goal: isolate WHICH inbound-DSP stage crushes the speech band. A recording of
the audio actually sent to Gemini showed 95% of energy below ~467 Hz (1-3.4 kHz
destroyed), yet offline tests proved audioop.ratecv (8k->48k->16k) and the AEC
(far-end silent) are BOTH spectrally clean. Remaining suspect: RNNoise, which is
a 48 kHz WIDEBAND denoiser being fed 8 kHz telephony upsampled to 48 kHz (no real
energy above ~3.4 kHz). This probe runs a known 100-3800 Hz sweep through the
EXACT app chain and prints band energy WITH vs WITHOUT RNNoise.

Run on EC2 (same venv as the server), no network / no call needed:

    python probe_dsp.py

Decisive result: if "with RNNoise" collapses to <1 kHz but "without" preserves
~3.4 kHz, RNNoise on upsampled narrowband is the root cause.
"""

import numpy as np
import audioop
from pyrnnoise import RNNoise


def bands(y, fr, label):
    Y = np.abs(np.fft.rfft(y * np.hanning(len(y)))) ** 2
    freqs = np.fft.rfftfreq(len(y), 1 / fr)
    tot = Y.sum() + 1e-20
    print(f"\n{label}")
    for lo, hi in [(0, 300), (300, 1000), (1000, 2000), (2000, 3400), (3400, 4000)]:
        m = (freqs >= lo) & (freqs < hi)
        print(f"  {lo:>4}-{hi:<4}Hz: {100 * Y[m].sum() / tot:6.2f}%")
    cum = np.cumsum(Y) / tot
    print(f"  95% energy below {freqs[np.searchsorted(cum, 0.95)]:.0f} Hz")


def main():
    fs = 8000
    t = np.linspace(0, 3, fs * 3, endpoint=False)
    sweep = np.sin(2 * np.pi * (100 * t + (3800 - 100) / (2 * 3) * t ** 2))
    x8 = (sweep * 20000).astype(np.int16).tobytes()
    bands(np.frombuffer(x8, dtype=np.int16).astype(np.float64) / 32768.0, 8000,
          "INPUT sweep @8k (reference — should reach ~3.4kHz)")

    # --- WITHOUT RNNoise: just resample 8k->48k->16k (already proven clean, sanity) ---
    p48, st = audioop.ratecv(x8, 2, 1, 8000, 48000, None)
    p16, _ = audioop.ratecv(p48, 2, 1, 48000, 16000, None)
    bands(np.frombuffer(p16, dtype=np.int16).astype(np.float64) / 32768.0, 16000,
          "WITHOUT RNNoise (8k->48k->16k)")

    # --- WITH RNNoise: mirror app exactly (ratecv 8k->48k, denoise_chunk, ratecv 48k->16k) ---
    den = RNNoise(sample_rate=48000)
    up_st = down_st = None
    p48, up_st = audioop.ratecv(x8, 2, 1, 8000, 48000, up_st)
    samples_48k = np.frombuffer(p48, dtype=np.int16)
    denoised = []
    for _prob, clean_frame in den.denoise_chunk(samples_48k.reshape(1, -1)):
        denoised.append(np.array(clean_frame, dtype=np.int16).flatten())
    clean_48k = np.concatenate(denoised).tobytes() if denoised else b""
    p16d, down_st = audioop.ratecv(clean_48k, 2, 1, 48000, 16000, down_st)
    bands(np.frombuffer(p16d, dtype=np.int16).astype(np.float64) / 32768.0, 16000,
          "WITH RNNoise (app chain: 8k->48k->RNNoise->16k)")

    print("\n--- verdict ---")
    print("If WITH-RNNoise collapses below ~1kHz while WITHOUT preserves ~3.4kHz,")
    print("RNNoise on upsampled narrowband telephony is the muffling root cause.")


if __name__ == "__main__":
    main()
