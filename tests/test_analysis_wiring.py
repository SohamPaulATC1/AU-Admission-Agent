"""Step 2 wiring in app.py: events, costs, catalog lookups, webhooks, startup (call-analysis-design.md §2)."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import course_catalog
from tests.harness.appctl import DIAGNOSTIC_LOG_TAGS, LogCapture, app, live_call_state
from tests.test_catalog_tool_handler import run_tool_calls

web_auth = app.web_auth


def run(coro):
    return asyncio.run(coro)


class DbCase(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)
        patcher = mock.patch.object(app, "CALL_HISTORY_DB_PATH", os.path.join(self.folder, "calls.db"))
        patcher.start()
        self.addCleanup(patcher.stop)
        app.init_call_history_db()

    def row(self, call_uuid="u-1"):
        conn = sqlite3.connect(app.CALL_HISTORY_DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            found = conn.execute("SELECT * FROM calls WHERE call_uuid=?", (call_uuid,)).fetchone()
            return dict(found) if found else None
        finally:
            conn.close()

    def queued(self, call_uuid="u-1"):
        app.persist_call_event_sync(call_uuid, "call_queued", {"name": "Asha Das", "phone": "9876543210"})


class TestPersistence(DbCase):
    def test_init_upgrades_the_schema(self):
        conn = sqlite3.connect(app.CALL_HISTORY_DB_PATH)
        try:
            names = {r[1] for r in conn.execute("PRAGMA table_info(calls)")}
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            conn.close()
        self.assertIn("gemini_cost_usd", names)
        self.assertIn("call_analysis", tables)

    def test_two_call_ended_events_add_up(self):
        self.queued()
        app.persist_call_event_sync("u-1", "call_ended", {"cost": "$0.0100", "cost_usd": 0.01,
                                                          "reason": "transferred", "programs_looked_up": ["A"]})
        app.persist_call_event_sync("u-1", "call_ended", {"cost": "$0.0200", "cost_usd": 0.02,
                                                          "reason": "done", "programs_looked_up": ["B"]})
        r = self.row()
        self.assertEqual((r["legs"], r["cost"], r["end_reason"], r["status"]), (2, "$0.0300", "done", "ended"))
        self.assertAlmostEqual(r["gemini_cost_usd"], 0.03)
        self.assertEqual(json.loads(r["programs_looked_up"]), ["A", "B"])

    def test_transcript_ready_adds_the_stt_cost(self):
        self.queued()
        app.persist_call_event_sync("u-1", "transcript_ready", {"path": "TRANSCRIPTS/u-1.txt", "stt_cost_usd": 0.0011})
        r = self.row()
        self.assertAlmostEqual(r["stt_cost_usd"], 0.0011)
        self.assertEqual(r["transcript_path"], "TRANSCRIPTS/u-1.txt")


class TestEvents(unittest.TestCase):
    def test_transcript_ready_is_queued_and_never_reaches_browsers(self):
        fake = mock.Mock()

        async def go():
            subscriber = asyncio.Queue()
            app.call_event_subscribers.add(subscriber)
            try:
                with mock.patch.object(app, "ANALYZER", fake), \
                        mock.patch.object(app, "persist_call_event_sync") as persist:
                    app.emit_call_event_local("u-1", "transcript_ready",
                                              {"path": "TRANSCRIPTS/u-1.txt", "stt_cost_usd": 0.001})
                    await asyncio.sleep(0.05)
                return subscriber.qsize(), persist.call_args
            finally:
                app.call_event_subscribers.discard(subscriber)

        size, persisted = run(go())
        self.assertEqual(size, 0)
        fake.enqueue.assert_called_once_with("u-1", "TRANSCRIPTS/u-1.txt")
        self.assertEqual(persisted.args[:2], ("u-1", "transcript_ready"))

    def test_without_an_analyzer_nothing_breaks(self):
        async def go():
            with mock.patch.object(app, "ANALYZER", None), mock.patch.object(app, "persist_call_event_sync"):
                app.emit_call_event_local("u-1", "transcript_ready", {"path": None})
                await asyncio.sleep(0.01)

        run(go())

    def test_without_cost_drops_every_cost_key(self):
        payload = json.dumps({"call_id": "u-1", "type": "call_ended",
                              "data": {"cost": "$0.03", "cost_usd": 0.03, "stt_cost_usd": 0.001, "reason": "done"}})
        self.assertEqual(json.loads(web_auth.without_cost(payload))["data"], {"reason": "done"})

    def test_call_ended_carries_the_numbers(self):
        call_state = live_call_state(with_denoiser=False, with_aec=False, tokens_text_in=2_000_000,
                                     catalog_found=["A"])
        with mock.patch.object(app, "emit_call_event") as emit, LogCapture():
            app.log_call_stats(call_state)
        call_uuid, event_type, data = emit.call_args.args
        self.assertEqual(event_type, "call_ended")
        self.assertEqual(data["programs_looked_up"], ["A"])
        expected = round(2 * app.PRICE_TEXT_INPUT, 6)   # 2M text-in tokens, nothing else
        self.assertAlmostEqual(data["cost_usd"], expected)
        self.assertEqual(data["cost"], f"${expected:.4f}")

    def test_media_process_never_loads_anthropic(self):
        # Spec §4: a media process (EVENT_FORWARD_URL set) imports app without the SDK.
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env = dict(os.environ, EVENT_FORWARD_URL="http://127.0.0.1:8000/internal/call-event",
                   INTERNAL_EVENT_TOKEN="test-token")
        code = "import sys; from tests.harness.appctl import app; print('anthropic' in sys.modules, app.ANALYZER)"
        result = subprocess.run([sys.executable, "-c", code], cwd=repo, env=env, capture_output=True,
                                text=True, timeout=300)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        self.assertEqual(result.stdout.split()[-2:], ["False", "None"])

    def test_log_tags(self):
        self.assertIn("[ANALYSIS]", DIAGNOSTIC_LOG_TAGS)
        self.assertIn("[PLIVO-COST]", DIAGNOSTIC_LOG_TAGS)


class TestCatalogFound(unittest.TestCase):
    def test_found_programs_are_recorded_once(self):
        args = {"programs": ["cse"], "fields": ["fees"]}
        call_state, _, _ = run_tool_calls(("get_program_details", args), ("get_program_details", args),
                                          ("list_programs", {}))
        expected = course_catalog.handle_tool_call("get_program_details", args)["results"][0]["name"]
        self.assertEqual(call_state["catalog_found"], [expected])


class TestFinishTranscript(unittest.TestCase):
    def test_job_with_on_done(self):
        transcript = mock.Mock()
        transcript.has_turns.return_value = True
        with mock.patch.object(app.call_transcript, "start_job") as job, \
                mock.patch.object(app, "emit_call_event") as emit:
            app.finish_transcript({"transcript": transcript, "call_uuid": "u-1"})
            args = job.call_args
            self.assertEqual(args.args, (transcript, "u-1", app.VERTEX_PROJECT, app.vertex_access_token))
            args.kwargs["on_done"]({"path": "p", "stt_cost_usd": 0.001})
        emit.assert_called_once_with("u-1", "transcript_ready", {"path": "p", "stt_cost_usd": 0.001})

    def test_nothing_said_still_reports(self):
        transcript = mock.Mock()
        transcript.has_turns.return_value = False
        with mock.patch.object(app.call_transcript, "start_job") as job, \
                mock.patch.object(app, "emit_call_event") as emit:
            app.finish_transcript({"transcript": transcript, "call_uuid": "u-1"})
        job.assert_not_called()
        emit.assert_called_once_with("u-1", "transcript_ready", {"path": None, "user_turns": 0, "stt_cost_usd": 0.0})

    def test_no_call_uuid_writes_the_file_but_reports_nothing(self):
        for has_turns in (True, False):
            transcript = mock.Mock()
            transcript.has_turns.return_value = has_turns
            with mock.patch.object(app.call_transcript, "start_job") as job, \
                    mock.patch.object(app, "emit_call_event") as emit:
                app.finish_transcript({"transcript": transcript, "call_uuid": None})
            emit.assert_not_called()
            if has_turns:
                self.assertIsNone(job.call_args.kwargs["on_done"])
                self.assertTrue(job.call_args.args[1].startswith("no-uuid-"))

    def test_no_transcript_object_does_nothing(self):
        with mock.patch.object(app.call_transcript, "start_job") as job, \
                mock.patch.object(app, "emit_call_event") as emit:
            app.finish_transcript({"transcript": None, "call_uuid": "u-1"})
        job.assert_not_called()
        emit.assert_not_called()


class TestWebhooks(DbCase):
    def setUp(self):
        super().setUp()
        for patcher in (mock.patch.object(web_auth, "PLIVO_SIGNATURE_CHECK", "off"),
                        mock.patch.object(app, "EVENT_FORWARD_URL", ""),
                        mock.patch.object(app, "PUBLIC_BASE_URL", "https://agent.example.com")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def post(self, path, form):
        async def go():
            return (await app.app.test_client().post(path, form=form)).status_code
        return run(go())

    def test_hangup_schedules_the_plivo_cost(self):
        with mock.patch.object(app.plivo_cost, "schedule") as schedule, \
                mock.patch.object(app, "emit_call_event"):
            self.assertEqual(self.post("/plivo-hangup", {"CallUUID": "h-1", "HangupCause": "NORMAL_CLEARING"}), 200)
        schedule.assert_called_once_with(app.CALL_HISTORY_DB_PATH, "h-1", app.plivo_client.calls.get)

    def test_hangup_in_a_media_process_schedules_nothing(self):
        with mock.patch.object(app, "EVENT_FORWARD_URL", "http://127.0.0.1:8000/internal/call-event"), \
                mock.patch.object(app.plivo_cost, "schedule") as schedule:
            self.post("/plivo-hangup", {"CallUUID": "h-1", "HangupCause": "NORMAL_CLEARING"})
        schedule.assert_not_called()

    def test_dial_action_stores_the_human_leg(self):
        self.queued("c-1")
        form = {"DialBLegStatus": "completed", "CallUUID": "c-1", "From": "+919876543210", "DialBLegUUID": "b-1"}
        self.assertEqual(self.post("/dial-action?call_uuid=c-1", form), 200)
        self.assertEqual(json.loads(self.row("c-1")["plivo_b_legs"]), ["b-1"])

    def test_dial_action_without_a_human_leg(self):
        self.queued("c-1")
        self.post("/dial-action?call_uuid=c-1", {"DialBLegStatus": "no-answer", "CallUUID": "c-1", "DialBLegUUID": ""})
        self.assertIsNone(self.row("c-1")["plivo_b_legs"])


class FakeAnalyzer:
    def __init__(self, db_path, **kwargs):
        self.db_path = db_path
        self.started = "never"

    def start(self, backfill_dir=None):
        self.started = backfill_dir


class TestStartup(DbCase):
    def setUp(self):
        super().setUp()
        for patcher in (mock.patch.object(app, "ANALYZER", None),
                        mock.patch.object(app.call_analysis, "Analyzer", FakeAnalyzer),
                        mock.patch.object(app.call_analysis, "ANALYSIS_ENABLED", True),
                        mock.patch.object(app.plivo_cost, "PLIVO_COST_ENABLED", True)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.queued("u-1")
        app.persist_call_event_sync("u-1", "call_connected", {})
        app.persist_call_event_sync("u-1", "call_ended", {"cost_usd": 0.01})

    def test_main_process_starts_the_worker_and_catches_up(self):
        with mock.patch.object(app, "EVENT_FORWARD_URL", ""), \
                mock.patch.object(app.plivo_cost, "schedule") as schedule:
            run(app.start_background_work())
            self.assertIsInstance(app.ANALYZER, FakeAnalyzer)
            self.assertEqual(app.ANALYZER.started, app.call_transcript.TRANSCRIPT_DIR)
        schedule.assert_called_once_with(app.CALL_HISTORY_DB_PATH, "u-1", app.plivo_client.calls.get,
                                         delays=(0, 180, 600))   # first one at once, then 2 s apart

    def test_media_process_starts_nothing(self):
        with mock.patch.object(app, "EVENT_FORWARD_URL", "http://127.0.0.1:8000/internal/call-event"), \
                mock.patch.object(app.plivo_cost, "schedule") as schedule:
            run(app.start_background_work())
            self.assertIsNone(app.ANALYZER)
        schedule.assert_not_called()

    def test_switches_off(self):
        with mock.patch.object(app, "EVENT_FORWARD_URL", ""), \
                mock.patch.object(app.call_analysis, "ANALYSIS_ENABLED", False), \
                mock.patch.object(app.plivo_cost, "PLIVO_COST_ENABLED", False), \
                mock.patch.object(app.plivo_cost, "schedule") as schedule:
            run(app.start_background_work())
            self.assertIsNone(app.ANALYZER)
        schedule.assert_not_called()

    def test_registered_before_serving(self):
        self.assertIn(app.start_background_work, app.app.before_serving_funcs)


if __name__ == "__main__":
    unittest.main()
