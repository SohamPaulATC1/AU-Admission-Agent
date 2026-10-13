"""The /api/admin/ routes: access, ranges, filters, CSV (docs/spec/admin-panel-design.md §2)."""

from __future__ import annotations

import csv
import io
import os
import shutil
import sqlite3
import tempfile
import unittest
from datetime import datetime
from unittest import mock

import admin_data
import call_analysis
from tests.harness.appctl import LogCapture, app
from tests.test_access_control import AuthTestCase, run
from tests.test_admin_data import add_analysis, add_call, add_campaign_row

web_auth = app.web_auth


def now_text():
    return datetime.now(app.ist_tz).strftime("%Y-%m-%d %H:%M:%S")


class AdminApiCase(AuthTestCase):
    def seed(self, fn):
        conn = sqlite3.connect(app.CALL_HISTORY_DB_PATH)
        try:
            with conn:
                return fn(conn)
        finally:
            conn.close()

    def call(self, role, method, path, **kwargs):
        async def go():
            client = await self.client(role)
            response = await getattr(client, method)(path, **kwargs)
            return response.status_code, await response.get_json()
        return run(go())

    def raw(self, role, path):
        async def go():
            client = await self.client(role)
            response = await client.get(path)
            return response.status_code, response.headers, await response.get_data()
        return run(go())

    def get(self, role, path):   # noqa: D102 (AuthTestCase.get returns text; the API tests want JSON)
        return self.call(role, "get", path)


class TestAccess(AdminApiCase):
    PATHS = ("/api/admin/overview", "/api/admin/calls", "/api/admin/calls.csv", "/api/admin/calls/u1",
             "/api/admin/campaigns", "/api/admin/campaigns.csv", "/api/admin/dnc", "/api/admin/dnc.csv")

    def setUp(self):
        super().setUp()
        self.seed(lambda conn: add_call(conn, "u1", now_text()))

    def test_no_login_is_401_an_operator_is_403_and_an_admin_gets_in(self):
        for path in self.PATHS:
            self.assertEqual(self.raw(None, path)[0], 401, path)
            self.assertEqual(self.raw("operator", path)[0], 403, path)
            self.assertEqual(self.raw("admin", path)[0], 200, path)


class TestOverviewApi(AdminApiCase):
    def test_the_default_range_is_the_last_seven_days_and_all_time_is_everything(self):
        self.seed(lambda conn: (add_call(conn, "new", now_text(), gemini=0.1),
                                add_call(conn, "old", "2020-01-01 10:00:00", gemini=0.2)))
        status, body = self.get("admin", "/api/admin/overview")
        self.assertEqual((status, body["calls"]), (200, 1))
        self.assertIsNotNone(body["range"]["from"])
        self.assertAlmostEqual(body["cost"]["total"], 0.1)
        status, body = self.get("admin", "/api/admin/overview?all=1")
        self.assertEqual((body["calls"], body["range"]), (2, {"from": None, "to": None}))

    def test_a_custom_range(self):
        self.seed(lambda conn: add_call(conn, "a", "2026-10-05 10:00:00"))
        status, body = self.get("admin", "/api/admin/overview?from=2026-10-05&to=2026-10-05")
        self.assertEqual((status, body["calls"]), (200, 1))
        self.assertEqual(body["range"], {"from": "2026-10-05", "to": "2026-10-05"})

    def test_a_bad_range_is_400_with_a_message(self):
        for query in ("from=yesterday&to=today", "from=2026-10-13&to=2026-10-01", "from=2026-10-01"):
            status, body = self.get("admin", "/api/admin/overview?" + query)
            self.assertEqual(status, 400, query)
            self.assertEqual(body["status"], "error")
            self.assertTrue(body["message"])


class TestCallsApi(AdminApiCase):
    def setUp(self):
        super().setUp()

        def seed(conn):
            add_call(conn, "a", now_text(), name="Asha", gemini=0.1)
            add_call(conn, "b", now_text(), name="Bimal", gemini=0.2)
            add_analysis(conn, "a", lead_status="hot", primary_program="CSE")
        self.seed(seed)

    def test_a_page_with_a_filter(self):
        status, body = self.get("admin", "/api/admin/calls?lead=hot")
        self.assertEqual(status, 200)
        self.assertEqual([r["call_uuid"] for r in body["rows"]], ["a"])
        self.assertEqual((body["total"], body["page"], body["programs"]), (1, 1, ["CSE"]))

    def test_bad_filters_and_pages_are_400(self):
        for query in ("lead=boiling", "analysis=maybe", "visit=x", "campaign=abc", "page=two", "from=bad&to=bad"):
            self.assertEqual(self.get("admin", "/api/admin/calls?" + query)[0], 400, query)

    def test_an_out_of_range_page_is_empty_not_an_error(self):
        status, body = self.get("admin", "/api/admin/calls?page=99999999999")
        self.assertEqual((status, body["rows"]), (200, []))


