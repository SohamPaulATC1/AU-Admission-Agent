"""Offline integration tests. Credentials, file writes, and providers are isolated."""
import asyncio
import base64
import contextlib
import importlib.util
import json
import logging
import os
from pathlib import Path
import shutil
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import numpy as np
from google.genai import _live_converters, types
from google.genai.errors import APIError

from call_runtime import PlivoPlayback, cancel_tasks

ROOT = Path(__file__).resolve().parents[1]
TEMP = None
app = None


def setUpModule():
    global TEMP, app
    TEMP = tempfile.TemporaryDirectory()
    directory = Path(TEMP.name)
    shutil.copy(ROOT / 'prompt1.txt', directory / 'prompt1.txt')
    (directory / 'playback_audio_files').mkdir()
    shutil.copy(ROOT / 'playback_audio_files/recorded1.wav', directory / 'playback_audio_files/recorded1.wav')
    previous = Path.cwd()
    converter = logging.Formatter.converter
    try:
        os.chdir(directory)
        with patch('dotenv.load_dotenv'), patch('plivo.RestClient'), patch('logging.FileHandler', return_value=logging.NullHandler()), patch.dict(os.environ, {
            'GOOGLE_API_KEY': 'offline-test-key', 'PLIVO_AUTH_ID': 'offline-test-id',
            'PLIVO_AUTH_TOKEN': 'offline-test-token', 'FROM_NUMBER': 'offline',
            'PUBLIC_BASE_URL': 'https://example.invalid',
            'LEADS_DIR': str(directory / 'leads'),
            'TRANSFER_CONTEXT_DB_PATH': str(directory / 'transfer.sqlite3'),
            'RECORD_GEMINI_INPUT': '1', 'HYBRID_VAD': '1',
        }, clear=True):
            spec = importlib.util.spec_from_file_location('app_under_test', ROOT / 'app_aec1.py')
            app = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(app)
            app.logger.disabled = True
    finally:
        os.chdir(previous)
        logging.Formatter.converter = converter


def tearDownModule():
    TEMP.cleanup()


class FakeVAD:
    def __init__(self, **kwargs):
        self.prob = .99

    def denoise_chunk(self, chunk):
        yield self.prob, chunk


class FakeAEC:
    def __init__(self):
        self.frames = 0
        self.clears = 0

    def process(self, data):
        self.frames += 1
        return data

    def add_far_end(self, data):
        pass

    def reset_far_end(self):
        self.clears += 1


class FakeWS:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.sent = []

    async def receive(self):
        return await self.incoming.get()

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    def event(self, data):
        self.incoming.put_nowait(json.dumps(data))

    def audio(self, pcm, size=160):
        ulaw = app.pcm_to_ulaw(pcm)
        for i in range(0, len(ulaw), size):
            self.event({'event': 'media', 'media': {
                'track': 'inbound', 'payload': base64.b64encode(ulaw[i:i + size]).decode(),
            }})


class FakeSession:
    def __init__(self, auto=False):
        self.incoming = asyncio.Queue()
        self.prompts = []
        self.inputs = []
        self.tools = []
        self.auto = auto

    async def send_client_content(self, **data):
        self.prompts.append(data)
        if self.auto:
            self.incoming.put_nowait(audio_response(complete=True))

    async def send_realtime_input(self, **data):
        self.inputs.append(data)
        if self.auto and 'activity_end' in data:
            self.incoming.put_nowait(audio_response(complete=True))

    async def send_tool_response(self, **data):
        self.tools.append(data)

    async def receive(self):
        while True:
            message = await self.incoming.get()
            if isinstance(message, Exception):
                raise message
            yield message
            if message.server_content and message.server_content.turn_complete:
                return


def server(**kwargs):
    return types.LiveServerMessage(server_content=types.LiveServerContent(**kwargs))


