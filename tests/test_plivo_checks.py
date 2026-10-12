"""Plivo webhook signatures and signed /media-stream URLs (docs/spec/admin-auth-design.md §3)."""

from __future__ import annotations

import asyncio
import re
import time
import unittest
from unittest import mock

from quart import websocket
from quart.testing import WebsocketResponseError

from tests.harness.appctl import LogCapture, app
from tests.test_web_auth import BASE, PLIVO_TOKEN, parse_stream_query, plivo_headers

web_auth = app.web_auth
STREAM_URL = re.compile(r"(wss://\S+?/media-stream\?[^\s<]+)")


def run(coro):
    return asyncio.run(coro)


async def _stub_media_stream():
    await websocket.send("accepted")


class PlivoCase(unittest.TestCase):
    def setUp(self):
        patchers = [
            mock.patch.object(web_auth, "PLIVO_AUTH_TOKEN", PLIVO_TOKEN),
            mock.patch.object(web_auth, "APP_SECRET", "s" * 40),
            mock.patch.object(web_auth, "PLIVO_SIGNATURE_CHECK", "enforce"),
            mock.patch.object(app, "PUBLIC_BASE_URL", BASE),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def post(self, path, form, headers):
        async def go():
            response = await app.app.test_client().post(path, form=form, headers=headers)
            return response.status_code, await response.get_data(as_text=True)
        return run(go())

    def get(self, path, headers):
        async def go():
            response = await app.app.test_client().get(path, headers=headers)
            return response.status_code, await response.get_data(as_text=True)
        return run(go())

    def connect(self, query):
        """Open /media-stream with the real hooks but a stub handler (no Gemini, no Plivo)."""
        async def go():
            with mock.patch.dict(app.app.view_functions, {"handle_media_stream": _stub_media_stream}):
                try:
                    async with app.app.test_client().websocket("/media-stream?" + query) as ws:
                        return await ws.receive()
                except WebsocketResponseError as error:
                    return error.response.status_code
        return run(go())


class TestWebhookSignatures(PlivoCase):
    def _ringing(self, headers, form=None):
        try:
            status, _ = self.post("/plivo-ringing", form or {"CallUUID": "ring-1"}, headers)
            return status, "ring-1" in app.call_ringing_status
        finally:
            app.call_ringing_status.pop("ring-1", None)

    def test_signed_request_passes(self):
        headers = plivo_headers("POST", BASE + "/plivo-ringing", {"CallUUID": "ring-1"})
        self.assertEqual(self._ringing(headers), (200, True))

    def test_unsigned_tampered_or_wrong_host_is_refused(self):
        with LogCapture() as log:
            self.assertEqual(self._ringing({}), (403, False))
            tampered = plivo_headers("POST", BASE + "/plivo-ringing", {"CallUUID": "ring-2"})
            self.assertEqual(self._ringing(tampered), (403, False))
            elsewhere = plivo_headers("POST", "https://evil.example.com/plivo-ringing", {"CallUUID": "ring-1"})
            self.assertEqual(self._ringing(elsewhere), (403, False))
        rejected = [l for l in log.lines if "🔐 [AUTH] plivo signature rejected endpoint=plivo_ringing" in l]
        self.assertEqual(len(rejected), 3, log.lines)

    def test_warn_mode_lets_it_through_and_logs(self):
        with mock.patch.object(web_auth, "PLIVO_SIGNATURE_CHECK", "warn"), LogCapture() as log:
            self.assertEqual(self._ringing({}), (200, True))
        self.assertTrue(any("plivo signature rejected" in l and "(warn, allowed)" in l for l in log.lines), log.lines)

    def test_off_mode_skips_the_check(self):
        with mock.patch.object(web_auth, "PLIVO_SIGNATURE_CHECK", "off"), LogCapture() as log:
            self.assertEqual(self._ringing({}), (200, True))
        self.assertFalse(any("[AUTH]" in l for l in log.lines), log.lines)

    def test_hangup_handler_still_reads_the_form(self):
        form = {"CallUUID": "h-1", "HangupCause": "USER_BUSY", "To": "+919876543210"}
        headers = plivo_headers("POST", BASE + "/plivo-hangup", form)
        with mock.patch.object(app, "emit_call_event") as emit:
            status, _ = self.post("/plivo-hangup", form, headers)
        self.assertEqual(status, 200)
        emit.assert_called_once_with("h-1", "call_failed", {"phone": "9876543210", "error": "USER_BUSY"})


class TestSignedStreamUrls(PlivoCase):
    def test_outbound_webhook_stream_url_carries_a_valid_token(self):
        query = "user_name=Asha%20Das&phone_number=9876543210&CallUUID=c-1"
        status, xml = self.get("/outbound-webhook?" + query, plivo_headers("GET", f"{BASE}/outbound-webhook?{query}"))
        self.assertEqual(status, 200)
        url = STREAM_URL.search(xml).group(1)
        self.assertTrue(url.startswith("wss://agent.example.com/media-stream?"
                                       "user_name=Asha%20Das&amp;phone_number=9876543210&amp;exp="), url)
        args = parse_stream_query(url.split("?", 1)[1])
        self.assertEqual(args["user_name"], "Asha Das")
        self.assertIsNone(web_auth.media_token_problem(args))

    def test_dial_action_stream_url_carries_a_valid_token(self):
        form = {"DialBLegStatus": "completed", "CallUUID": "c-9", "From": "+919876543210"}
        headers = plivo_headers("POST", BASE + "/dial-action?call_uuid=c-9", form)
        status, xml = self.post("/dial-action?call_uuid=c-9", form, headers)
        self.assertEqual(status, 200)
        args = parse_stream_query(STREAM_URL.search(xml).group(1).split("?", 1)[1])
        self.assertEqual(args["phone_number"], "+919876543210")
        self.assertEqual(args["transfer_context_id"], "c-9")
        self.assertEqual(args["dial_status"], "completed")
        self.assertEqual(args["is_transfer_return"], "1")
        self.assertIsNone(web_auth.media_token_problem(args))

    def test_dial_action_with_empty_call_uuid_connects(self):
        form = {"DialBLegStatus": "completed", "CallUUID": "", "From": "+919876543210"}
        headers = plivo_headers("POST", BASE + "/dial-action", form)
        status, xml = self.post("/dial-action", form, headers)
        self.assertEqual(status, 200)
        query = STREAM_URL.search(xml).group(1).split("?", 1)[1].replace("&amp;", "&")
        self.assertNotIn("transfer_context_id", query)
        self.assertEqual(self.connect(query), "accepted")


class TestMediaStreamToken(PlivoCase):
    def _query(self, now=None):
        return web_auth.media_stream_query({"user_name": "Asha Das", "phone_number": "98"},
                                           now=now).replace("&amp;", "&")

    def test_valid_token_reaches_the_handler(self):
        self.assertEqual(self.connect(self._query()), "accepted")

    def test_bad_tokens_are_refused_before_the_handler(self):
        with LogCapture() as log:
            self.assertEqual(self.connect("user_name=Asha&phone_number=98"), 403)
            self.assertEqual(self.connect(self._query().replace("phone_number=98", "phone_number=99")), 403)
            self.assertEqual(self.connect(self._query(now=time.time() - 200)), 403)
        reasons = [l.split("reason=")[1] for l in log.lines if "🔐 [AUTH] media-stream rejected" in l]
        self.assertEqual(reasons, ["missing", "bad-sig", "expired"], log.lines)

    def test_warn_and_off_modes(self):
        with mock.patch.object(web_auth, "PLIVO_SIGNATURE_CHECK", "warn"), LogCapture() as log:
            self.assertEqual(self.connect("phone_number=98"), "accepted")
        self.assertTrue(any("media-stream rejected reason=missing (warn, allowed)" in l for l in log.lines), log.lines)
        with mock.patch.object(web_auth, "PLIVO_SIGNATURE_CHECK", "off"):
            self.assertEqual(self.connect("phone_number=98"), "accepted")


if __name__ == "__main__":
    unittest.main()
