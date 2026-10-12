"""campaigns.py: tables, settings, state moves and reads (docs/spec/campaigns-design.md §1, §2, §4)."""

from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime
from unittest import mock

import call_store
import campaigns
from call_store import IST
from tests.test_call_store import CALLS_TABLE

NOW = datetime(2026, 10, 13, 10, 0, tzinfo=IST)   # a Tuesday, 10:00 IST

# The spec's defaults, patched in so a .env with CAMPAIGN_* keys cannot change the tests.
BOUNDS = dict(MAX_CONCURRENT=8, DEFAULT_CONCURRENCY=5, HOURS_START="09:00", HOURS_END="21:00",
              MAX_RETRIES=3, MIN_RETRY_GAP_MIN=30, RECENT_DAYS=30, CALL_TIMEOUT_MIN=30, DIAL_GAP_S=0)

SETTINGS = {"concurrency": 2, "hours_start": "09:00", "hours_end": "21:00", "max_retries": 2, "retry_gap_min": 120}


def new_db():
    conn = sqlite3.connect(":memory:")
    conn.execute(CALLS_TABLE)
    call_store.upgrade(conn)
    campaigns.upgrade(conn)
    return conn


def add_campaign(conn, phones, settings=None, state=None, now=NOW):
    contacts = [{"sheet_row": i + 2, "name": f"Contact {i}", "phone": phone, "extra": {"City": "Kolkata"}}
                for i, phone in enumerate(phones)]
    campaign_id = campaigns.create_campaign(conn, "Test list", {**SETTINGS, **(settings or {})},
                                            created_by="operator", file_name="list.xlsx", contacts=contacts,
                                            rejected_count=0, now=now)
    if state:
        conn.execute("UPDATE campaigns SET state=? WHERE id=?", (state, campaign_id))
    return campaign_id


class CampaignCase(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.multiple(campaigns, **BOUNDS)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.conn = new_db()
        self.addCleanup(self.conn.close)

    def contact(self, phone):
        found = self.conn.execute("SELECT status, attempts, last_cause, next_attempt_at, call_uuid, finished_at "
                                  "FROM campaign_contacts WHERE phone=?", (phone,)).fetchone()
        return dict(zip(("status", "attempts", "last_cause", "next_attempt_at", "call_uuid", "finished_at"), found))

    def campaign(self, campaign_id):
        found = self.conn.execute("SELECT state, state_reason, started_at, finished_at, dial_errors "
                                  "FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
        return dict(zip(("state", "state_reason", "started_at", "finished_at", "dial_errors"), found))


class TestSchema(CampaignCase):
    def test_upgrade_twice_is_fine(self):
        campaigns.upgrade(self.conn)
        tables = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"campaigns", "campaign_contacts", "do_not_call"} <= tables)

    def test_same_phone_twice_in_one_campaign_is_refused(self):
        with self.assertRaises(sqlite3.IntegrityError):
            add_campaign(self.conn, ["9876543210", "9876543210"])

    def test_new_campaign_is_a_draft_with_pending_contacts(self):
        campaign_id = add_campaign(self.conn, ["9876543210"])
        self.assertEqual(self.campaign(campaign_id)["state"], "draft")
        self.assertEqual(self.contact("9876543210")["status"], "pending")
        self.assertEqual(self.contact("9876543210")["attempts"], 0)

    def test_masked(self):
        self.assertEqual(campaigns.masked("9876543210"), "xxxxxx3210")


