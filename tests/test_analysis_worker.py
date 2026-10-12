"""call_analysis.Analyzer: the Haiku queue in the main process (docs/spec/call-analysis-design.md §2)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sqlite3
import tempfile
import unittest
from unittest import mock

import call_analysis
import call_store
import course_catalog
from tests.test_call_analysis import make_response
from tests.test_call_store import CALLS_TABLE, PROGRAM, SAMPLE_FIELDS

TRANSCRIPT = "call_uuid: u-1\n\n[00:00] Neha: নমস্কার\n[00:05] User: হ্যাঁ বলুন, B.Tech নিয়ে জানতে চাই\n"
NEHA_ONLY = "call_uuid: u-1\n\n[00:00] Neha: Hello, this is Neha\n"


def run(coro):
    return asyncio.run(coro)


def good_reply(fields=None):
    return make_response(json.dumps(fields or SAMPLE_FIELDS))


class FakeStatusError(Exception):
    def __init__(self, status):
        super().__init__(f"status {status}")
        self.status_code = status


class FakeMessages:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []
        self.active = 0
        self.max_active = 0
        self.gate = None

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.gate is not None:
                await self.gate.wait()
            reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
            if isinstance(reply, Exception):
                raise reply
            return reply
        finally:
            self.active -= 1


class FakeClient:
    def __init__(self, replies):
        self.messages = FakeMessages(replies)


class FakeSleep:
    def __init__(self):
        self.delays = []

    async def __call__(self, delay):
        self.delays.append(delay)


class LogCaptureFor:
    def __init__(self, logger):
        self.logger = logger

    def __enter__(self):
        self.lines = []
        capture = self

        class _Handler(logging.Handler):
            def emit(self, record):
                capture.lines.append(record.getMessage())

        self._handler = _Handler(level=logging.INFO)
        self._level = self.logger.level
        self.logger.setLevel(logging.INFO)
        self.logger.addHandler(self._handler)
        return self

    def __exit__(self, *exc_info):
        self.logger.removeHandler(self._handler)
        self.logger.setLevel(self._level)
        return False


def make_db(folder, rows=("u-1",)):
    path = os.path.join(folder, "calls.db")
    conn = sqlite3.connect(path)
    with conn:
        conn.execute(CALLS_TABLE)
        for uuid in rows:
            conn.execute("INSERT INTO calls (call_uuid, name, phone, status, duration_sec, end_reason) "
                         "VALUES (?, 'Asha Das', '9876543210', 'ended', 95, 'call completed')", (uuid,))
        call_store.upgrade(conn)
    conn.close()
    return path


def select(db, sql, args=()):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        found = conn.execute(sql, args).fetchone()
        return dict(found) if found else None
    finally:
        conn.close()


def write_sync(db, fn, *args, **kwargs):
    conn = sqlite3.connect(db)
    try:
        with conn:
            return fn(conn, *args, **kwargs)
    finally:
        conn.close()


class WorkerCase(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)
        self.db = make_db(self.folder)

    def transcript(self, call_uuid="u-1", text=TRANSCRIPT):
        path = os.path.join(self.folder, f"{call_uuid}.txt")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def analyzer(self, client, **kwargs):
        self.sleep = FakeSleep()
        return call_analysis.Analyzer(self.db, client_factory=lambda: client, sleep=self.sleep, **kwargs)

    def analysis(self, call_uuid="u-1"):
        return select(self.db, "SELECT * FROM call_analysis WHERE call_uuid=?", (call_uuid,))

    def call(self, call_uuid="u-1"):
        return select(self.db, "SELECT * FROM calls WHERE call_uuid=?", (call_uuid,))


class TestAnalyze(WorkerCase):
    def test_good_reply_fills_the_row(self):
        client = FakeClient([good_reply()])
        with LogCaptureFor(call_analysis.logger) as log:
            status = run(self.analyzer(client).analyze("u-1", self.transcript()))
        self.assertEqual(status, "ok")
        row = self.analysis()
        school = course_catalog.RECORDS[0]["school"]
        self.assertEqual((row["status"], row["lead_status"], row["school_code"], row["school_name"]),
                         ("ok", "hot", school["code"], school["name"]))
        self.assertEqual((row["transferred"], row["attempts"], row["input_tokens"]), (0, 1, 3000))
        self.assertEqual(json.loads(row["raw_json"]), SAMPLE_FIELDS)
        self.assertAlmostEqual(self.call()["nlp_cost_usd"], 0.000605)
        [request] = client.messages.calls
        self.assertEqual(request["model"], "us.anthropic.claude-haiku-5-5")
        self.assertIn("Contact name: Asha Das", request["messages"][0]["content"])
        [line] = [l for l in log.lines if "[ANALYSIS]" in l]
        self.assertIn("🧠 [ANALYSIS] call=u-1 status=ok lead=hot prospective=yes tokens=3000/500 "
                      "cost=$0.0006", line)
        self.assertIn("school_from=haiku", line)
        for private in ("হ্যাঁ", "Asha", "9876543210"):
            self.assertNotIn(private, line)

    def test_no_caller_speech_needs_no_haiku(self):
        client = FakeClient([good_reply()])
        analyzer = self.analyzer(client)
        self.assertEqual(run(analyzer.analyze("u-1", self.transcript(text=NEHA_ONLY))), "no_conversation")
        self.assertEqual(run(analyzer.analyze("u-1", None)), "no_conversation")
        self.assertEqual(client.messages.calls, [])
        row = self.analysis()
        self.assertEqual((row["status"], row["lead_status"], row["input_tokens"]), ("ok", "no_conversation", 0))

    def test_missing_file_is_skipped(self):
        status = run(self.analyzer(FakeClient([good_reply()])).analyze("u-1", os.path.join(self.folder, "gone.txt")))
        self.assertEqual(status, "skipped")
        self.assertEqual((self.analysis()["status"], self.analysis()["error"]), ("skipped", "transcript missing"))

    def test_school_falls_back_to_the_lookups(self):
        last = course_catalog.RECORDS[-1]
        write_sync(self.db, call_store.add_leg, "u-1", 0.01, [last["name"]])
        client = FakeClient([good_reply(dict(SAMPLE_FIELDS, primary_program=None))])
        with LogCaptureFor(call_analysis.logger) as log:
            run(self.analyzer(client).analyze("u-1", self.transcript()))
        row = self.analysis()
        self.assertEqual((row["primary_program"], row["school_code"]), (None, last["school"]["code"]))
        self.assertTrue(any("school_from=lookup" in l for l in log.lines))

    def test_refusal_is_failed_and_still_costed(self):
        client = FakeClient([make_response(None, stop="refusal", category="cyber", tokens=(1000, 10))])
        self.assertEqual(run(self.analyzer(client).analyze("u-1", self.transcript())), "failed")
        self.assertEqual((self.analysis()["status"], self.analysis()["error"]), ("failed", "refusal: cyber"))
        self.assertAlmostEqual(self.call()["nlp_cost_usd"], (1000 * 0.11 + 10 * 0.55) / 1e6)
        self.assertEqual(len(client.messages.calls), 1, "a refusal is not retried")

    def test_retry_then_success(self):
        client = FakeClient([FakeStatusError(529), good_reply()])
        analyzer = self.analyzer(client)
        self.assertEqual(run(analyzer.analyze("u-1", self.transcript())), "ok")
        self.assertEqual((self.sleep.delays, self.analysis()["attempts"]), ([30], 2))

    def test_retries_exhausted(self):
        client = FakeClient([FakeStatusError(500), FakeStatusError(500), FakeStatusError(500), good_reply()])
        analyzer = self.analyzer(client)
        self.assertEqual(run(analyzer.analyze("u-1", self.transcript())), "failed")
        self.assertEqual(self.sleep.delays, [30, 120])
        row = self.analysis()
        self.assertEqual((row["status"], row["error"], row["attempts"]), ("failed", "FakeStatusError 500", 3))

    def test_bad_request_is_not_retried(self):
        analyzer = self.analyzer(FakeClient([FakeStatusError(400)]))
        self.assertEqual(run(analyzer.analyze("u-1", self.transcript())), "failed")
        self.assertEqual((self.sleep.delays, self.analysis()["attempts"]), ([], 1))

    def test_client_factory_failure_ends_failed(self):
        def no_credentials():
            raise ValueError("no region")

        analyzer = call_analysis.Analyzer(self.db, client_factory=no_credentials, sleep=FakeSleep())
        self.assertEqual(run(analyzer.analyze("u-1", self.transcript())), "failed")
        self.assertEqual((self.analysis()["error"], self.analysis()["attempts"]), ("ValueError", 3))

    def test_missing_sdk_fails_at_once(self):
        def no_sdk():
            raise ModuleNotFoundError("No module named 'anthropic'")

        analyzer = call_analysis.Analyzer(self.db, client_factory=no_sdk, sleep=FakeSleep())
        self.assertEqual(run(analyzer.analyze("u-1", self.transcript())), "failed")
        self.assertEqual((self.analysis()["error"], self.analysis()["attempts"]), ("anthropic not installed", 1))

    def test_client_is_built_off_the_event_loop(self):
        import threading
        built_in = []

        def factory():
            built_in.append(threading.current_thread() is threading.main_thread())
            return FakeClient([good_reply()])

        analyzer = call_analysis.Analyzer(self.db, client_factory=factory, sleep=FakeSleep())
        self.assertEqual(run(analyzer.analyze("u-1", self.transcript())), "ok")
        self.assertEqual(built_in, [False])

    def test_start_without_aws_keys_logs_one_error(self):
        async def go():
            analyzer = self.analyzer(FakeClient([good_reply()]))
            with mock.patch.dict(os.environ, {"AWS_ACCESS_KEY_ID": "", "AWS_PROFILE": ""}):
                analyzer.start()
            await analyzer.stop()

        with LogCaptureFor(call_analysis.logger) as log:
            run(go())
        self.assertEqual(sum("no AWS credentials" in l for l in log.lines), 1)

    def test_missing_aws_keys_fail_at_once(self):
        # anthropic's Bedrock auth raises RuntimeError("could not resolve credentials from session").
        analyzer = self.analyzer(FakeClient([RuntimeError("could not resolve credentials from session")]))
        self.assertEqual(run(analyzer.analyze("u-1", self.transcript())), "failed")
        row = self.analysis()
        self.assertEqual((row["error"], row["attempts"], self.sleep.delays), ("aws credentials", 1, []))

    def test_transferred_from_the_legs(self):
        write_sync(self.db, call_store.add_leg, "u-1", 0.01, [])
        write_sync(self.db, call_store.add_leg, "u-1", 0.01, [])
        run(self.analyzer(FakeClient([good_reply()])).analyze("u-1", self.transcript()))
        self.assertEqual(self.analysis()["transferred"], 1)

    def test_a_silent_return_leg_keeps_the_first_legs_analysis(self):
        # Leg 2 of a transfer had no turns (transcript_ready with path None): re-read the stored file.
        path = self.transcript()
        write_sync(self.db, call_store.add_stt, "u-1", 0.001, path)
        client = FakeClient([good_reply()])
        self.assertEqual(run(self.analyzer(client).analyze("u-1", None)), "ok")
        self.assertEqual(self.analysis()["lead_status"], "hot")
        self.assertEqual(len(client.messages.calls), 1)

    def test_call_without_a_calls_row(self):
        path = self.transcript(call_uuid="u-x")
        self.assertEqual(run(self.analyzer(FakeClient([good_reply()])).analyze("u-x", path)), "ok")
        self.assertEqual(self.analysis("u-x")["lead_status"], "hot")


class TestQueue(WorkerCase):
    def setUp(self):
        super().setUp()   # its database already has u-1
        write_sync(self.db, lambda conn: conn.executemany(
            "INSERT INTO calls (call_uuid, name, status) VALUES (?, 'X', 'ended')", [("u-2",), ("u-3",)]))

    def test_concurrency_limit_and_dedupe(self):
        paths = {u: self.transcript(call_uuid=u) for u in ("u-1", "u-2", "u-3")}
        client = FakeClient([good_reply()])

        async def go():
            client.messages.gate = asyncio.Event()
            analyzer = self.analyzer(client, concurrency=2)
            analyzer.start()
            self.assertTrue(analyzer.enqueue("u-1", paths["u-1"]))
            self.assertFalse(analyzer.enqueue("u-1", paths["u-1"]), "already queued")
            analyzer.enqueue("u-2", paths["u-2"])
            analyzer.enqueue("u-3", paths["u-3"])
            for _ in range(500):
                if client.messages.active == 2:
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(client.messages.active, 2)
            self.assertTrue(analyzer.enqueue("u-1", paths["u-1"]), "running now: a new transcript re-queues it")
            client.messages.gate.set()
            await asyncio.wait_for(analyzer.join(), 5)
            await analyzer.stop()

        run(go())
        self.assertEqual(client.messages.max_active, 2)
        self.assertEqual(len(client.messages.calls), 4)

    def test_a_requeued_call_waits_for_its_running_analysis(self):
        # A transfer's second transcript must not race the first run: the newest file is analysed last.
        path = self.transcript()
        order = []

        async def go():
            analyzer = self.analyzer(FakeClient([good_reply()]), concurrency=2)
            gate = asyncio.Event()

            async def fake_analyze(call_uuid, _path):
                order.append(("start", call_uuid))
                if len(order) == 1:
                    await gate.wait()
                order.append(("end", call_uuid))

            analyzer.analyze = fake_analyze
            analyzer.start()
            analyzer.enqueue("u-1", path)
            for _ in range(500):
                if order:
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(analyzer.enqueue("u-1", path))
            for _ in range(20):
                await asyncio.sleep(0.01)   # the second worker is free, but must wait for the lock
            self.assertEqual(order, [("start", "u-1")])
            gate.set()
            await asyncio.wait_for(analyzer.join(), 5)
            await analyzer.stop()
            return analyzer._running

        self.assertEqual(run(go()), {})
        self.assertEqual(order, [("start", "u-1"), ("end", "u-1"), ("start", "u-1"), ("end", "u-1")])

    def test_backfill_failure_is_logged(self):
        analyzer = self.analyzer(FakeClient([good_reply()]))
        with mock.patch.object(call_analysis, "_transcript_files", side_effect=sqlite3.OperationalError("locked")), \
                LogCaptureFor(call_analysis.logger) as log:
            self.assertEqual(run(analyzer.backfill(self.folder)), 0)
        self.assertTrue(any("❌ [ANALYSIS] backfill failed: OperationalError" in l for l in log.lines))

    def test_worker_survives_a_crash(self):
        async def go():
            analyzer = self.analyzer(FakeClient([good_reply()]))
            analyzer.analyze = mock.AsyncMock(side_effect=[RuntimeError("boom"), "ok"])
            analyzer.start()
            analyzer.enqueue("u-1", "a")
            analyzer.enqueue("u-2", "b")
            await asyncio.wait_for(analyzer.join(), 5)
            await analyzer.stop()
            return analyzer.analyze.await_count

        with LogCaptureFor(call_analysis.logger) as log:
            self.assertEqual(run(go()), 2)
        self.assertTrue(any("❌ [ANALYSIS] crashed call=u-1" in l for l in log.lines))


class TestBackfill(WorkerCase):
    def test_old_transcripts_are_queued_once(self):
        old_line = "transcription_cost_usd: 0.0011 (audio_in=125 text_in=311 output=20 tokens, model=m)\n"
        folder = os.path.join(self.folder, "TRANSCRIPTS")
        os.makedirs(folder)
        for uuid in ("u-1", "u-2"):
            with open(os.path.join(folder, f"{uuid}.txt"), "w", encoding="utf-8") as handle:
                handle.write(TRANSCRIPT + "\n" + old_line)
        with open(os.path.join(folder, "notes.md"), "w", encoding="utf-8") as handle:
            handle.write("not a transcript")
        write_sync(self.db, lambda conn: conn.execute("INSERT INTO calls (call_uuid) VALUES ('u-2')"))
        write_sync(self.db, call_store.save_failure, "u-2", "x", attempts=1)   # finished: not queued

        async def go():
            analyzer = self.analyzer(FakeClient([good_reply()]))
            first = await analyzer.backfill(folder)
            second = await analyzer.backfill(folder)   # u-1 still queued: not twice
            return first, second, analyzer._queue.qsize()

        with LogCaptureFor(call_analysis.logger) as log:
            self.assertEqual(run(go()), (1, 0, 1))
        self.assertAlmostEqual(self.call()["stt_cost_usd"], 0.0011)
        self.assertEqual(self.call()["transcript_path"], os.path.join(folder, "u-1.txt"))
        self.assertTrue(any("🧠 [ANALYSIS] backfill queued=1 of 2" in l for l in log.lines))

    def test_missing_folder(self):
        self.assertEqual(run(self.analyzer(FakeClient([good_reply()])).backfill(os.path.join(self.folder, "no"))), 0)


if __name__ == "__main__":
    unittest.main()
