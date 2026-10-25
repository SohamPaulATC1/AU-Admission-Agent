"""Multi-process support (2026-10-11, operator): call tags on log lines, and dashboard events
forwarded from a media-only process to the main process over /internal/call-event.
"""

from __future__ import annotations

import asyncio
import http.server
import inspect
import json
import logging
import os
import subprocess
import sys
import threading
import unittest
from unittest import mock

from tests.harness.appctl import REPO_ROOT, LogCapture, app

TOKEN = "test-token-123"


class _ListHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.addFilter(app.CallTagFilter())
        self.setFormatter(logging.Formatter(app.LOG_FORMAT))
        self.lines = []

    def emit(self, record):
        self.lines.append(self.format(record))


def _with_handler(fn):
    handler = _ListHandler()
    previous = app.logger.level
    app.logger.setLevel(logging.INFO)  # the harness quiets the app logger
    app.logger.addHandler(handler)
    try:
        fn()
    finally:
        app.logger.removeHandler(handler)
        app.logger.setLevel(previous)
    return handler.lines


class TestCallTag(unittest.TestCase):
    def test_no_tag_outside_a_call(self):
        lines = _with_handler(lambda: app.logger.info("server line"))
        self.assertTrue(lines[0].endswith("[INFO] server line"), lines)

    def test_tag_inside_a_call(self):
        def log():
            token = app.call_log_tag.set("[abcd1234] ")
            try:
                app.logger.info("call line")
            finally:
                app.call_log_tag.reset(token)
        lines = _with_handler(log)
        self.assertTrue(lines[0].endswith("[INFO] [abcd1234] call line"), lines)

    def test_concurrent_calls_keep_their_own_tag(self):
        async def call(tag):
            app.call_log_tag.set(f"[{tag}] ")
            await asyncio.sleep(0)
            app.logger.info(f"from {tag}")
            child = asyncio.create_task(asyncio.sleep(0, result=None))  # child inherits the tag
            await child
            app.logger.info(f"again {tag}")

        async def both():
            await asyncio.gather(call("aaaa1111"), call("bbbb2222"))

        lines = _with_handler(lambda: asyncio.run(both()))
        for line in lines:
            tag = "aaaa1111" if "aaaa1111" in line.split("] ", 2)[-1] else "bbbb2222"
            self.assertIn(f"[{tag}] ", line)
        self.assertEqual(len(lines), 4)

    def test_handle_media_stream_tags_right_after_the_start_event(self):
        source = inspect.getsource(app.handle_media_stream)
        start = source.index("await wait_for_plivo_start(plivo_ws, call_state)")
        tag = source.index("call_log_tag.set(")
        first_task = source.index("asyncio.create_task(")
        self.assertLess(start, tag)
        self.assertLess(tag, first_task, "tasks created before the tag would log untagged")


class _Receiver:
    """A real HTTP server on localhost (own thread) standing in for the main process."""

    def __init__(self):
        self.events, self.tokens = [], []

    async def start(self):
        receiver = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                receiver.tokens.append(self.headers.get("X-Internal-Token"))
                receiver.events.append(json.loads(body))
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{self.server.server_address[1]}/internal/call-event"

    async def stop(self):
        self.server.shutdown()
        self.server.server_close()


