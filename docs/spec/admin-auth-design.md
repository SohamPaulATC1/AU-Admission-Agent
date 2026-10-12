# Step 1: login and protection

Status: design approved by the operator 2026-10-12 (all four parts). Shared decisions:
`docs/spec/admin-system-overview.md`. Implemented 2026-10-12, plan docs/spec/admin-auth-plan.md; live on EC2 2026-10-12 (enforce).

## Problem

The agent's URL goes to another team. Today anyone with it can:
- open the dashboard (`/`), the history API (`/api/call-history`) and the live feed (`/call-events`),
  which show names and phone numbers;
- place calls: `/trigger-call` checks `X-API-Key`, but `index.html` sends the fallback value
  `default-dev-token` from the browser (logged in HANDOFF §10 as "not fixed"; this step fixes it, by
  operator decision 2026-10-12);
- open `/media-stream` directly and start a Gemini Live session billed to us, with no phone call;
- forge Plivo callbacks, e.g. a `/plivo-hangup` POST that marks a call failed (and, after step 3,
  frees a dispatcher slot that is still in use).

Goal: nothing that spends money or shows data is reachable without a login or a Plivo proof.

## 1. Logins and pages

### Configuration

Read from the environment (`.env` locally; `/etc/au-app/common.env` on EC2, which all four
processes load):

| Variable | Use |
|---|---|
| `OPERATOR_USERNAME`, `OPERATOR_PASSWORD` | the calling team's shared login |
| `ADMIN_USERNAME`, `ADMIN_PASSWORD` | the owner's shared login |
| `APP_SECRET` | signs the session cookie and the `/media-stream` token; at least 32 characters |
| `PLIVO_SIGNATURE_CHECK` | `enforce` (default), `warn` or `off`; see section 3 |

- Checked in `__main__` before the server starts (not at import, so the test suite imports `app`
  without them): any value missing, `APP_SECRET` shorter than 32, or both logins being the same
  username and password raises and the process does not start.
- Passwords are compared with `hmac.compare_digest`, both pairs always checked (no early exit), and
  are never logged.
- This changes `.env`, which CLAUDE.md lists as frozen; the operator's request for logins from
  `.env` lifts that for these keys.

### Session

- Quart's built-in signed cookie session (`app.secret_key = APP_SECRET`; itsdangerous ships with
  Quart, nothing new installed). The cookie holds only `{"role": "operator" | "admin"}`.
- Cookie `au_session`: HttpOnly, SameSite=Lax, Secure when `PUBLIC_BASE_URL` starts with
  `https://` (so local `http://localhost` still works), lifetime 12 hours (`session.permanent`,
  `PERMANENT_SESSION_LIFETIME`). After 12 hours the user logs in again.
- Rotating `APP_SECRET` logs everyone out; that is the way to revoke all sessions.

### Pages and endpoints added

- `GET /login`: a small standalone page (`login.html`, AU logo, username, password, one error line).
- `POST /login` (form fields `username`, `password`): on a match sets the session and redirects
  admin to `/admin`, operator to `/`. On a miss: the login page again with "Wrong username or
  password", HTTP 401.
- `POST /logout`: clears the session, redirects to `/login`.
- `GET /api/me`: `{"role": "operator" | "admin"}`. The calls page uses it to show the Admin link
  (admin only) and to notice an expired session.
- `GET /admin`: `admin.html`, an empty shell in this step (header, logout, link to the calls page);
  steps 2-4 fill it.

### Login throttle

