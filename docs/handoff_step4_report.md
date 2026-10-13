# Step 4 report (EC2): the admin panel

Written 2026-10-13 by the EC2 agent, for the Windows agent and the operator. No PII: calls are named
by id prefix only, and no name, number, transcript, summary or CSV content appears here.

**Status: built, reviewed and deployed on EC2 since 2026-10-13 13:31 IST. Prompt v4 is live. The
operator checked the panel in the browser ("the things look good"). They then had the word
"campaign" replaced by "Bulk calls" / "call list" everywhere people see it (§2, W1-W7), deployed
at 13:50 IST.** The finer browser checks (re-run, do-not-call add/remove, log masking on a real
call; handoff §8 steps 6-9) were not reported item by item. Nothing is committed (branch
`dev`, HEAD `e619921` = `origin/dev`).

## 1. Suite

| Run | Result |
|---|---|
| Baseline (EC2, `e619921`, before any change) | Ran 995, FAILED (failures=4, skipped=4, expected failures=1) |
| After the build | Ran 1104, FAILED (failures=4, skipped=4, expected failures=1) |
| Final, after the wording change (§2) | Ran 1108, FAILED (failures=4, skipped=4, expected failures=1) |

The 4 failures are the same in both runs: the 3 known ones in `tests/test_prompt_catalog.py` and
the EC2-only `test_gemini_backend.TestVertexEndpoint.test_defaults`.

`GEMINI_MODEL` pins are `[109, 1908, 2430, 2440, 2871]`, as the plan predicted. The baseline pin
`[108, 1906, 2428, 2438, 2869]` passed before the change.

New tests: 109. The plan had 103; 6 cover the deviations below.

| File | Tests |
|---|---|
| `test_admin_data.py` | 60 |
| `test_admin_api.py` | 41 |
| `test_admin_page.py` | 8 |
| `test_log_masking.py` | 3 |

## 2. Execution mode and reviews

The work was subagent-driven. Each task had a fresh implementer and a fresh reviewer, then one final
whole-change review. **My harness cannot choose the subagent model, so they did not necessarily run
on Sonnet 5.5** (same as steps 2 and 3). The implementers copied the plan code exactly; I applied
the review fixes inline and reran the tests after each one. `node` is not installed here, so nobody
could syntax-check the page's script mechanically. Two reviewers read it in full and found no
syntax error.

| Task | Status | Review result | Changed because of the review |
|---|---|---|---|
| 1 range, costs, overview | done | approve | D1, two extra tests |
| 2 calls table, detail, transcript | done | approve | D2 |
| 3 campaign reports, do-not-call | done | approve (reviewed with Task 5) | D5 |
| 4 CSV, read routes | done | approve | D3 |
| 5 re-run, do-not-call writes | done | approve | D4 |
| 6 `admin.html` | done | approve | D6 |
| 7 log masking, docs | done | final review: ready to deploy | D7 (transferred as yes/no), D8 (Apply with the same range) |

Plan note: Task 5's `is_busy` already used the names this server's `Analyzer` has (`_queued`,
`_running`), so it needed no change.

### Deviations

| # | File | Change | Why | Cost if wrong |
|---|---|---|---|---|
| D1 | `admin_data.parse_range` | `to = 9999-12-31` is refused (400) | the query adds a day to `to`: an overflow gave a 500 | none |
| D2 | `admin_data.call_detail` | the "earlier" cost part is shown only when `gemini_cost_usd` is null **and** the old `cost` text exists | an unanswered new call showed "Earlier calls $0". Overview totals are unchanged | none |
| D3 | app.py `admin_campaigns_csv`, `admin_dnc_csv` | query and CSV building both run in `call_store.run` | the plan built these two CSVs on the event loop; the do-not-call list has no limit, and 8000 carries live audio | none |
| D4 | `admin_data.failed_analyses`, `transcript_file` (new `_inside`, `_usable_transcript`) | a re-run uses the stored transcript only if it is inside `TRANSCRIPT_DIR` (the same guard the detail view uses) and not empty; the test fixtures point `TRANSCRIPT_DIR` at their temp folder | the plan checked `isfile` only: a path tampered into the DB outside `TRANSCRIPTS/` would be read and sent to Bedrock, and an empty file would save `no_conversation` over a good analysis | such a call is answered 409 / counted as `no_transcript` |
| D5 | `admin_data.add_dnc` | a note that is not a string is refused (400) | `{"note": {...}}` was stored as its Python repr | none |
| D6 | `admin.html` | `loadCall` drops a reply for a call no longer shown and sets the re-run target only after drawing; Re-run is disabled before the POST (re-enabled on error); a malformed hash falls back to Overview instead of a blank page; an empty transcript counts as "not available" and greys out Re-run | the re-run could act on another call than the one on screen; a double click sent two requests; `decodeURIComponent` threw on a hand-edited URL | none |
| D7 | `admin_data._decoded_analysis` | `transferred` is shown as yes/no | it is 0/1 from code, not in `BOOL_FIELDS` | none |
| D8 | `admin.html` `go()` | Apply or a quick button with the current range reloads the view | the same hash fires no `hashchange`, so nothing happened | none |

