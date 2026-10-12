"""plivo_cost.py: Plivo telephony cost from call records (docs/spec/call-analysis-design.md §2.4)."""

from __future__ import annotations

import asyncio
import os
import shutil
import sqlite3
import tempfile
import types
import unittest
from unittest import mock

import call_store
import plivo_cost
from tests.test_analysis_worker import FakeSleep, LogCaptureFor, make_db, select, write_sync


def run(coro):
    return asyncio.run(coro)


class TestAmount(unittest.TestCase):
    def test_amount(self):
        self.assertEqual(plivo_cost.amount({"total_amount": "0.01667"}), 0.01667)
        self.assertEqual(plivo_cost.amount(types.SimpleNamespace(total_amount="0.5")), 0.5)
        for missing in ({"total_amount": ""}, {"total_amount": None}, {}, {"total_amount": "abc"}, None):
            self.assertIsNone(plivo_cost.amount(missing), missing)


class TestFetch(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)
        self.db = make_db(self.folder)
        flag = mock.patch.object(plivo_cost, "_logged_fields", True)   # the one-time field log is tested below
        flag.start()
        self.addCleanup(flag.stop)

    def fetch(self, records, **kwargs):
        """records: call uuid -> list of replies (dict or Exception), consumed per lookup."""
        self.sleep = FakeSleep()
        lookups = []

        def get_call(uuid):
            lookups.append(uuid)
            reply = records[uuid].pop(0) if len(records[uuid]) > 1 else records[uuid][0]
            if isinstance(reply, Exception):
                raise reply
            return reply

        result = run(plivo_cost.fetch_cost(self.db, "u-1", get_call, sleep=self.sleep, **kwargs))
        return result, lookups

    def cost_row(self):
        return select(self.db, "SELECT plivo_cost_usd, plivo_cost_status FROM calls WHERE call_uuid='u-1'")

    def test_a_leg_only(self):
        result, _ = self.fetch({"u-1": [{"total_amount": "0.01667"}]})
        self.assertEqual(result, 0.01667)
        self.assertEqual(self.cost_row(), {"plivo_cost_usd": 0.01667, "plivo_cost_status": "ok"})
        self.assertEqual(self.sleep.delays, [60])

    def test_human_legs_are_added(self):
        write_sync(self.db, call_store.add_b_leg, "u-1", "b-1")
        result, lookups = self.fetch({"u-1": [{"total_amount": "0.02"}], "b-1": [{"total_amount": "0.03"}]})
        self.assertAlmostEqual(result, 0.05)
        self.assertEqual(lookups, ["u-1", "b-1"])

    def test_retried_until_the_record_has_a_cost(self):
        result, _ = self.fetch({"u-1": [{"total_amount": ""}, RuntimeError("404"), {"total_amount": "0.01"}]})
        self.assertEqual(result, 0.01)
        self.assertEqual(self.sleep.delays, [60, 180, 600])

    def test_gives_up(self):
        with LogCaptureFor(plivo_cost.logger) as log:
            result, _ = self.fetch({"u-1": [{"total_amount": ""}]})
        self.assertIsNone(result)
        self.assertEqual(self.cost_row(), {"plivo_cost_usd": None, "plivo_cost_status": "failed"})
        self.assertTrue(any("💰 [PLIVO-COST] call=u-1 status=failed" in l for l in log.lines))

    def test_record_fields_are_logged_once(self):
        with mock.patch.object(plivo_cost, "_logged_fields", False), LogCaptureFor(plivo_cost.logger) as log:
            self.fetch({"u-1": [{"total_amount": "0.01", "to_number": "9876543210"}]})
            self.fetch({"u-1": [{"total_amount": "0.01"}]})
        field_lines = [l for l in log.lines if "record fields" in l]
        self.assertEqual(field_lines, ["💰 [PLIVO-COST] record fields: ['to_number', 'total_amount']"])
        self.assertFalse(any("9876543210" in l for l in log.lines))
        billing = [l for l in log.lines if "record billing" in l]
        self.assertEqual(len(billing), 1)
        self.assertIn("'total_amount': '0.01'", billing[0])

    def test_a_database_error_is_logged_not_raised(self):
        with mock.patch.object(plivo_cost.call_store, "set_plivo_cost", side_effect=sqlite3.OperationalError("x")), \
                LogCaptureFor(plivo_cost.logger) as log:
            result, _ = self.fetch({"u-1": [{"total_amount": "0.01"}]})
        self.assertIsNone(result)
        self.assertTrue(any("❌ [PLIVO-COST] call=u-1 fetch crashed: OperationalError" in l for l in log.lines))

    def test_schedule_holds_the_task(self):
        async def go():
            task = plivo_cost.schedule(self.db, "u-1", lambda uuid: {"total_amount": "0.01"},
                                       delays=(0,), sleep=FakeSleep())
            self.assertIn(task, plivo_cost._tasks)
            await task
            await asyncio.sleep(0)
            return task in plivo_cost._tasks

        self.assertFalse(run(go()))


if __name__ == "__main__":
    unittest.main()
