"""Audio playback and response ownership for the live calling bridge.

No network clients or application startup side effects live in this module.
"""

import asyncio
import base64
from collections import deque
from dataclasses import dataclass
import json
import logging
import time

import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class SpeechDecision:
    started: bool = False
    ended: bool = False
    voice_evidence: bool = False
    required_db: float = -50.0
    noise_db: float = -60.0
    barge_in: bool = False


class SpeechGate:
    """Require sustained speech confidence AND pre-AGC energy for new activity.

    Uses stricter onset during playback, a lower continuation threshold, and a
    noise floor learned only from low-confidence idle frames. This cannot
    establish speaker identity when another person talks near the phone.
    """

    def __init__(
        self, onset_ms=80, barge_ms=240, offset_ms=400,
        start_probability=.75, barge_probability=.92, continue_probability=.40,
        minimum_db=-50.0, barge_minimum_db=-42.0,
        snr_db=6.0, barge_snr_db=10.0,
    ):
        self.onset_ms = onset_ms
        self.barge_ms = barge_ms
        self.offset_ms = offset_ms
        self.start_probability = start_probability
        self.barge_probability = barge_probability
        self.continue_probability = continue_probability
        self.minimum_db = minimum_db
        self.barge_minimum_db = barge_minimum_db
        self.snr_db = snr_db
        self.barge_snr_db = barge_snr_db
        self.noise_levels = deque(maxlen=150)
        self.noise_db = -60.0
        self.rejected_candidates = 0
        self.reset()

    def reset(self):
        self.speaking = False
        self.candidate_ms = 0
        self.candidate_gap_ms = 0
        self.candidate_total_gap_ms = 0
        self.silence_ms = 0
        self.candidate_barge = False

    def update(self, probability, rms_db, assistant_active, frame_ms=20):
        probability = float(probability)
        rms_db = float(rms_db)
        if not np.isfinite(probability) or not np.isfinite(rms_db):
            probability, rms_db = 0.0, -100.0
        if not self.speaking and not assistant_active and probability < .2:
            self.noise_levels.append(rms_db)
            if len(self.noise_levels) >= 10:
                self.noise_db = float(np.clip(np.percentile(self.noise_levels, 40), -80, -30))
        if self.speaking:
            required_db = max(self.minimum_db - 5, self.noise_db + 3)
            voice = probability >= self.continue_probability and rms_db >= required_db
            self.silence_ms = 0 if voice else self.silence_ms + frame_ms
            ended = self.silence_ms >= self.offset_ms
            if ended:
                self.reset()
            return SpeechDecision(
                ended=ended, voice_evidence=voice,
                required_db=required_db, noise_db=self.noise_db,
            )
        threshold = self.barge_probability if assistant_active else self.start_probability
        minimum = self.barge_minimum_db if assistant_active else self.minimum_db
        margin = self.barge_snr_db if assistant_active else self.snr_db
        required_db = max(minimum, self.noise_db + margin)
        voice = probability >= threshold and rms_db >= required_db
        if self.candidate_ms and assistant_active != self.candidate_barge:
            self.candidate_ms = self.candidate_gap_ms = self.candidate_total_gap_ms = 0
        self.candidate_barge = assistant_active
        if voice:
            self.candidate_ms += frame_ms
            self.candidate_gap_ms = 0
        else:
            self.candidate_gap_ms += frame_ms
            if self.candidate_ms:
                self.candidate_total_gap_ms += frame_ms
            # Tolerate isolated weak frames, but bound their total so scattered
            # noise cannot accumulate an onset longer than the input preroll.
            if self.candidate_gap_ms > 20 or self.candidate_total_gap_ms > 60:
                if self.candidate_ms:
                    self.rejected_candidates += 1
                self.candidate_ms = self.candidate_total_gap_ms = 0
        duration = self.barge_ms if assistant_active else self.onset_ms
        started = voice and self.candidate_ms >= duration
        if started:
            self.speaking = True
            self.silence_ms = 0
            self.candidate_ms = self.candidate_gap_ms = self.candidate_total_gap_ms = 0
        return SpeechDecision(
            started=started, voice_evidence=voice, required_db=required_db,
            noise_db=self.noise_db, barge_in=bool(started and assistant_active),
        )


def soft_limit(samples, threshold=0.9):
    magnitude = np.abs(samples)
    limited = np.sign(samples) * (
        threshold + (1 - threshold)
        * np.tanh((magnitude - threshold) / (1 - threshold))
    )
    return np.where(magnitude <= threshold, samples, limited)


