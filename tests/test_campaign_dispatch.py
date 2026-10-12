"""campaigns.py dispatcher: picking, slots, hours, retries, hangups, backstop (docs/spec/campaigns-design.md §2)."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import campaigns
from call_store import IST
from tests.test_campaigns_store import NOW, CampaignCase, add_campaign


def phones(n, start=9000000001):
    return [str(start + i) for i in range(n)]


def at(hour, minute=0):
    return datetime(2026, 10, 13, hour, minute, tzinfo=IST)


class DispatchCase(CampaignCase):
    def due(self, now=NOW):
        return campaigns.due_contacts(self.conn, now)

    def set_contact(self, phone, **values):
        columns = ", ".join(f"{k}=?" for k in values)
        self.conn.execute(f"UPDATE campaign_contacts SET {columns} WHERE phone=?", (*values.values(), phone))


class TestPicking(DispatchCase):
    def test_picks_up_to_the_campaign_concurrency(self):
        add_campaign(self.conn, phones(5), state="running")
        picked = self.due()
        self.assertEqual([p["phone"] for p in picked], ["9000000001", "9000000002"])
        self.assertEqual(picked[0]["attempt"], 1)
        self.assertEqual(self.contact("9000000001")["status"], "calling")
        self.assertEqual(self.contact("9000000001")["attempts"], 1)
        self.assertEqual(self.contact("9000000003")["status"], "pending")

    def test_global_cap_and_round_robin(self):
        first = add_campaign(self.conn, phones(5), {"concurrency": 5}, state="running")
        second = add_campaign(self.conn, phones(5, 9100000001), {"concurrency": 5}, state="running")
        with mock.patch.object(campaigns, "MAX_CONCURRENT", 3):
            picked = self.due()
        self.assertEqual([p["campaign_id"] for p in picked], [first, second, first])

    def test_least_recently_dialled_campaign_goes_first(self):
        first = add_campaign(self.conn, phones(2), state="running")
        second = add_campaign(self.conn, phones(2, 9100000001), state="running")
        self.conn.execute("UPDATE campaigns SET last_dial_at='2026-10-13 09:59:00' WHERE id=?", (first,))
        with mock.patch.object(campaigns, "MAX_CONCURRENT", 1):
            self.assertEqual([p["campaign_id"] for p in self.due()], [second])

    def test_calling_contacts_hold_their_slots(self):
        add_campaign(self.conn, phones(4), state="running")
        self.set_contact("9000000001", status="calling", dialed_at="2026-10-13 09:58:00")
        self.set_contact("9000000002", status="calling", dialed_at="2026-10-13 09:58:00")
        self.assertEqual(self.due(), [])   # after a restart too: the rows are what counts

    def test_live_calls_of_other_campaigns_use_the_global_cap(self):
        paused = add_campaign(self.conn, phones(2), state="paused")
        add_campaign(self.conn, phones(2, 9100000001), state="running")
        self.set_contact("9000000001", status="calling", dialed_at="2026-10-13 09:58:00")
        with mock.patch.object(campaigns, "MAX_CONCURRENT", 2):
            picked = self.due()
        self.assertEqual(len(picked), 1)
        self.assertNotEqual(picked[0]["campaign_id"], paused)

    def test_only_running_campaigns_dial(self):
        for state in ("draft", "paused", "stopped", "done"):
            add_campaign(self.conn, phones(1, 9000000001 + 10 * len(state)), state=state)
        self.assertEqual(self.due(), [])

    def test_no_double_pick(self):
        add_campaign(self.conn, phones(2), state="running")
        self.assertEqual(len(self.due()), 2)
        self.assertEqual(self.due(), [])

    def test_do_not_call_is_cancelled_and_the_next_one_taken(self):
        add_campaign(self.conn, phones(3), {"concurrency": 1}, state="running")
        self.conn.execute("INSERT INTO do_not_call (phone) VALUES ('+91 90000 00001')")
        self.assertEqual([p["phone"] for p in self.due()], ["9000000002"])
        self.assertEqual((self.contact("9000000001")["status"], self.contact("9000000001")["last_cause"]),
                         ("cancelled", "dnc"))

    def test_retry_wait_only_when_due(self):
        add_campaign(self.conn, phones(1), state="running")
        self.set_contact("9000000001", status="retry_wait", attempts=1, next_attempt_at="2026-10-13 10:30:00")
        self.assertEqual(self.due(at(10, 29)), [])
        self.assertEqual(self.due(at(10, 30))[0]["attempt"], 2)


class TestHours(DispatchCase):
    def test_outside_hours_dials_nothing(self):
        add_campaign(self.conn, phones(2), state="running")
        for when in (at(8, 59), at(21, 0), at(23, 30), datetime(2026, 10, 13, 2, 0, tzinfo=IST)):
            self.assertEqual(self.due(when), [], when)
        self.assertEqual(len(self.due(at(20, 59))), 2)

    def test_campaign_hours(self):
        add_campaign(self.conn, phones(1), {"hours_start": "10:00", "hours_end": "12:00"}, state="running")
        self.assertEqual(self.due(at(9, 30)), [])
        self.assertEqual(len(self.due(at(11, 0))), 1)

    def test_env_bounds_win_over_campaign_hours(self):
        add_campaign(self.conn, phones(1), state="running")   # 09:00-21:00 stored
        with mock.patch.object(campaigns, "HOURS_END", "18:00"):
            self.assertEqual(self.due(at(19, 0)), [])

    def test_retry_due_at_night_waits_for_the_window(self):
        add_campaign(self.conn, phones(1), state="running")
        self.set_contact("9000000001", status="retry_wait", attempts=1, next_attempt_at="2026-10-13 22:00:00")
        self.assertEqual(self.due(at(23, 0)), [])
        self.assertEqual(self.due(datetime(2026, 10, 14, 9, 0, tzinfo=IST))[0]["phone"], "9000000001")

    def test_time_zone_is_ist(self):
        add_campaign(self.conn, phones(1), state="running")
        utc_16_00 = datetime(2026, 10, 13, 16, 0, tzinfo=timezone.utc)    # 21:30 IST: outside
        utc_10_30 = datetime(2026, 10, 13, 10, 30, tzinfo=timezone.utc)   # 16:00 IST: inside
        self.assertEqual(self.due(utc_16_00), [])
        self.assertEqual(len(self.due(utc_10_30)), 1)


class TestStillDue(DispatchCase):
    def test_still_due(self):
        campaign_id = add_campaign(self.conn, phones(1), {"hours_end": "18:00"}, state="running")
        self.assertTrue(campaigns.still_due(self.conn, campaign_id, at(17, 59)))
        self.assertFalse(campaigns.still_due(self.conn, campaign_id, at(18, 0)))
        campaigns.apply_action(self.conn, campaign_id, "pause", NOW)
        self.assertFalse(campaigns.still_due(self.conn, campaign_id, NOW))
        self.assertFalse(campaigns.still_due(self.conn, 999, NOW))


class TestDialResults(DispatchCase):
    def test_record_dial(self):
        campaign_id = add_campaign(self.conn, phones(1), state="running")
        self.conn.execute("UPDATE campaigns SET dial_errors=2 WHERE id=?", (campaign_id,))
        [picked] = self.due()
        campaigns.record_dial(self.conn, picked["id"], "req-1")
        self.assertEqual(self.contact("9000000001")["call_uuid"], "req-1")
        self.assertEqual(self.campaign(campaign_id)["dial_errors"], 0)

    def test_dial_error_does_not_count_an_attempt(self):
        add_campaign(self.conn, phones(2), state="running")
        self.set_contact("9000000002", status="retry_wait", attempts=1, next_attempt_at="2026-10-13 09:00:00")
        first, second = self.due()
        self.assertFalse(campaigns.dial_failed(self.conn, first["id"]))
        self.assertFalse(campaigns.dial_failed(self.conn, second["id"]))
        self.assertEqual((self.contact("9000000001")["status"], self.contact("9000000001")["attempts"]), ("pending", 0))
        self.assertEqual((self.contact("9000000002")["status"], self.contact("9000000002")["attempts"],
                          self.contact("9000000002")["next_attempt_at"]), ("retry_wait", 1, "2026-10-13 09:00:00"))

    def test_three_dial_errors_pause_the_campaign(self):
        campaign_id = add_campaign(self.conn, phones(3), {"concurrency": 3}, state="running")
        picked = self.due()
        results = [campaigns.dial_failed(self.conn, p["id"]) for p in picked]
        self.assertEqual(results, [False, False, True])
        self.assertEqual((self.campaign(campaign_id)["state"], self.campaign(campaign_id)["state_reason"]),
                         ("paused", "dial_errors"))
        self.assertEqual({self.contact(p)["status"] for p in phones(3)}, {"pending"})

    def test_dial_error_after_an_operator_pause_does_not_claim_the_pause(self):
        campaign_id = add_campaign(self.conn, phones(3), {"concurrency": 3}, state="running")
        picked = self.due()
        campaigns.dial_failed(self.conn, picked[0]["id"])
        campaigns.dial_failed(self.conn, picked[1]["id"])
        campaigns.apply_action(self.conn, campaign_id, "pause", NOW)   # resets dial_errors too
        self.conn.execute("UPDATE campaigns SET dial_errors=2 WHERE id=?", (campaign_id,))
        self.assertFalse(campaigns.dial_failed(self.conn, picked[2]["id"]))
        self.assertIsNone(self.campaign(campaign_id)["state_reason"])

    def test_release_in_a_stopped_campaign_cancels(self):
        campaign_id = add_campaign(self.conn, phones(1), state="running")
        [picked] = self.due()
        campaigns.apply_action(self.conn, campaign_id, "stop", NOW)
        campaigns.release(self.conn, picked["id"])
        self.assertEqual((self.contact("9000000001")["status"], self.contact("9000000001")["last_cause"]),
                         ("cancelled", "stopped"))

    def test_pick_holds_the_write_lock(self):
        add_campaign(self.conn, phones(1), state="running")
        self.conn.commit()
        self.due()
        self.assertTrue(self.conn.in_transaction)

    def test_release(self):
        add_campaign(self.conn, phones(1), state="running")
        [picked] = self.due()
        campaigns.release(self.conn, picked["id"])
        self.assertEqual((self.contact("9000000001")["status"], self.contact("9000000001")["attempts"]), ("pending", 0))


class TestHangupOutcome(unittest.TestCase):
    def test_answered(self):
        self.assertEqual(campaigns.hangup_outcome({"AnswerTime": "2026-10-13 10:00:05", "HangupCause": "NORMAL_CLEARING"}),
                         "answered")
        self.assertEqual(campaigns.hangup_outcome({"BillDuration": "12", "HangupCause": "USER_BUSY"}), "answered")

    def test_retry(self):
        for form in ({"HangupCause": "NO_ANSWER"}, {"HangupCause": "USER_BUSY", "BillDuration": "0", "AnswerTime": ""},
                     {"HangupCause": "Busy Line"}, {"HangupCause": "Ring Timeout Reached"},
                     {"HangupCause": "ORIGINATOR_CANCEL"}, {"CallStatus": "no-answer"}, {"CallStatus": "BUSY"},
                     {"CallStatus": "timeout"}, {"HangupCause": "no_answer"}, {"HangupCause": "BUSY LINE "}):
            self.assertEqual(campaigns.hangup_outcome(form), "retry", form)

    def test_failed(self):
        for form in ({"HangupCause": "UNALLOCATED_NUMBER"}, {"HangupCause": "NORMAL_CLEARING"}, {},
                     {"BillDuration": "abc", "HangupCause": "CALL_REJECTED"}):
            self.assertEqual(campaigns.hangup_outcome(form), "failed", form)


class TestOnHangup(DispatchCase):
    def setUp(self):
        super().setUp()
        self.campaign_id = add_campaign(self.conn, phones(1), state="running")   # 2 retries, 120 min apart
        [picked] = self.due()
        campaigns.record_dial(self.conn, picked["id"], "req-1")

    def test_not_a_campaign_call(self):
        self.assertIsNone(campaigns.on_hangup(self.conn, "single-call", {"HangupCause": "NO_ANSWER"}, NOW))

    def test_hangup_before_the_dial_was_recorded_is_matched_by_number(self):
        add_campaign(self.conn, phones(1, 9300000001), state="running")
        [picked] = self.due()   # calling, call_uuid not stored yet (place_call still returning)
        result = campaigns.on_hangup(self.conn, "early-1", {"HangupCause": "USER_BUSY", "To": "919300000001"}, NOW)
        self.assertEqual((result["contact_id"], result["status"]), (picked["id"], "retry_wait"))
        self.assertEqual(self.contact("9300000001")["call_uuid"], "early-1")
        campaigns.record_dial(self.conn, picked["id"], "early-1")   # arrives late: harmless
        self.assertEqual(self.contact("9300000001")["status"], "retry_wait")
        self.assertIsNone(campaigns.on_hangup(self.conn, "other", {"HangupCause": "USER_BUSY", "To": "+919999999999"},
                                              NOW))

    def test_number_fallback_ignores_an_old_stuck_pick(self):
        add_campaign(self.conn, phones(1, 9300000001), state="running")
        self.due(NOW - timedelta(minutes=10))   # picked 10 min ago, never recorded (crash)
        self.assertIsNone(campaigns.on_hangup(self.conn, "single", {"HangupCause": "USER_BUSY", "To": "919300000001"},
                                              NOW))
        self.assertEqual(self.contact("9300000001")["status"], "calling")

    def test_second_callback_changes_nothing(self):
        self.assertEqual(campaigns.on_hangup(self.conn, "req-1", {"BillDuration": "40"}, NOW)["status"], "done")
        self.assertIsNone(campaigns.on_hangup(self.conn, "req-1", {"HangupCause": "NO_ANSWER"}, NOW))
        self.assertEqual(self.contact("9000000001")["status"], "done")

    def test_answered_is_done(self):
        result = campaigns.on_hangup(self.conn, "req-1", {"BillDuration": "40", "HangupCause": "NORMAL_CLEARING"}, NOW)
        self.assertEqual((result["status"], result["cause"]), ("done", "NORMAL_CLEARING"))
        self.assertEqual(self.contact("9000000001")["finished_at"], "2026-10-13 10:00:00")

    def test_no_answer_waits_for_a_retry(self):
        campaigns.on_hangup(self.conn, "req-1", {"HangupCause": "NO_ANSWER"}, NOW)
        contact = self.contact("9000000001")
        self.assertEqual((contact["status"], contact["next_attempt_at"], contact["finished_at"]),
                         ("retry_wait", "2026-10-13 12:00:00", None))
        self.assertIsNone(campaigns.on_hangup(self.conn, "req-1", {"HangupCause": "NO_ANSWER"}, NOW))   # once only

    def test_retries_used_up(self):
        self.set_contact("9000000001", attempts=3)   # first dial + 2 retries
        self.assertEqual(campaigns.on_hangup(self.conn, "req-1", {"HangupCause": "USER_BUSY"}, NOW)["status"], "failed")

    def test_hard_failure_is_not_retried(self):
        self.assertEqual(campaigns.on_hangup(self.conn, "req-1", {"HangupCause": "UNALLOCATED_NUMBER"}, NOW)["status"],
                         "failed")

    def test_stopped_campaign_cancels_instead_of_retrying(self):
        campaigns.apply_action(self.conn, self.campaign_id, "stop", NOW)
        result = campaigns.on_hangup(self.conn, "req-1", {"HangupCause": "NO_ANSWER"}, NOW)
        self.assertEqual((result["status"], self.contact("9000000001")["last_cause"]), ("cancelled", "stopped"))

    def test_whole_life_of_a_contact(self):
        campaigns.on_hangup(self.conn, "req-1", {"HangupCause": "NO_ANSWER"}, NOW)
        self.assertEqual(self.due(NOW + timedelta(minutes=119)), [])
        [second] = self.due(NOW + timedelta(minutes=120))
        self.assertEqual(second["attempt"], 2)
        campaigns.record_dial(self.conn, second["id"], "req-2")
        campaigns.on_hangup(self.conn, "req-2", {"AnswerTime": "2026-10-13 12:00:09"}, NOW + timedelta(minutes=125))
        self.assertEqual(self.contact("9000000001")["status"], "done")
        [finished] = campaigns.finish_done(self.conn, NOW + timedelta(minutes=126))
        self.assertEqual((finished["id"], finished["counts"]["done"]), (self.campaign_id, 1))


class TestBackstopAndDone(DispatchCase):
    def test_backstop_ends_a_lost_call_without_retry(self):
        add_campaign(self.conn, phones(2), state="running")
        self.set_contact("9000000001", status="calling", attempts=1, dialed_at="2026-10-13 09:29:00")
        self.set_contact("9000000002", status="calling", attempts=1, dialed_at="2026-10-13 09:31:00")
        self.assertEqual(campaigns.expire_stuck(self.conn, NOW), 1)
        self.assertEqual((self.contact("9000000001")["status"], self.contact("9000000001")["last_cause"]),
                         ("done", "no_hangup_event"))
        self.assertEqual(self.contact("9000000002")["status"], "calling")

    def test_finish_done(self):
        running = add_campaign(self.conn, phones(1), state="running")
        paused = add_campaign(self.conn, phones(1, 9100000001), state="paused")
        stopped = add_campaign(self.conn, phones(1, 9200000001), state="stopped")
        self.set_contact("9000000001", status="failed")
        self.set_contact("9100000001", status="retry_wait")
        self.set_contact("9200000001", status="cancelled")
        self.assertEqual([f["id"] for f in campaigns.finish_done(self.conn, NOW)], [running])
        self.assertEqual(self.campaign(running)["state"], "done")
        self.assertEqual(self.campaign(running)["finished_at"], "2026-10-13 10:00:00")
        self.assertEqual((self.campaign(paused)["state"], self.campaign(stopped)["state"]), ("paused", "stopped"))
        self.assertEqual(campaigns.finish_done(self.conn, NOW), [])


if __name__ == "__main__":
    unittest.main()
