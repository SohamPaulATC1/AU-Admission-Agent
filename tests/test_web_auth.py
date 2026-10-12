"""web_auth.py: logins, throttle, media-stream tokens, Plivo signatures (docs/spec/admin-auth-design.md)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
import urllib.parse
from unittest import mock

from plivo.utils.signature_v3 import construct_get_url, construct_post_url, get_signature_v3

import web_auth

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CREDS = dict(OPERATOR_USERNAME="op", OPERATOR_PASSWORD="op-pass",
             ADMIN_USERNAME="boss", ADMIN_PASSWORD="boss-pass", APP_SECRET="s" * 40)
PLIVO_TOKEN = "plivo-test-token"
BASE = "https://agent.example.com"


def plivo_headers(method, url, form=None, token=PLIVO_TOKEN, nonce="12345"):
    """The headers Plivo would send for this request, made with the SDK's own signer."""
    if method == "POST":
        base = construct_post_url(url, dict(form or {}))
    else:
        base = construct_get_url(url, {})
    signature = get_signature_v3(token.encode(), base.decode(), nonce.encode()).decode()
    return {"X-Plivo-Signature-V3": signature, "X-Plivo-Signature-V3-Nonce": nonce}


def parse_stream_query(query):
    """The stream query as the websocket sees it (XML &amp; undone, values decoded)."""
    return dict(urllib.parse.parse_qsl(query.replace("&amp;", "&"), keep_blank_values=True))


