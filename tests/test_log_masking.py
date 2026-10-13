"""No full phone number or contact name in the call logs (docs/spec/admin-panel-design.md §4)."""

from __future__ import annotations

import unittest
from unittest import mock

from tests.harness.appctl import APP_PY, LogCapture, app
from tests.test_access_control import AuthTestCase, run
from tests.test_plivo_checks import PlivoCase

web_auth = app.web_auth
NUMBER = "9876543210"


class TestOutboundWebhookLog(PlivoCase):
    def test_the_answered_line_shows_the_last_four_digits_and_no_name(self):
        with mock.patch.object(web_auth, "PLIVO_SIGNATURE_CHECK", "off"), LogCapture() as log:
            status, _ = self.get(f"/outbound-webhook?user_name=Asha+Das&phone_number={NUMBER}", {})
        self.assertEqual(status, 200)
        (line,) = [l for l in log.lines if "Outbound call answered by" in l]
        self.assertIn("xxxxxx3210", line)
        self.assertFalse([l for l in log.lines if NUMBER in l or "Asha" in l], log.lines)


class TestTriggerCallLog(AuthTestCase):
    def test_the_initiating_line_is_masked(self):
        async def go():
            client = await self.client("operator")
            return (await client.post("/trigger-call", json={"user_name": "Asha Das", "phone_number": NUMBER})).status_code
        with mock.patch.object(app, "place_call", mock.AsyncMock(return_value="uuid-1")), LogCapture() as log:
            self.assertEqual(run(go()), 200)
        (line,) = [l for l in log.lines if "Initiating outbound call" in l]
        self.assertIn("xxxxxx3210", line)
        self.assertFalse([l for l in log.lines if NUMBER in l], log.lines)


class TestMediaStreamLogSource(unittest.TestCase):
    def test_the_connected_line_is_masked(self):
        with open(APP_PY, encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("Caller: {campaigns.masked(phone_number)}", source)
        self.assertNotIn("Caller: {phone_number}", source)


if __name__ == "__main__":
    unittest.main()
