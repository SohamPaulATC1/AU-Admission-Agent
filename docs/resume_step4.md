# Resume here: admin system, step 4 (admin panel)

> **Superseded 2026-10-13:** the questions below were answered, and the spec
> (`docs/spec/admin-panel-design.md`), plan (`docs/spec/admin-panel-plan.md`) and EC2 handoff
> (`docs/handoff_step4.md`) are written. Kept for the history of the decisions.

Written 2026-10-12, when the operator paused step 4 after it was opened. A new chat can start it
cold from this file. Read it, then `docs/spec/admin-system-overview.md`, then CLAUDE.md "Resume here".

## Where step 4 stands

- **Brainstorming started, nothing decided, nothing written.** Classified **architectural**: full
  process (one question at a time with a recommendation, then design sections, a written spec in
  `docs/spec/`, a plan in `docs/spec/`, then execution, likely handed to the EC2 agent via a handoff
  file like `docs/handoff_step3.md`).
- **Open question 1 (asked, not answered): scope of the first version.**
  - **A (recommended):** five pages.
    1. Overview: a date range; calls, connected, hot and warm leads, visits fixed, total cost and the
       four parts, cost per call, cost per hot lead.
    2. Calls & leads table: lead status, program, school, visit day, cost; filters and search.
    3. Call detail: transcript, every analysis field, the four costs, a re-run analysis button.
    4. Campaign reports: funnel (contacts, reached, hot, visits) and total cost per campaign.
    5. Do-not-call list: add and remove numbers.

    Each table gets an Excel/CSV export.
  - **B:** A without campaign reports and the do-not-call list.
  - **C:** A plus charts.
- Questions still to ask after that (about 4): who besides the owner uses `/admin` (assumed: only
  the owner); export format (CSV only, or .xlsx too; xlsx can be written with stdlib `zipfile`, no
  new dependency); how a "hot lead" and "cost per lead" are counted (which `lead_status` values);
  re-run analysis rules (cost, how often); the date range defaults.

## What step 4 builds on

- `/admin` (`admin.html`) is an empty shell behind `ADMIN_PAGE` (admin login only; the operator is
  sent to `/`). New routes need `web_auth.ENDPOINT_ACCESS` entries (`ADMIN_PAGE` / `ADMIN_API`).
- Data in `call_history.db` (WAL; written only by 8000):
  - `calls`: name, phone, status, timings, `cost` (old text), `gemini_cost_usd`, `legs`,
    `stt_cost_usd`, `nlp_cost_usd`, `plivo_cost_usd`, `plivo_cost_status`, `plivo_b_legs`,
    `transcript_path`, `programs_looked_up`.
  - `call_analysis`: 26 Haiku fields (lead_status, is_prospective, interest_score 1-10, academics,
    programs, school, scholarship, visit, callback, objections, summary, next_action,
    agent_issues), status / error / tokens.
  - `campaigns`, `campaign_contacts` (joined to calls by `call_uuid`), `do_not_call`.
  - Transcript files in `TRANSCRIPTS/<call_uuid>.txt`.
- Code: `call_store.py` (sync functions + `call_store.run`), `call_analysis.py` (`Analyzer` worker
  in 8000; a re-run can call its enqueue), `campaigns.py`, `web_auth.py` (`without_cost` is for the
  operator; the admin sees costs).

## Leftovers that belong to step 4

1. Old calls (before step 2) have no per-part cost, only the `cost` text: totals must fall back to it.
2. Do-not-call management UI (the table exists; today only `sqlite3` on EC2 adds numbers).
3. Per-campaign cost (sum of the calls behind `campaign_contacts.call_uuid`).
4. `/outbound-webhook` logs the full number and name of every answered call: mask it (campaigns
   multiply this).
5. Re-run analysis from the panel (overview decision for step 2).
6. Optional, from the overview: a nightly copy of `call_history.db` to S3.

## State of the other steps when step 4 was paused

- Step 1 (logins) and step 2 (analysis + full cost): live and verified.
- Step 3 (campaigns): built and deployed on EC2 (`4e08359`), reviewed on Windows (good). **Live
  campaigns A/B and the controls check not run yet** (need 09:00-21:00 IST and the operator's test
  numbers; handoff §8 steps 4-7).
- Prompt v4 (Neha never uses a name): done on Windows, **uncommitted**: `PROMPT_FILES/prompt_au_v4.txt`,
  `app.py` (`PROMPT_FILE`, `GREETING_EVENT`), `tests/test_prompt_v4.py`, `tests/_vertex_smoke.py`,
  the 4_8 re-pin `[108, 1906, 2428, 2438, 2869]`. Needs commit + push, then EC2 `git pull` and an
  idle restart (startup logs `📝 Prompt: PROMPT_FILES/prompt_au_v4.txt`).

## How the operator works (same as before)

Spec -> plan -> execution with written approval at each stage; subagent-driven, every subagent on
Sonnet 5.5; no local browser checks (EC2 only); terse answers, progress as percentages; commit only
when asked; never read `.env`.