class TestCheckConfig(unittest.TestCase):
    def test_complete_config_passes(self):
        with mock.patch.multiple(web_auth, **CREDS, PLIVO_AUTH_TOKEN=PLIVO_TOKEN):
            web_auth.check_config()

    def test_each_missing_value_is_named_without_values(self):
        for name in CREDS:
            with self.subTest(name=name), \
                    mock.patch.multiple(web_auth, **{**CREDS, name: ""}, PLIVO_AUTH_TOKEN=PLIVO_TOKEN):
                with self.assertRaises(ValueError) as caught:
                    web_auth.check_config()
                message = str(caught.exception)
                self.assertIn(name, message)
                self.assertNotIn("op-pass", message)
                self.assertNotIn("boss-pass", message)

    def test_short_secret_is_refused(self):
        with mock.patch.multiple(web_auth, **{**CREDS, "APP_SECRET": "s" * 31}, PLIVO_AUTH_TOKEN=PLIVO_TOKEN):
            with self.assertRaisesRegex(ValueError, "at least 32"):
                web_auth.check_config()

    def test_identical_logins_are_refused(self):
        same = {**CREDS, "OPERATOR_USERNAME": "boss", "OPERATOR_PASSWORD": "boss-pass"}
        with mock.patch.multiple(web_auth, **same, PLIVO_AUTH_TOKEN=PLIVO_TOKEN):
            with self.assertRaisesRegex(ValueError, "must differ"):
                web_auth.check_config()

    def test_plivo_token_needed_unless_checks_are_off(self):
        with mock.patch.multiple(web_auth, **CREDS, PLIVO_AUTH_TOKEN="", PLIVO_SIGNATURE_CHECK="enforce"):
            with self.assertRaisesRegex(ValueError, "PLIVO_AUTH_TOKEN"):
                web_auth.check_config()
        with mock.patch.multiple(web_auth, **CREDS, PLIVO_AUTH_TOKEN="", PLIVO_SIGNATURE_CHECK="off"):
            web_auth.check_config()

    def test_unknown_check_mode_fails_at_import(self):
        env = {**os.environ, "PLIVO_SIGNATURE_CHECK": "maybe"}
        result = subprocess.run([sys.executable, "-c", "import web_auth"], cwd=REPO_ROOT, env=env,
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("PLIVO_SIGNATURE_CHECK must be", result.stderr)


class TestMatchLogin(unittest.TestCase):
    def test_each_pair_gives_its_role(self):
        with mock.patch.multiple(web_auth, **CREDS):
            self.assertEqual(web_auth.match_login("boss", "boss-pass"), "admin")
            self.assertEqual(web_auth.match_login("op", "op-pass"), "operator")

    def test_wrong_or_crossed_pairs_fail(self):
        with mock.patch.multiple(web_auth, **CREDS):
            self.assertIsNone(web_auth.match_login("op", "wrong"))
            self.assertIsNone(web_auth.match_login("op", "boss-pass"))
            self.assertIsNone(web_auth.match_login("boss", "op-pass"))
            self.assertIsNone(web_auth.match_login("", ""))

    def test_same_username_different_passwords(self):
        shared = {**CREDS, "OPERATOR_USERNAME": "team", "ADMIN_USERNAME": "team"}
        with mock.patch.multiple(web_auth, **shared):
            self.assertEqual(web_auth.match_login("team", "boss-pass"), "admin")
            self.assertEqual(web_auth.match_login("team", "op-pass"), "operator")

    def test_unconfigured_never_matches(self):
        empty = {name: "" for name in CREDS}
        with mock.patch.multiple(web_auth, **empty):
            self.assertIsNone(web_auth.match_login("", ""))

    def test_non_ascii_input_does_not_raise(self):
        with mock.patch.multiple(web_auth, **CREDS):
            self.assertIsNone(web_auth.match_login("নেহা", "পাসওয়ার্ড"))


class TestThrottle(unittest.TestCase):
    def setUp(self):
        web_auth._login_failures.clear()
        self.addCleanup(web_auth._login_failures.clear)

    def test_blocked_at_the_fifth_failure(self):
        for n in range(4):
            web_auth.note_login_failure("1.1.1.1", now=100.0 + n)
        self.assertFalse(web_auth.login_blocked("1.1.1.1", now=110.0))
        web_auth.note_login_failure("1.1.1.1", now=111.0)
        self.assertTrue(web_auth.login_blocked("1.1.1.1", now=112.0))

    def test_window_expires(self):
        for n in range(5):
            web_auth.note_login_failure("1.1.1.1", now=100.0 + n)
        self.assertTrue(web_auth.login_blocked("1.1.1.1", now=100.0 + web_auth.LOGIN_WINDOW_S - 1))
        self.assertFalse(web_auth.login_blocked("1.1.1.1", now=105.0 + web_auth.LOGIN_WINDOW_S))

    def test_other_ip_is_not_blocked(self):
        for n in range(5):
            web_auth.note_login_failure("1.1.1.1", now=100.0 + n)
        self.assertFalse(web_auth.login_blocked("2.2.2.2", now=106.0))

    def test_stale_ips_are_dropped(self):
        web_auth.note_login_failure("1.1.1.1", now=0.0)
        web_auth.note_login_failure("2.2.2.2", now=web_auth.LOGIN_WINDOW_S + 10)
        self.assertNotIn("1.1.1.1", web_auth._login_failures)


class TestClientIp(unittest.TestCase):
    def test_nginx_header_trusted_only_from_localhost(self):
        self.assertEqual(web_auth.client_ip("127.0.0.1", {"X-Real-IP": "49.36.1.2"}), "49.36.1.2")
        self.assertEqual(web_auth.client_ip("49.36.9.9", {"X-Real-IP": "1.2.3.4"}), "49.36.9.9")
        self.assertEqual(web_auth.client_ip("127.0.0.1", {}), "127.0.0.1")
        self.assertEqual(web_auth.client_ip(None, {}), "")


class TestMediaToken(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(web_auth, "APP_SECRET", "s" * 40)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_round_trip(self):
        query = web_auth.media_stream_query({"user_name": "Asha Das", "phone_number": "9876543210"}, now=1000)
        self.assertTrue(query.startswith("user_name=Asha%20Das&amp;phone_number=9876543210&amp;exp=1120&amp;sig="))
        args = parse_stream_query(query)
        self.assertEqual(args["user_name"], "Asha Das")
        self.assertIsNone(web_auth.media_token_problem(args, now=1000))

    def test_round_trip_with_awkward_name(self):
        name = "Asha Das & Co/1 নেহা"
        query = web_auth.media_stream_query({"user_name": name, "phone_number": "98"}, now=1000)
        self.assertNotIn("&C", query.replace("&amp;", ""))   # the & inside the name is encoded
        args = parse_stream_query(query)
        self.assertEqual(args["user_name"], name)
        self.assertIsNone(web_auth.media_token_problem(args, now=1000))

    def test_empty_values_are_left_out(self):
        query = web_auth.media_stream_query({"phone_number": "98", "transfer_context_id": ""}, now=1000)
        self.assertNotIn("transfer_context_id", query)
        self.assertIsNone(web_auth.media_token_problem(parse_stream_query(query), now=1000))

    def test_expiry(self):
        args = parse_stream_query(web_auth.media_stream_query({"phone_number": "98"}, now=1000))
        self.assertIsNone(web_auth.media_token_problem(args, now=1000 + web_auth.MEDIA_TOKEN_TTL_S))
        self.assertEqual(web_auth.media_token_problem(args, now=1001 + web_auth.MEDIA_TOKEN_TTL_S), "expired")

    def test_tampering(self):
        args = parse_stream_query(web_auth.media_stream_query({"phone_number": "98"}, now=1000))
        self.assertEqual(web_auth.media_token_problem({**args, "phone_number": "99"}, now=1000), "bad-sig")
        self.assertEqual(web_auth.media_token_problem({**args, "user_name": "x"}, now=1000), "bad-sig")
        self.assertEqual(web_auth.media_token_problem({**args, "exp": "9999999999"}, now=1000), "bad-sig")
        with mock.patch.object(web_auth, "APP_SECRET", "t" * 40):
            self.assertEqual(web_auth.media_token_problem(args, now=1000), "bad-sig")

    def test_missing_parts(self):
        args = parse_stream_query(web_auth.media_stream_query({"phone_number": "98"}, now=1000))
        self.assertEqual(web_auth.media_token_problem({"phone_number": "98"}), "missing")
        self.assertEqual(web_auth.media_token_problem({k: v for k, v in args.items() if k != "exp"}), "missing")
        self.assertEqual(web_auth.media_token_problem({k: v for k, v in args.items() if k != "sig"}), "missing")


class TestPlivoSignature(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(web_auth, "PLIVO_AUTH_TOKEN", PLIVO_TOKEN)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_post_with_form(self):
        sent = {"CallUUID": "u-1", "HangupCause": "NORMAL_CLEARING"}
        headers = plivo_headers("POST", BASE + "/plivo-hangup", sent)
        form = {name: [value] for name, value in sent.items()}
        self.assertTrue(web_auth.plivo_signature_ok("POST", BASE + "/", "/plivo-hangup", "", form, headers))

    def test_get_with_plivo_appended_params(self):
        query = "user_name=Asha%20Das&phone_number=98&CallUUID=u-1&From=%2B9122"
        headers = plivo_headers("GET", f"{BASE}/outbound-webhook?{query}")
        self.assertTrue(web_auth.plivo_signature_ok("GET", BASE, "/outbound-webhook", query, {}, headers))

    def test_failures(self):
        sent = {"CallUUID": "u-1"}
        good = plivo_headers("POST", BASE + "/plivo-ringing", sent)
        form = {"CallUUID": ["u-1"]}
        ok = web_auth.plivo_signature_ok
        self.assertFalse(ok("POST", BASE, "/plivo-ringing", "", {"CallUUID": ["u-2"]}, good))
        self.assertFalse(ok("POST", "https://other.example.com", "/plivo-ringing", "", form, good))
        self.assertFalse(ok("POST", BASE, "/plivo-ringing", "", form, {}))
        self.assertFalse(ok("POST", BASE, "/plivo-ringing", "", form,
                            plivo_headers("POST", BASE + "/plivo-ringing", sent, token="other")))
        self.assertFalse(ok("POST", BASE, "/plivo-ringing", "", form,
                            {"X-Plivo-Signature-V3": "x", "X-Plivo-Signature-V3-Nonce": "y"}))
        self.assertFalse(ok("POST", None, "/plivo-ringing", "", form, good))
        self.assertFalse(ok("HEAD", BASE, "/plivo-ringing", "", form, good))
        with mock.patch.object(web_auth, "PLIVO_AUTH_TOKEN", ""):
            self.assertFalse(ok("POST", BASE, "/plivo-ringing", "", form, good))


class TestWithoutCost(unittest.TestCase):
    def test_cost_removed_rest_kept(self):
        payload = json.dumps({"call_id": "u-1", "type": "call_ended",
                              "data": {"cost": "$0.12", "reason": "done"}, "timestamp": "10:00:00"})
        event = json.loads(web_auth.without_cost(payload))
        self.assertEqual(event["data"], {"reason": "done"})
        self.assertEqual(event["call_id"], "u-1")

    def test_no_cost_returns_the_same_text(self):
        payload = json.dumps({"call_id": "u-1", "type": "call_ringing", "data": {}})
        self.assertIs(web_auth.without_cost(payload), payload)
        odd = json.dumps({"call_id": "u-1", "type": "x", "data": "text"})
        self.assertIs(web_auth.without_cost(odd), odd)


class TestAccessTable(unittest.TestCase):
    def test_every_class_is_known(self):
        classes = {web_auth.OPEN, web_auth.PLIVO, web_auth.MEDIA, web_auth.OPERATOR_PAGE,
                   web_auth.OPERATOR_API, web_auth.ADMIN_PAGE, web_auth.ADMIN_API}
        self.assertLessEqual(set(web_auth.ENDPOINT_ACCESS.values()), classes)

    def test_session_verdict(self):
        verdict = web_auth.session_verdict
        for access in (web_auth.OPERATOR_PAGE, web_auth.OPERATOR_API):
            self.assertIsNone(verdict(access, "operator"))
            self.assertIsNone(verdict(access, "admin"))
            self.assertEqual(verdict(access, None), "login")
        for access in (web_auth.ADMIN_PAGE, web_auth.ADMIN_API):
            self.assertEqual(verdict(access, "operator"), "forbidden")
            self.assertIsNone(verdict(access, "admin"))
            self.assertEqual(verdict(access, None), "login")
        self.assertEqual(verdict(web_auth.OPERATOR_API, "someone"), "login")


if __name__ == "__main__":
    unittest.main()