async def _wait_for(predicate, timeout=5.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("timed out")
        await asyncio.sleep(0.01)


class TestForwarding(unittest.TestCase):
    def setUp(self):
        app._forward_loop = app._forward_queue = None

    def tearDown(self):
        app._forward_loop = app._forward_queue = None

    def test_events_reach_the_main_process_in_order_with_the_token(self):
        async def scenario():
            receiver = _Receiver()
            url = await receiver.start()
            subscriber = asyncio.Queue()
            app.call_event_subscribers.add(subscriber)
            try:
                with mock.patch.object(app, "EVENT_FORWARD_URL", url), \
                        mock.patch.object(app, "INTERNAL_EVENT_TOKEN", TOKEN), \
                        mock.patch.object(app, "persist_call_event_sync") as persist:
                    app.emit_call_event("uuid-1", "call_connected", {"phone": "99"})
                    app.emit_call_event("uuid-1", "ai_transcript", {"text": "hello"})
                    app.emit_call_event("uuid-1", "call_ended", {"reason": "done"})
                    await _wait_for(lambda: len(receiver.events) == 3)
                    persist.assert_not_called()
            finally:
                app.call_event_subscribers.discard(subscriber)
                await receiver.stop()
            return receiver, subscriber

        receiver, subscriber = asyncio.run(scenario())
        self.assertEqual([e["type"] for e in receiver.events], ["call_connected", "ai_transcript", "call_ended"])
        first = dict(receiver.events[0])
        event_id = first.pop("event_id")   # 2026-10-13: lets the main process drop a retried copy
        self.assertEqual(first, {"call_id": "uuid-1", "type": "call_connected", "data": {"phone": "99"}})
        self.assertRegex(event_id, r"^[0-9a-f]{16}$")
        self.assertEqual(len({e["event_id"] for e in receiver.events}), 3)
        self.assertEqual(receiver.tokens, [TOKEN] * 3)
        self.assertTrue(subscriber.empty(), "a media-only process must not emit locally")

    def test_an_unreachable_main_process_only_logs_a_warning(self):
        async def scenario():
            with mock.patch.object(app, "EVENT_FORWARD_URL", "http://127.0.0.1:9/internal/call-event"), \
                    mock.patch.object(app, "INTERNAL_EVENT_TOKEN", TOKEN), \
                    mock.patch.object(app, "EVENT_FORWARD_RETRY_DELAYS_S", (0, 0)), LogCapture() as log:
                app.emit_call_event("uuid-2", "ai_transcript", {"text": "x"})
                await _wait_for(lambda: any("not delivered" in line for line in log.lines))
            return log.lines

        lines = asyncio.run(scenario())
        self.assertTrue(any("ai_transcript for uuid-2 not delivered" in line for line in lines), lines)
        self.assertEqual(sum("retry" in line for line in lines), 2, lines)   # retried twice, then given up


class _FlakyClient:
    """Answers post() with the given outcomes in turn: an exception is raised, an int is a status code."""

    def __init__(self, *outcomes):
        self.outcomes, self.posts = list(outcomes), []

    async def post(self, url, json=None, headers=None):
        self.posts.append(json)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return mock.Mock(status_code=outcome)


class TestForwardRetry(unittest.TestCase):
    EVENT = {"call_id": "uuid-9", "type": "call_ended", "data": {"cost_usd": 0.1}, "event_id": "ab" * 8}

    def deliver(self, client):
        delays = []

        async def sleep(seconds):
            delays.append(seconds)

        with mock.patch.object(app, "EVENT_FORWARD_URL", "http://127.0.0.1:9/internal/call-event"), LogCapture() as log:
            ok = asyncio.run(app._deliver_event(client, dict(self.EVENT), {}, sleep=sleep))
        return ok, delays, log.lines

    def test_a_dropped_connection_is_retried_with_the_same_event(self):
        import httpx
        client = _FlakyClient(httpx.ReadError(""), 200)
        ok, delays, _ = self.deliver(client)
        self.assertTrue(ok)
        self.assertEqual(delays, [0.5])
        self.assertEqual(client.posts[0], client.posts[1])   # same event_id both times

    def test_it_gives_up_after_two_retries(self):
        import httpx
        client = _FlakyClient(httpx.ReadError(""), httpx.ConnectError("x"), httpx.RemoteProtocolError("y"))
        ok, delays, lines = self.deliver(client)
        self.assertFalse(ok)
        self.assertEqual(delays, [0.5, 2.0])
        self.assertTrue(any("call_ended for uuid-9 not delivered" in line for line in lines), lines)

    def test_a_refusal_is_not_retried(self):
        client = _FlakyClient(403)
        ok, delays, lines = self.deliver(client)
        self.assertEqual((ok, delays, len(client.posts)), (False, [], 1))
        self.assertTrue(any("rejected: HTTP 403" in line for line in lines), lines)


class TestDuplicateEvents(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(app, "_seen_event_ids", app.collections.OrderedDict())
        patcher.start()
        self.addCleanup(patcher.stop)

    def post(self, event):
        async def go():
            with mock.patch.object(app, "INTERNAL_EVENT_TOKEN", TOKEN), \
                    mock.patch.object(app, "emit_call_event_local") as local:
                client = app.app.test_client()
                response = await client.post("/internal/call-event", json=event, headers={"X-Internal-Token": TOKEN})
                return response.status_code, await response.get_json(), local.call_count
        return asyncio.run(go())

    def test_a_retried_copy_is_applied_once(self):
        event = {"call_id": "uuid-9", "type": "call_ended", "data": {"cost_usd": 0.1}, "event_id": "cd" * 8}
        self.assertEqual(self.post(event), (200, {"status": "ok"}, 1))
        self.assertEqual(self.post(event), (200, {"status": "ok", "duplicate": True}, 0))

    def test_events_without_an_id_are_still_handled(self):   # a media process not yet restarted
        event = {"call_id": "uuid-9", "type": "call_ended", "data": {}}
        self.assertEqual(self.post(event)[2], 1)
        self.assertEqual(self.post(event)[2], 1)

    def test_the_memory_is_bounded(self):
        with mock.patch.object(app, "SEEN_EVENT_IDS_MAX", 3):
            for i in range(5):
                app._already_seen(f"id-{i}")
            self.assertEqual(list(app._seen_event_ids), ["id-2", "id-3", "id-4"])
            self.assertFalse(app._already_seen("id-0"))   # forgotten: only the last few are kept

    def test_the_main_process_emits_locally(self):
        async def scenario():
            subscriber = asyncio.Queue()
            app.call_event_subscribers.add(subscriber)
            try:
                with mock.patch.object(app, "EVENT_FORWARD_URL", ""), \
                        mock.patch.object(app, "forward_call_event") as forward:
                    app.emit_call_event("uuid-3", "ai_transcript", {"text": "y"})
                    forward.assert_not_called()
            finally:
                app.call_event_subscribers.discard(subscriber)
            return subscriber

        self.assertFalse(asyncio.run(scenario()).empty())


class TestInternalEndpoint(unittest.TestCase):
    def _post(self, body, token=TOKEN, configured=TOKEN, forward_url=""):
        async def scenario():
            subscriber = asyncio.Queue()
            app.call_event_subscribers.add(subscriber)
            try:
                with mock.patch.object(app, "INTERNAL_EVENT_TOKEN", configured), \
                        mock.patch.object(app, "EVENT_FORWARD_URL", forward_url), \
                        mock.patch.object(app, "forward_call_event") as forward, \
                        mock.patch.object(app, "persist_call_event_sync"):
                    headers = {"X-Internal-Token": token} if token is not None else {}
                    response = await app.app.test_client().post("/internal/call-event", json=body, headers=headers)
                    forward_calls = forward.call_count
            finally:
                app.call_event_subscribers.discard(subscriber)
            delivered = []
            while not subscriber.empty():
                delivered.append(subscriber.get_nowait())
            return response.status_code, delivered, forward_calls

        return asyncio.run(scenario())

    def test_valid_event_reaches_the_dashboard(self):
        status, delivered, _ = self._post({"call_id": "uuid-4", "type": "ai_transcript", "data": {"text": "z"}})
        self.assertEqual(status, 200)
        self.assertEqual(len(delivered), 1)
        self.assertIn('"call_id": "uuid-4"', delivered[0])
        self.assertIn('"type": "ai_transcript"', delivered[0])

    def test_wrong_or_missing_token_is_404(self):
        body = {"call_id": "u", "type": "ai_transcript", "data": {}}
        self.assertEqual(self._post(body, token="nope")[:2], (404, []))
        self.assertEqual(self._post(body, token=None)[:2], (404, []))

    def test_endpoint_is_off_when_no_token_is_configured(self):
        body = {"call_id": "u", "type": "ai_transcript", "data": {}}
        self.assertEqual(self._post(body, token="", configured="")[:2], (404, []))

    def test_malformed_event_is_400(self):
        self.assertEqual(self._post({"call_id": "u", "data": {}})[:2], (400, []))
        self.assertEqual(self._post({"call_id": "u", "type": "x", "data": "str"})[:2], (400, []))

    def test_a_received_event_is_never_forwarded_again(self):
        status, delivered, forward_calls = self._post(
            {"call_id": "u", "type": "ai_transcript", "data": {}}, forward_url="http://127.0.0.1:1/x")
        self.assertEqual((status, len(delivered), forward_calls), (200, 1, 0))


class TestStartupValidation(unittest.TestCase):
    def test_forward_url_without_token_fails_at_startup(self):
        env = dict(os.environ, PYTHONIOENCODING="utf-8", EVENT_FORWARD_URL="http://127.0.0.1:5001/internal/call-event")
        env.pop("INTERNAL_EVENT_TOKEN", None)
        result = subprocess.run([sys.executable, "-c", "from tests.harness.appctl import app"], cwd=REPO_ROOT,
                                env=env, capture_output=True, text=True, timeout=300)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("EVENT_FORWARD_URL is set but INTERNAL_EVENT_TOKEN is empty", result.stderr)


if __name__ == "__main__":
    unittest.main()
