"""aec1: path-B variant of aec.py (same public API, selected with AEC_IMPL=aec1).

aec.py is kept byte-identical (tests/test_preservation_4_7_aec_integrity.py).
This copy fixes the adaptation defect found in the TEST2 live call (2026-09-24):
stock aec.py adapts on every block, and its Kalman step mu = P / (Pe + 1e-10)
approaches a normalised NLMS step of 2 (the stability edge) whenever the near
end is almost silent. On a handset line, where the phone already cancels the
echo, the filter chased line noise, drifted, and subtracted a filtered copy of
the assistant's voice from a silent line, i.e. it injected echo. The fixes:

1. Learn only while the agent is playing and the near end could contain echo:
   no update when the far block is silent, when the near block is below
   NEAR_ADAPT_FLOOR (nothing there to learn), or during double-talk (Geigel
   detector with a hangover), so the caller's own voice does not train it.
2. Step-size ceiling and noise floor: the normalised step mu * |X|^2 is capped
   at MU_MAX, and the observation-noise term never drops below a floor, so a
   near-silent residual can no longer produce a full-size step.
3. Slow forgetting: a small leakage on the weights each adaptation, so noise
   that does get learned decays instead of accumulating.
4. Divergence check with reset, as a two-path filter: a background filter
   learns; the foreground filter, which produces the output, starts at zero
   (output = input) and only takes the background weights once they have been
   shown to remove energy. If the foreground output stays louder than the
   input it is reset to zero; a diverging background is reset too. Quiet
   double-talk the Geigel detector misses can then disturb only the background.

The residual echo suppressor is also skipped when the linear echo estimate is
louder than the mic signal itself, which only happens when the estimate is
wrong.
"""

import numpy as np
from numpy.fft import rfft, irfft

_INT16_SCALE = 32768.0

# Normalised step ceiling (NLMS-equivalent). Stock aec.py reaches ~2.0, the
# stability edge. 1.0 beat 0.5 on every offline case (TEST_FILES/_aec1_sweep.py).
MU_MAX = 1.0
# Per-bin floor on the observation-noise term of the Kalman gain.
NOISE_FLOOR = 1e-6
# Weight leakage per adaptation block (slow forgetting, ~40 s time constant).
LEAKAGE = 5e-4
# Near-end block energy (sum of squares, float scale) below which there is no
# echo worth learning: 160 samples at -60 dBFS.
NEAR_ADAPT_FLOOR = 160 * 1e-6
# Geigel double-talk detector: near peak above this fraction of the far peak
# over the modelled echo window means the caller is talking. 0.5 = -6 dB, the
# same return ceiling the barge-in gate uses (ECHO_MAX_RETURN_DB).
DTD_THRESHOLD = 0.5
DTD_HANGOVER_BLOCKS = 10  # 200 ms at 20 ms blocks
# Smoothed block energies (input, background output, foreground output) drive
# the two-path decisions. 0.1 = ~10-block (200 ms) memory.
ENERGY_SMOOTHING = 0.1
# Background weights are copied to the foreground when the background output is
# at least 3 dB below the input and at least 1 dB below the foreground output.
PROMOTE_BELOW_INPUT = 0.5
PROMOTE_BELOW_FOREGROUND = 0.8
# Foreground output above input by +1 dB for 200 ms: reset foreground to zero.
FOREGROUND_DIVERGE_RATIO = 1.26
FOREGROUND_DIVERGE_BLOCKS = 10
# Background output above input by +3 dB for 500 ms: reset background.
BACKGROUND_DIVERGE_RATIO = 2.0
BACKGROUND_DIVERGE_BLOCKS = 25
# Far peak over the modelled window above which the filter can output an echo
# estimate at all (-60 dBFS).
FAR_WINDOW_PEAK_FLOOR = 1e-3


