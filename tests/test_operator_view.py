"""The calls page for the operator: no costs, no API key (docs/spec/admin-auth-design.md §2)."""

from __future__ import annotations

import asyncio
import os
import types
import unittest
from unittest import mock

from tests.harness.appctl import REPO_ROOT, app
from tests.test_access_control import AuthTestCase, run


class TestHistoryCost(AuthTestCase):
    def setUp(self):
        super().setUp()
        app.persist_call_event_sync("u-1", "call_queued", {"name": "Asha", "phone": "9876543210"})
        app.persist_call_event_sync("u-1", "call_ended", {"cost": "$0.0123", "reason": "done"})

    def _rows(self, role):
        async def go():
            client = await self.client(role)
            response = await client.get("/api/call-history")
            return (await response.get_json())["calls"]
        return run(go())

    def test_operator_rows_have_no_cost(self):
        [row] = self._rows("operator")
        self.assertEqual(row["call_uuid"], "u-1")
        self.assertNotIn("cost", row)

    def test_admin_rows_keep_the_cost(self):
        [row] = self._rows("admin")
        self.assertEqual(row["cost"], "$0.0123")


class TestLiveFeedCost(AuthTestCase):
    def _first_event(self, role):
        async def go():
            client = await self.client(role)
            before = len(app.call_event_subscribers)
            async with client.request("/call-events") as connection:
                await connection.send_complete()
                for _ in range(200):   # wait until the stream has subscribed
                    if len(app.call_event_subscribers) > before:
                        break
                    await asyncio.sleep(0.01)
                with mock.patch.object(app, "persist_call_event_sync"):
                    app.emit_call_event_local("u-2", "call_ended", {"cost": "$1.5000", "reason": "bye"})
                chunk = await asyncio.wait_for(connection.receive(), 2)
                await connection.disconnect()
            return chunk.decode("utf-8")
        return run(go())

    def test_operator_event_has_no_cost(self):
        chunk = self._first_event("operator")
        self.assertIn('"reason": "bye"', chunk)
        self.assertNotIn("cost", chunk)

    def test_admin_event_keeps_the_cost(self):
        self.assertIn('"cost": "$1.5000"', self._first_event("admin"))


class TestTriggerCall(AuthTestCase):
    def _post(self, role, **kwargs):
        async def go():
            client = await self.client(role)
            response = await client.post("/trigger-call", **kwargs)
            return response.status_code, await response.get_json()
        made = types.SimpleNamespace(request_uuid="req-1")
        with mock.patch.object(app.plivo_client.calls, "create", return_value=made) as create, \
                mock.patch.object(app, "emit_call_event"):
            result = run(go())
        return result, create

    def test_operator_session_is_enough(self):
        (status, body), create = self._post("operator", json={"user_name": "Asha", "phone_number": "9876543210"})
        self.assertEqual(status, 200)
        self.assertEqual(body["call_uuid"], "req-1")
        self.assertEqual(create.call_args.kwargs["to_"], "+919876543210")

    def test_form_body_is_refused(self):
        (status, _), create = self._post("operator", form={"user_name": "Asha", "phone_number": "9876543210"})
        self.assertEqual(status, 415)
        create.assert_not_called()

    def test_non_object_json_is_refused(self):
        (status, _), create = self._post("operator", json=["9876543210"])
        self.assertEqual(status, 400)
        create.assert_not_called()

    def test_api_key_is_gone(self):
        self.assertFalse(hasattr(app, "API_AUTH_TOKEN"))


class TestDashboardPage(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(REPO_ROOT, "index.html"), encoding="utf-8") as handle:
            self.html = handle.read()

    def test_no_browser_secret(self):
        self.assertNotIn("default-dev-token", self.html)
        self.assertNotIn("X-API-Key", self.html)

    def test_login_aware(self):
        self.assertIn('action="/logout"', self.html)
        self.assertIn('id="adminLink"', self.html)
        self.assertIn('fetch("/api/me")', self.html)
        self.assertGreaterEqual(self.html.count('window.location.href = "/login"'), 3)


if __name__ == "__main__":
    unittest.main()
