"""Logins, pages and default deny in app.py (docs/spec/admin-auth-design.md §1-2)."""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
import urllib.parse
from datetime import timedelta
from unittest import mock

from tests.harness.appctl import APP_PY, DIAGNOSTIC_LOG_TAGS, LogCapture, app

web_auth = app.web_auth
CREDS = dict(OPERATOR_USERNAME="op", OPERATOR_PASSWORD="op-pass",
             ADMIN_USERNAME="boss", ADMIN_PASSWORD="boss-pass", APP_SECRET="s" * 40)
LOGINS = {"operator": ("op", "op-pass"), "admin": ("boss", "boss-pass")}


def run(coro):
    return asyncio.run(coro)


def path_of(location):
    return urllib.parse.urlsplit(location or "").path


class AuthTestCase(unittest.TestCase):
    """Patched logins, a known session secret, and a cookie the plain-http test client keeps."""

    def setUp(self):
        patchers = [
            mock.patch.multiple(web_auth, **CREDS),
            # secret_key is a config-backed descriptor (no __delete__), so patch the config key itself.
            mock.patch.dict(app.app.config, {"SECRET_KEY": CREDS["APP_SECRET"], "SESSION_COOKIE_SECURE": False}),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        web_auth._login_failures.clear()
        self.addCleanup(web_auth._login_failures.clear)
        folder = tempfile.mkdtemp()
        db_patch = mock.patch.object(app, "CALL_HISTORY_DB_PATH", os.path.join(folder, "calls.db"))
        db_patch.start()
        self.addCleanup(db_patch.stop)
        app.init_call_history_db()

    async def client(self, role=None):
        client = app.app.test_client()
        if role is not None:
            username, password = LOGINS[role]
            response = await client.post("/login", form={"username": username, "password": password})
            self.assertEqual(response.status_code, 302)
        return client

    def get(self, role, path):
        async def go():
            client = await self.client(role)
            response = await client.get(path)
            return response.status_code, path_of(response.headers.get("Location")), \
                (await response.get_data()).decode("utf-8", "replace")   # PNG/WAV bodies are not text
        return run(go())


class TestAccessMatrix(AuthTestCase):
    def test_calls_page(self):
        self.assertEqual(self.get(None, "/")[:2], (302, "/login"))
        self.assertEqual(self.get("operator", "/")[0], 200)
        self.assertEqual(self.get("admin", "/")[0], 200)

    def test_admin_page(self):
        self.assertEqual(self.get(None, "/admin")[:2], (302, "/login"))
        self.assertEqual(self.get("operator", "/admin")[:2], (302, "/"))
        status, _, body = self.get("admin", "/admin")
        self.assertEqual(status, 200)
        self.assertIn("Admin", body)

    def test_api_me(self):
        status, _, body = self.get(None, "/api/me")
        self.assertEqual(status, 401)
        self.assertIn("login required", body)
        self.assertIn('"role":"operator"', self.get("operator", "/api/me")[2].replace(" ", ""))
        self.assertIn('"role":"admin"', self.get("admin", "/api/me")[2].replace(" ", ""))

    def test_history_and_live_feed_need_a_login(self):
        self.assertEqual(self.get(None, "/api/call-history")[0], 401)
        self.assertEqual(self.get("operator", "/api/call-history")[0], 200)
        self.assertEqual(self.get(None, "/call-events")[0], 401)

    def test_trigger_call_needs_a_login(self):
        async def go():
            client = await self.client(None)
            return await client.post("/trigger-call", json={"user_name": "A", "phone_number": "9876543210"})
        with mock.patch.object(app.plivo_client.calls, "create") as create:
            response = run(go())
        self.assertEqual(response.status_code, 401)
        create.assert_not_called()

    def test_open_endpoints(self):
        self.assertEqual(self.get(None, "/login")[0], 200)
        self.assertEqual(self.get(None, "/AU_Logo.png")[0], 200)
        self.assertEqual(self.get(None, "/playback_audio_files/transfer.wav")[0], 200)

    def test_static_is_refused_even_for_admin(self):
        with LogCapture() as log:
            self.assertEqual(self.get(None, "/static/x.css")[0], 403)
            self.assertEqual(self.get("admin", "/static/x.css")[0], 403)
        self.assertTrue(any("🔐 [AUTH] refused endpoint=static" in line for line in log.lines), log.lines)

    def test_forged_session_cookie_is_refused(self):
        async def go():
            login =await app.app.test_client().post("/login", form={"username": "boss", "password": "boss-pass"})
            value = login.headers["Set-Cookie"].split(";")[0].split("=", 1)[1]
            payload, _, signature = value.rpartition(".")
            forged = [payload + "." + signature[::-1],    # right payload, wrong signature
                      "x" + value,                        # garbled payload
                      "garbage"]                          # not a signed value at all
            results = []
            for cookie_value in forged:
                client = app.app.test_client()
                headers = {"Cookie": f"au_session={cookie_value}"}
                me = await client.get("/api/me", headers=headers)
                admin = await client.get("/admin", headers=headers)
                results.append((me.status_code, admin.status_code, path_of(admin.headers.get("Location"))))
            return results
        self.assertEqual(run(go()), [(401, 302, "/login")] * 3)

    def test_unknown_path_is_404(self):
        self.assertEqual(self.get(None, "/no-such-page")[0], 404)


class TestDefaultDeny(unittest.TestCase):
    def test_access_table_matches_the_routes(self):
        endpoints = {rule.endpoint for rule in app.app.url_map.iter_rules()}
        self.assertEqual(endpoints - {"static"}, set(web_auth.ENDPOINT_ACCESS))


class TestLogin(AuthTestCase):
    def _post_login(self, username, password, client=None):
        async def go():
            c = client or app.app.test_client()
            response = await c.post("/login", form={"username": username, "password": password})
            return response, await response.get_data(as_text=True)
        return run(go())

    def test_wrong_password(self):
        response, body = self._post_login("op", "nope")
        self.assertEqual(response.status_code, 401)
        self.assertIn("Wrong username or password.", body)
        self.assertNotIn("au_session", response.headers.get("Set-Cookie", ""))

    def test_each_role_lands_on_its_page_with_a_safe_cookie(self):
        for role, target in (("operator", "/"), ("admin", "/admin")):
            with self.subTest(role=role):
                response, _ = self._post_login(*LOGINS[role])
                self.assertEqual(response.status_code, 302)
                self.assertEqual(path_of(response.headers["Location"]), target)
                cookie = response.headers["Set-Cookie"]
                self.assertTrue(cookie.startswith("au_session="), cookie)
                self.assertIn("HttpOnly", cookie)
                self.assertIn("SameSite=Lax", cookie)

    def test_logout_ends_the_session(self):
        async def go():
            client = await self.client("admin")
            out = await client.post("/logout")
            me = await client.get("/api/me")
            return out.status_code, path_of(out.headers.get("Location")), me.status_code
        self.assertEqual(run(go()), (302, "/login", 401))

    def test_throttle_refuses_even_the_right_password(self):
        for _ in range(web_auth.LOGIN_MAX_FAILURES):
            self.assertEqual(self._post_login("op", "nope")[0].status_code, 401)
        response, body = self._post_login(*LOGINS["operator"])
        self.assertEqual(response.status_code, 429)
        self.assertIn("Too many attempts", body)

    def test_empty_form_on_unconfigured_box_is_refused(self):
        with mock.patch.multiple(web_auth, OPERATOR_USERNAME="", OPERATOR_PASSWORD="",
                                 ADMIN_USERNAME="", ADMIN_PASSWORD=""):
            response, _ = self._post_login("", "")
        self.assertEqual(response.status_code, 401)

    def test_log_lines_never_hold_the_password(self):
        with LogCapture() as log:
            self._post_login("op", "op-pass-typo")
            self._post_login(*LOGINS["operator"])
        self.assertTrue(any("🔐 [AUTH] login failed" in line for line in log.lines), log.lines)
        self.assertTrue(any("🔐 [AUTH] login ok role=operator" in line for line in log.lines), log.lines)
        self.assertFalse(any("op-pass" in line for line in log.lines), log.lines)
        self.assertIn("[AUTH]", DIAGNOSTIC_LOG_TAGS)

    def test_login_page_has_the_form_and_no_marker(self):
        status, _, body = self.get(None, "/login")
        self.assertEqual(status, 200)
        self.assertIn('action="/login"', body)
        self.assertNotIn(app.LOGIN_ERROR_MARK, body)


class TestSessionConfig(unittest.TestCase):
    def test_cookie_settings(self):
        config = app.app.config
        self.assertEqual(config["SESSION_COOKIE_NAME"], "au_session")
        self.assertEqual(config["SESSION_COOKIE_SAMESITE"], "Lax")
        self.assertEqual(config["PERMANENT_SESSION_LIFETIME"], timedelta(hours=12))
        self.assertFalse(config["SESSION_REFRESH_EACH_REQUEST"])

    def test_secure_flag_follows_public_base_url_and_startup_checks_config(self):
        with open(APP_PY, encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn('SESSION_COOKIE_SECURE=(PUBLIC_BASE_URL or "").startswith("https://")', source)
        self.assertIn("web_auth.check_config()", source[source.index('if __name__ == "__main__":'):])


if __name__ == "__main__":
    unittest.main()