class TestCsv(AdminApiCase):
    def parse(self, body):
        self.assertTrue(body.startswith(b"\xef\xbb\xbf"), "the file starts with a UTF-8 BOM")
        return list(csv.reader(io.StringIO(body.decode("utf-8-sig"))))

    def test_calls_csv(self):
        self.seed(lambda conn: (add_call(conn, "a", now_text(), name="আশা দাস", gemini=0.1, phone="+919876543210"),
                                add_call(conn, "b", now_text(), name="=HYPERLINK(1)", gemini=0.2)))
        status, headers, body = self.raw("admin", "/api/admin/calls.csv?from=2026-01-01&to=2099-12-31")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/csv; charset=utf-8")
        self.assertEqual(headers["Content-Disposition"], 'attachment; filename="calls_2026-01-01_2099-12-31.csv"')
        rows = self.parse(body)
        self.assertEqual(rows[0], list(admin_data.CALLS_CSV_HEADERS))
        by_id = {r[0]: r for r in rows[1:]}
        self.assertEqual(by_id["a"][2], "আশা দাস")
        self.assertEqual(by_id["a"][3], "+919876543210")   # a phone is not escaped
        self.assertEqual(by_id["b"][2], "'=HYPERLINK(1)")   # a formula is
        self.assertEqual(len(rows), 3)

    def test_the_all_time_file_name(self):
        headers = self.raw("admin", "/api/admin/calls.csv?all=1")[1]
        self.assertEqual(headers["Content-Disposition"], 'attachment; filename="calls_all.csv"')

    def test_filters_apply_to_the_export_and_a_bad_one_is_400(self):
        self.seed(lambda conn: (add_call(conn, "a", now_text()), add_call(conn, "b", now_text()),
                                add_analysis(conn, "a", lead_status="hot")))
        rows = self.parse(self.raw("admin", "/api/admin/calls.csv?lead=hot")[2])
        self.assertEqual([r[0] for r in rows[1:]], ["a"])
        self.assertEqual(self.raw("admin", "/api/admin/calls.csv?lead=boiling")[0], 400)

    def test_a_long_export_is_cut_with_a_note(self):
        self.seed(lambda conn: [add_call(conn, f"u{i}", now_text()) for i in range(5)])
        with mock.patch.object(admin_data, "CSV_MAX_ROWS", 3):
            rows = self.parse(self.raw("admin", "/api/admin/calls.csv")[2])
        self.assertEqual(len(rows), 1 + 3 + 1)
        self.assertEqual(rows[-1], [admin_data.CSV_CUT_NOTE])

    def test_campaigns_and_dnc_csv(self):
        def seed(conn):
            add_call(conn, "c1", now_text(), gemini=0.1)
            add_campaign_row(conn, "October list", now_text(), contacts=[("9000000001", "c1", now_text())])
            admin_data.add_dnc(conn, "9876543210", "=cmd|' /C calc'!A0")
        self.seed(seed)
        status, headers, body = self.raw("admin", "/api/admin/campaigns.csv")
        # Operator 2026-10-13: "call list", not "campaign", in everything people see (the file name too).
        self.assertEqual(headers["Content-Disposition"].split("filename=")[1].startswith('"call_lists_'), True)
        rows = self.parse(body)
        self.assertEqual((rows[0], rows[1][1], rows[1][4]), (list(admin_data.CAMPAIGNS_CSV_HEADERS),
                                                              "October list", "1"))
        status, headers, body = self.raw("admin", "/api/admin/dnc.csv")
        self.assertEqual(headers["Content-Disposition"], 'attachment; filename="do_not_call_all.csv"')
        rows = self.parse(body)
        self.assertEqual((rows[0], rows[1][0], rows[1][2]), (list(admin_data.DNC_CSV_HEADERS), "9876543210",
                                                              "'=cmd|' /C calc'!A0"))


