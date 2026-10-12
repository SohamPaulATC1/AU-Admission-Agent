# Step 1: Login and Protection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Nothing in the web app that spends money or shows data is reachable without a login (operator or admin) or a Plivo proof (request signature, signed `/media-stream` URL).

**Architecture:** A new module `web_auth.py` holds the config, the access table and plain functions (login match, throttle, media-stream token, Plivo signature, cost removal). `app.py` wires them into one `before_request` hook (default deny over the access table), one `before_websocket` hook (media token), the `/login` `/logout` `/api/me` `/admin` routes, and the signed stream URLs. Two small HTML pages (`login.html`, `admin.html`) and edits to `index.html`.

**Tech Stack:** Python 3.12 (venv312), Quart 0.20 (built-in signed cookie session), `plivo` SDK (`plivo.utils.signature_v3`), stdlib `hmac`/`hashlib`/`json`/`unittest`.

**Spec:** `docs/spec/admin-auth-design.md` (shared decisions: `docs/spec/admin-system-overview.md`)

## Global Constraints

- No new dependency; `requirements.txt` unchanged. Quart's session (itsdangerous) and `plivo.utils.signature_v3` are already installed.
- `.env` gets five new keys (`OPERATOR_USERNAME`, `OPERATOR_PASSWORD`, `ADMIN_USERNAME`, `ADMIN_PASSWORD`, `APP_SECRET`), added by the operator by hand. Never read or print `.env` or the credential JSON.
- `PLIVO_SIGNATURE_CHECK` values exactly `enforce` (default), `warn`, `off`. `APP_SECRET` at least 32 characters. Session cookie `au_session`, HttpOnly, SameSite=Lax, Secure when `PUBLIC_BASE_URL` starts with `https://`, lifetime 12 hours. Login throttle: 5 failures per client IP within 15 minutes. Media token lifetime 120 seconds.
- Log tag `[AUTH]` with the `🔐` prefix. Never log a password, cookie, signature or nonce.
- Tests are stdlib `unittest` only. Run the suite with `venv312/Scripts/python.exe -m unittest discover -s tests -t .` Never write test code through bash heredocs; use the Write/Edit tools.
- Tests never touch the network, `.env`, the real `call_history.db` or the Plivo account.
- The line `@app.route("/trigger-call", methods=["POST"])` stays byte-identical (two tests slice `app.py`'s source up to it).
- When app.py's line count changes, re-pin the `GEMINI_MODEL` line list in `tests/test_preservation_4_8_resumption_recording_stats.py` (`test_model_identifier_is_recorded`): still 5 entries, same sites; replace the list and add one comment line.
- `aec.py`, `aec1.py`, `bargein.py` untouched. No new `call_state` key.
- No commits unless the operator asks (CLAUDE.md). Each task ends with a full-suite run instead.
- Suite baseline before this plan: 636 run, 3 failures (all in `tests/test_prompt_catalog.py`, pre-existing, leave them), 2 skipped, 1 expected failure. "Green" below means no failure beyond those 3.

## Review Focus

1. Logins not configured (a dev box with no `.env` values): an empty username and password must never log in. Tests: Task 1 `test_unconfigured_never_matches`, Task 2 `test_empty_form_on_unconfigured_box_is_refused`.
2. Plivo appends its own call parameters (`CallUUID`, `From`, `To`, ...) to a GET answer URL's query: the signature must still verify. Tests: Task 1 `test_get_with_plivo_appended_params`, Task 3 `test_outbound_webhook_stream_url_carries_a_valid_token` (query includes `CallUUID`).
3. A transfer return with an empty `call_uuid` (blank parameter): the stream URL from `/dial-action` must still pass the websocket check through Quart's real query parsing. Tests: Task 1 `test_empty_values_are_left_out`, Task 3 `test_dial_action_with_empty_call_uuid_connects`.
4. The signature hook reads the POST form before the handler: the handler must still see every field. Test: Task 3 `test_hangup_handler_still_reads_the_form`.
5. Contact names with spaces, `&`, `/` or non-ASCII (they travel through the answer URL into the stream URL): the token must survive the round trip. Tests: Task 1 `test_round_trip_with_awkward_name`, Task 3 `test_outbound_webhook_stream_url_carries_a_valid_token` (name with a space).

## Files

| File | Change |
|---|---|
| `web_auth.py` | Create. Config, access table, login match, throttle, client IP, media token, Plivo signature, cost removal. No Quart import. |
| `app.py` | Modify. Import `web_auth`; session config; `enforce_access` and `enforce_media_token` hooks; `/login` `/logout` `/api/me` `/admin`; signed stream URLs in `outbound_webhook` / `dial_action`; cost removal in `get_call_history` / `call_events_stream`; `/trigger-call` session + JSON; `API_AUTH_TOKEN` removed; `web_auth.check_config()` in `__main__`. |
| `login.html`, `admin.html` | Create. Standalone pages. |
| `index.html` | Modify. No API key; 401 redirects; Logout button; Admin link for admin. |
| `tests/test_web_auth.py` | Create (Task 1). |
| `tests/test_access_control.py` | Create (Task 2). |
| `tests/test_plivo_checks.py` | Create (Task 3). |
| `tests/test_operator_view.py` | Create (Task 4). |
| `tests/harness/appctl.py` | Modify. `"[AUTH]"` in `DIAGNOSTIC_LOG_TAGS`. |
| `tests/test_preservation_4_8_resumption_recording_stats.py` | Modify. Re-pin (Tasks 2, 3, 4). |
| `CLAUDE.md`, `docs/HANDOFF.md`, `docs/handoff1.md`, `docs/spec/admin-auth-design.md`, `docs/spec/admin-system-overview.md` | Modify (Task 5). |

---

### Task 1: `web_auth.py`

**Files:**
- Create: `web_auth.py`
- Test: `tests/test_web_auth.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - Module constants `OPERATOR_USERNAME`, `OPERATOR_PASSWORD`, `ADMIN_USERNAME`, `ADMIN_PASSWORD`, `APP_SECRET`, `PLIVO_AUTH_TOKEN` (str), `PLIVO_SIGNATURE_CHECK` (`"enforce" | "warn" | "off"`), `APP_SECRET_MIN_CHARS = 32`, `LOGIN_MAX_FAILURES = 5`, `LOGIN_WINDOW_S = 900`, `MEDIA_TOKEN_TTL_S = 120`, `PLIVO_SIGNATURE_HEADER = "X-Plivo-Signature-V3"`, `PLIVO_NONCE_HEADER = "X-Plivo-Signature-V3-Nonce"`.
  - Access classes `OPEN`, `PLIVO`, `MEDIA`, `OPERATOR_PAGE`, `OPERATOR_API`, `ADMIN_PAGE`, `ADMIN_API` (str) and `ENDPOINT_ACCESS: dict[str, str]` (endpoint function name to class).
  - `check_config() -> None` (raises `ValueError`).
  - `session_verdict(access: str, role: str | None) -> None | "login" | "forbidden"`.
  - `match_login(username: str, password: str) -> "admin" | "operator" | None`.
  - `login_blocked(ip: str, now: float | None = None) -> bool`, `note_login_failure(ip: str, now: float | None = None) -> None`, module dict `_login_failures` (tests clear it).
  - `client_ip(remote_addr: str | None, headers) -> str`.
  - `media_stream_query(params: dict, now: float | None = None) -> str` (joined with `&amp;` for Plivo XML), `media_token_problem(args, now: float | None = None) -> None | "missing" | "bad-sig" | "expired"`.
  - `plivo_signature_ok(method: str, public_base_url: str | None, path: str, query_string: str, form: dict, headers) -> bool`.
  - `without_cost(event_payload: str) -> str`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_web_auth.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `venv312/Scripts/python.exe -m unittest tests.test_web_auth -v`
Expected: ERROR, `ModuleNotFoundError: No module named 'web_auth'`.

- [ ] **Step 3: Write `web_auth.py`**

```python
"""Logins, sessions and Plivo request checks for the web app.

Design: docs/spec/admin-auth-design.md. Plain functions and the access table; app.py wires them
into Quart's before_request / before_websocket hooks and the /login routes.
"""

import hashlib
import hmac
import json
import os
import time
import urllib.parse

from plivo.utils.signature_v3 import validate_v3_signature

# Two shared logins (operator decision 2026-10-12). Read at import; app.py imports this module
# after load_dotenv(). check_config() refuses to start the server without them.
OPERATOR_USERNAME = os.getenv("OPERATOR_USERNAME", "")
OPERATOR_PASSWORD = os.getenv("OPERATOR_PASSWORD", "")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
# Signs the session cookie and the /media-stream token; the same value in every process.
APP_SECRET = os.getenv("APP_SECRET", "")
APP_SECRET_MIN_CHARS = 32
PLIVO_AUTH_TOKEN = os.getenv("PLIVO_AUTH_TOKEN", "")

# enforce: reject a bad Plivo signature or media-stream token; warn: log it and let it through
# (first deploy); off: no check (rollback).
PLIVO_SIGNATURE_CHECK = os.getenv("PLIVO_SIGNATURE_CHECK", "enforce").strip().lower()
if PLIVO_SIGNATURE_CHECK not in ("enforce", "warn", "off"):
    raise ValueError(f"PLIVO_SIGNATURE_CHECK must be 'enforce', 'warn' or 'off', got {PLIVO_SIGNATURE_CHECK!r}")

# Access classes (spec §2).
OPEN = "open"                    # no check
PLIVO = "plivo"                  # Plivo's request signature
MEDIA = "media"                  # signed /media-stream token (websocket)
OPERATOR_PAGE = "operator_page"  # operator or admin login, else redirect to /login
OPERATOR_API = "operator_api"    # operator or admin login, else 401 JSON
ADMIN_PAGE = "admin_page"        # admin login; an operator is sent to /
ADMIN_API = "admin_api"          # admin login; an operator gets 403

# Every endpoint app.py registers. Missing = refused (default deny); a test keeps this table and
# app.url_map identical.
ENDPOINT_ACCESS = {
    "serve_dashboard": OPERATOR_PAGE,
    "get_call_history": OPERATOR_API,
    "call_events_stream": OPERATOR_API,
    "trigger_call": OPERATOR_API,
    "api_me": OPERATOR_API,
    "admin_page": ADMIN_PAGE,
    "login_page": OPEN,
    "login_submit": OPEN,
    "logout": OPEN,
    "serve_audio": OPEN,             # Plivo fetches these WAVs
    "serve_atc_logo": OPEN,
    "serve_au_logo": OPEN,
    "internal_call_event": OPEN,     # own X-Internal-Token check; nginx returns 404 for /internal/
    "outbound_webhook": PLIVO,
    "hangup": PLIVO,
    "transfer_xml": PLIVO,
    "dial_action": PLIVO,
    "plivo_ringing": PLIVO,
    "plivo_hangup": PLIVO,
    "handle_media_stream": MEDIA,
}


def check_config():
    """Raise ValueError if the login config is unusable. Called once at startup; names only, never values."""
    names = ["OPERATOR_USERNAME", "OPERATOR_PASSWORD", "ADMIN_USERNAME", "ADMIN_PASSWORD", "APP_SECRET"]
    if PLIVO_SIGNATURE_CHECK != "off":
        names.append("PLIVO_AUTH_TOKEN")
    missing = [name for name in names if not globals()[name]]
    if missing:
        raise ValueError(f"Login config missing: {', '.join(missing)}")
    if len(APP_SECRET) < APP_SECRET_MIN_CHARS:
        raise ValueError(f"APP_SECRET must be at least {APP_SECRET_MIN_CHARS} characters")
    if (OPERATOR_USERNAME, OPERATOR_PASSWORD) == (ADMIN_USERNAME, ADMIN_PASSWORD):
        raise ValueError("Operator and admin logins must differ")


def session_verdict(access, role):
    """For a login class: None (allowed), 'login' (no valid session) or 'forbidden' (wrong role)."""
    if role == "admin" or (role == "operator" and access in (OPERATOR_PAGE, OPERATOR_API)):
        return None
    return "forbidden" if role == "operator" else "login"


def _same(given, expected):
    return hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8"))


def match_login(username, password):
    """'admin', 'operator' or None. Both pairs are always compared (no early exit)."""
    if not (ADMIN_USERNAME and ADMIN_PASSWORD and OPERATOR_USERNAME and OPERATOR_PASSWORD):
        return None   # unconfigured: an empty pair must never get in
    admin = _same(username, ADMIN_USERNAME) & _same(password, ADMIN_PASSWORD)
    operator = _same(username, OPERATOR_USERNAME) & _same(password, OPERATOR_PASSWORD)
    if admin:
        return "admin"
    return "operator" if operator else None


LOGIN_MAX_FAILURES = 5
LOGIN_WINDOW_S = 15 * 60
_login_failures = {}   # client ip -> failure times (time.monotonic) inside the window


def _recent_failures(ip, now):
    times = [t for t in _login_failures.get(ip, ()) if now - t < LOGIN_WINDOW_S]
    if times:
        _login_failures[ip] = times
    else:
        _login_failures.pop(ip, None)
    return times


def login_blocked(ip, now=None):
    now = time.monotonic() if now is None else now
    return len(_recent_failures(ip, now)) >= LOGIN_MAX_FAILURES


def note_login_failure(ip, now=None):
    now = time.monotonic() if now is None else now
    for other in list(_login_failures):   # drop stale IPs so the table cannot grow without bound
        _recent_failures(other, now)
    _login_failures.setdefault(ip, []).append(now)


def client_ip(remote_addr, headers):
    """nginx's X-Real-IP, trusted only when the request came from nginx on this box."""
    real = (headers.get("X-Real-IP") or "").strip()
    if remote_addr == "127.0.0.1" and real:
        return real
    return remote_addr or ""


MEDIA_TOKEN_TTL_S = 120


def _media_sig(params, exp):
    # JSON of the sorted pairs: unambiguous whatever characters a value holds.
    message = json.dumps(["media-stream", sorted(params.items()), exp])
    return hmac.new(APP_SECRET.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()


def media_stream_query(params, now=None):
    """Query for the /media-stream URL handed to Plivo: params plus exp and sig, joined with &amp;
    (it goes inside Plivo XML). Empty values are left out; the handler defaults a missing one to ""."""
    now = time.time() if now is None else now
    values = {name: str(value) for name, value in params.items() if value not in (None, "")}
    exp = str(int(now) + MEDIA_TOKEN_TTL_S)
    pairs = list(values.items()) + [("exp", exp), ("sig", _media_sig(values, exp))]
    return "&amp;".join(f"{name}={urllib.parse.quote(value)}" for name, value in pairs)


def media_token_problem(args, now=None):
    """None if the websocket's query carries a valid, unexpired token, else 'missing', 'bad-sig' or 'expired'."""
    sig, exp = args.get("sig") or "", args.get("exp") or ""
    if not sig or not exp:
        return "missing"
    params = {name: args.get(name) for name in args if name not in ("sig", "exp")}
    if not hmac.compare_digest(sig.encode("utf-8"), _media_sig(params, exp).encode("utf-8")):
        return "bad-sig"
    now = time.time() if now is None else now
    if not exp.isdigit() or int(exp) < now:
        return "expired"
    return None


PLIVO_SIGNATURE_HEADER = "X-Plivo-Signature-V3"
PLIVO_NONCE_HEADER = "X-Plivo-Signature-V3-Nonce"


def plivo_signature_ok(method, public_base_url, path, query_string, form, headers):
    """True if Plivo signed this request. The URL is rebuilt from PUBLIC_BASE_URL because behind
    nginx the request URL is http://127.0.0.1:<port>/...; form is the POST body as a dict of lists."""
    signature, nonce = headers.get(PLIVO_SIGNATURE_HEADER), headers.get(PLIVO_NONCE_HEADER)
    if not (signature and nonce and PLIVO_AUTH_TOKEN and public_base_url):
        return False
    uri = public_base_url.rstrip("/") + path + (f"?{query_string}" if query_string else "")
    method = method.upper()
    try:
        return validate_v3_signature(method, uri, nonce, PLIVO_AUTH_TOKEN, signature,
                                     dict(form) if method == "POST" else {})
    except Exception:   # the SDK's argument validators raise on a bad method, URL or header
        return False


def without_cost(event_payload):
    """A dashboard event (JSON text) without data.cost, for operator browsers."""
    event = json.loads(event_payload)
    data = event.get("data")
    if isinstance(data, dict) and "cost" in data:
        del data["cost"]
        return json.dumps(event)
    return event_payload
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `venv312/Scripts/python.exe -m unittest tests.test_web_auth -v`
Expected: all OK.

- [ ] **Step 5: Run the full suite**

Run: `venv312/Scripts/python.exe -m unittest discover -s tests -t .`
Expected: green (only the 3 known `test_prompt_catalog` failures). app.py is untouched in this task.

---

### Task 2: Logins, pages and default deny in `app.py`

**Files:**
- Modify: `app.py` (imports at line 2, `import web_auth` after `import call_transcript` at line 51, session config after `app = Quart(__name__)` at line 1035, hook and routes after `serve_dashboard` at line 1316-1320, `__main__` at line 3683)
- Create: `login.html`, `admin.html`
- Modify: `tests/harness/appctl.py` (`DIAGNOSTIC_LOG_TAGS`), `tests/test_preservation_4_8_resumption_recording_stats.py` (re-pin)
- Test: `tests/test_access_control.py`

**Interfaces:**
- Consumes: Task 1's `web_auth` (`ENDPOINT_ACCESS`, the class constants, `session_verdict`, `match_login`, `login_blocked`, `note_login_failure`, `client_ip`, `check_config`, `APP_SECRET`, `PLIVO_SIGNATURE_CHECK`).
- Produces:
  - `app.web_auth` (the module, as imported by app.py).
  - `async def enforce_access()` registered with `@app.before_request`. In this task the `PLIVO` class is let through; Task 3 replaces that branch.
  - Routes `login_page` (GET `/login`), `login_submit` (POST `/login`), `logout` (POST `/logout`), `api_me` (GET `/api/me`, returns `{"role": ...}`), `admin_page` (GET `/admin`).
  - `LOGIN_ERROR_MARK = "<!--LOGIN_ERROR-->"` and `login_response(message: str = "", status: int = 200) -> Response`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_access_control.py`:

```python
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
            mock.patch.object(app.app, "secret_key", CREDS["APP_SECRET"]),
            mock.patch.dict(app.app.config, {"SESSION_COOKIE_SECURE": False}),
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
                await response.get_data(as_text=True)
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

    def test_secure_flag_follows_public_base_url_and_startup_checks_config(self):
        with open(APP_PY, encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn('SESSION_COOKIE_SECURE=(PUBLIC_BASE_URL or "").startswith("https://")', source)
        self.assertIn("web_auth.check_config()", source[source.index('if __name__ == "__main__":'):])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `venv312/Scripts/python.exe -m unittest tests.test_access_control -v`
Expected: ERROR at import, `AttributeError: module 'app' has no attribute 'web_auth'`.

- [ ] **Step 3: Wire `web_auth` into `app.py`**

1. Line 2, replace
```python
from quart import Quart, websocket, Response, request , send_from_directory , send_file
```
with
```python
from quart import Quart, websocket, Response, request , send_from_directory , send_file, session, redirect, abort
```

2. After the two lines
```python
# Post-call transcript files (call_transcript.py). Imported after load_dotenv() so its config sees .env.
import call_transcript
```
add
```python

# Logins and Plivo request checks (web_auth.py). Imported after load_dotenv() so its config sees .env.
import web_auth
```

3. Replace the line `app = Quart(__name__)` (line 1035) with
```python
app = Quart(__name__)
# Login sessions (docs/spec/admin-auth-design.md §1): a signed cookie holding only the role.
app.secret_key = web_auth.APP_SECRET or None
app.config.update(
    SESSION_COOKIE_NAME="au_session",
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=(PUBLIC_BASE_URL or "").startswith("https://"),
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
)
```

4. Replace the `serve_dashboard` route
```python
@app.route("/", methods=["GET"])
async def serve_dashboard():
    """Serves the frontend dashboard."""
    logger.info("Serving the dashboard UI")
    return await send_file("index.html")
```
with
```python
@app.route("/", methods=["GET"])
async def serve_dashboard():
    """Serves the frontend dashboard."""
    logger.info("Serving the dashboard UI")
    return await send_file("index.html")


@app.before_request
async def enforce_access():
    """Default deny (docs/spec/admin-auth-design.md §2): every endpoint must be in web_auth.ENDPOINT_ACCESS."""
    endpoint = request.endpoint
    if endpoint is None:
        return None   # no such route: Quart answers 404 itself
    access = web_auth.ENDPOINT_ACCESS.get(endpoint)
    if access in (web_auth.OPEN, web_auth.PLIVO):
        return None
    if access is None or access == web_auth.MEDIA:
        logger.warning(f"🔐 [AUTH] refused endpoint={endpoint}")
        return {"status": "error", "message": "forbidden"}, 403
    verdict = web_auth.session_verdict(access, session.get("role"))
    if verdict is None:
        return None
    if access in (web_auth.OPERATOR_PAGE, web_auth.ADMIN_PAGE):
        return redirect("/" if verdict == "forbidden" else "/login")
    if verdict == "forbidden":
        return {"status": "error", "message": "forbidden"}, 403
    return {"status": "error", "message": "login required"}, 401


LOGIN_ERROR_MARK = "<!--LOGIN_ERROR-->"


def login_response(message="", status=200):
    """login.html with its error line filled in (fixed texts only, never user input)."""
    with open("login.html", encoding="utf-8") as handle:
        html = handle.read()
    return Response(html.replace(LOGIN_ERROR_MARK, message), status=status, mimetype="text/html")


@app.route("/login", methods=["GET"])
async def login_page():
    return login_response()


@app.route("/login", methods=["POST"])
async def login_submit():
    ip = web_auth.client_ip(request.remote_addr, request.headers)
    if web_auth.login_blocked(ip):
        logger.warning(f"🔐 [AUTH] login throttled ip={ip}")
        return login_response("Too many attempts. Try again later.", 429)
    form = await request.form
    role = web_auth.match_login(form.get("username", ""), form.get("password", ""))
    if role is None:
        web_auth.note_login_failure(ip)
        logger.warning(f"🔐 [AUTH] login failed ip={ip}")
        return login_response("Wrong username or password.", 401)
    session.clear()
    session["role"] = role
    session.permanent = True
    logger.info(f"🔐 [AUTH] login ok role={role} ip={ip}")
    return redirect("/admin" if role == "admin" else "/")


@app.route("/logout", methods=["POST"])
async def logout():
    session.clear()
    return redirect("/login")


@app.route("/api/me", methods=["GET"])
async def api_me():
    return {"role": session.get("role")}


@app.route("/admin", methods=["GET"])
async def admin_page():
    return await send_file("admin.html")
```

5. In `__main__`, after the line `logger.info(f"📝 Prompt: {PROMPT_FILE} ({len(RAW_SYSTEM_PROMPT)} chars)")` add
```python
    web_auth.check_config()
    logger.info(f"🔐 [AUTH] logins configured, plivo checks={web_auth.PLIVO_SIGNATURE_CHECK}")
```

- [ ] **Step 4: Create `login.html`**

```html
<!DOCTYPE html>
<html lang="en">

<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Sign in | AU Admission Assist</title>
    <link rel="icon" href="/atc.png?v=2" type="image/png">
    <style>
        :root {
            --bg-primary: #f4f7fb;
            --bg-card: #ffffff;
            --text-primary: #0a1b2d;
            --text-secondary: #4a627d;
            --border-color: #d6e2ee;
            --accent: #005aab;
            --accent-hover: #004585;
            --danger: #e11d48;
            --focus-ring: 0 0 0 3px rgba(0, 90, 171, 0.18);
        }

        * { box-sizing: border-box; }

        body {
            margin: 0;
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            background: var(--bg-primary);
            color: var(--text-primary);
            font-family: "Montserrat", system-ui, sans-serif;
        }

        .card {
            width: 100%;
            max-width: 360px;
            padding: 32px;
            background: var(--bg-card);
            border: 1px solid var(--border-color);
            border-radius: 14px;
            box-shadow: 0 4px 18px -2px rgba(0, 35, 75, 0.06);
        }

        .card img { display: block; height: 48px; margin: 0 auto 16px; }
        h1 { margin: 0 0 20px; font-size: 1.1rem; text-align: center; }
        label { display: block; margin: 12px 0 6px; font-size: 0.85rem; color: var(--text-secondary); }

        input {
            width: 100%;
            padding: 10px 12px;
            border: 1px solid var(--border-color);
            border-radius: 8px;
            font: inherit;
        }

        input:focus { outline: none; border-color: var(--accent); box-shadow: var(--focus-ring); }

        button {
            width: 100%;
            margin-top: 20px;
            padding: 11px;
            border: 0;
            border-radius: 8px;
            background: var(--accent);
            color: #ffffff;
            font: inherit;
            font-weight: 600;
            cursor: pointer;
        }

        button:hover { background: var(--accent-hover); }
        .error { min-height: 1.2em; margin: 12px 0 0; color: var(--danger); font-size: 0.85rem; text-align: center; }
    </style>
</head>

<body>
    <form class="card" method="post" action="/login">
        <img src="/AU_Logo.png?v=2" alt="Adamas University">
        <h1>AU Admission Assist</h1>
        <label for="username">Username</label>
        <input id="username" name="username" autocomplete="username" required autofocus>
        <label for="password">Password</label>
        <input id="password" name="password" type="password" autocomplete="current-password" required>
        <button type="submit">Sign in</button>
        <p class="error" role="alert"><!--LOGIN_ERROR--></p>
    </form>
</body>

</html>
```

- [ ] **Step 5: Create `admin.html`**

```html
<!DOCTYPE html>
<html lang="en">

<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Admin | AU Admission Assist</title>
    <link rel="icon" href="/atc.png?v=2" type="image/png">
    <style>
        :root {
            --bg-primary: #f4f7fb;
            --bg-card: #ffffff;
            --text-primary: #0a1b2d;
            --text-secondary: #4a627d;
            --border-color: #d6e2ee;
            --accent: #005aab;
            --accent-light: #e8f2fc;
        }

        * { box-sizing: border-box; }

        body {
            margin: 0;
            background: var(--bg-primary);
            color: var(--text-primary);
            font-family: "Montserrat", system-ui, sans-serif;
        }

        nav {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 12px 24px;
            background: var(--bg-card);
            border-bottom: 1px solid var(--border-color);
        }

        nav img { height: 40px; }
        .actions { display: flex; gap: 10px; align-items: center; }
        .actions form { margin: 0; }

        .nav-link {
            height: 40px;
            padding: 0 14px;
            display: inline-flex;
            align-items: center;
            background: var(--accent-light);
            border: 1px solid var(--border-color);
            border-radius: 10px;
            color: var(--text-primary);
            font: inherit;
            font-size: 0.85rem;
            font-weight: 600;
            text-decoration: none;
            cursor: pointer;
        }

        main { max-width: 960px; margin: 40px auto; padding: 0 24px; }
        h1 { font-size: 1.3rem; }
        p { color: var(--text-secondary); }
    </style>
</head>

<body>
    <nav>
        <img src="/AU_Logo.png?v=2" alt="Adamas University">
        <div class="actions">
            <a class="nav-link" href="/">Calls</a>
            <form method="post" action="/logout"><button class="nav-link" type="submit">Logout</button></form>
        </div>
    </nav>
    <main>
        <h1>Admin</h1>
        <p>Call costs, transcripts, lead results and campaign reports will appear here.</p>
    </main>
</body>

</html>
```

- [ ] **Step 6: Add the log tag**

In `tests/harness/appctl.py`, in `DIAGNOSTIC_LOG_TAGS`, after the line `"[TRANSCRIPT]",   # post-call transcript job (call_transcript.py)` add
```python
    "[AUTH]",         # logins, refused endpoints, Plivo signature and media-stream token checks
```

- [ ] **Step 7: Run the new tests**

Run: `venv312/Scripts/python.exe -m unittest tests.test_access_control -v`
Expected: all OK.

- [ ] **Step 8: Re-pin the GEMINI_MODEL lines**

Run: `venv312/Scripts/python.exe -m unittest tests.test_preservation_4_8_resumption_recording_stats -v`
Expected: `test_model_identifier_is_recorded` fails, showing the new list of 5 line numbers. Check each new line in `app.py` is still the same site (the definition, `log_call_stats`, the connect call, the `🧬 [MODEL]` line, the `[ANOMALY]` line). Replace the list in `self.assertEqual(references, [...])` with the new one and add one comment line above it:
```python
        # And again for the admin login (import web_auth, session config, login routes and hook).
```
Run the test again. Expected: OK.

- [ ] **Step 9: Run the full suite**

Run: `venv312/Scripts/python.exe -m unittest discover -s tests -t .`
Expected: green (only the 3 known `test_prompt_catalog` failures). In particular `tests.test_multiprocess_events` still passes (`internal_call_event` is `OPEN`).

---

### Task 3: Plivo signatures and signed `/media-stream` URLs

**Files:**
- Modify: `app.py` (`enforce_access` from Task 2; new `plivo_signature_gate` and `enforce_media_token`; `outbound_webhook` at line 1349; `dial_action` at line 1414)
- Modify: `tests/test_preservation_4_8_resumption_recording_stats.py` (re-pin)
- Test: `tests/test_plivo_checks.py`

**Interfaces:**
- Consumes: Task 1's `web_auth.plivo_signature_ok`, `media_stream_query`, `media_token_problem`, `PLIVO_SIGNATURE_CHECK`, `ENDPOINT_ACCESS`, `MEDIA`; Task 2's `enforce_access`. Task 1's test helpers `plivo_headers`, `parse_stream_query`, `PLIVO_TOKEN`, `BASE` from `tests.test_web_auth`.
- Produces: `async def plivo_signature_gate(endpoint: str) -> Response | None`; `async def enforce_media_token()` registered with `@app.before_websocket`; stream URLs from `/outbound-webhook` and `/dial-action` carry `exp` and `sig`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_plivo_checks.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `venv312/Scripts/python.exe -m unittest tests.test_plivo_checks -v`
Expected: FAIL. Unsigned webhooks return 200 instead of 403, stream URLs have no `exp`/`sig` (`AttributeError` on `.group` or a `missing` token problem), and bad media tokens get `"accepted"` instead of 403.

- [ ] **Step 3: Check Plivo signatures in `enforce_access`**

In `enforce_access` (Task 2), replace
```python
    if access in (web_auth.OPEN, web_auth.PLIVO):
        return None
```
with
```python
    if access == web_auth.OPEN:
        return None
    if access == web_auth.PLIVO:
        return await plivo_signature_gate(endpoint)
```
and add, directly after the `enforce_access` function:
```python
async def plivo_signature_gate(endpoint):
    """Only Plivo may call its webhooks (X-Plivo-Signature-V3). PLIVO_SIGNATURE_CHECK=warn logs and allows."""
    if web_auth.PLIVO_SIGNATURE_CHECK == "off":
        return None
    form = (await request.form).to_dict(flat=False) if request.method == "POST" else {}
    if web_auth.plivo_signature_ok(request.method, PUBLIC_BASE_URL, request.path,
                                   request.query_string.decode("utf-8", "replace"), form, request.headers):
        return None
    if web_auth.PLIVO_SIGNATURE_CHECK == "warn":
        logger.warning(f"🔐 [AUTH] plivo signature rejected endpoint={endpoint} method={request.method} (warn, allowed)")
        return None
    logger.warning(f"🔐 [AUTH] plivo signature rejected endpoint={endpoint} method={request.method}")
    return Response("forbidden", status=403)


@app.before_websocket
async def enforce_media_token():
    """Only Plivo, holding a stream URL this app signed in the last 120 s, may open /media-stream.
    Runs before handle_media_stream, so a refused connect never starts Gemini, STT or a recording."""
    if web_auth.ENDPOINT_ACCESS.get(websocket.endpoint) != web_auth.MEDIA:
        logger.warning(f"🔐 [AUTH] refused websocket endpoint={websocket.endpoint}")
        abort(403)
    if web_auth.PLIVO_SIGNATURE_CHECK == "off":
        return
    problem = web_auth.media_token_problem(websocket.args)
    if problem is None:
        return
    if web_auth.PLIVO_SIGNATURE_CHECK == "warn":
        logger.warning(f"🔐 [AUTH] media-stream rejected reason={problem} (warn, allowed)")
        return
    logger.warning(f"🔐 [AUTH] media-stream rejected reason={problem}")
    abort(403)
```

- [ ] **Step 4: Sign the stream URL in `outbound_webhook`**

In `outbound_webhook`, replace
```python
    import urllib.parse
    encoded_name = urllib.parse.quote(user_name)
    encoded_phone = urllib.parse.quote(phone_number)
    
    ws_base_url = PUBLIC_BASE_URL.replace("https://", "wss://").replace("http://", "ws://")
```
with
```python
    stream_query = web_auth.media_stream_query({"user_name": user_name, "phone_number": phone_number})

    ws_base_url = PUBLIC_BASE_URL.replace("https://", "wss://").replace("http://", "ws://")
```
and replace
```python
            {ws_base_url}/media-stream?user_name={encoded_name}&amp;phone_number={encoded_phone}
```
with
```python
            {ws_base_url}/media-stream?{stream_query}
```

- [ ] **Step 5: Sign the stream URL in `dial_action`**

In `dial_action`, replace
```python
    encoded_phone = urllib.parse.quote(original_from)
    encoded_context_id = urllib.parse.quote(call_uuid or "")
    
    # Map Plivo's successful states to 'completed' so handle_media_stream reads it correctly
    is_success = "completed" if dial_status in ["hangup", "answered", "completed"] else "failed"
    encoded_status   = urllib.parse.quote(is_success)
```
with
```python
    # Map Plivo's successful states to 'completed' so handle_media_stream reads it correctly
    is_success = "completed" if dial_status in ["hangup", "answered", "completed"] else "failed"
    stream_query = web_auth.media_stream_query({
        "phone_number": original_from,
        "transfer_context_id": call_uuid or "",
        "dial_status": is_success,
        "is_transfer_return": "1",
    })
```
and replace
```python
            wss://{ws_host}/media-stream?phone_number={encoded_phone}&amp;transfer_context_id={encoded_context_id}&amp;dial_status={encoded_status}&amp;is_transfer_return=1
```
with
```python
            wss://{ws_host}/media-stream?{stream_query}
```

- [ ] **Step 6: Run the new tests**

Run: `venv312/Scripts/python.exe -m unittest tests.test_plivo_checks tests.test_access_control tests.test_web_auth -v`
Expected: all OK.

- [ ] **Step 7: Re-pin the GEMINI_MODEL lines**

Run: `venv312/Scripts/python.exe -m unittest tests.test_preservation_4_8_resumption_recording_stats -v`
Expected: `test_model_identifier_is_recorded` fails with the new list of 5. Check the 5 sites as in Task 2, replace the list, and add one comment line:
```python
        # And again for the Plivo signature gate and the media-stream token hook.
```
Run again. Expected: OK.

- [ ] **Step 8: Run the full suite**

Run: `venv312/Scripts/python.exe -m unittest discover -s tests -t .`
Expected: green (only the 3 known `test_prompt_catalog` failures).

---

### Task 4: The calls page for the operator: no costs, no API key

**Files:**
- Modify: `app.py` (`API_AUTH_TOKEN` line 133; `get_call_history`; `call_events_stream`; `trigger_call`)
- Modify: `index.html` (CSS before `/* ─── Theme Toggle ─── */` at line 270; `nav-right` at line 1457; `connectSSE` at line 1968; `triggerBackendCall` at line 1998; the history `fetch` at line 2319)
- Modify: `tests/test_preservation_4_8_resumption_recording_stats.py` (re-pin)
- Test: `tests/test_operator_view.py`

**Interfaces:**
- Consumes: Task 1's `web_auth.without_cost`; Task 2's session (`session.get("role")`) and `AuthTestCase` (patched logins, temp call-history DB, `client(role)` coroutine) and `run` from `tests.test_access_control`.
- Produces: `/api/call-history` and `/call-events` without `cost` for the operator; `/trigger-call` that needs only the session and a JSON object body; `app.API_AUTH_TOKEN` no longer exists.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_operator_view.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `venv312/Scripts/python.exe -m unittest tests.test_operator_view -v`
Expected: FAIL. The operator row and event still carry `cost`, `/trigger-call` returns 401 (API key check), `API_AUTH_TOKEN` exists, `index.html` still has the key.

- [ ] **Step 3: Remove costs for the operator in `app.py`**

1. In `get_call_history`, replace
```python
    args = dict(request.args)
    
    def fetch_history():
```
with
```python
    args = dict(request.args)
    show_cost = session.get("role") == "admin"   # the operator never sees costs (spec §2)
    
    def fetch_history():
```
and replace
```python
                d = dict(r)
                if d["created_at"]:
```
with
```python
                d = dict(r)
                if not show_cost:
                    d.pop("cost", None)
                if d["created_at"]:
```

2. In `call_events_stream`, replace
```python
    q = asyncio.Queue(maxsize=200)
    call_event_subscribers.add(q)

    async def event_generator():
        try:
            while True:
                payload = await q.get()
                yield f"data: {payload}\n\n"
```
with
```python
    q = asyncio.Queue(maxsize=200)
    call_event_subscribers.add(q)
    show_cost = session.get("role") == "admin"   # the operator never sees costs (spec §2)

    async def event_generator():
        try:
            while True:
                payload = await q.get()
                if not show_cost:
                    payload = web_auth.without_cost(payload)
                yield f"data: {payload}\n\n"
```

- [ ] **Step 4: `/trigger-call` takes the session and JSON only**

1. Delete the line
```python
API_AUTH_TOKEN = os.getenv("API_AUTH_TOKEN", "default-dev-token")
```

2. In `trigger_call`, keep the `@app.route("/trigger-call", methods=["POST"])` line exactly as it is and replace
```python
    api_key = request.headers.get("X-API-Key")
    if not api_key or api_key != API_AUTH_TOKEN:
        logger.warning(f"Unauthorized access attempt to /trigger-call from {request.remote_addr}")
        return {"status": "error", "message": "Unauthorized"}, 401

    data = await request.get_json()
```
with
```python
    # The login session (enforce_access) decides who may call. JSON only: a cross-site form cannot
    # send it with our cookie, which blocks CSRF.
    if not request.is_json:
        return {"status": "error", "message": "JSON body required"}, 415
    data = await request.get_json(silent=True)
    if not isinstance(data, dict):
        return {"status": "error", "message": "JSON object required"}, 400
```

- [ ] **Step 5: Update `index.html`**

1. Directly before the line `        /* ─── Theme Toggle ─── */` add
```css
        /* ─── Admin link / Logout ─── */
        .logout-form {
            margin: 0;
        }

        .nav-link {
            height: 40px;
            padding: 0 14px;
            display: inline-flex;
            align-items: center;
            background: var(--bg-subtle);
            border: 1px solid var(--border-color);
            border-radius: 10px;
            color: var(--text-primary);
            font: inherit;
            font-size: 0.85rem;
            font-weight: 600;
            text-decoration: none;
            cursor: pointer;
        }

        .nav-link:hover {
            background: var(--accent-light);
            border-color: var(--accent);
            color: var(--accent);
        }

        .nav-link[hidden] {
            display: none;
        }

```

2. Replace
```html
        <div class="nav-right">
            <div class="sse-indicator" id="sseIndicator">
```
with
```html
        <div class="nav-right">
            <a href="/admin" class="nav-link" id="adminLink" hidden>Admin</a>
            <form method="post" action="/logout" class="logout-form">
                <button type="submit" class="nav-link">Logout</button>
            </form>
            <div class="sse-indicator" id="sseIndicator">
```

3. In `connectSSE`, replace
```js
            evtSource.onerror = () => {
                sseIndicator.className = "sse-indicator error";
                sseLabel.textContent = "Reconnecting";
```
with
```js
            evtSource.onerror = () => {
                sseIndicator.className = "sse-indicator error";
                sseLabel.textContent = "Reconnecting";

                // An expired login also ends the stream: go to the login page instead of retrying.
                fetch("/api/me").then((r) => { if (r.status === 401) window.location.href = "/login"; }).catch(() => { });
```

4. Replace
```js
        connectSSE();

```
with
```js
        connectSSE();

        // Admin link only for the admin login (the server enforces it either way).
        fetch("/api/me").then((r) => (r.ok ? r.json() : null)).then((me) => {
            if (me && me.role === "admin") document.getElementById("adminLink").hidden = false;
        }).catch(() => { });

```

5. In `triggerBackendCall`, replace
```js
                    headers: { "Content-Type": "application/json", "X-API-Key": "default-dev-token" },
                    body: JSON.stringify({ user_name: name, phone_number: phoneDigits }),
                });

```
with
```js
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ user_name: name, phone_number: phoneDigits }),
                });
                if (response.status === 401) {
                    window.location.href = "/login";
                    return { ok: false, message: "Login expired.", callId: null };
                }

```

6. In the history loader, replace
```js
                const res = await fetch(url);
                const data = await res.json();
```
with
```js
                const res = await fetch(url);
                if (res.status === 401) {
                    window.location.href = "/login";
                    return;
                }
                const data = await res.json();
```

The cost text in the history list (`if (call.cost)`) needs no change: the server leaves `cost` out for the operator.

- [ ] **Step 6: Run the new tests**

Run: `venv312/Scripts/python.exe -m unittest tests.test_operator_view tests.test_access_control -v`
Expected: all OK.

- [ ] **Step 7: Re-pin the GEMINI_MODEL lines**

Run: `venv312/Scripts/python.exe -m unittest tests.test_preservation_4_8_resumption_recording_stats -v`
Expected: `test_model_identifier_is_recorded` fails with the new list of 5 (`API_AUTH_TOKEN` removed above the definition shifts everything by -1, the rest moves with the edits). Check the 5 sites, replace the list, and add one comment line:
```python
        # And again for the operator view (API_AUTH_TOKEN removed, cost removal, /trigger-call JSON).
```
Run again. Expected: OK.

- [ ] **Step 8: Run the full suite**

Run: `venv312/Scripts/python.exe -m unittest discover -s tests -t .`
Expected: green (only the 3 known `test_prompt_catalog` failures). `test_no_pacing_sleep_exists_in_the_sender` (tests 4_5 and 5_4) still finds the `@app.route("/trigger-call"` marker.

---

### Task 5: Docs and the local check

**Files:**
- Modify: `CLAUDE.md`, `docs/HANDOFF.md:964`, `docs/handoff1.md:48`, `docs/spec/admin-auth-design.md` (status line), `docs/spec/admin-system-overview.md` (build order item 1)

**Interfaces:**
- Consumes: Tasks 1-4 done, suite green.
- Produces: docs that match the code; the operator's deploy checklist (spec §6).

`<date>` below means the day this task runs, as `YYYY-MM-DD`.

- [ ] **Step 1: Update the docs**

1. `docs/HANDOFF.md` line 964: append to the bullet
```
 **Fixed 2026-10-12 (admin-auth step 1, operator decision):** `API_AUTH_TOKEN` and the browser key are gone; `/trigger-call` needs an operator or admin login session (`docs/spec/admin-auth-design.md`).
```
2. `docs/handoff1.md` line 48: replace `- \`API_AUTH_TOKEN\` issue: noted in HANDOFF §10, not to be fixed.` with
```
- `API_AUTH_TOKEN` issue: fixed 2026-10-12 by the admin login (docs/spec/admin-auth-design.md); `/trigger-call` needs a login session.
```
3. `docs/spec/admin-auth-design.md` first status line: append ` Implemented <date>, plan docs/spec/admin-auth-plan.md; not yet on EC2.`
4. `docs/spec/admin-system-overview.md`, build order item 1: append ` Implemented <date>; EC2 rollout per that spec's §6.`
5. `CLAUDE.md`:
   - In the standing rules line, replace `API_AUTH_TOKEN issue is noted in HANDOFF §10, not to be fixed.` with `/trigger-call needs a login session since the admin-auth step (API_AUTH_TOKEN removed).`
   - Under "Resume here", add a new first item:
```
000. **Admin login and protection (step 1 of the admin system), built and tested offline <date>, not yet on EC2.** `web_auth.py`, hooks and routes in app.py, `login.html`, `admin.html`. Spec `docs/spec/admin-auth-design.md`, plan `docs/spec/admin-auth-plan.md`, overview `docs/spec/admin-system-overview.md`. Next: operator adds `OPERATOR_USERNAME/PASSWORD`, `ADMIN_USERNAME/PASSWORD`, `APP_SECRET` (32+ chars) to `.env` and `/etc/au-app/common.env`, plus `PLIVO_SIGNATURE_CHECK=warn` on EC2; deploy per spec §6; one live call (and a transfer); no `[AUTH]` rejections, then `enforce`. Then step 2 (data model + Haiku NLP).
```
   - Update the suite line to the new count (from Step 2 below).
   - Add a session-log entry `### <date>: admin login and protection (step 1)` with: what was built (the five config keys, the access table and default deny, the two Plivo checks and the `PLIVO_SIGNATURE_CHECK` switch, operator cost removal, `/trigger-call` session + JSON), the new test files and their counts, and the 4_8 re-pins.

- [ ] **Step 2: Run the full suite and note the count**

Run: `venv312/Scripts/python.exe -m unittest discover -s tests -t .`
Expected: green (only the 3 known `test_prompt_catalog` failures). Put the run count into CLAUDE.md's suite line.

- [ ] **Step 3: Local check (operator)**

The operator adds the five keys to the local `.env` (never read by the agent), then runs `venv312/Scripts/python.exe app.py` and checks in a browser at `http://localhost:8000`:
- `/` redirects to `/login`; a wrong password shows "Wrong username or password."
- The operator login lands on `/`, no Admin link, no cost in the history list; Logout works.
- The admin login lands on `/admin`; its Calls link shows the calls page with costs and an Admin link.
- Startup log shows `🔐 [AUTH] logins configured, plivo checks=enforce`. With one key removed from `.env`, startup stops with `Login config missing: <name>`.

EC2 rollout is spec §6 (warn first, one live call, then enforce), done by the operator; it is not part of this plan's tasks.