class _PFDKF:
    """Partitioned-block frequency-domain Kalman adaptive filter (linear AEC core).

    N : number of partitions (blocks) of the modeled echo impulse response
    M : block/frame size in samples
    Total modeled echo path length = N * M samples.

    H is the adapting (background) filter; H_fg is the foreground filter that
    produces the output, updated only by promote() and reset_foreground().
    """

    def __init__(self, N, M, A=0.999, P_initial=1e2, P_floor=1e-2, partial_constrain=True):
        self.N = N
        self.M = M
        self.N_freq = 1 + M
        self.N_fft = 2 * M
        self.A2 = A ** 2
        self.P_initial = float(P_initial)
        self.P_floor = float(P_floor)
        self.partial_constrain = partial_constrain
        self.p = 0

        self.x = np.zeros(2 * self.M, dtype=np.float32)
        self.X = np.zeros((self.N, self.N_freq), dtype=np.complex128)
        self.window = np.hanning(self.M).astype(np.float32)
        self.reset_background()
        self.reset_foreground()

    def reset_background(self):
        """Forget the learned echo path. The far-end history in X is kept."""
        self.P = np.full((self.N, self.N_freq), self.P_initial, dtype=np.float64)
        self.H = np.zeros((self.N, self.N_freq), dtype=np.complex128)
        self.p = 0

    def reset_foreground(self):
        self.H_fg = np.zeros((self.N, self.N_freq), dtype=np.complex128)

    def promote(self):
        self.H_fg = self.H.copy()

    def filt(self, x, d):
        """Run one block. x = far-end (M), d = near-end/mic (M). Returns the
        foreground error e_fg (M), which is the echo-cancelled near-end, its echo
        estimate y_fg (M), and the background error e_bg (M) used to adapt."""
        assert len(x) == self.M
        self.x[self.M:] = x
        X = rfft(self.x)
        self.X[1:] = self.X[:-1]
        self.X[0] = X
        self.x[:self.M] = self.x[self.M:]

        y_bg = irfft(np.sum(self.H * self.X, axis=0))[self.M:]
        y_fg = irfft(np.sum(self.H_fg * self.X, axis=0))[self.M:]
        return d - y_fg, y_fg, d - y_bg

    def update(self, e):
        e_fft = np.zeros(self.N_fft, dtype=np.float32)
        e_fft[self.M:] = e * self.window
        E = rfft(e_fft)

        X2 = np.sum(np.abs(self.X) ** 2, axis=0)
        Pe = 0.5 * self.P * X2 + np.maximum(np.abs(E) ** 2 / self.N, NOISE_FLOOR)
        mu = self.P / Pe
        # Cap the normalised step: mu * X2 <= MU_MAX.
        mu = np.minimum(mu, MU_MAX / (X2 + 1e-10))

        shrink = np.clip(1.0 - 0.5 * mu * X2, 0.0, 1.0)
        self.P = self.A2 * shrink * self.P + (1 - self.A2) * np.abs(self.H) ** 2
        np.maximum(self.P, self.P_floor, out=self.P)
        G = mu * self.X.conj()
        self.H *= 1.0 - LEAKAGE
        self.H += E * G

        if self.partial_constrain:
            h = irfft(self.H[self.p])
            h[self.M:] = 0
            self.H[self.p] = rfft(h)
            self.p = (self.p + 1) % self.N
        else:
            for p in range(self.N):
                h = irfft(self.H[p])
                h[self.M:] = 0
                self.H[p] = rfft(h)


