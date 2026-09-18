import asyncio
import base64
import json
import time
import unittest

import numpy as np

from aec import AcousticEchoCanceller
from call_runtime import (
    OutputResampler, PlaybackError, PlivoPlayback, ResponseTracker, cancel_tasks,
    SpeechGate, soft_limit,
)


class DSPTests(unittest.TestCase):
    def test_limiter_is_symmetric_monotonic_and_bounded(self):
        x = np.linspace(-4, 4, 10001)
        y = soft_limit(x)
        np.testing.assert_allclose(y, -y[::-1], atol=1e-12)
        self.assertTrue(np.all(np.diff(y) >= 0))
        self.assertLessEqual(np.max(np.abs(y)), 1)
        np.testing.assert_array_equal(soft_limit(x[np.abs(x) < .9]), x[np.abs(x) < .9])

    def test_resampler_preserves_voice_band_and_rejects_alias(self):
        def tone(frequency):
            t = np.arange(24000) / 24000
            pcm = (10000 * np.sin(2 * np.pi * frequency * t)).astype('<i2').tobytes()
            resampler = OutputResampler()
            output = resampler.process(pcm) + resampler.finish()
            return np.frombuffer(output, '<i2')[100:-100].astype(float)
        speech = np.sqrt(np.mean(tone(1000) ** 2))
        alias = np.sqrt(np.mean(tone(6000) ** 2))
        self.assertGreater(speech, 6500)
        self.assertLess(alias / speech, .01)

    def test_resampling_is_independent_of_chunk_and_sample_boundaries(self):
        pcm = np.random.default_rng(3).integers(-15000, 15000, 24001, dtype=np.int16).tobytes()
        whole = OutputResampler()
        expected = whole.process(pcm) + whole.finish()
        split = OutputResampler()
        output = b''.join(split.process(pcm[i:i + 719]) for i in range(0, len(pcm), 719)) + split.finish()
        self.assertEqual(output, expected)
        self.assertEqual(split.finish(), b'')
        split.process(pcm[:1000])
        split.reset()
        self.assertEqual(split.process(pcm) + split.finish(), expected)

    def test_aec_clear_removes_timeline_but_retains_learned_weights(self):
        aec = AcousticEchoCanceller()
        aec._filter.H.fill(.1)
        aec._filter.X.fill(3)
        aec._filter.x.fill(1)
        aec.add_far_end(bytes(320))
        aec.reset_far_end()
        self.assertEqual(aec._far_fifo.size, 0)
        self.assertFalse(aec._filter.X.any())
        self.assertFalse(aec._filter.x.any())
        self.assertTrue(np.all(aec._filter.H == .1))