class TestSettings(CampaignCase):
    def test_defaults(self):
        self.assertEqual(campaigns.check_settings({}), {"concurrency": 5, "hours_start": "09:00", "hours_end": "21:00",
                                                        "max_retries": 2, "retry_gap_min": 120})

    def test_limits_carry_bounds_and_defaults(self):
        limits = campaigns.limits()
        self.assertEqual((limits["max_concurrent"], limits["hours_start"], limits["hours_end"]), (8, "09:00", "21:00"))
        self.assertEqual(limits["defaults"], campaigns.check_settings({}))

    def test_concurrency_bounds(self):
        self.assertEqual(campaigns.check_settings({"concurrency": 8})["concurrency"], 8)
        self.assertEqual(campaigns.check_settings({"concurrency": "3"})["concurrency"], 3)
        for bad in (0, 9, -1, True, 2.5, "two", []):
            with self.assertRaises(campaigns.CampaignError, msg=repr(bad)):
                campaigns.check_settings({"concurrency": bad})

    def test_hours_inside_the_env_bounds(self):
        self.assertEqual(campaigns.check_settings({"hours_start": "10:00", "hours_end": "18:30"})["hours_end"], "18:30")
        for start, end in (("08:00", "12:00"), ("10:00", "21:30"), ("12:00", "12:00"), ("14:00", "10:00"),
                           ("9:00", "12:00"), ("10:00", "24:00"), ("ten", "12:00")):
            with self.assertRaises(campaigns.CampaignError, msg=(start, end)):
                campaigns.check_settings({"hours_start": start, "hours_end": end})

    def test_retries_and_gap(self):
        self.assertEqual(campaigns.check_settings({"max_retries": 0})["max_retries"], 0)
        with self.assertRaises(campaigns.CampaignError):
            campaigns.check_settings({"max_retries": 4})
        with self.assertRaises(campaigns.CampaignError):
            campaigns.check_settings({"retry_gap_min": 29})

    def test_require_all_for_an_edit(self):
        with self.assertRaises(campaigns.CampaignError):
            campaigns.check_settings({"concurrency": 2}, require_all=True)
        self.assertEqual(campaigns.check_settings(SETTINGS, require_all=True), SETTINGS)

    def test_name(self):
        self.assertEqual(campaigns.check_name("  October list "), "October list")
        for bad in ("", "   ", None, "x" * 101):
            with self.assertRaises(campaigns.CampaignError):
                campaigns.check_name(bad)


class TestStateMoves(CampaignCase):
    def test_start(self):
        campaign_id = add_campaign(self.conn, ["9876543210"])
        self.assertEqual(campaigns.apply_action(self.conn, campaign_id, "start", NOW), "running")
        self.assertEqual(self.campaign(campaign_id)["started_at"], "2026-10-13 10:00:00")
        with self.assertRaises(campaigns.BadMove):
            campaigns.apply_action(self.conn, campaign_id, "start", NOW)

    def test_pause_and_resume_clear_the_reason(self):
        campaign_id = add_campaign(self.conn, ["9876543210"], state="running")
        self.assertEqual(campaigns.apply_action(self.conn, campaign_id, "pause", NOW), "paused")
        self.conn.execute("UPDATE campaigns SET state_reason='dial_errors', dial_errors=3 WHERE id=?", (campaign_id,))
        self.assertEqual(campaigns.apply_action(self.conn, campaign_id, "resume", NOW), "running")
        self.assertEqual((self.campaign(campaign_id)["state_reason"], self.campaign(campaign_id)["dial_errors"]),
                         (None, 0))

    def test_stop_cancels_only_what_has_not_been_dialled(self):
        campaign_id = add_campaign(self.conn, ["9000000001", "9000000002", "9000000003", "9000000004"],
                                   state="running")
        for phone, status in (("9000000002", "retry_wait"), ("9000000003", "calling"), ("9000000004", "done")):
            self.conn.execute("UPDATE campaign_contacts SET status=? WHERE phone=?", (status, phone))
        self.assertEqual(campaigns.apply_action(self.conn, campaign_id, "stop", NOW), "stopped")
        self.assertEqual([self.contact(p)["status"] for p in ("9000000001", "9000000002", "9000000003", "9000000004")],
                         ["cancelled", "cancelled", "calling", "done"])
        self.assertEqual(self.contact("9000000001")["last_cause"], "stopped")
        self.assertEqual(self.campaign(campaign_id)["finished_at"], "2026-10-13 10:00:00")
        for action in campaigns.ACTIONS:
            with self.assertRaises(campaigns.BadMove):
                campaigns.apply_action(self.conn, campaign_id, action, NOW)

    def test_a_move_raced_by_another_writer_is_a_bad_move(self):
        campaign_id = add_campaign(self.conn, ["9876543210"])
        real_state = campaigns._state

        def stale(conn, cid):   # read "draft", then another request stops it before the write
            seen = real_state(conn, cid)
            conn.execute("UPDATE campaigns SET state='stopped' WHERE id=?", (cid,))
            return seen

        with mock.patch.object(campaigns, "_state", stale):
            with self.assertRaises(campaigns.BadMove):
                campaigns.apply_action(self.conn, campaign_id, "start", NOW)
        self.assertEqual(self.campaign(campaign_id)["state"], "stopped")

    def test_unknown_campaign_and_action(self):
        with self.assertRaises(campaigns.NotFound):
            campaigns.apply_action(self.conn, 999, "start", NOW)
        campaign_id = add_campaign(self.conn, ["9876543210"])
        with self.assertRaises(campaigns.CampaignError) as caught:
            campaigns.apply_action(self.conn, campaign_id, "explode", NOW)
        self.assertNotIsInstance(caught.exception, campaigns.BadMove)

    def test_settings_edit_only_in_draft_or_paused(self):
        campaign_id = add_campaign(self.conn, ["9876543210"])
        changed = {**SETTINGS, "concurrency": 4}
        campaigns.update_settings(self.conn, campaign_id, changed)
        self.assertEqual(self.conn.execute("SELECT concurrency FROM campaigns WHERE id=?", (campaign_id,)).fetchone()[0], 4)
        campaigns.apply_action(self.conn, campaign_id, "start", NOW)
        with self.assertRaises(campaigns.BadMove):
            campaigns.update_settings(self.conn, campaign_id, changed)
        campaigns.apply_action(self.conn, campaign_id, "pause", NOW)
        campaigns.update_settings(self.conn, campaign_id, {**changed, "concurrency": 1})
        with self.assertRaises(campaigns.NotFound):
            campaigns.update_settings(self.conn, 999, changed)