class TestDetailApi(AdminApiCase):
    def setUp(self):
        super().setUp()
        self.folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)
        patcher = mock.patch.object(app.call_transcript, "TRANSCRIPT_DIR", self.folder)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_call_with_its_transcript(self):
        path = os.path.join(self.folder, "a.txt")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("Neha: Hello\n")
        self.seed(lambda conn: (add_call(conn, "a", now_text(), gemini=0.1, transcript_path=path),
                                add_analysis(conn, "a", lead_status="hot", summary="Visit fixed.")))
        status, body = self.get("admin", "/api/admin/calls/a")
        self.assertEqual(status, 200)
        self.assertEqual(body["transcript"], "Neha: Hello\n")
        self.assertEqual(body["analysis"]["lead_status"], "hot")
        self.assertAlmostEqual(body["cost"]["total"], 0.1)

    def test_an_unknown_call_is_404(self):
        status, body = self.get("admin", "/api/admin/calls/nope")
        self.assertEqual((status, body["status"]), (404, "error"))

    def test_an_id_that_tries_to_leave_the_route_is_404(self):
        self.assertEqual(self.get("admin", "/api/admin/calls/..%2F..%2Fetc%2Fpasswd")[0], 404)


class TestCampaignsAndDncApi(AdminApiCase):
    def test_campaign_reports(self):
        def seed(conn):
            add_call(conn, "c1", now_text(), gemini=0.1)
            add_analysis(conn, "c1", lead_status="hot")
            add_campaign_row(conn, "October list", now_text(), contacts=[("9000000001", "c1", now_text())])
        self.seed(seed)
        status, body = self.get("admin", "/api/admin/campaigns")
        self.assertEqual(status, 200)
        self.assertEqual((body["campaigns"][0]["name"], body["campaigns"][0]["hot"]), ("October list", 1))
        self.assertEqual(self.get("admin", "/api/admin/campaigns?from=bad&to=bad")[0], 400)

    def test_dnc_list_and_search(self):
        self.seed(lambda conn: (admin_data.add_dnc(conn, "9876543210", "parent called"),
                                admin_data.add_dnc(conn, "9000000001", "")))
        self.assertEqual(len(self.get("admin", "/api/admin/dnc")[1]["numbers"]), 2)
        self.assertEqual([n["phone"] for n in self.get("admin", "/api/admin/dnc?q=parent")[1]["numbers"]],
                         ["9876543210"])
        self.assertEqual(self.get("admin", "/api/admin/dnc?q=" + "x" * 101)[0], 400)


class WriteCase(AdminApiCase):
    def setUp(self):
        super().setUp()
        self.analyzer = call_analysis.Analyzer(app.CALL_HISTORY_DB_PATH)
        patcher = mock.patch.object(app, "ANALYZER", self.analyzer)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)
        # A re-run only uses a file inside the transcript folder (deviation: same guard as the detail view).
        folder = mock.patch.object(app.call_transcript, "TRANSCRIPT_DIR", self.folder)
        folder.start()
        self.addCleanup(folder.stop)

    def transcript(self, call_uuid):
        """A transcript file for the call; returns its path (store it as transcript_path)."""
        path = os.path.join(self.folder, f"{call_uuid}.txt")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("Neha: Hello\nUser: Yes, tell me\n")
        return path

    def post(self, role, path, body=None):
        return self.call(role, "post", path, json={} if body is None else body)

    def queued(self):
        items = []
        while not self.analyzer._queue.empty():
            items.append(self.analyzer._queue.get_nowait())
        return items


class TestRerunPathGuard(WriteCase):
    def test_a_stored_path_outside_the_transcript_folder_is_never_sent_for_analysis(self):
        outside = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        stray = os.path.join(outside, "x.txt")
        with open(stray, "w", encoding="utf-8") as handle:
            handle.write("User: hello\n")
        self.seed(lambda conn: (add_call(conn, "x", now_text(), transcript_path=stray),
                                add_analysis(conn, "x", "failed")))
        self.assertEqual(self.post("admin", "/api/admin/calls/x/rerun")[0], 409)
        status, body = self.post("admin", "/api/admin/rerun-failed")
        self.assertEqual((status, body["queued"], body["no_transcript"]), (202, 0, 1))
        self.assertEqual(self.queued(), [])


    def test_an_empty_transcript_file_is_not_re_run(self):
        path = os.path.join(self.folder, "e.txt")
        open(path, "w", encoding="utf-8").close()
        self.seed(lambda conn: (add_call(conn, "e", now_text(), transcript_path=path),
                                add_analysis(conn, "e", lead_status="hot")))
        self.assertEqual(self.post("admin", "/api/admin/calls/e/rerun")[0], 409)
        self.assertEqual(self.queued(), [])