Open reviewer notes, not changed:
- The overview, calls and campaign views don't drop late replies (only call detail does); the DNC
  search is one request per keystroke. Both are cosmetic, and the second is a known item from
  handoff §7.6.
- "Re-run all failed (n)" counts failed calls without a usable transcript (the reply's
  `no_transcript` explains them).
- A CSV link clicked after the session expired shows the 401 JSON instead of redirecting to the login.
- The "Plivo costs not fetched yet" note also counts old answered calls (known item, §7.6).

### Operator change after the checks (2026-10-13): no "campaign" wording

The operator checked the panel ("the things look good"). Then they ruled that the word "campaign"
confuses the calling team: the agent does one job, calling prospective students and guardians for
the 2027 admissions. Their choices were option A (rename what people see only), settings tucked
away, and the list name made optional.

| # | File | Change |
|---|---|---|
| W1 | `index.html`, `admin.html` | nav link "Campaigns" became **"Bulk calls"** |
| W2 | `campaigns.html` | title and heading are "Bulk calls"; "New campaign" became **"Upload a call list"**; "All campaigns" became "Uploaded lists"; the Stop confirm says "list" |
| W3 | `campaigns.html` | **List name is optional**: left empty, the page fills it with the file name (without `.xlsx`) plus today's IST date. The server still requires 1-100 characters, so its checks are unchanged |
| W4 | `campaigns.html` | the five settings (calls at once, from/to, retries, minutes between tries) sit under a closed **"Advanced settings"**, with the defaults filled in. If the browser refuses a value there, the section opens itself |
| W5 | `admin.html` | tab "Campaigns" became **"Call lists"**; the Calls filter, the call-detail field and the table header say "Call list" |
| W6 | `admin_data.py`, app.py | the campaign-reports CSV has the column "Call list" and the file name `call_lists_<range>.csv` |
| W7 | `campaigns.py` | the error messages a person reads say "list" / "call list" (e.g. "No such call list", "List name must be 1-100 characters") |

Not renamed, on purpose: the URL `/campaigns`, the `/api/campaigns` and `/api/admin/campaigns`
routes, the database tables, code names and log tags (`📣 [CAMPAIGN]`). Nobody but the admin sees
those, and renaming them would touch tested code and the live database for no gain.

Tests: `tests/test_campaign_page.py` gained `TestPlainWords` (4 tests). It checks that the visible
text and readable script strings of all three pages never say "campaign" (it was confirmed to catch
the old wording), the link and title, the optional list name, and the settings under Advanced.
`test_admin_api` now expects the `call_lists_` file name. Suite: **1108 run, 4 failures** (the same
known 4), 4 skipped, 1 expected failure. `GEMINI_MODEL` pins are unchanged. Restarted idle at
13:50 IST; the startup lines are the same as below, with no error.

## 3. Deploy

- DB backup with the SQLite backup API: `call_history.db.bak-20261013-0801` (server UTC).
- Restart at 13:31 IST, after the operator's standing clearance and my check: 0 calls queued,
  ringing or connected, 0 campaigns `running`, 0 contacts `calling`, last call update the previous
  evening. All 4 services are active.

Startup lines:
- `📝 Prompt: PROMPT_FILES/prompt_au_v4.txt (14227 chars)` four times, so **prompt v4 is live**.
- `🔐 [AUTH] logins configured, plivo checks=enforce` four times.
- `🧠 [ANALYSIS] worker started model=us.anthropic.claude-haiku-5-5 concurrency=2` and
  `📣 [CAMPAIGN] dispatcher started max_concurrent=8 hours=09:00-21:00 tick=2.0s`, both from the
  8000 process only.
- No traceback, no `tick failed`, no `❌`.

