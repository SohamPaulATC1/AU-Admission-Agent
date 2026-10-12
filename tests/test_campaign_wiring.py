"""Step 3 wiring in app.py: routes, the tick, the hangup hook, startup (docs/spec/campaigns-design.md §2, §4, §5)."""

from __future__ import annotations

import asyncio
import base64
import json
import sqlite3
import types
import unittest
from datetime import datetime
from unittest import mock

from call_store import IST
from tests.harness.appctl import DIAGNOSTIC_LOG_TAGS, LogCapture, app
from tests.test_access_control import AuthTestCase, run
from tests.test_analysis_wiring import DbCase
from tests.test_campaign_upload import make_xlsx
from tests.test_campaigns_store import BOUNDS, NOW, add_campaign

web_auth = app.web_auth
FILE = make_xlsx([["Name", "Phone", "City"], ["Asha", "9876543210", "Kolkata"], ["Bad", "12345", "Howrah"]])


def b64(data):
    return base64.b64encode(data).decode()


class BoundsMixin:
    def patch_bounds(self):
        patcher = mock.patch.multiple(app.campaigns, **BOUNDS)
        patcher.start()
        self.addCleanup(patcher.stop)

    def db(self):
        conn = sqlite3.connect(app.CALL_HISTORY_DB_PATH)
        self.addCleanup(conn.close)
        return conn

    def contact(self, phone):
        found = self.db().execute("SELECT status, attempts, call_uuid, last_cause FROM campaign_contacts WHERE phone=?",
                                  (phone,)).fetchone()
        return dict(zip(("status", "attempts", "call_uuid", "last_cause"), found))


class CampaignApiCase(BoundsMixin, AuthTestCase):
    def setUp(self):
        super().setUp()
        self.patch_bounds()

    def call(self, role, method, path, **kwargs):
        async def go():
            client = await self.client(role)
            response = await getattr(client, method)(path, **kwargs)
            return response.status_code, await response.get_json()
        return run(go())

    def upload(self, role="operator", **overrides):
        body = {"name": "October list", "file_name": "list.xlsx", "file_b64": b64(FILE), **overrides}
        return self.call(role, "post", "/api/campaigns", json=body)


class TestAccess(CampaignApiCase):
    def test_pages_and_apis_need_a_login(self):
        self.assertEqual(self.get(None, "/campaigns")[:2], (302, "/login"))
        self.assertEqual(self.get(None, "/api/campaigns")[0], 401)
        self.assertEqual(self.get(None, "/api/campaigns/1")[0], 401)
        for role in ("operator", "admin"):
            self.assertEqual(self.get(role, "/campaigns")[0], 200, role)
            self.assertEqual(self.get(role, "/api/campaigns")[0], 200, role)

    def test_posts_need_a_login(self):
        self.assertEqual(self.call(None, "post", "/api/campaigns", json={})[0], 401)
        self.assertEqual(self.call(None, "post", "/api/campaigns/1/action", json={"action": "start"})[0], 401)
        self.assertEqual(self.call(None, "post", "/api/campaigns/1/settings", json={})[0], 401)

    def test_every_campaign_route_is_open_to_both_roles(self):
        for endpoint in ("campaigns_page", "list_campaigns_api", "create_campaign_api", "campaign_detail_api",
                         "campaign_action_api", "campaign_settings_api"):
            self.assertIn(web_auth.ENDPOINT_ACCESS[endpoint], (web_auth.OPERATOR_PAGE, web_auth.OPERATOR_API))