class TestAnalyzerIsBusy(unittest.TestCase):
    def test_queued_running_and_idle(self):
        analyzer = call_analysis.Analyzer(":memory:")
        self.assertFalse(analyzer.is_busy("a"))
        analyzer.enqueue("a", None)
        self.assertTrue(analyzer.is_busy("a"))
        analyzer._queued.discard("a")
        analyzer._running["a"] = [None, 1]
        self.assertTrue(analyzer.is_busy("a"))
        analyzer._running.pop("a")
        self.assertFalse(analyzer.is_busy("a"))


class TestRerun(WriteCase):
    PATH = "/api/admin/calls/a/rerun"

    def setUp(self):
        super().setUp()
        self.path = self.transcript("a")
        self.seed(lambda conn: (add_call(conn, "a", now_text(), transcript_path=self.path),
                                add_analysis(conn, "a", lead_status="hot", summary="Visit fixed.")))

    def analysis(self, call_uuid):
        return self.seed(lambda conn: conn.execute("SELECT lead_status, summary FROM call_analysis WHERE call_uuid=?",
                                                   (call_uuid,)).fetchone())

    def test_only_the_admin_may(self):
        self.assertEqual(self.post(None, self.PATH)[0], 401)
        self.assertEqual(self.post("operator", self.PATH)[0], 403)
        self.assertEqual(self.analyzer._queue.qsize(), 0)

    def test_a_non_json_post_is_415(self):
        async def go():
            client = await self.client("admin")
            return (await client.post(self.PATH, data="x=1")).status_code
        self.assertEqual(run(go()), 415)

    def test_unknown_call_is_404_and_a_server_without_a_worker_is_503(self):
        self.assertEqual(self.post("admin", "/api/admin/calls/nope/rerun")[0], 404)
        with mock.patch.object(app, "ANALYZER", None):
            status, body = self.post("admin", self.PATH)
        self.assertEqual((status, body["status"]), (503, "error"))

    def test_it_queues_once_with_the_transcript_and_leaves_the_old_analysis_alone(self):
        self.assertEqual(self.post("admin", self.PATH), (202, {"status": "queued"}))
        status, body = self.post("admin", self.PATH)
        self.assertEqual(status, 409)
        self.assertEqual(self.queued(), [("a", self.path)])
        self.assertEqual(self.analysis("a"), ("hot", "Visit fixed."))

    def test_a_call_without_a_transcript_is_409_and_its_analysis_stays(self):
        # A run with no transcript text would save no_conversation over every field (Analyzer.analyze).
        self.seed(lambda conn: (add_call(conn, "b", now_text()),
                                add_analysis(conn, "b", lead_status="warm", summary="Kept.")))
        status, body = self.post("admin", "/api/admin/calls/b/rerun")
        self.assertEqual(status, 409)
        self.assertIn("transcript", body["message"])
        self.assertEqual(self.analyzer._queue.qsize(), 0)
        self.assertEqual(self.analysis("b"), ("warm", "Kept."))

    def test_a_transcript_file_that_is_gone_is_409(self):
        os.remove(self.path)
        self.assertEqual(self.post("admin", self.PATH)[0], 409)
        self.assertEqual(self.analyzer._queue.qsize(), 0)

    def test_a_call_being_analysed_right_now_is_409(self):
        self.analyzer._running["a"] = [None, 1]
        self.assertEqual(self.post("admin", self.PATH)[0], 409)
        self.assertEqual(self.analyzer._queue.qsize(), 0)


