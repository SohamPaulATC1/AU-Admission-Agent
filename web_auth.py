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
    "campaigns_page": OPERATOR_PAGE,
    "list_campaigns_api": OPERATOR_API,
    "create_campaign_api": OPERATOR_API,
    "campaign_detail_api": OPERATOR_API,
    "campaign_action_api": OPERATOR_API,
    "campaign_settings_api": OPERATOR_API,
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
    """A dashboard event (JSON text) without its cost fields (cost, *cost_usd), for operator browsers."""
    event = json.loads(event_payload)
    data = event.get("data")
    if isinstance(data, dict):
        costly = [key for key in data if key == "cost" or key.endswith("cost_usd")]
        if costly:
            for key in costly:
                del data[key]
            return json.dumps(event)
    return event_payload