class AcousticEchoCanceller:
    def __init__(
        self,
        frame_size=160,
        num_blocks=24,
        sample_rate=8000,
        enable_res=True,
        res_beta=0.3,
        res_gain_floor=0.15,
        far_activity_energy=1e-3,
        max_far_backlog_frames=64,
    ):
        self.M = int(frame_size)
        self.sample_rate = sample_rate
        self.enable_res = enable_res
        self.res_beta = float(res_beta)
        self.res_gain_floor = float(res_gain_floor)
        self.far_activity_energy = float(far_activity_energy)
        self._max_far_samples = int(max_far_backlog_frames) * self.M

        self._filter = _PFDKF(N=int(num_blocks), M=self.M)

        # Far-end (reference / what we play out) FIFO of float32 samples.
        self._far_fifo = np.zeros(0, dtype=np.float32)
        # Near-end remainder carried between calls if a chunk isn't a whole
        # number of blocks (defensive; Plivo chunks are normally exactly M).
        self._near_buf = np.zeros(0, dtype=np.float32)

        # Per-block far peaks over the modelled echo window, for the Geigel DTD.
        self._far_peaks = np.zeros(int(num_blocks), dtype=np.float32)
        self._dt_hangover = 0
        self._ema_in = 0.0
        self._ema_bg = 0.0
        self._ema_fg = 0.0
        self._fg_bad_blocks = 0
        self._bg_bad_blocks = 0

        # Counters, read by app.py's call stats (duck-typed; aec.py has none).
        self.stats = {
            "blocks": 0,
            "far_active_blocks": 0,
            "adapted_blocks": 0,
            "doubletalk_blocks": 0,
            "near_silent_blocks": 0,
            "promotions": 0,
            "foreground_resets": 0,
            "background_resets": 0,
        }

    # ---- far-end (reference) ------------------------------------------------

    def add_far_end(self, pcm16_bytes):
        """Push outbound audio (PCM16, 8 kHz, mono) that is being played to the
        caller. Call this from the Plivo sender for each chunk actually sent, so
        the reference is aligned to real playback time."""
        if not pcm16_bytes:
            return
        samples = np.frombuffer(pcm16_bytes, dtype=np.int16).astype(np.float32) / _INT16_SCALE
        self._far_fifo = np.concatenate([self._far_fifo, samples])
        # Bound the backlog: drop the oldest so transport delay stays inside the
        # filter's modeled window and memory can't grow without limit.
        if self._far_fifo.size > self._max_far_samples:
            self._far_fifo = self._far_fifo[-self._max_far_samples:]

    def reset_far_end(self):
        """Clear the far-end backlog. Call on session reconnect: the ~1 s gap
        desynchronizes far/near timing, but the learned echo path (filter
        weights) stays valid because the acoustics didn't change."""
        self._far_fifo = np.zeros(0, dtype=np.float32)
        self._near_buf = np.zeros(0, dtype=np.float32)

    def _pop_far(self, n):
        """Pop n far-end samples; zero-pad if the AI isn't playing / not enough
        buffered (correct: no far-end signal => nothing to cancel)."""
        if self._far_fifo.size >= n:
            block = self._far_fifo[:n]
            self._far_fifo = self._far_fifo[n:]
            return block
        block = np.zeros(n, dtype=np.float32)
        if self._far_fifo.size:
            block[: self._far_fifo.size] = self._far_fifo
            self._far_fifo = np.zeros(0, dtype=np.float32)
        return block

    # ---- near-end (mic) -----------------------------------------------------

    def process(self, pcm16_bytes):
        """Echo-cancel inbound mic audio (PCM16, 8 kHz, mono). Returns PCM16
        bytes of the same total length (over time). Safe to call every chunk
        even when the AI is silent (far-end is zeros => near-end passes through
        essentially unchanged)."""
        if not pcm16_bytes:
            return pcm16_bytes

        near = np.frombuffer(pcm16_bytes, dtype=np.int16).astype(np.float32) / _INT16_SCALE
        if self._near_buf.size:
            near = np.concatenate([self._near_buf, near])
            self._near_buf = np.zeros(0, dtype=np.float32)

        M = self.M
        n_blocks = near.size // M
        remainder = near.size - n_blocks * M
        if remainder:
            self._near_buf = near[n_blocks * M:].copy()

        if n_blocks == 0:
            # Not enough for a block yet; nothing to emit this call.
            return b""

        out = np.empty(n_blocks * M, dtype=np.float32)
        for i in range(n_blocks):
            d = near[i * M:(i + 1) * M]
            x = self._pop_far(M)
            e, y, e_bg = self._filter.filt(x, d)
            self._adapt(x, d, e_bg)
            # Checked over the whole modelled window, not just active blocks: the
            # filter still outputs an echo estimate for 480 ms after playback.
            if float(np.max(self._far_peaks)) > FAR_WINDOW_PEAK_FLOOR:
                if self._two_path(float(np.dot(d, d)), float(np.dot(e, e)),
                                  float(np.dot(e_bg, e_bg))):
                    e, y = d.copy(), np.zeros_like(d)

            if self.enable_res:
                e = self._suppress_residual(x, d, e, y)

            out[i * M:(i + 1) * M] = e

        np.clip(out, -1.0, 1.0, out=out)
        return (out * (_INT16_SCALE - 1)).astype(np.int16).tobytes()

    def _adapt(self, x, d, e_bg):
        """Update the background filter if this block is safe to learn from."""
        self.stats["blocks"] += 1
        self._far_peaks[1:] = self._far_peaks[:-1]
        self._far_peaks[0] = float(np.max(np.abs(x)))
        far_peak = float(np.max(self._far_peaks))
        if far_peak > 0.0 and float(np.max(np.abs(d))) > DTD_THRESHOLD * far_peak:
            self._dt_hangover = DTD_HANGOVER_BLOCKS
        elif self._dt_hangover:
            self._dt_hangover -= 1

        if float(np.dot(x, x)) <= self.far_activity_energy:
            return
        self.stats["far_active_blocks"] += 1
        if self._dt_hangover:
            self.stats["doubletalk_blocks"] += 1
        elif float(np.dot(d, d)) < NEAR_ADAPT_FLOOR:
            self.stats["near_silent_blocks"] += 1
        else:
            self._filter.update(e_bg)
            self.stats["adapted_blocks"] += 1

    def _two_path(self, d_energy, fg_energy, bg_energy):
        """Promote / reset decisions on smoothed energies. Returns True when the
        foreground was just reset, so this block's output must be the input."""
        a = ENERGY_SMOOTHING
        self._ema_in += a * (d_energy - self._ema_in)
        self._ema_fg += a * (fg_energy - self._ema_fg)
        self._ema_bg += a * (bg_energy - self._ema_bg)
        f = self._filter

        if not np.all(np.isfinite(f.H)):
            f.reset_background()
            self._ema_bg = self._ema_in
            self.stats["background_resets"] += 1
        elif self._ema_bg > BACKGROUND_DIVERGE_RATIO * self._ema_in:
            self._bg_bad_blocks += 1
            if self._bg_bad_blocks >= BACKGROUND_DIVERGE_BLOCKS:
                f.reset_background()
                self._bg_bad_blocks = 0
                self._ema_bg = self._ema_in
                self.stats["background_resets"] += 1
        else:
            self._bg_bad_blocks = 0
            if (self._ema_bg < PROMOTE_BELOW_INPUT * self._ema_in
                    and self._ema_bg < PROMOTE_BELOW_FOREGROUND * self._ema_fg):
                f.promote()
                self._ema_fg = self._ema_bg
                self.stats["promotions"] += 1

        if self._ema_fg > FOREGROUND_DIVERGE_RATIO * self._ema_in:
            self._fg_bad_blocks += 1
            if self._fg_bad_blocks >= FOREGROUND_DIVERGE_BLOCKS:
                f.reset_foreground()
                self._fg_bad_blocks = 0
                self._ema_fg = self._ema_in
                self.stats["foreground_resets"] += 1
                return True
        else:
            self._fg_bad_blocks = 0
        return False

    def _suppress_residual(self, x, d, e, y):
        """Scalar residual-echo suppressor.

        Only engages when the far-end is actually active (AI playing). Applies a
        Wiener-like gain using the linear echo estimate `y` as the interference
        reference. When the user is talking (double-talk), the error energy `e`
        is large relative to the echo estimate, so the gain stays near 1 and the
        user's voice is preserved. When only residual echo remains, the gain
        drops toward the floor and suppresses it. Skipped when the estimate is
        louder than the mic signal: no real echo exceeds the signal it is in."""
        x_energy = float(np.dot(x, x))
        if x_energy <= self.far_activity_energy:
            return e  # far-end silent: no echo to suppress, leave near-end alone

        y_energy = float(np.dot(y, y))
        if y_energy > float(np.dot(d, d)):
            return e
        e_energy = float(np.dot(e, e))
        gain = e_energy / (e_energy + self.res_beta * y_energy + 1e-10)
        if gain < self.res_gain_floor:
            gain = self.res_gain_floor
        elif gain > 1.0:
            gain = 1.0
        return e * gain