class TestUpload(CampaignApiCase):
    def test_upload_creates_a_draft(self):
        status, body = self.upload()
        self.assertEqual(status, 200, body)
        self.assertEqual((body["accepted"], body["extra_columns"], body["extras_dropped"]), (1, ["City"], 0))
        self.assertEqual(body["rejected"], [{"sheet_row": 3, "name": "Bad", "phone": "12345", "reason": "bad_number"}])
        state, created_by, file_name = self.db().execute("SELECT state, created_by, file_name FROM campaigns").fetchone()
        self.assertEqual((state, created_by, file_name), ("draft", "operator", "list.xlsx"))

    def test_admin_can_upload_too(self):
        self.assertEqual(self.upload("admin")[0], 200)

    def test_json_only(self):
        self.assertEqual(self.call("operator", "post", "/api/campaigns", form={"name": "x"})[0], 415)
        self.assertEqual(self.call("operator", "post", "/api/campaigns", json=["x"])[0], 400)

    def test_bad_inputs_are_400_and_store_nothing(self):
        for overrides in ({"file_b64": "not base64!!"}, {"file_b64": b64(b"name,phone")}, {"name": ""},
                          {"concurrency": 9}, {"hours_start": "07:00"}):
            status, body = self.upload(**overrides)
            self.assertEqual(status, 400, overrides)
            self.assertEqual(body["status"], "error")
        self.assertEqual(self.db().execute("SELECT COUNT(*) FROM campaigns").fetchone()[0], 0)

    def test_all_rejected_file_lists_the_rows(self):
        status, body = self.upload(file_b64=b64(make_xlsx([["Name", "Phone"], ["A", "123"]])))
        self.assertEqual((status, [r["reason"] for r in body["rejected"]]), (400, ["bad_number"]))


class TestControls(CampaignApiCase):
    def setUp(self):
        super().setUp()
        self.campaign_id = self.upload()[1]["id"]

    def action(self, action, campaign_id=None, role="operator"):
        return self.call(role, "post", f"/api/campaigns/{campaign_id or self.campaign_id}/action", json={"action": action})

    def test_moves(self):
        self.assertEqual(self.action("start"), (200, {"status": "success", "state": "running"}))
        self.assertEqual(self.action("start")[0], 409)
        self.assertEqual(self.action("pause", role="admin")[1]["state"], "paused")
        self.assertEqual(self.action("explode")[0], 400)
        self.assertEqual(self.action("start", campaign_id=999)[0], 404)
        self.assertEqual(self.call("operator", "post", f"/api/campaigns/{self.campaign_id}/action", form={})[0], 415)

    def test_settings(self):
        path = f"/api/campaigns/{self.campaign_id}/settings"
        good = {"concurrency": 3, "hours_start": "10:00", "hours_end": "18:00", "max_retries": 1, "retry_gap_min": 60}
        self.assertEqual(self.call("operator", "post", path, json=good)[0], 200)
        self.assertEqual(self.call("operator", "post", path, json={**good, "concurrency": 9})[0], 400)
        self.assertEqual(self.call("operator", "post", path, json={"concurrency": 3})[0], 400)   # all five required
        self.action("start")
        self.assertEqual(self.call("operator", "post", path, json=good)[0], 409)
        self.assertEqual(self.call("operator", "post", "/api/campaigns/999/settings", json=good)[0], 404)

    def test_detail_and_list_show_the_lead_but_never_a_cost(self):
        conn = self.db()
        with conn:
            conn.execute("UPDATE campaign_contacts SET status='done', call_uuid='u-1'")
            conn.execute("INSERT INTO calls (call_uuid, name, phone, cost, gemini_cost_usd) "
                         "VALUES ('u-1', 'Asha', '9876543210', '$0.14', 0.14)")
            conn.execute("INSERT INTO call_analysis (call_uuid, status, lead_status, campus_visit_day, summary) "
                         "VALUES ('u-1', 'ok', 'hot', 'Saturday', 'Wants B.Tech.')")
        for role in ("operator", "admin"):
            status, detail = self.call(role, "get", f"/api/campaigns/{self.campaign_id}")
            self.assertEqual(status, 200)
            self.assertEqual(detail["contacts"][0]["lead_status"], "hot")
            _, listing = self.call(role, "get", "/api/campaigns")
            self.assertEqual(listing["limits"]["max_concurrent"], 8)
            self.assertNotIn("cost", json.dumps([detail, listing]))
        self.assertEqual(self.call("operator", "get", f"/api/campaigns/{self.campaign_id}?status=bogus")[0], 400)
        self.assertEqual(self.call("operator", "get", f"/api/campaigns/{self.campaign_id}?page=x")[0], 200)
        self.assertEqual(self.call("operator", "get",
                                   f"/api/campaigns/{self.campaign_id}?page=99999999999999999999")[0], 200)
        self.assertEqual(self.call("operator", "get", "/api/campaigns/999")[0], 404)