class TestReads(CampaignCase):
    def setUp(self):
        super().setUp()
        self.campaign_id = add_campaign(self.conn, ["9000000001", "9000000002", "9000000003"])
        self.conn.execute("UPDATE campaign_contacts SET status='done', call_uuid='u-1' WHERE phone='9000000001'")
        self.conn.execute("UPDATE campaign_contacts SET status='done', call_uuid='u-2' WHERE phone='9000000002'")
        self.conn.execute("INSERT INTO call_analysis (call_uuid, status, lead_status, campus_visit_day, summary) "
                          "VALUES ('u-1', 'ok', 'hot', 'Saturday', 'Wants B.Tech CSE.')")
        self.conn.execute("INSERT INTO call_analysis (call_uuid, status, lead_status) VALUES ('u-2', 'failed', 'cold')")

    def test_list_has_counts_per_status(self):
        [item] = campaigns.list_campaigns(self.conn)
        self.assertEqual(item["name"], "Test list")
        self.assertEqual(item["counts"], {"pending": 1, "calling": 0, "retry_wait": 0, "done": 2, "failed": 0,
                                          "cancelled": 0})

    def test_detail_joins_only_finished_analyses(self):
        detail = campaigns.campaign_detail(self.conn, self.campaign_id)
        self.assertEqual(detail["total"], 3)
        self.assertEqual(detail["extra_columns"], ["City"])
        first, second, third = detail["contacts"]
        self.assertEqual((first["lead_status"], first["campus_visit_day"], first["summary"]),
                         ("hot", "Saturday", "Wants B.Tech CSE."))
        self.assertIsNone(second["lead_status"])   # the analysis failed: nothing shown
        self.assertEqual(third["extra"], {"City": "Kolkata"})
        self.assertEqual(third["sheet_row"], 4)

    def test_detail_filter_and_pages(self):
        self.assertEqual(campaigns.campaign_detail(self.conn, self.campaign_id, status="pending")["total"], 1)
        with mock.patch.object(campaigns, "PAGE_SIZE", 2):
            self.assertEqual(len(campaigns.campaign_detail(self.conn, self.campaign_id, page=2)["contacts"]), 1)
        with self.assertRaises(campaigns.CampaignError):
            campaigns.campaign_detail(self.conn, self.campaign_id, status="bogus")
        with self.assertRaises(campaigns.NotFound):
            campaigns.campaign_detail(self.conn, 999)

    def test_no_cost_in_any_reply(self):
        self.conn.execute("UPDATE calls SET cost='$1.00'")   # calls is not read; this must stay true
        text = json.dumps([campaigns.list_campaigns(self.conn), campaigns.campaign_detail(self.conn, self.campaign_id)])
        self.assertNotIn("cost", text)


if __name__ == "__main__":
    unittest.main()