- Per client IP: 5 failed logins within 15 minutes, then `429` ("Too many attempts, try again
  later") for the rest of that window, even with the right password.
- Client IP: `X-Real-IP` when the request comes from `127.0.0.1` (nginx), otherwise the socket
  address. A header from anywhere else is ignored, so it cannot be spoofed.
- In memory, per process. `/login` is only routed to the main process by nginx, so one process
  holds the count; a restart clears it (acceptable).

## 2. Who reaches what (default deny)

One table in `app.py` maps every endpoint to its access class, and one `before_request` hook
enforces it. An endpoint missing from the table is refused (403, `[AUTH]` log line), so a route
added later is closed until someone classifies it. Quart's built-in `static` endpoint, if it is
registered, is not in the table (nothing is served from it) and so is refused too.

| Endpoint (route) | Class | Not logged in | Operator | Admin |
|---|---|---|---|---|
| `serve_dashboard` (`/`) | page: operator | redirect `/login` | yes | yes |
| `get_call_history` (`/api/call-history`) | api: operator | 401 | yes, no `cost` field | yes |
| `call_events_stream` (`/call-events`) | api: operator | 401 | yes, no `cost` in events | yes |
| `trigger_call` (`/trigger-call`) | api: operator | 401 | yes | yes |
| `api_me` (`/api/me`) | api: operator | 401 | yes | yes |
| `admin_page` (`/admin`) | page: admin | redirect `/login` | redirect `/` | yes |
| later `/api/admin/*` | api: admin | 401 | 403 | yes |
| `login_page`, `login_submit`, `logout` | open | | | |
| `serve_audio`, `serve_atc_logo`, `serve_au_logo` | open (Plivo fetches the audio; logos) | | | |
| `internal_call_event` | open here; keeps its own token check, nginx returns 404 | | | |
| `outbound_webhook`, `hangup`, `transfer_xml`, `dial_action`, `plivo_ringing`, `plivo_hangup` | plivo: signature (section 3) | | | |
| `handle_media_stream` (websocket) | media: signed token (section 3) | | | |

- Not logged in: a page class redirects to `/login`; an api class gets `401 {"status": "error",
  "message": "login required"}`.
- Costs for the operator are removed on the server:
  - `/api/call-history`: the `cost` key is dropped from every row.
  - `/call-events`: each subscriber's queue remembers the role it connected with; for an operator the
    generator drops `data.cost` from `call_ended` events before sending. Admin events are unchanged.
- `/trigger-call`: the `X-API-Key` check and `API_AUTH_TOKEN` are removed; the session decides. It
  accepts only `Content-Type: application/json`. With SameSite=Lax cookies that blocks cross-site
  forms (CSRF): a cross-site page cannot send JSON with our cookie without a CORS preflight, which we
  never allow. The `@app.route("/trigger-call", methods=["POST"])` line stays as it is (two tests
  slice the source up to it).
- `index.html`: the hardcoded `X-API-Key` header goes. Every `fetch` that gets a 401 redirects to
  `/login`. The live feed's `onerror` asks `/api/me`; a 401 there redirects to `/login`. Cost text is
  shown only when present (the server already leaves it out for the operator). A Logout button and,
  for admin, an Admin link in the header.

## 3. Plivo-facing protection

### Webhook signatures

- The six Plivo endpoints check `X-Plivo-Signature-V3` and `X-Plivo-Signature-V3-Nonce` with the
  SDK's `plivo.utils.signature_v3.validate_v3_signature(method, uri, nonce, auth_token,
  v3_signature, params)`, using `PLIVO_AUTH_TOKEN` (the same token the REST client uses).
- `uri` is rebuilt, because behind nginx `request.url` is `http://127.0.0.1:8000/...`:
  `PUBLIC_BASE_URL` (trailing `/` stripped) + `request.path` + `?` + the raw query string (as sent,
  not re-encoded) when there is one. That is the URL Plivo was given.
- `params`: the POST form as a dict of lists (`to_dict(flat=False)`); empty for GET (the SDK takes
  GET parameters from the URL).
- A missing header or a mismatch: `403`, and `🔐 [AUTH] plivo signature rejected endpoint=<name>
  method=<m>` (no header values logged).

### Signed `/media-stream` URLs

- `/outbound-webhook` and `/dial-action` build the stream URL they hand Plivo. Both add
  `exp=<unix time + 120>` and `sig=<hex HMAC-SHA256>` to its query.
