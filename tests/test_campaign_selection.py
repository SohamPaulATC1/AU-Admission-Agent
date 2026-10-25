"""The tick box per contact (operator, 2026-10-13): only ticked contacts are called; all start ticked."""

from __future__ import annotations

import os
import re
import sqlite3
import types
import unittest
from unittest import mock

import campaigns
from tests.harness.appctl import app
from tests.test_access_control import AuthTestCase
from tests.test_campaign_dispatch import DispatchCase, phones
from tests.test_campaign_wiring import CampaignApiCase, TickCase, run
from tests.test_campaigns_store import NOW, add_campaign
from tests.test_call_store import CALLS_TABLE

PAGE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web", "campaigns.html")


def selected(conn):
    return [bool(r[0]) for r in conn.execute("SELECT selected FROM campaign_contacts ORDER BY id")]


class TestSchema(unittest.TestCase):
    def test_an_old_table_gets_the_column_with_every_contact_ticked(self):
        conn = sqlite3.connect(":memory:")
        conn.execute(CALLS_TABLE)
        old = [c for c in campaigns.CONTACT_COLUMNS if c[0] != "selected"]
        campaigns._create(conn, "campaign_contacts", old)
        conn.execute("INSERT INTO campaign_contacts (campaign_id, phone, status) VALUES (1, '9000000001', 'pending')")
        campaigns.upgrade(conn)
        campaigns.upgrade(conn)   # twice: no duplicate-column error
        self.assertEqual(selected(conn), [True])


class TestSetSelected(DispatchCase):
    def setUp(self):
        super().setUp()
        self.campaign_id = add_campaign(self.conn, phones(3))
        self.ids = [r[0] for r in self.conn.execute("SELECT id FROM campaign_contacts ORDER BY id")]

    def test_every_contact_starts_ticked(self):
        self.assertEqual(selected(self.conn), [True, True, True])
        detail = campaigns.campaign_detail(self.conn, self.campaign_id)
        self.assertEqual((detail["campaign"]["selected"], detail["campaign"]["selectable"]), (3, 3))
        self.assertTrue(all(k["selected"] is True for k in detail["contacts"]))

    def test_untick_one_and_select_all(self):
        self.assertEqual(campaigns.set_selected(self.conn, self.campaign_id, False, [self.ids[1]]), (1, 2, 3))
        self.assertEqual(selected(self.conn), [True, False, True])
        self.assertEqual(campaigns.set_selected(self.conn, self.campaign_id, False), (3, 0, 3))
        self.assertEqual(campaigns.set_selected(self.conn, self.campaign_id, True), (3, 3, 3))

    def test_ids_of_another_list_are_left_alone(self):
        other = add_campaign(self.conn, phones(1, 9100000001))
        (other_id,) = [r[0] for r in self.conn.execute("SELECT id FROM campaign_contacts WHERE campaign_id=?", (other,))]
        self.assertEqual(campaigns.set_selected(self.conn, self.campaign_id, False, [other_id])[0], 0)
        self.assertEqual(selected(self.conn)[-1], True)

    def test_only_in_draft_or_paused(self):
        campaigns.apply_action(self.conn, self.campaign_id, "start", NOW)
        with self.assertRaises(campaigns.BadMove):
            campaigns.set_selected(self.conn, self.campaign_id, False)
        campaigns.apply_action(self.conn, self.campaign_id, "pause", NOW)
        self.assertEqual(campaigns.set_selected(self.conn, self.campaign_id, False, [self.ids[0]])[0], 1)
        with self.assertRaises(campaigns.NotFound):
            campaigns.set_selected(self.conn, 999, False)

    def test_a_called_contact_cannot_be_unticked(self):
        self.conn.execute("UPDATE campaign_contacts SET status='done' WHERE id=?", (self.ids[0],))
        self.assertEqual(campaigns.set_selected(self.conn, self.campaign_id, False), (2, 0, 2))
        self.assertEqual(selected(self.conn)[0], True)

    def test_start_and_resume_need_a_ticked_contact(self):
        campaigns.set_selected(self.conn, self.campaign_id, False)
        with self.assertRaises(campaigns.BadMove):
            campaigns.apply_action(self.conn, self.campaign_id, "start", NOW)
        campaigns.set_selected(self.conn, self.campaign_id, True, [self.ids[2]])
        self.assertEqual(campaigns.apply_action(self.conn, self.campaign_id, "start", NOW), "running")
        campaigns.apply_action(self.conn, self.campaign_id, "pause", NOW)
        campaigns.set_selected(self.conn, self.campaign_id, False)
        with self.assertRaises(campaigns.BadMove):
            campaigns.apply_action(self.conn, self.campaign_id, "resume", NOW)
        self.assertEqual(campaigns.apply_action(self.conn, self.campaign_id, "stop", NOW), "stopped")