Other checks:
- `PRAGMA index_list(calls)` lists `calls_created_at`.
- Logged out: `/admin` gives 302 to `/login`; `/api/admin/overview` and `/api/admin/calls.csv` give 401.

## 4. Browser checks

The operator looked at the panel and said it looks good. That covers the views in general. The
items below were not reported one by one, so they stay open, except log masking (step 9), checked
below.

**First real call after the deploy** (2026-10-13 13:53 IST, a single call by the operator, prompt v4,
call `887d19b6`; the operator says it went smoothly). Every pipeline step ran with no `❌` and no
traceback:

| What | Result |
|---|---|
| Call | ended, 122 s, 1 leg |
| Transcript | `status=ok`, Bengali, 9 caller turns, no fallback |
| Haiku analysis | `status=ok`, lead `callback_requested`, prospective yes, score 5; 8970/549 tokens, $0.0015, 5.3 s, 1 attempt, `cache_write=7047` |
| Plivo cost | `status=ok`, $0.01187, about 60 s after hangup |
| Costs on the row | Gemini $0.1417, STT $0.0029, Haiku $0.0015, Plivo $0.0119 (about $0.158 in total) |
| **Log masking (§8 step 9)** | `answered by: xxxxxx…`, `Caller: xxxxxx…` and `Initiating outbound call to xxxxxx…` once each; 0 unmasked lines and 0 full 10-digit numbers after those markers (counted, not printed) |

Handoff §8:
- **Step 4, access:** the operator on `/admin` goes to `/`, and `/api/admin/overview` gives a 403 JSON.
- **Step 5, the five views:** check them, and compare the Overview "All time" values with the
  database. The DB check today (counts and sums only) gives:
  - calls 31, connected 27, total cost $3.9161;
  - leads: hot 1, warm 1 (plus callback_requested 1, not_interested 2);
  - analyses: 5, all `ok`, none failed;
  - campaigns 0, do-not-call 0.

  The page must show Calls 31, Connected 27, Hot 1, Warm 1 and Total $3.9161. "Call lists" will be
  empty until a list has been uploaded and run.
- **Step 6, re-run one call:** a call of the operator's choice.
- **Step 7, "Re-run all failed":** nothing to do (0 failed).
- **Step 8, do-not-call:** a number the operator gives me.
- **Step 9, log masking:** checked on the next real call.

### Where the Haiku analysis is shown (asked by the operator, 2026-10-13)

| Where | Who | What |
|---|---|---|
| Admin panel, **Call detail** (Calls & leads, click a row) | admin | Everything: summary, next action and agent issues first, then every analysis field (lead status, interest 1-10, prospective + reason, respondent, academics, programs, school, scholarship, visit, callback, objections, language, transferred, analysis status/error, analysed at), the four costs, the transcript, the Re-run button |
| Admin panel, **Calls & leads** table | admin | Per call: lead status, primary program, school, visit day, interest score, analysis status. Filters (lead, analysis, visit, program, school, call list, search) and CSV export |
| Admin panel, **Overview** | admin | Hot, Warm and Visits fixed counts, cost per hot lead, notes for failed or pending analyses |
| **Bulk calls** page, a list's contacts | operator and admin | Lead status, visit day and summary only (step 3 decision), no cost and no transcript |
| Calls page `/` | both | no analysis; single calls are analysed too, and the results show in the admin panel |

## 5. Rollback

There is no kill switch: the views are read-only apart from re-run and do-not-call. Rollback is
`git revert` of the step 4 commit (or a checkout of `e619921`, the code just before step 4, which
keeps prompt v4) plus an idle restart. The index is harmless to older code. Prompt v4 rolls back with
`PROMPT_FILE=PROMPT_FILES/prompt_au_v3.txt` in `.env` plus an idle restart.

## 6. Uncommitted

Modified:
- `admin.html`, `app.py`, `call_analysis.py`, `web_auth.py`
- wording change: `campaigns.html`, `index.html`, `campaigns.py`, `tests/test_campaign_page.py`, `tests/test_admin_api.py`
- `tests/harness/appctl.py`, `tests/test_preservation_4_8_resumption_recording_stats.py`
- `CLAUDE.md`, `docs/spec/admin-panel-design.md`, `docs/spec/admin-system-overview.md`

New:
- `admin_data.py`
- `tests/test_admin_data.py`, `tests/test_admin_api.py`, `tests/test_admin_page.py`, `tests/test_log_masking.py`
- this report

The step 3 live campaigns (A, B, controls) are still not run; that report is unchanged. Commit and
push to `origin/dev` on the operator's word.
