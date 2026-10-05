"""GEMINI_BACKEND -- Vertex AI or Google AI Studio for the Live API (2026-09-29).

"vertex" (the default) connects to Vertex AI with the service-account key at
VERTEX_CREDENTIALS_PATH; "studio" connects to AI Studio with GOOGLE_API_KEY.
Same model ID and config on both. Nothing here opens a network connection or
reads the key file: the SDK client and the credentials are faked.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import sys
import threading
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from tests.harness.appctl import REPO_ROOT, app


class FakeCredentials:
    def __init__(self, token=None, valid=False, expiry=None):
        self.token = token
        self.valid = valid
        self.expiry = expiry
        self.refresh_threads = []

    def refresh(self, request):
        self.refresh_threads.append(threading.get_ident())
        self.token = "fake-token"
        self.valid = True
        self.expiry = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1)


class TestAppSelectsTheBackend(unittest.TestCase):
    def _import_app(self, value):
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        env.pop("GEMINI_BACKEND", None)
        if value is not None:
            env["GEMINI_BACKEND"] = value
        code = ("from tests.harness.appctl import app; "
                "print(app.GEMINI_BACKEND, app._vertex_credentials)")
        return subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, env=env,
                              capture_output=True, text=True, timeout=300)

    def test_default_is_vertex_and_the_key_is_not_loaded_at_import(self):
        result = self._import_app(None)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split()[-2:], ["vertex", "None"])

    def test_studio_is_selectable(self):
        result = self._import_app(" STUDIO ")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split()[-2], "studio")

    def test_unknown_value_fails_at_startup(self):
        result = self._import_app("aistudio")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("GEMINI_BACKEND must be 'vertex' or 'studio', got 'aistudio'",
                      result.stderr)


class TestVertexEndpoint(unittest.TestCase):
    def test_multi_regions_use_the_rep_hostnames(self):
        self.assertEqual(app.vertex_base_url("eu"), "https://aiplatform.eu.rep.googleapis.com/")
        self.assertEqual(app.vertex_base_url("us"), "https://aiplatform.us.rep.googleapis.com/")

    def test_a_region_keeps_the_sdk_default(self):
        self.assertIsNone(app.vertex_base_url("us-central1"))

    def test_defaults(self):
        self.assertEqual(app.VERTEX_PROJECT, "silver-shift-490819-k0")
        self.assertEqual(app.VERTEX_LOCATION, "us-central1")  # TEST6: eu stalled mid-turn
        self.assertEqual(os.path.dirname(app.VERTEX_CREDENTIALS_PATH), REPO_ROOT)


class TestClientConstruction(unittest.TestCase):
    def test_vertex_client(self):
        creds = FakeCredentials()
        with mock.patch.object(app, "GEMINI_BACKEND", "vertex"), \
                mock.patch.object(app, "VERTEX_LOCATION", "eu"), \
                mock.patch.object(app, "load_vertex_credentials", return_value=creds), \
                mock.patch.object(app.genai, "Client") as client_cls:
            app.create_gemini_client()
        kwargs = client_cls.call_args.kwargs
        self.assertIs(kwargs["vertexai"], True)
        self.assertEqual(kwargs["project"], app.VERTEX_PROJECT)
        self.assertEqual(kwargs["location"], "eu")
        self.assertIs(kwargs["credentials"], creds)
        self.assertEqual(kwargs["http_options"].base_url,
                         "https://aiplatform.eu.rep.googleapis.com/")
        self.assertNotIn("api_key", kwargs)

    def test_vertex_regional_client_has_no_base_url_override(self):
        with mock.patch.object(app, "GEMINI_BACKEND", "vertex"), \
                mock.patch.object(app, "VERTEX_LOCATION", "us-central1"), \
                mock.patch.object(app, "load_vertex_credentials", return_value=FakeCredentials()), \
                mock.patch.object(app.genai, "Client") as client_cls:
            app.create_gemini_client()
        self.assertIsNone(client_cls.call_args.kwargs["http_options"])

    def test_studio_client(self):
        with mock.patch.object(app, "GEMINI_BACKEND", "studio"), \
                mock.patch.object(app, "load_vertex_credentials") as load, \
                mock.patch.object(app.genai, "Client") as client_cls:
            app.create_gemini_client()
        self.assertEqual(client_cls.call_args.kwargs, {"api_key": app.LIVE_API_KEY})
        load.assert_not_called()

    def test_real_sdk_accepts_the_vertex_arguments(self):
        """No network: google-genai builds the client without connecting."""
        from google.oauth2 import credentials as oauth2_credentials

        creds = oauth2_credentials.Credentials(token="offline")
        with mock.patch.object(app, "GEMINI_BACKEND", "vertex"), \
                mock.patch.object(app, "VERTEX_LOCATION", "eu"), \
                mock.patch.object(app, "load_vertex_credentials", return_value=creds):
            client = app.create_gemini_client()
        api = client._api_client
        self.assertTrue(api.vertexai)
        self.assertIsNone(api.api_key)
        self.assertEqual(api.project, app.VERTEX_PROJECT)
        self.assertEqual(api._websocket_base_url(), "wss://aiplatform.eu.rep.googleapis.com/")

    def test_backend_description(self):
        with mock.patch.object(app, "GEMINI_BACKEND", "studio"):
            self.assertEqual(app.describe_gemini_backend(), "backend=studio")
        with mock.patch.object(app, "GEMINI_BACKEND", "vertex"):
            self.assertEqual(app.describe_gemini_backend(),
                             f"backend=vertex project={app.VERTEX_PROJECT} "
                             f"location={app.VERTEX_LOCATION}")


class TestTokenRefresh(unittest.TestCase):
    def test_needs_refresh(self):
        now = datetime(2026, 9, 29, 12, 0, 0)
        self.assertTrue(app.vertex_token_needs_refresh(FakeCredentials(), now))
        fresh = FakeCredentials("t", True, now + timedelta(minutes=30))
        self.assertFalse(app.vertex_token_needs_refresh(fresh, now))
        near_expiry = FakeCredentials("t", True, now + timedelta(minutes=4))
        self.assertTrue(app.vertex_token_needs_refresh(near_expiry, now))

    def test_refresh_runs_off_the_event_loop_thread_and_only_when_needed(self):
        creds = FakeCredentials()

        async def scenario():
            await app.ensure_vertex_token()
            await app.ensure_vertex_token()
            return threading.get_ident()

        with mock.patch.object(app, "load_vertex_credentials", return_value=creds):
            loop_thread = asyncio.run(scenario())
        self.assertEqual(len(creds.refresh_threads), 1)
        self.assertNotEqual(creds.refresh_threads[0], loop_thread)


class _FakeLive:
    def __init__(self):
        self.connected = 0

    @contextlib.asynccontextmanager
    async def _session(self):
        self.connected += 1
        yield "session"

    def connect(self, model, config):
        return self._session()


class TestConnectRefreshesFirst(unittest.TestCase):
    def _connect(self, backend):
        live = _FakeLive()
        client = mock.Mock()
        client.aio.live = live
        ensured = []

        async def fake_ensure():
            ensured.append(live.connected)

        async def scenario():
            async with app.connect_live_with_timeout(client, app.GEMINI_MODEL, None) as session:
                return session

        with mock.patch.object(app, "GEMINI_BACKEND", backend), \
                mock.patch.object(app, "ensure_vertex_token", fake_ensure):
            session = asyncio.run(scenario())
        return session, ensured

    def test_vertex_token_is_ensured_before_connecting(self):
        session, ensured = self._connect("vertex")
        self.assertEqual(session, "session")
        self.assertEqual(ensured, [0])

    def test_studio_does_not_touch_vertex_credentials(self):
        session, ensured = self._connect("studio")
        self.assertEqual(session, "session")
        self.assertEqual(ensured, [])


class TestKeyFileIsNotCommittable(unittest.TestCase):
    def test_gitignored(self):
        with open(os.path.join(REPO_ROOT, ".gitignore"), "r", encoding="utf-8") as handle:
            ignored = handle.read().splitlines()
        self.assertIn("silver-shift-*.json", ignored)
        self.assertTrue(os.path.basename(app.VERTEX_CREDENTIALS_PATH).startswith("silver-shift-"))


if __name__ == "__main__":
    unittest.main()