class TestDialling(DispatchCase):
    def test_unticked_contacts_are_never_dialled_and_the_list_still_finishes(self):
        campaign_id = add_campaign(self.conn, phones(3), {"concurrency": 3})
        ids = [r[0] for r in self.conn.execute("SELECT id FROM campaign_contacts ORDER BY id")]
        campaigns.set_selected(self.conn, campaign_id, False, [ids[0], ids[2]])
        campaigns.apply_action(self.conn, campaign_id, "start", NOW)
        picked = self.due()
        self.assertEqual([p["id"] for p in picked], [ids[1]])
        self.assertEqual(self.due(), [])
        self.assertEqual(campaigns.finish_done(self.conn, NOW), [])   # one still on a call
        campaigns.record_dial(self.conn, ids[1], "req-1")
        campaigns.on_hangup(self.conn, "req-1", {"BillDuration": "30"}, NOW)
        [done] = campaigns.finish_done(self.conn, NOW)
        self.assertEqual(done["counts"]["cancelled"], 2)
        self.assertEqual({self.contact(p)["last_cause"] for p in ("9000000001", "9000000003")}, {"not_selected"})

    def test_a_paused_list_with_only_unticked_contacts_left_stays_paused(self):
        campaign_id = add_campaign(self.conn, phones(2), state="paused")
        campaigns.set_selected(self.conn, campaign_id, False)
        self.assertEqual(campaigns.finish_done(self.conn, NOW), [])
        self.assertEqual(self.campaign(campaign_id)["state"], "paused")


class TestSelectApi(CampaignApiCase):
    def setUp(self):
        super().setUp()
        self.campaign_id = self.upload()[1]["id"]
        conn = self.db()
        self.contact_id = conn.execute("SELECT id FROM campaign_contacts").fetchone()[0]

    def select(self, body, role="operator", campaign_id=None):
        return self.call(role, "post", f"/api/campaigns/{campaign_id or self.campaign_id}/select", json=body)

    def test_untick_and_select_all_for_both_roles(self):
        self.assertEqual(self.select({"ids": [self.contact_id], "selected": False}),
                         (200, {"status": "success", "changed": 1, "selected": 0, "selectable": 1}))
        self.assertEqual(self.select({"all": True, "selected": True}, role="admin")[1]["selected"], 1)

    def test_bad_requests(self):
        for body in ({"ids": [self.contact_id]}, {"ids": [self.contact_id], "selected": "no"},
                     {"selected": False}, {"ids": ["1"], "selected": False}, {"ids": [True], "selected": False},
                     {"ids": "1", "selected": False}):
            self.assertEqual(self.select(body)[0], 400, body)
        self.assertEqual(self.select({"all": True, "selected": False}, campaign_id=999)[0], 404)
        self.assertEqual(self.call("operator", "post", f"/api/campaigns/{self.campaign_id}/select", form={})[0], 415)
        self.assertEqual(self.call(None, "post", f"/api/campaigns/{self.campaign_id}/select",
                                   json={"all": True, "selected": False})[0], 401)

    def test_locked_while_running(self):
        self.call("operator", "post", f"/api/campaigns/{self.campaign_id}/action", json={"action": "start"})
        self.assertEqual(self.select({"all": True, "selected": False})[0], 409)

    def test_start_with_nobody_ticked_is_409(self):
        self.select({"all": True, "selected": False})
        status, body = self.call("operator", "post", f"/api/campaigns/{self.campaign_id}/action",
                                 json={"action": "start"})
        self.assertEqual(status, 409)
        self.assertIn("No contact is selected", body["message"])


class TestTickSkipsUnticked(TickCase):
    def test_the_dispatcher_calls_only_the_ticked(self):
        campaign_id = self.running(["9000000001", "9000000002"], concurrency=2)
        conn = self.db()
        with conn:
            conn.execute("UPDATE campaign_contacts SET selected=0 WHERE phone='9000000001'")
        with mock.patch.object(app.plivo_client.calls, "create",
                               return_value=types.SimpleNamespace(request_uuid="r")) as create:
            run(app.campaign_tick(NOW))
        self.assertEqual([c.kwargs["to_"] for c in create.call_args_list], ["+919000000002"])
        self.assertEqual(self.contact("9000000001")["status"], "pending")
        self.assertEqual(self.db().execute("SELECT state FROM campaigns WHERE id=?", (campaign_id,)).fetchone()[0],
                         "running")


class TestPage(unittest.TestCase):
    def setUp(self):
        with open(PAGE, encoding="utf-8") as handle:
            self.html = handle.read()

    def test_tick_boxes_select_all_and_the_start_confirm(self):
        self.assertIn('allBox.id = "selectAll"', self.html)
        self.assertIn('changeSelection({ all: true, selected: allBox.checked })', self.html)
        self.assertIn('changeSelection({ ids: [k.id], selected: box.checked })', self.html)
        self.assertIn('"/api/campaigns/" + openId + "/select"', self.html)
        self.assertRegex(self.html, r'if \(action === "start"\) \{[^}]*Start calling ')
        self.assertIn('$("startBtn").disabled = c.selected === 0;', self.html)

    def test_rows_are_numbered_from_one(self):
        self.assertIn("return Number(sheetRow) - 1;", self.html)
        self.assertEqual(re.findall(r"el\(\"td\", (?:k|r)\.sheet_row\)", self.html), [])
        self.assertNotRegex(self.html, r"\[r\.sheet_row,")

    def test_hidden_buttons_really_hide(self):
        self.assertIn("[hidden] { display: none !important; }", self.html)


class TestPageServed(AuthTestCase):
    def test_both_roles_get_the_page(self):
        for role in ("operator", "admin"):
            self.assertEqual(self.get(role, "/campaigns")[0], 200, role)


if __name__ == "__main__":
    unittest.main()