class OutputResampler:
    """Stateful 24 kHz PCM16 -> 8 kHz decimator with a 97-tap low-pass FIR.

    The filter has 2 ms group delay and a 3.6 kHz cutoff. Carrying its history
    AND decimation phase makes arbitrarily split Gemini chunks equivalent to
    processing one contiguous waveform. finish() emits the filter tail.
    """

    def __init__(self):
        positions = np.arange(97) - 48
        self.kernel = (
            (2 * 3600 / 24000)
            * np.sinc((2 * 3600 / 24000) * positions)
            * np.hamming(97)
        )
        self.kernel /= self.kernel.sum()
        self.reset()

    def reset(self):
        self.history = np.zeros(96)
        self.samples_seen = 0
        self.pending_byte = b""

    def process(self, pcm):
        pcm = self.pending_byte + pcm
        self.pending_byte = pcm[len(pcm) - len(pcm) % 2:]
        pcm = pcm[:len(pcm) - len(pcm) % 2]
        if not pcm:
            return b""
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
        joined = np.concatenate((self.history, samples))
        filtered = np.convolve(joined, self.kernel, mode="valid")
        output = filtered[(-self.samples_seen) % 3::3]
        self.history = joined[-96:].copy()
        self.samples_seen += len(samples)
        return np.clip(np.rint(output), -32768, 32767).astype("<i2").tobytes()

    def finish(self):
        if self.pending_byte:
            raise ValueError("Gemini returned an incomplete PCM16 sample")
        tail = self.process(bytes(96 * 2)) if self.samples_seen else b""
        self.reset()
        return tail


@dataclass
class ResponseTurn:
    key: tuple
    kind: str
    deadline: float | None
    retries: int = 0
    stale: bool = False
    has_audio: bool = False
    has_tool: bool = False


class ResponseTracker:
    """Own deadlines by session/turn, retaining stale turns until their boundary.

    Gemini Live doesn't attach a client turn ID to server content. Its ordered
    interrupted -> turn_complete boundary is therefore retained in this FIFO;
    late content for that turn cannot complete the next turn's deadline.
    """

    def __init__(self, timeout):
        self.timeout = timeout
        self.epoch = 0
        self.serial = 0
        self.turns = deque()
        self.active = None

    def new_session(self):
        self.epoch += 1
        self.turns.clear()
        self.active = None
        return self.epoch

    def begin(self, kind, retries=0):
        self.serial += 1
        turn = ResponseTurn(
            (self.epoch, self.serial), kind,
            time.monotonic() + self.timeout, retries,
        )
        self.turns.append(turn)
        self.active = turn
        return turn

    def invalidate(self):
        for turn in self.turns:
            turn.stale = True
            turn.deadline = None
        self.active = None

    def current(self, epoch):
        if epoch != self.epoch or not self.turns:
            return None
        return self.turns[0]

    def complete(self, epoch):
        turn = self.current(epoch)
        if turn is not None:
            self.turns.popleft()
            turn.deadline = None
            if self.active is turn:
                self.active = None
        return turn

    def expired(self, now):
        turn = self.active
        return (
            turn is not None and not turn.stale
            and turn.deadline is not None and now >= turn.deadline
        )


class PlaybackError(RuntimeError):
    pass


@dataclass
class PlaybackMark:
    name: str
    owner: object
    tag: str
    version: int
    future: asyncio.Future
    deadline: float | None = None