class TickCase(BoundsMixin, DbCase):
    def setUp(self):
        super().setUp()
        self.patch_bounds()
        base_url = mock.patch.object(app, "PUBLIC_BASE_URL", "https://agent.example.com")
        base_url.start()
        self.addCleanup(base_url.stop)
        emit = mock.patch.object(app, "emit_call_event")
        self.emit = emit.start()
        self.addCleanup(emit.stop)

    def running(self, phones, **settings):
        conn = self.db()
        with conn:
            return add_campaign(conn, phones, settings, state="running")


class TestTick(TickCase):
    def test_tick_dials_and_stores_the_call_uuid(self):
        self.running(["9876543210"])
        made = types.SimpleNamespace(request_uuid="req-1")
        with mock.patch.object(app.plivo_client.calls, "create", return_value=made) as create, LogCapture() as log:
            run(app.campaign_tick(NOW))
        self.assertEqual(create.call_args.kwargs["to_"], "+919876543210")
        self.assertIn("/outbound-webhook?", create.call_args.kwargs["answer_url"])
        self.assertEqual(create.call_args.kwargs["hangup_url"], "https://agent.example.com/plivo-hangup")
        self.assertEqual(self.contact("9876543210"), {"status": "calling", "attempts": 1, "call_uuid": "req-1",
                                                      "last_cause": None})
        self.emit.assert_called_once_with("req-1", "call_queued", {"name": "Contact 0", "phone": "9876543210"})
        dial_lines = [line for line in log.lines if "[CAMPAIGN] dial" in line]
        self.assertEqual(len(dial_lines), 1, log.lines)
        self.assertIn("xxxxxx3210", dial_lines[0])
        self.assertFalse(any("9876543210" in line or "Contact 0" in line for line in log.lines), log.lines)

    def test_the_excel_name_never_reaches_the_answer_url(self):
        # Spec decision 4: the answer URL's user_name is put into Gemini's prompt and greeting.
        self.running(["9876543210"])
        with mock.patch.object(app.plivo_client.calls, "create", return_value=types.SimpleNamespace(request_uuid="r")) as create:
            run(app.campaign_tick(NOW))
        self.assertIn("user_name=Unknown&", create.call_args.kwargs["answer_url"])
        self.assertNotIn("Contact", create.call_args.kwargs["answer_url"])

    def test_empty_name_goes_to_plivo_as_unknown(self):
        campaign_id = self.running(["9876543210"])
        conn = self.db()
        with conn:
            conn.execute("UPDATE campaign_contacts SET name='' WHERE campaign_id=?", (campaign_id,))
        with mock.patch.object(app.plivo_client.calls, "create", return_value=types.SimpleNamespace(request_uuid="r")) as create:
            run(app.campaign_tick(NOW))
        self.assertIn("user_name=Unknown", create.call_args.kwargs["answer_url"])

    def test_three_dial_errors_pause_and_spare_the_contacts(self):
        campaign_id = self.running(["9000000001", "9000000002", "9000000003", "9000000004"], concurrency=4)
        with mock.patch.object(app.plivo_client.calls, "create", side_effect=RuntimeError("plivo down")) as create, \
                LogCapture() as log:
            run(app.campaign_tick(NOW))
        self.assertEqual(create.call_count, 3)
        state, reason = self.db().execute("SELECT state, state_reason FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
        self.assertEqual((state, reason), ("paused", "dial_errors"))
        self.assertEqual({(self.contact(p)["status"], self.contact(p)["attempts"])
                          for p in ("9000000001", "9000000002", "9000000003", "9000000004")}, {("pending", 0)})
        self.assertFalse(any("plivo down" in line for line in log.lines))   # error text can hold a number

    def test_failed_record_dial_releases_the_undialled_picks(self):
        # Approved deviation 2: a DB error mid-tick must not strand undialled picks in 'calling'
        # (the 30-min backstop would wrongly mark them done). The one that was really dialled stays calling.
        phones = ["9000000001", "9000000002", "9000000003"]
        self.running(phones, concurrency=3)
        made = types.SimpleNamespace(request_uuid="req-1")
        with mock.patch.object(app.plivo_client.calls, "create", return_value=made) as create, \
                mock.patch.object(app.campaigns, "record_dial",
                                  side_effect=sqlite3.OperationalError("database is locked")), \
                LogCapture():
            with self.assertRaises(sqlite3.OperationalError):
                run(app.campaign_tick(NOW))
        self.assertEqual(create.call_count, 1)
        dialled = create.call_args.kwargs["to_"][3:]
        self.assertEqual((self.contact(dialled)["status"], self.contact(dialled)["attempts"]), ("calling", 1))
        self.assertEqual({(self.contact(p)["status"], self.contact(p)["attempts"]) for p in phones if p != dialled},
                         {("pending", 0)})

    def test_stop_during_a_tick_stops_the_remaining_dials(self):
        # Dials are spaced out; a Stop pressed between two of them must win (spec §2: nothing new is dialled).
        campaign_id = self.running(["9000000001", "9000000002", "9000000003"], concurrency=3)
        made = types.SimpleNamespace(request_uuid="req-1")

        def create(**kwargs):
            conn = sqlite3.connect(app.CALL_HISTORY_DB_PATH)
            with conn:
                app.campaigns.apply_action(conn, campaign_id, "stop", NOW)
            conn.close()
            return made

        with mock.patch.object(app.plivo_client.calls, "create", side_effect=create) as plivo, LogCapture():
            run(app.campaign_tick(NOW))
        self.assertEqual(plivo.call_count, 1)
        self.assertEqual(sorted(self.contact(p)["status"] for p in ("9000000001", "9000000002", "9000000003")),
                         ["calling", "cancelled", "cancelled"])

    def test_tick_outside_hours_dials_nothing(self):
        self.running(["9876543210"])
        with mock.patch.object(app.plivo_client.calls, "create") as create:
            run(app.campaign_tick(datetime(2026, 10, 13, 22, 0, tzinfo=IST)))
        create.assert_not_called()

    def test_tick_runs_the_backstop_and_finishes_campaigns(self):
        campaign_id = self.running(["9876543210"])
        conn = self.db()
        with conn:
            conn.execute("UPDATE campaign_contacts SET status='calling', attempts=1, dialed_at='2026-10-13 09:00:00'")
        with mock.patch.object(app.plivo_client.calls, "create") as create:
            run(app.campaign_tick(NOW))
        create.assert_not_called()
        self.assertEqual(self.contact("9876543210")["last_cause"], "no_hangup_event")
        self.assertEqual(self.db().execute("SELECT state FROM campaigns WHERE id=?", (campaign_id,)).fetchone()[0], "done")

    def test_loop_survives_a_failing_tick(self):
        calls = []

        async def failing_tick():
            calls.append(1)
            raise sqlite3.OperationalError("database is locked")

        async def go():
            with mock.patch.object(app, "campaign_tick", failing_tick), mock.patch.object(app.campaigns, "TICK_S", 0):
                try:
                    await asyncio.wait_for(app.campaign_loop(), 0.2)
                except asyncio.TimeoutError:
                    pass

        with LogCapture() as log:
            run(go())
        self.assertGreaterEqual(len(calls), 2)
        self.assertTrue(any("[CAMPAIGN] tick failed" in line for line in log.lines))


class TestHangupHook(TickCase):
    def setUp(self):
        super().setUp()
        for patcher in (mock.patch.object(web_auth, "PLIVO_SIGNATURE_CHECK", "off"),
                        mock.patch.object(app, "EVENT_FORWARD_URL", ""),
                        mock.patch.object(app, "_hangup_fields_logged", False),
                        mock.patch.object(app.plivo_cost, "schedule")):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.running(["9876543210"])
        conn = self.db()
        with conn:
            conn.execute("UPDATE campaign_contacts SET status='calling', attempts=1, call_uuid='req-1', "
                         "dialed_at='2026-10-13 09:59:00'")

    def post(self, form):
        async def go():
            status = (await app.app.test_client().post("/plivo-hangup", form=form)).status_code
            await asyncio.gather(*app._background_tasks)
            return status
        return run(go())

    def test_hangup_webhook_settles_the_contact(self):
        with LogCapture() as log:
            self.assertEqual(self.post({"CallUUID": "req-1", "HangupCause": "NO_ANSWER", "CallStatus": "no-answer",
                                        "BillDuration": "0", "AnswerTime": "", "To": "+919876543210"}), 200)
        self.assertEqual(self.contact("9876543210")["status"], "retry_wait")
        fields = [line for line in log.lines if "[CAMPAIGN] hangup fields" in line]
        self.assertEqual(len(fields), 1)
        self.assertIn("HangupCause", fields[0])
        self.assertFalse(any("9876543210" in line for line in log.lines if "[CAMPAIGN]" in line))

    def test_single_call_hangup_changes_nothing(self):
        self.post({"CallUUID": "other", "HangupCause": "NO_ANSWER"})
        self.assertEqual(self.contact("9876543210")["status"], "calling")

    def test_media_process_does_not_write(self):
        with mock.patch.object(app, "EVENT_FORWARD_URL", "http://127.0.0.1:8000/internal/call-event"):
            self.post({"CallUUID": "req-1", "HangupCause": "NO_ANSWER"})
        self.assertEqual(self.contact("9876543210")["status"], "calling")


class TestStartup(DbCase):
    def setUp(self):
        super().setUp()
        for patcher in (mock.patch.object(app, "CAMPAIGN_TASK", None),
                        mock.patch.object(app, "ANALYZER", None),
                        mock.patch.object(app.call_analysis, "ANALYSIS_ENABLED", False),
                        mock.patch.object(app.plivo_cost, "PLIVO_COST_ENABLED", False)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def started(self, forward, enabled):
        async def go():
            with mock.patch.object(app, "EVENT_FORWARD_URL", forward), \
                    mock.patch.object(app.campaigns, "CAMPAIGNS_ENABLED", enabled):
                await app.start_background_work()
                task = app.CAMPAIGN_TASK
                if task is not None:
                    task.cancel()
                return isinstance(task, asyncio.Task)
        return run(go())

    def test_main_process_starts_the_dispatcher(self):
        with LogCapture() as log:
            self.assertTrue(self.started("", True))
        self.assertTrue(any("[CAMPAIGN] dispatcher started" in line for line in log.lines))

    def test_switched_off_or_media_process_starts_nothing(self):
        self.assertFalse(self.started("", False))
        self.assertFalse(self.started("http://127.0.0.1:8000/internal/call-event", True))

    def test_main_process_uses_wal(self):
        with mock.patch.object(app, "EVENT_FORWARD_URL", ""):
            app.init_call_history_db()
        conn = sqlite3.connect(app.CALL_HISTORY_DB_PATH)
        try:
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            conn.close()
        self.assertTrue({"campaigns", "campaign_contacts", "do_not_call"} <= tables)

    def test_log_tag(self):
        self.assertIn("[CAMPAIGN]", DIAGNOSTIC_LOG_TAGS)


if __name__ == "__main__":
    unittest.main()