class SpeechGateTests(unittest.TestCase):
    def test_short_loud_bursts_and_quiet_high_confidence_noise_do_not_interrupt(self):
        gate = SpeechGate()
        for _ in range(4):
            for _ in range(5):  # 100 ms clicks, with confidence deliberately high.
                self.assertFalse(gate.update(.99, -20, True).started)
            for _ in range(3):
                self.assertFalse(gate.update(0, -70, True).started)
        for _ in range(100):
            self.assertFalse(gate.update(.99, -55, True).started)
        self.assertFalse(gate.speaking)
        self.assertEqual(gate.rejected_candidates, 4)

    def test_sustained_speech_interrupts_after_confirmation(self):
        gate = SpeechGate()
        for _ in range(11):
            self.assertFalse(gate.update(.99, -25, True).started)
        decision = gate.update(.99, -25, True)
        self.assertTrue(decision.started)
        self.assertTrue(decision.barge_in)
        self.assertTrue(gate.speaking)
        self.assertFalse(gate.update(.99, -25, True).started)

    def test_short_quiet_reply_works_as_soon_as_playback_finishes(self):
        gate = SpeechGate()
        gate.update(.99, -25, True)
        for _ in range(3):
            self.assertFalse(gate.update(.85, -47, False).started)
        self.assertTrue(gate.update(.85, -47, False).started)

    def test_adaptive_floor_requires_speech_above_steady_background(self):
        gate = SpeechGate()
        for _ in range(50):
            gate.update(.05, -38, False)
        for _ in range(20):
            self.assertFalse(gate.update(.99, -33, True).started)
        for _ in range(11):
            self.assertFalse(gate.update(.99, -25, True).started)
        self.assertTrue(gate.update(.99, -25, True).started)
        self.assertAlmostEqual(gate.noise_db, -38)
        gate.reset()
        self.assertAlmostEqual(gate.noise_db, -38)
        self.assertFalse(gate.update(.99, -25, True).started)

    def test_hysteresis_keeps_quiet_syllables_and_short_pauses_in_same_turn(self):
        gate = SpeechGate()
        for _ in range(4):
            gate.update(.99, -30, False)
        for _ in range(15):
            self.assertFalse(gate.update(.50, -48, False).ended)
        for _ in range(10):  # 200 ms pause must not end the sentence.
            self.assertFalse(gate.update(0, -70, False).ended)
        self.assertFalse(gate.update(.50, -48, False).ended)
        for _ in range(19):
            self.assertFalse(gate.update(0, -70, False).ended)
        self.assertTrue(gate.update(0, -70, False).ended)
        self.assertFalse(gate.speaking)

    def test_scattered_weak_frames_cannot_accumulate_an_unbounded_onset(self):
        gate = SpeechGate()
        for _ in range(30):
            self.assertFalse(gate.update(.99, -25, True).started)
            self.assertFalse(gate.update(.5, -25, True).started)
        self.assertFalse(gate.speaking)
        self.assertGreater(gate.rejected_candidates, 0)

    def test_nonfinite_measurements_never_start_speech(self):
        gate = SpeechGate()
        for probability, level in [(float('nan'), -20), (.99, float('inf')),
                                   (float('inf'), -20), (.99, float('nan'))]:
            for _ in range(20):
                self.assertFalse(gate.update(probability, level, False).started)


class TrackerTests(unittest.TestCase):
    def test_old_completion_does_not_clear_new_deadline(self):
        tracker = ResponseTracker(30)
        epoch = tracker.new_session()
        first = tracker.begin('user')
        tracker.invalidate()
        second = tracker.begin('user')
        deadline = second.deadline
        self.assertIs(tracker.current(epoch), first)
        self.assertTrue(tracker.complete(epoch).stale)
        self.assertIs(tracker.active, second)
        self.assertEqual(second.deadline, deadline)
        self.assertIsNone(tracker.complete(epoch - 1))
        self.assertIs(tracker.complete(epoch), second)
        self.assertIsNone(second.deadline)
        self.assertIsNone(tracker.active)

    def test_new_connection_invalidates_old_epoch(self):
        tracker = ResponseTracker(30)
        epoch = tracker.new_session()
        tracker.begin('user')
        tracker.new_session()
        second = tracker.begin('recovery')
        self.assertIsNone(tracker.complete(epoch))
        self.assertIs(tracker.active, second)


class FakeWS:
    def __init__(self):
        self.messages = []
        self.times = []

    async def send(self, raw):
        self.messages.append(json.loads(raw))
        self.times.append(time.monotonic())


class PlaybackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.ws = FakeWS()
        self.aec = AcousticEchoCanceller()
        self.completed = []
        async def complete(mark):
            self.completed.append(mark)
        self.playback = PlivoPlayback(
            self.ws, 'stream', self.aec, lambda chunk: bytes(len(chunk) * 2), complete,
            ack_timeout=.5,
        )
        self.task = asyncio.create_task(self.playback.run())

    async def asyncTearDown(self):
        await cancel_tasks([self.task])

    async def wait_event(self, event, count=1):
        async def wait():
            while len([m for m in self.ws.messages if m['event'] == event]) < count:
                await asyncio.sleep(.001)
        await asyncio.wait_for(wait(), 4)
        return [m for m in self.ws.messages if m['event'] == event][-1]

    async def test_pacing_keeps_reference_bounded_with_live_input(self):
        sizes = []
        async def inbound():
            next_frame = time.monotonic()
            while True:
                self.aec.process(bytes(320))
                sizes.append(self.aec._far_fifo.size)
                next_frame += .02
                await asyncio.sleep(max(0, next_frame - time.monotonic()))
        reader = asyncio.create_task(inbound())
        try:
            self.playback.feed((1, 1), b'\xff' * 16000)
            done = self.playback.finish((1, 1))
            mark = await self.wait_event('checkpoint')
            times = [t for m, t in zip(self.ws.messages, self.ws.times) if m['event'] == 'playAudio']
            self.assertEqual(len(times), 100)
            self.assertGreaterEqual(times[-1] - times[0], 1.9)
            self.assertLessEqual(max(sizes), 640)
            self.assertFalse(done.done())
            await self.playback.acknowledge({'event': 'playedStream', 'streamId': 'stream', 'name': mark['name']})
            self.assertTrue(done.result())
            self.assertFalse(self.playback.busy)
        finally:
            await cancel_tasks([reader])

    async def test_tail_is_padded_and_completion_requires_matching_ack(self):
        self.playback.feed('owner', b'\x10' * 159)
        done = self.playback.finish('owner', 'greeting')
        mark = await self.wait_event('checkpoint')
        audio = next(m for m in self.ws.messages if m['event'] == 'playAudio')
        self.assertEqual(base64.b64decode(audio['media']['payload']), b'\x10' * 159 + b'\xff')
        self.assertTrue(self.playback.busy)
        for stream, name in [('other', mark['name']), ('stream', 'unknown')]:
            await self.playback.acknowledge({'event': 'playedStream', 'streamId': stream, 'name': name})
        self.assertFalse(done.done())
        await self.playback.acknowledge({'event': 'playedStream', 'streamId': 'stream', 'name': mark['name']})
        self.assertEqual([m.tag for m in self.completed], ['greeting'])
        self.assertTrue(done.result())

    async def test_clear_cancels_sleeping_old_frames_and_waits_for_ack(self):
        self.playback.feed('old', b'\x10' * 1600)
        old_done = self.playback.finish('old')
        old_mark = next(iter(self.playback.marks))
        await self.wait_event('playAudio')
        await self.playback.clear()
        clear_index = len(self.ws.messages) - 1
        self.assertEqual(self.ws.messages[clear_index], {'event': 'clearAudio', 'streamId': 'stream'})
        self.assertTrue(old_done.cancelled())
        self.playback.feed('new', b'\x20' * 160)
        new_done = self.playback.finish('new')
        await asyncio.sleep(.04)
        self.assertEqual(len(self.ws.messages), clear_index + 1)
        await self.playback.acknowledge({'event': 'playedStream', 'streamId': 'stream', 'name': old_mark})
        self.assertEqual(self.completed, [])
        await self.playback.acknowledge({'event': 'clearedAudio', 'streamId': 'stream'})
        mark = await self.wait_event('checkpoint')
        audio = [m for m in self.ws.messages[clear_index + 1:] if m['event'] == 'playAudio']
        self.assertEqual(len(audio), 1)
        self.assertEqual(base64.b64decode(audio[0]['media']['payload']), b'\x20' * 160)
        await self.playback.acknowledge({'event': 'playedStream', 'streamId': 'stream', 'name': mark['name']})
        self.assertTrue(new_done.result())
        self.assertFalse(self.playback.busy)

    async def test_blocked_websocket_send_has_a_bounded_lifetime(self):
        async def blocked_send(raw):
            await asyncio.Event().wait()
        self.ws.send = blocked_send
        self.playback.ack_timeout = .01
        self.playback.feed('owner', b'\xff' * 160)
        completion = self.playback.finish('owner')
        with self.assertRaises(TimeoutError):
            await self.task
        self.assertTrue(self.playback.closed)
        self.assertTrue(completion.cancelled())

    async def test_missing_ack_fails_closed_and_buffer_is_bounded(self):
        self.playback.feed('owner', b'\xff' * 160)
        self.playback.finish('owner')
        await self.wait_event('checkpoint')
        with self.assertRaises(PlaybackError):
            self.playback.check_deadlines(time.monotonic() + 1)
        self.assertTrue(self.playback.busy)
        self.assertFalse(self.completed)
        with self.assertRaises(PlaybackError):
            self.playback.feed('owner', bytes(self.playback.max_buffer_bytes + 1))


if __name__ == '__main__':
    unittest.main()