class PlivoPlayback:
    """One paced sender for all audio, with remote completion acknowledgements."""

    def __init__(
        self, ws, stream_id, aec, decode_ulaw, on_complete,
        ack_timeout=5.0, max_buffer_seconds=60.0,
    ):
        self.ws = ws
        self.stream_id = stream_id
        self.aec = aec
        self.decode_ulaw = decode_ulaw
        self.on_complete = on_complete
        self.ack_timeout = ack_timeout
        self.max_buffer_bytes = int(max_buffer_seconds * 8000)
        self.queue = asyncio.Queue()
        self.lock = asyncio.Lock()
        self.version = 0
        self.serial = 0
        self.buffered_bytes = 0
        self.marks = {}
        self.current_owner = None
        self.dirty = False
        self.next_frame_at = 0.0
        self.clear_future = None
        self.clear_deadline = None
        self.last_started_owner = None
        self.closed = False
        self.last_send_at = None

    @property
    def busy(self):
        return self.dirty or bool(self.marks) or self.clear_future is not None

    def feed(self, owner, ulaw):
        if self.closed:
            raise PlaybackError("Playback is closed")
        if not ulaw:
            return
        if self.buffered_bytes + len(ulaw) > self.max_buffer_bytes:
            raise PlaybackError("Outbound audio exceeded the bounded playback buffer")
        self.current_owner = owner
        self.dirty = True
        self.buffered_bytes += len(ulaw)
        self.queue.put_nowait((self.version, owner, bytes(ulaw)))

    def finish(self, owner, tag="response"):
        self.serial += 1
        name = f"play-{self.version}-{self.serial}"
        mark = PlaybackMark(
            name, owner, tag, self.version,
            asyncio.get_running_loop().create_future(),
        )
        self.marks[name] = mark
        self.queue.put_nowait((self.version, owner, mark))
        return mark.future

    async def clear(self, reason="interruption"):
        # Serializes clear against the final pre-send epoch check. No old frame
        # may be sent after clearAudio, even if it was sleeping for pacing.
        async with self.lock:
            logger.info('Clear requested: stream=%s owner=%s reason=%s',
                        self.stream_id, self.current_owner, reason)
            self.version += 1
            self.buffered_bytes = 0
            self.dirty = False
            self.current_owner = None
            self.last_started_owner = None
            self.next_frame_at = 0
            while not self.queue.empty():
                self.queue.get_nowait()
            for mark in self.marks.values():
                mark.future.cancel()
            self.marks.clear()
            self.aec.reset_far_end()
            if self.clear_future is None:
                self.clear_future = asyncio.get_running_loop().create_future()
                self.clear_deadline = time.monotonic() + self.ack_timeout
                await self._send({
                    "event": "clearAudio", "streamId": self.stream_id,
                })

    async def acknowledge(self, message):
        if self.closed:
            return
        if message.get("streamId") != self.stream_id:
            return
        if message.get("event") == "clearedAudio":
            if self.clear_future is not None:
                logger.info('Clear acknowledged: stream=%s', self.stream_id)
                self.clear_future.set_result(True)
                self.clear_future = None
                self.clear_deadline = None
                # Don't retain references for audio discarded at the provider.
                self.aec.reset_far_end()
            return
        if message.get("event") != "playedStream":
            return
        mark = self.marks.get(message.get("name"))
        if mark is None or mark.version != self.version or mark.deadline is None:
            return
        self.marks.pop(mark.name)
        if not self.marks and self.queue.empty() and self.buffered_bytes == 0:
            self.dirty = False
        if not mark.future.done():
            mark.future.set_result(True)
        await self.on_complete(mark)

    def check_deadlines(self, now):
        if self.clear_deadline is not None and now >= self.clear_deadline:
            raise PlaybackError("Plivo did not acknowledge clearAudio")
        if any(m.deadline is not None and now >= m.deadline for m in self.marks.values()):
            raise PlaybackError("Plivo did not acknowledge playback completion")

    async def _send(self, message):
        await asyncio.wait_for(self.ws.send(json.dumps(message)), timeout=self.ack_timeout)

    async def _frame(self, version, owner, chunk):
        if self.clear_future is not None:
            await asyncio.wait_for(
                asyncio.shield(self.clear_future), timeout=self.ack_timeout,
            )
        delay = self.next_frame_at - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        async with self.lock:
            if version != self.version:
                return False
            await self._send({
                "event": "playAudio",
                "media": {
                    "contentType": "audio/x-mulaw", "sampleRate": 8000,
                    "payload": base64.b64encode(chunk).decode("ascii"),
                },
            })
            self.last_send_at = time.monotonic()
            gap_ms = (self.last_send_at - self.next_frame_at) * 1000
            if self.last_started_owner == owner and gap_ms > 60:
                logger.warning('Playback gap: stream=%s owner=%s gap_ms=%.1f buffered_ms=%.1f',
                               self.stream_id, owner, gap_ms, self.buffered_bytes / 8)
            self.aec.add_far_end(self.decode_ulaw(chunk))
            duration = len(chunk) / 8000
            if self.last_send_at - self.next_frame_at >= duration:
                # Re-anchor after a missed frame; don't burst a stalled backlog.
                self.next_frame_at = self.last_send_at + duration
            else:
                # Preserve the absolute clock across normal scheduler jitter.
                self.next_frame_at += duration
            if self.last_started_owner != owner:
                self.last_started_owner = owner
                logger.info('First Plivo send: stream=%s owner=%s at=%.6f',
                            self.stream_id, owner, self.last_send_at)
            return True

    async def run(self):
        buffer = bytearray()
        buffer_version = self.version
        try:
            while True:
                version, owner, item = await self.queue.get()
                if version != self.version:
                    continue
                if buffer_version != version:
                    buffer.clear()
                    buffer_version = version
                if isinstance(item, bytes):
                    buffer.extend(item)
                    while len(buffer) >= 160:
                        chunk = bytes(buffer[:160])
                        del buffer[:160]
                        if not await self._frame(version, owner, chunk):
                            buffer.clear()
                            break
                        self.buffered_bytes -= 160
                else:
                    if buffer:
                        size = len(buffer)
                        chunk = bytes(buffer).ljust(160, b"\xff")
                        buffer.clear()
                        if not await self._frame(version, owner, chunk):
                            continue
                        self.buffered_bytes -= size
                    async with self.lock:
                        if version != self.version:
                            continue
                        item.deadline = time.monotonic() + self.ack_timeout
                        await self._send({
                            "event": "checkpoint", "streamId": self.stream_id,
                            "name": item.name,
                        })
        finally:
            self.closed = True
            for mark in self.marks.values():
                mark.future.cancel()
            if self.clear_future is not None:
                self.clear_future.cancel()


async def cancel_tasks(tasks):
    """Cancel AND retrieve every owned task, including already failed tasks."""
    tasks = list(tasks)
    for task in tasks:
        if not task.done():
            task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