def audio_response(complete=False, tool=None):
    pcm = (np.sin(np.arange(480) * .15) * 6000).astype('<i2').tobytes()
    message = server(
        model_turn=types.Content(parts=[types.Part(inline_data=types.Blob(data=pcm, mime_type='audio/pcm;rate=24000'))]),
        turn_complete=complete,
    )
    if tool:
        message.tool_call = types.LiveServerToolCall(function_calls=[types.FunctionCall(
            id='tool-1', name=tool, args={'summary_of_whole_call': 'Test summary',
                                      'call_summary': 'Test transfer', 'language': 'english'},
        )])
    return message


class FakeClient:
    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)
        self.configs = []
        self.exits = 0
        self.aio = SimpleNamespace(live=SimpleNamespace(connect=self.connect), aclose=AsyncMock())

    @contextlib.asynccontextmanager
    async def connect(self, *, model, config):
        self.configs.append(config)
        result = next(self.outcomes)
        if isinstance(result, Exception):
            raise result
        try:
            yield result
        finally:
            self.exits += 1


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.vad_patch = patch.object(app, 'RNNoise', FakeVAD)
        self.vad_patch.start()
        self.addCleanup(self.vad_patch.stop)
        self.tasks = []
        self.states = []
        self.client = SimpleNamespace(calls=SimpleNamespace(delete=Mock(), transfer=Mock()))
        self.client_patch = patch.object(app, 'plivo_client', self.client)
        self.client_patch.start()
        self.addCleanup(self.client_patch.stop)
        self.lead_patch = patch.object(app, 'save_lead', Mock())
        self.lead_patch.start()
        self.addCleanup(self.lead_patch.stop)

    async def asyncTearDown(self):
        await cancel_tasks(self.tasks)
        for state in self.states:
            await cancel_tasks(state['background_tasks'])

    def task(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.append(task)
        return task

    def state(self, greeting=True):
        state = app.create_call_state('Test', 'test')
        state.update(
            greeting_completed=greeting, playing_disclaimer=False,
            stream_id='stream', call_uuid='call', call_deadline=time.monotonic() + 60,
            session_ready=True, initial_prompt='Say the initial greeting', aec=FakeAEC(),
        )
        state['responses'].new_session()
        ws = FakeWS()
        state['playback'] = PlivoPlayback(ws, 'stream', state['aec'], app.ulaw_to_pcm,
                                         lambda mark: app.playback_completed(state, mark), ack_timeout=.5)
        self.states.append(state)
        return state, ws

    async def until(self, predicate, timeout=2):
        async def wait():
            while not predicate():
                for task in self.tasks:
                    if task.done() and not task.cancelled() and task.exception():
                        raise task.exception()
                await asyncio.sleep(.001)
        await asyncio.wait_for(wait(), timeout)

    async def test_cancelling_coordinator_reaps_every_child(self):
        children = {str(i): self.task(asyncio.Event().wait()) for i in range(4)}
        coordinator = self.task(app.coordinate_call_tasks(children, {}))
        await asyncio.sleep(0)
        coordinator.cancel()
        await asyncio.gather(coordinator, return_exceptions=True)
        self.assertTrue(all(task.cancelled() for task in children.values()))

    async def test_failed_child_also_cleans_up_siblings(self):
        async def fail():
            raise RuntimeError('simulated')
        children = {'failure': asyncio.create_task(fail()), 'sibling': asyncio.create_task(asyncio.Event().wait())}
        with self.assertRaisesRegex(RuntimeError, 'simulated'):
            await app.coordinate_call_tasks(children, {})
        self.assertTrue(children['sibling'].cancelled())

    async def test_empty_turn_gets_one_recovery_then_terminates(self):
        state, _ = self.state()
        session = FakeSession()
        original = state['responses'].begin('user')
        self.task(app.stream_gemini_to_plivo(session, None, state, self.client))
        session.incoming.put_nowait(server(turn_complete=True))
        await self.until(lambda: len(session.prompts) == 1)
        self.assertIsNone(original.deadline)
        self.assertEqual(state['responses'].active.retries, 1)
        session.incoming.put_nowait(server(turn_complete=True))
        await self.until(lambda: state['pending_end_call'])
        self.assertIsNone(state['responses'].active)
        self.assertEqual(len(session.prompts), 1)
        self.assertTrue(await app.execute_pending_terminal_action(None, state, self.client, None))
        self.client.calls.delete.assert_called_once_with(call_uuid='call')

    async def test_late_interruption_completion_and_tool_do_not_affect_new_turn(self):
        state, _ = self.state()
        session = FakeSession()
        old = state['responses'].begin('user')
        state['responses'].invalidate()
        new = state['responses'].begin('user')
        deadline = new.deadline
        self.task(app.stream_gemini_to_plivo(session, None, state, self.client))
        session.incoming.put_nowait(server(interrupted=True))
        session.incoming.put_nowait(audio_response(tool='endCall'))
        await self.until(lambda: bool(session.tools))
        self.assertFalse(state['pending_end_call'])
        self.assertTrue(state['playback'].queue.empty())
        self.assertEqual(session.tools[0]['function_responses'][0].response['status'], 'cancelled')
        session.incoming.put_nowait(server(turn_complete=True))
        await self.until(lambda: state['responses'].current(state['responses'].epoch) is new)
        self.assertTrue(old.stale)
        self.assertEqual(new.deadline, deadline)
        self.assertIs(state['responses'].active, new)

    async def test_intermediate_completion_retains_audio_owner_until_idle(self):
        state, ws = self.state(greeting=False)
        session = FakeSession()
        turn = state['responses'].begin('greeting')
        state['turn_complete'] = False
        self.task(state['playback'].run())
        self.task(app.stream_gemini_to_plivo(session, None, state, self.client))
        intermediate = audio_response(complete=True)
        intermediate.server_content.interaction_status = types.InteractionStatus.IN_PROGRESS
        intermediate.usage_metadata = types.UsageMetadata(total_token_count=10)
        session.incoming.put_nowait(intermediate)
        await self.until(lambda: state['usage_total_updates'] == 1)
        await self.until(lambda: any(m['event'] == 'playAudio' for m in ws.sent))
        self.assertIs(state['responses'].active, turn)
        self.assertIsNotNone(turn.deadline)
        self.assertFalse(state['turn_complete'])
        self.assertFalse(state['greeting_completed'])
        self.assertFalse(state['playback'].marks)
        self.assertEqual(session.prompts, [])
        final = audio_response(complete=True)
        final.server_content.interaction_status = types.InteractionStatus.IDLE
        session.incoming.put_nowait(final)
        await self.until(lambda: any(m['event'] == 'checkpoint' for m in ws.sent))
        self.assertIsNone(state['responses'].active)
        self.assertIsNone(turn.deadline)
        # Both 20 ms segments and the padded filter tail reached Plivo.
        audio = [m for m in ws.sent if m['event'] == 'playAudio']
        self.assertEqual(len(audio), 3)
        marks = [m for m in ws.sent if m['event'] == 'checkpoint']
        self.assertEqual(len(marks), 1)
        self.assertFalse(state['greeting_completed'])
        await state['playback'].acknowledge({
            'event': 'playedStream', 'streamId': 'stream', 'name': marks[0]['name'],
        })
        self.assertTrue(state['greeting_completed'])

    async def test_empty_intermediate_completion_does_not_retry_or_end_call(self):
        state, _ = self.state()
        session = FakeSession()
        turn = state['responses'].begin('user')
        self.task(app.stream_gemini_to_plivo(session, None, state, self.client))
        message = types.LiveServerMessage.model_validate(
            _live_converters._LiveServerMessage_from_mldev({
                'serverContent': {'turnComplete': True, 'interactionStatus': 'IN_PROGRESS'},
            })
        )
        message.usage_metadata = types.UsageMetadata(total_token_count=10)
        session.incoming.put_nowait(message)
        await self.until(lambda: state['usage_total_updates'] == 1)
        self.assertEqual(session.prompts, [])
        self.assertFalse(state['pending_end_call'])
        self.assertIs(state['responses'].active, turn)
        self.assertIsNotNone(turn.deadline)
        # A genuinely empty final completion still gets the existing bounded retry.
        session.incoming.put_nowait(server(turn_complete=True, interaction_status=types.InteractionStatus.IDLE))
        await self.until(lambda: len(session.prompts) == 1)
        self.assertIsNone(turn.deadline)
        self.assertEqual(state['responses'].active.retries, 1)

    async def test_interrupted_background_turn_cannot_rearm_new_turn_deadline(self):
        state, _ = self.state()
        session = FakeSession()
        old = state['responses'].begin('user')
        state['responses'].invalidate()
        new = state['responses'].begin('user')
        deadline = new.deadline
        self.task(app.stream_gemini_to_plivo(session, None, state, self.client))
        session.incoming.put_nowait(server(
            interrupted=True, turn_complete=True,
            interaction_status=types.InteractionStatus.IN_PROGRESS,
        ))
        await self.until(lambda: state['responses'].current(state['responses'].epoch) is new)
        self.assertIsNone(old.deadline)
        self.assertEqual(new.deadline, deadline)
        self.assertIs(state['responses'].active, new)

    async def test_input_noise_rejected_but_sustained_speech_clears_once_with_preroll(self):
        state, ws = self.state()
        session = FakeSession()
        turn = state['responses'].begin('user')
        state['playback'].feed(turn.key, b'\xff' * 8000)
        self.task(app.read_plivo_events(ws, state))
        self.task(app.stream_plivo_to_gemini(None, session, state))

        async def feed(frames, amplitude):
            before = state['chunk_count']
            ws.audio(np.full(160 * frames, amplitude, dtype='<i2').tobytes())
            await self.until(lambda: state['chunk_count'] >= before + frames)

        # Fake RNNoise always returns .99: energy/duration must still protect playback.
        await feed(5, 2000)
        await feed(3, 0)
        await feed(20, 30)
        self.assertFalse(state['is_speaking'])
        self.assertEqual(session.inputs, [])
        self.assertFalse(any(m['event'] == 'clearAudio' for m in ws.sent))
        await feed(12, 2000)
        await self.until(lambda: sum('audio' in m for m in session.inputs) >= 15)
        self.assertEqual(sum('activity_start' in m for m in session.inputs), 1)
        self.assertEqual(sum(m['event'] == 'clearAudio' for m in ws.sent), 1)
        self.assertEqual(state['barge_in_count'], 1)
        pcm = b''.join(m['audio'].data for m in session.inputs if 'audio' in m)
        self.assertGreaterEqual(len(pcm), 240 * 32)  # Confirmation did not lose onset.
        await feed(5, 2000)
        self.assertEqual(sum(m['event'] == 'clearAudio' for m in ws.sent), 1)

    async def test_noise_alone_does_not_raise_input_gain(self):
        state, ws = self.state()
        session = FakeSession()
        self.task(app.read_plivo_events(ws, state))
        self.task(app.stream_plivo_to_gemini(None, session, state))
        ws.audio(np.full(160 * 20, 30, dtype='<i2').tobytes())
        await self.until(lambda: state['chunk_count'] == 20)
        self.assertEqual(state['agc_current_gain_lin'], 1.0)
        self.assertEqual(session.inputs, [])

    async def test_greeting_audio_is_discarded_including_partial_frame_at_boundary(self):
        state, ws = self.state(greeting=False)
        session = FakeSession()
        state['session'] = session
        self.task(app.read_plivo_events(ws, state))
        self.task(state['playback'].run())
        self.task(app.stream_plivo_to_gemini(None, session, state))
        # Six old frames and half an old frame must never reach the next turn.
        ws.audio(np.full(160 * 6 + 80, -2000, dtype='<i2').tobytes())
        await self.until(lambda: state['aec'].frames == 6)
        self.assertFalse(state['is_speaking'])
        self.assertTrue(state['input_audio_queue'].empty())
        self.assertEqual(session.inputs, [])
        state['playback'].feed('greeting', b'\xff' * 160)
        state['playback'].finish('greeting', 'greeting')
        await self.until(lambda: any(m['event'] == 'checkpoint' for m in ws.sent))
        mark = next(m for m in ws.sent if m['event'] == 'checkpoint')
        ws.event({'event': 'playedStream', 'streamId': 'stream', 'name': mark['name']})
        await self.until(lambda: state['greeting_completed'])
        ws.audio(np.full(160 * 4, 2000, dtype='<i2').tobytes())
        await self.until(lambda: any('audio' in m for m in session.inputs))
        self.assertEqual(sum('activity_start' in m for m in session.inputs), 1)
        pcm = b''.join(m['audio'].data for m in session.inputs if 'audio' in m)
        self.assertTrue(np.all(np.frombuffer(pcm, '<i2') >= 0))

    async def test_reader_keeps_draining_during_reconnect(self):
        state, ws = self.state()
        state['session_ready'] = False
        reader = self.task(app.read_plivo_events(ws, state))
        ws.audio(bytes(320 * 10))
        await self.until(lambda: state['aec'].frames == 10)
        self.assertTrue(state['input_audio_queue'].empty())
        state['session_ready'] = True
        state['responses'].new_session()
        ws.audio(bytes(320))
        await self.until(lambda: state['input_audio_queue'].qsize() == 1)
        epoch, _, _ = state['input_audio_queue'].get_nowait()
        self.assertEqual(epoch, state['responses'].epoch)
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)
        self.assertFalse(state['plivo_disconnected'])

    async def test_goodbye_waits_for_remote_playback_even_after_deadline(self):
        state, ws = self.state()
        session = FakeSession()
        state['responses'].begin('user')
        self.task(state['playback'].run())
        self.task(app.stream_gemini_to_plivo(session, None, state, self.client))
        session.incoming.put_nowait(audio_response(complete=True, tool='endCall'))
        await self.until(lambda: any(m['event'] == 'checkpoint' for m in ws.sent))
        state['terminal_action_deadline'] = time.monotonic() - 1
        self.assertFalse(await app.execute_pending_terminal_action(None, state, self.client, None))
        self.client.calls.delete.assert_not_called()
        mark = next(m for m in ws.sent if m['event'] == 'checkpoint')
        await state['playback'].acknowledge({'event': 'playedStream', 'streamId': 'stream', 'name': mark['name']})
        self.assertTrue(await app.execute_pending_terminal_action(None, state, self.client, None))
        self.client.calls.delete.assert_called_once()

    async def test_transfer_waits_for_ack_and_preserves_summary(self):
        state, ws = self.state()
        session = FakeSession()
        state['responses'].begin('user')
        self.task(state['playback'].run())
        self.task(app.stream_gemini_to_plivo(session, None, state, self.client))
        session.incoming.put_nowait(audio_response(complete=True, tool='transferCall'))
        await self.until(lambda: any(m['event'] == 'checkpoint' for m in ws.sent))
        self.assertFalse(await app.execute_pending_terminal_action(None, state, self.client, None))
        self.client.calls.transfer.assert_not_called()
        mark = next(m for m in ws.sent if m['event'] == 'checkpoint')
        await state['playback'].acknowledge({'event': 'playedStream', 'streamId': 'stream', 'name': mark['name']})
        with patch.object(app, 'save_transfer_context') as save, patch.object(app, 'POST_TRANSFER_DELAY_SECONDS', 0):
            self.assertTrue(await app.execute_pending_terminal_action(None, state, self.client, None))
        self.assertEqual(json.loads(save.call_args.args[1])['call_summary'], 'Test transfer')
        self.client.calls.transfer.assert_called_once()
        self.client.calls.delete.assert_not_called()

    async def test_retries_connection_503_and_receive_1011(self):
        state, _ = self.state(greeting=False)
        session1, session2 = FakeSession(), FakeSession()
        client = FakeClient([APIError(503, {'message': 'unavailable'}), session1, session2])
        ready = asyncio.Event(); ready.set()
        with patch.object(app, 'GEMINI_RECONNECT_DELAY_SECONDS', 0):
            task = self.task(app.run_gemini_sessions(client, state, 'system', 'greeting', ready))
            await self.until(lambda: bool(session1.prompts))
            state['responses'].complete(state['responses'].epoch)
            state['greeting_completed'] = True
            state['session_resumption_handle'] = 'idle-snapshot'
            session1.incoming.put_nowait(APIError(1011, {'message': 'temporary internal error'}))
            await self.until(lambda: bool(session2.prompts))
            self.assertEqual(len(client.configs), 3)
            self.assertEqual(client.configs[-1].session_resumption.handle, 'idle-snapshot')
            self.assertFalse(task.done())
            self.assertEqual(state['responses'].epoch, 3)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(client.exits, 2)

    async def test_rejected_resumption_handle_falls_back_to_context(self):
        state, _ = self.state()
        state['session_resumption_handle'] = 'expired'
        state['conversation_log'] = [{'role': 'user', 'text': 'B.Tech CSE fees'}]
        session = FakeSession()
        client = FakeClient([APIError(400, {'message': 'Invalid resumption handle'}), session])
        ready = asyncio.Event(); ready.set()
        with patch.object(app, 'GEMINI_RECONNECT_DELAY_SECONDS', 0):
            self.task(app.run_gemini_sessions(client, state, 'system', 'greeting', ready))
            await self.until(lambda: bool(session.prompts))
        self.assertIsNone(client.configs[1].session_resumption.handle)
        self.assertIn('B.Tech CSE fees', session.prompts[0]['turns'].parts[0].text)

    async def test_mid_turn_disconnect_restores_context_without_replaying_old_activity(self):
        state, _ = self.state()
        state['conversation_log'] = [{'role': 'user', 'text': 'Interested in B.Tech CSE'}]
        first, second = FakeSession(), FakeSession()
        client = FakeClient([first, second])
        ready = asyncio.Event(); ready.set()
        with patch.object(app, 'GEMINI_RECONNECT_DELAY_SECONDS', 0):
            self.task(app.run_gemini_sessions(client, state, 'system', 'greeting', ready))
            await self.until(lambda: bool(first.prompts))
            state['session_resumption_handle'] = 'mid-turn-snapshot'
            state['user_activity_open'] = True
            first.incoming.put_nowait(APIError(1011, {'message': 'temporary failure'}))
            await self.until(lambda: bool(second.prompts))
        self.assertIsNone(client.configs[1].session_resumption.handle)
        self.assertIn('B.Tech CSE', second.prompts[0]['turns'].parts[0].text)
        self.assertFalse(state['user_activity_open'])

    async def test_nonrecoverable_config_failure_does_not_retry(self):
        state, _ = self.state()
        client = FakeClient([APIError(400, {'message': 'Invalid model configuration'})])
        ready = asyncio.Event(); ready.set()
        with self.assertRaises(APIError):
            await app.run_gemini_sessions(client, state, 'system', 'greeting', ready)
        self.assertEqual(len(client.configs), 1)

    async def test_disconnect_during_farewell_seals_and_drains_without_reconnecting(self):
        state, ws = self.state()
        session = FakeSession()
        client = FakeClient([session])
        ready = asyncio.Event(); ready.set()
        self.task(state['playback'].run())
        self.task(app.run_gemini_sessions(client, state, 'system', 'greeting', ready))
        await self.until(lambda: bool(session.prompts))
        session.incoming.put_nowait(audio_response(tool='endCall'))
        session.incoming.put_nowait(APIError(1011, {'message': 'temporary failure'}))
        await self.until(lambda: any(m['event'] == 'checkpoint' for m in ws.sent))
        self.assertEqual(len(client.configs), 1)
        self.assertFalse(await app.execute_pending_terminal_action(None, state, self.client, None))
        mark = next(m for m in ws.sent if m['event'] == 'checkpoint')
        await state['playback'].acknowledge({'event': 'playedStream', 'streamId': 'stream', 'name': mark['name']})
        self.assertTrue(await app.execute_pending_terminal_action(None, state, self.client, None))

    async def test_cleanup_does_not_swallow_original_session_error(self):
        @contextlib.asynccontextmanager
        async def connection(**kwargs):
            try:
                yield FakeSession()
            finally:
                raise APIError(1000, {'message': 'closed'})
        client = SimpleNamespace(aio=SimpleNamespace(live=SimpleNamespace(connect=connection)))
        with self.assertRaisesRegex(app.GeminiSessionDisconnected, 'original'):
            async with app.connect_live_with_timeout(client, 'model', {}):
                raise app.GeminiSessionDisconnected('original')

    async def test_goaway_uses_sdk_duration_string_and_waits_for_idle(self):
        state, _ = self.state()
        state['responses'].begin('user')
        session = FakeSession()
        self.task(app.stream_gemini_to_plivo(session, None, state, self.client))
        session.incoming.put_nowait(types.LiveServerMessage(
            go_away=types.LiveServerGoAway(time_left='30s')))
        await self.until(lambda: state['go_away_received'])
        self.assertGreater(state['go_away_deadline'], time.monotonic() + 28)
        # The current response is still owned; GoAway didn't abandon it.
        self.assertIsNotNone(state['responses'].active)
        state['responses'].complete(state['responses'].epoch)
        with self.assertRaises(app.GeminiSessionDisconnected):
            await app.supervise_model_session(state, state['responses'].epoch)

    async def test_recovery_attempts_are_bounded(self):
        state, _ = self.state(greeting=False)
        client = FakeClient([APIError(503, {'message': 'unavailable'}) for _ in range(3)])
        ready = asyncio.Event(); ready.set()
        with patch.object(app, 'GEMINI_RECONNECT_DELAY_SECONDS', 0), patch.object(app, 'MAX_GEMINI_RECONNECTS', 2):
            await app.run_gemini_sessions(client, state, 'system', 'greeting', ready)
        self.assertEqual(len(client.configs), 3)
        self.assertTrue(state['terminate_session'])
        self.client.calls.delete.assert_called_once()

    async def test_missing_playback_ack_ends_call_without_opening_microphone(self):
        provider = FakeClient([FakeSession(auto=True)])
        client = app.app.test_client()
        samples = [base64.b64encode(b'\xff' * 160).decode()]
        with patch.object(app.genai, 'Client', return_value=provider), patch.object(app, 'DISCLAIMER_ULAW_CHUNKS', samples), patch.object(app, 'PLAYBACK_ACK_TIMEOUT_SECONDS', .02):
            async with client.websocket('/media-stream?user_name=Test&phone_number=test') as ws:
                await ws.send(json.dumps({'event': 'start', 'start': {'callId': 'call', 'streamId': 'stream'}}))
                # The provider intentionally never acknowledges the disclaimer.
                await self.until(lambda: provider.aio.aclose.await_count == 1)
        self.client.calls.delete.assert_called_once_with(call_uuid='call')

    async def test_disconnect_during_greeting_closes_session_and_owned_tasks(self):
        provider = FakeClient([FakeSession(auto=True)])
        client = app.app.test_client()
        samples = [base64.b64encode(b'\xff' * 160).decode()]
        with patch.object(app.genai, 'Client', return_value=provider), patch.object(app, 'DISCLAIMER_ULAW_CHUNKS', samples):
            async with client.websocket('/media-stream?user_name=Test&phone_number=test') as ws:
                await ws.send(json.dumps({'event': 'start', 'start': {'callId': 'call', 'streamId': 'stream'}}))
                while True:
                    message = json.loads(await asyncio.wait_for(ws.receive(), 2))
                    if message['event'] == 'checkpoint':
                        break
                # Close the socket without sending a Plivo stop event.
            await self.until(lambda: provider.aio.aclose.await_count == 1)
        active = {task.get_coro().__qualname__ for task in asyncio.all_tasks() if not task.done()}
        for name in ['stream_gemini_to_plivo', 'stream_plivo_to_gemini', 'PlivoPlayback.run',
                     'run_gemini_sessions', 'read_plivo_events']:
            self.assertNotIn(name, active)
        self.assertEqual(provider.exits, 1)

    async def test_silence_watchdog_does_not_prompt_during_speech(self):
        state, _ = self.state()
        state['is_speaking'] = True
        session = FakeSession()
        await app.silence_watchdog(session, state, delay=0)
        self.assertEqual(session.prompts, [])

    async def test_sse_has_no_timeout_and_heartbeats_clean_up_subscription(self):
        async with app.app.test_request_context('/call-events'):
            response = await app.call_events_stream()
        self.assertIsNone(response.timeout)
        with patch.object(app, 'SSE_HEARTBEAT_SECONDS', .001):
            async with response.response as body:
                iterator = body.__aiter__()
                heartbeat = await anext(iterator)
                self.assertIn(': heartbeat', heartbeat.decode() if isinstance(heartbeat, bytes) else heartbeat)
        self.assertEqual(len(app.call_event_subscribers), 0)

    async def test_no_plivo_recording_and_manual_vad_despite_old_env(self):
        self.assertFalse(app.HYBRID_VAD)
        async with app.app.test_request_context('/outbound-webhook?user_name=Test&phone_number=test'):
            response = await app.outbound_webhook()
        xml = await response.get_data(as_text=True)
        self.assertNotIn('<Record', xml)
        self.assertIn('noiseCancellation="true"', xml)

    async def test_full_websocket_lifecycle_with_disclaimer_and_greeting(self):
        session = FakeSession(auto=True)
        provider = FakeClient([session])
        client = app.app.test_client()
        samples = [base64.b64encode(b'\xff' * 160).decode()] * 2
        with (patch.object(app.genai, 'Client', return_value=provider),
              patch.object(app, 'DISCLAIMER_ULAW_CHUNKS', samples),
              patch.object(app.wave, 'open', wraps=app.wave.open) as audio_files):
            async with client.websocket('/media-stream?user_name=Test&phone_number=test') as ws:
                await ws.send(json.dumps({'event': 'start', 'start': {'callId': 'call', 'streamId': 'stream'}}))
                marks = []
                while len(marks) < 2:
                    message = json.loads(await asyncio.wait_for(ws.receive(), 3))
                    if message['event'] == 'checkpoint':
                        marks.append(message['name'])
                        # Prior to the greeting acknowledgement, early audio is muted.
                        await ws.send(json.dumps({'event': 'media', 'media': {
                            'payload': base64.b64encode(b'\xff' * 160).decode(), 'track': 'inbound',
                        }}))
                        await ws.send(json.dumps({'event': 'playedStream', 'streamId': 'stream', 'name': message['name']}))
                self.assertEqual(len(session.prompts), 1)
                self.assertEqual(session.inputs, [])
                voice = app.pcm_to_ulaw(np.full(160, 2000, dtype='<i2').tobytes())
                for _ in range(4):
                    await ws.send(json.dumps({'event': 'media', 'media': {
                        'payload': base64.b64encode(voice).decode(), 'track': 'inbound',
                    }}))
                await self.until(lambda: any('audio' in m for m in session.inputs))
                await ws.send(json.dumps({'event': 'stop'}))
            await self.until(lambda: provider.aio.aclose.await_count == 1)
            audio_files.assert_not_called()  # Caller input did not open a recording.
        self.client.calls.delete.assert_not_called()
        self.assertEqual(provider.exits, 1)
        app.save_lead.assert_called_once()


if __name__ == '__main__':
    unittest.main()