class TestRerunFailed(WriteCase):
    PATH = "/api/admin/rerun-failed"

    def seed_failed(self, count, status="failed", prefix=None, transcript=True):
        prefix = prefix or status

        def seed(conn):
            for i in range(count):
                call_uuid = f"{prefix}{i}"
                path = self.transcript(call_uuid) if transcript else None
                add_call(conn, call_uuid, f"2026-10-12 10:00:{i:02d}", transcript_path=path)
                add_analysis(conn, call_uuid, status)
        self.seed(seed)

    def test_only_the_admin_may(self):
        self.assertEqual(self.post(None, self.PATH)[0], 401)
        self.assertEqual(self.post("operator", self.PATH)[0], 403)

    def test_no_worker_is_503(self):
        with mock.patch.object(app, "ANALYZER", None):
            self.assertEqual(self.post("admin", self.PATH)[0], 503)

    def test_it_queues_the_failed_ones_newest_first_and_not_the_skipped(self):
        self.seed_failed(3)
        self.seed_failed(2, "skipped")
        self.seed_failed(1, "ok")
        status, body = self.post("admin", self.PATH)
        self.assertEqual((status, body["queued"], body["busy"], body["remaining"]), (202, 3, 0, 0))
        self.assertEqual([item[0] for item in self.queued()], ["failed2", "failed1", "failed0"])

    def test_at_most_fifty_per_click_and_it_says_how_many_remain(self):
        self.seed_failed(60)
        status, body = self.post("admin", self.PATH)
        self.assertEqual((body["queued"], body["remaining"]), (50, 10))
        self.assertEqual(len(self.queued()), 50)
        status, body = self.post("admin", self.PATH)   # the first 50 are still queued, so they count as busy
        self.assertEqual((body["queued"], body["busy"], body["remaining"]), (10, 50, 0))

    def test_a_cap_below_the_count(self):
        self.seed_failed(5)
        with mock.patch.object(admin_data, "RERUN_MAX", 2):
            body = self.post("admin", self.PATH)[1]
        self.assertEqual((body["queued"], body["remaining"]), (2, 3))

    def test_nothing_failed_queues_nothing(self):
        body = self.post("admin", self.PATH)[1]
        self.assertEqual((body["queued"], body["busy"], body["remaining"], body["no_transcript"]), (0, 0, 0, 0))

    def test_failed_calls_without_a_transcript_are_counted_not_queued(self):
        self.seed_failed(2)
        self.seed_failed(3, prefix="nofile", transcript=False)
        body = self.post("admin", self.PATH)[1]
        self.assertEqual((body["queued"], body["remaining"], body["no_transcript"]), (2, 0, 3))
        self.assertEqual(sorted(item[0] for item in self.queued()), ["failed0", "failed1"])

    def test_the_calls_list_reports_the_failed_total_for_the_button(self):
        self.seed_failed(3)
        self.assertEqual(self.get("admin", "/api/admin/calls")[1]["failed_total"], 3)


class TestDncWrites(WriteCase):
    def test_only_the_admin_may(self):
        for path in ("/api/admin/dnc", "/api/admin/dnc/remove"):
            self.assertEqual(self.post(None, path, {"phone": "9876543210"})[0], 401, path)
            self.assertEqual(self.post("operator", path, {"phone": "9876543210"})[0], 403, path)

    def test_add_then_list_then_remove_and_the_log_is_masked(self):
        with LogCapture() as log:
            self.assertEqual(self.post("admin", "/api/admin/dnc", {"phone": "+91 98765-43210", "note": "asked"}),
                             (200, {"status": "success", "phone": "9876543210"}))
            self.assertEqual([n["phone"] for n in self.get("admin", "/api/admin/dnc")[1]["numbers"]], ["9876543210"])
            self.assertEqual(self.post("admin", "/api/admin/dnc/remove", {"phone": "9876543210"})[0], 200)
        self.assertEqual(self.get("admin", "/api/admin/dnc")[1]["numbers"], [])
        admin_lines = [line for line in log.lines if "[ADMIN]" in line]
        self.assertEqual(len(admin_lines), 2)
        self.assertTrue(all("xxxxxx3210" in line and "9876543210" not in line for line in admin_lines), admin_lines)

    def test_bad_input(self):
        for body in ({"phone": "12345"}, {"phone": ""}, {}, {"phone": "9876543210", "note": "x" * 201}):
            self.assertEqual(self.post("admin", "/api/admin/dnc", body)[0], 400, body)
        self.assertEqual(self.post("admin", "/api/admin/dnc/remove", {"phone": "junk"})[0], 400)
        self.assertEqual(self.post("admin", "/api/admin/dnc/remove", {"phone": "9876543210"})[0], 404)

    def test_a_non_json_post_is_415_and_a_list_body_is_400(self):
        async def go(**kwargs):
            client = await self.client("admin")
            return (await client.post("/api/admin/dnc", **kwargs)).status_code
        self.assertEqual(run(go(data="phone=9876543210")), 415)
        self.assertEqual(run(go(json=["9876543210"])), 400)

    def test_a_number_added_here_stops_a_campaign_upload(self):
        self.post("admin", "/api/admin/dnc", {"phone": "9876543210"})
        conn = sqlite3.connect(app.CALL_HISTORY_DB_PATH)
        self.addCleanup(conn.close)
        listed = {app.campaigns.normalise_phone(r[0]) for r in conn.execute("SELECT phone FROM do_not_call")}
        self.assertIn("9876543210", listed)


if __name__ == "__main__":
    unittest.main()