- `sig` = HMAC-SHA256 with `APP_SECRET` over `"media-stream\n"` + the decoded query parameters
  except `sig`, sorted by name, as `name=value` lines (so `quote` vs `urlencode` encoding does not
  matter, and no parameter or `exp` can be changed without breaking it).
- A `before_websocket` hook checks it before `handle_media_stream` runs, so no Gemini session, STT or
  recording starts for a bad connect: a missing or wrong `sig`, or `exp` in the past, aborts the
  handshake with 403 and logs `🔐 [AUTH] media-stream rejected reason=<missing|bad-sig|expired>`.
  All four processes share `APP_SECRET`, so whichever one nginx picks can check it.
- 120 seconds covers Plivo fetching the answer XML and opening the stream, including the
  `reconnecting.wav` play on a transfer return.
- Keeping the check in a hook, not inside `handle_media_stream`, leaves the offline harness (which
  drives the call functions directly) untouched.

### Rollout switch

`PLIVO_SIGNATURE_CHECK` covers both checks above:
- `enforce` (default): reject as described.
- `warn`: log the same `[AUTH]` line with `(warn, allowed)` and let the request through.
- `off`: no check (rollback).
First EC2 deploy runs `warn` (set in `common.env`); after one live call, ideally including a transfer,
shows no `[AUTH]` lines, switch to `enforce`. Without this step a URL mismatch under `enforce` would
silently break every call.

## 4. Error handling and logging

- New log tag `[AUTH]` (added to `DIAGNOSTIC_LOG_TAGS`): login ok (role, IP), login failed (IP),
  throttled (IP), plivo signature rejected, media-stream rejected, endpoint not in the access table.
  Never a password, cookie, signature or nonce.
- Auth code never touches the audio path: the hooks run before a route or websocket handler starts.

## 5. Testing (stdlib unittest, Quart test client)

- Access matrix: every endpoint in the table × not logged in / operator / admin gives the expected
  status, redirect or body.
- Default deny: walk `app.url_map`; every endpoint except `static` is in the table, and `static` is
  refused.
- Login: right and wrong pairs for each role, same username with different passwords, throttle at
  the 6th failure, `X-Real-IP` ignored unless from `127.0.0.1`, startup check raises on each bad
  config case.
- Cost removal: history rows and a `call_ended` event for operator vs admin.
- `/trigger-call`: JSON required, no API key path left, `index.html` no longer contains
  `default-dev-token` or `X-API-Key`.
- Plivo signature: a request signed with the SDK's own `get_signature_v3` passes; tampered param,
  wrong URL, missing header fail; `warn` lets a bad one through with the log line; `off` skips.
- Media token: valid passes; expired, tampered parameter, wrong `sig`, missing `sig` fail; the
  stream URLs from `/outbound-webhook` and `/dial-action` carry a valid token.
- Existing suite stays green. No new `call_state` key. `app.py` grows, so the 4_8 `GEMINI_MODEL`
  pins are re-pinned. `test_multiprocess_events` (posts to `/internal/call-event`) keeps passing
  because that endpoint is open in the table.

## 6. Deployment (EC2)

1. Add the five values and `PLIVO_SIGNATURE_CHECK=warn` to `/etc/au-app/common.env`; also put the
   logins and `APP_SECRET` in local `.env`.
2. nginx: make sure `location /` passes `proxy_set_header X-Real-IP $remote_addr;` (check the
   current file first).
3. `git pull`, restart `au-app@800{0,1,2,3}` when no call is live.
4. Log in as each role; one live call (and a transfer if possible); grep `[AUTH]`.
5. No mismatch lines: set `PLIVO_SIGNATURE_CHECK=enforce`, restart when idle.
6. Hand the URL and the operator login to the calling team. Keep the admin login with the owner.

## Out of scope

- Per-person accounts, password change UI, audit log of who placed which call (shared logins by
  operator decision).
- Rate limits on `/trigger-call` beyond the login (step 3's dispatcher caps concurrency).
- HTTPS setup: assumed already in place (Plivo needs `wss://`); `PUBLIC_BASE_URL` is `https://`.
