"""
Acoustic Echo Cancellation (AEC) for the Plivo <-> Gemini voice pipeline.

Self-contained, pure-numpy. No native build dependencies.

Why this exists
---------------
On speakerphone the AI's own voice leaves the callee's speaker, bleeds back into
the callee's microphone, and returns to us as inbound audio. RNNoise is a *noise*
suppressor — it preserves speech, so it does NOT remove echoed AI speech. Left
uncancelled, that echo trips the manual VAD, fires a false barge-in, interrupts
the AI, and loops. This module removes the linear echo (and gates residual echo)
BEFORE the audio reaches RNNoise/VAD.

Algorithm
---------
Partitioned-block frequency-domain adaptive filter with a Kalman step-size
(PFDKF). The partitioned filter models the whole echo path — bulk transport
delay plus room/handset tail — up to ``frame_size * num_blocks`` samples. The
Kalman gain adapts the step size automatically and is inherently more robust to
double-talk (user + AI speaking at once) than a fixed-step NLMS, so it neither
diverges nor cancels the near-end talker.

A light scalar residual-echo suppressor (RES) follows the linear stage to knock
down the nonlinear residual that a linear filter cannot model (phone speakers
are very nonlinear). RES is gated on far-end activity so user-only audio is left
untouched.

The linear filter core (PFDKF filt/update math) is adapted from the Apache-2.0
"pyaec" reference by ewan xu, ported to numpy>=2.0 dtypes and wrapped for
real-time streaming (int16 <-> float, far-end FIFO, RES, delay alignment).

Integration (8 kHz, mono, PCM16):
    aec = AcousticEchoCanceller()
    aec.add_far_end(outbound_pcm16_bytes)   # call from the Plivo sender, per sent chunk
    clean = aec.process(inbound_pcm16_bytes) # call on the inbound mic path, before RNNoise
"""

import numpy as np
from numpy.fft import rfft, irfft

_INT16_SCALE = 32768.0


class _PFDKF:
    """Partitioned-block frequency-domain Kalman adaptive filter (linear AEC core).

    N : number of partitions (blocks) of the modeled echo impulse response
    M : block/frame size in samples
    Total modeled echo path length = N * M samples.
    """

    def __init__(self, N, M, A=0.999, P_initial=1e2, P_floor=1e-2, partial_constrain=True):
        self.N = N
        self.M = M
        self.N_freq = 1 + M
        self.N_fft = 2 * M
        self.A2 = A ** 2
        self.P_floor = float(P_floor)
        self.partial_constrain = partial_constrain
        self.p = 0

        self.x = np.zeros(2 * self.M, dtype=np.float32)
        self.P = np.full((self.N, self.N_freq), float(P_initial), dtype=np.float64)
        self.X = np.zeros((self.N, self.N_freq), dtype=np.complex128)
        self.H = np.zeros((self.N, self.N_freq), dtype=np.complex128)
        self.window = np.hanning(self.M).astype(np.float32)

    def filt(self, x, d):
        """Run one block. x = far-end (M), d = near-end/mic (M). Returns error e (M)
        which is the echo-cancelled near-end, plus y (linear echo estimate, M)."""
        assert len(x) == self.M
        self.x[self.M:] = x
        X = rfft(self.x)
        self.X[1:] = self.X[:-1]
        self.X[0] = X
        self.x[:self.M] = self.x[self.M:]

        Y = np.sum(self.H * self.X, axis=0)
        y = irfft(Y)[self.M:]
        e = d - y
        return e, y

    def update(self, e):
        e_fft = np.zeros(self.N_fft, dtype=np.float32)
        e_fft[self.M:] = e * self.window
        E = rfft(e_fft)

        X2 = np.sum(np.abs(self.X) ** 2, axis=0)
        Pe = 0.5 * self.P * X2 + np.abs(E) ** 2 / self.N
        mu = self.P / (Pe + 1e-10)
        # Clamp the covariance-shrink factor to [0, 1]: it must never drive P
        # negative (large mu during double-talk) nor to exactly zero (far-end
        # active with no near-end yet, e.g. during the echo transport delay),
        # which would freeze adaptation for the rest of the call. A P floor adds
        # standing process noise so the filter keeps tracking a changing echo path.
        shrink = np.clip(1.0 - 0.5 * mu * X2, 0.0, 1.0)
        self.P = self.A2 * shrink * self.P + (1 - self.A2) * np.abs(self.H) ** 2
        np.maximum(self.P, self.P_floor, out=self.P)
        G = mu * self.X.conj()
        self.H += E * G

        if self.partial_constrain:
            # Gradient-constrain one partition per block (cheaper; converges over N blocks)
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
    """Streaming AEC wrapper.

    Parameters
    ----------
    frame_size : samples per processing block (default 160 = 20 ms @ 8 kHz,
        matching one Plivo mu-law chunk so downstream framing is unchanged).
    num_blocks : number of partitions. Total modeled echo path =
        frame_size * num_blocks. Default 24 blocks * 20 ms = 480 ms, which
        comfortably covers IP-telephony round-trip echo delay plus tail.
    enable_res : enable the residual echo suppressor after the linear filter.
    res_beta   : residual suppressor aggressiveness (higher = more suppression).
    res_gain_floor : minimum RES gain (limits max suppression, avoids artifacts).
    max_far_backlog_frames : cap on buffered far-end frames (bounds memory and
        keeps echo delay inside the modeled window if the sender bursts ahead).
    """

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
        """Discard abandoned playback references, preserving learned weights."""
        self._far_fifo = np.zeros(0, dtype=np.float32)
        self._near_buf = np.zeros(0, dtype=np.float32)
        self._filter.X.fill(0)
        self._filter.x.fill(0)

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
            e, y = self._filter.filt(x, d)
            self._filter.update(e)

            if self.enable_res:
                e = self._suppress_residual(x, d, e, y)

            out[i * M:(i + 1) * M] = e

        np.clip(out, -1.0, 1.0, out=out)
        return (out * (_INT16_SCALE - 1)).astype(np.int16).tobytes()

    def _suppress_residual(self, x, d, e, y):
        """Scalar residual-echo suppressor.

        Only engages when the far-end is actually active (AI playing). Applies a
        Wiener-like gain using the linear echo estimate `y` as the interference
        reference. When the user is talking (double-talk), the error energy `e`
        is large relative to the echo estimate, so the gain stays near 1 and the
        user's voice is preserved. When only residual echo remains, the gain
        drops toward the floor and suppresses it."""
        x_energy = float(np.dot(x, x))
        if x_energy <= self.far_activity_energy:
            return e  # far-end silent: no echo to suppress, leave near-end alone

        e_energy = float(np.dot(e, e))
        y_energy = float(np.dot(y, y))
        gain = e_energy / (e_energy + self.res_beta * y_energy + 1e-10)
        if gain < self.res_gain_floor:
            gain = self.res_gain_floor
        elif gain > 1.0:
            gain = 1.0
        return e * gain
