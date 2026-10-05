# Handoff 1 — EC2 Bring-up Report (2026-09-29)

Agent running on EC2 `ubuntu@52.66.116.33`, app dir
`/home/ubuntu/AU-Admission-Assist/`.

---

## Bring-up checklist results (section 4 of handoff1.md)

### Step 1: File hash verification — ✅ PASS

All 9 hashed files match exactly:

| File | Match |
|---|---|
| `app.py` | ✅ `b324565a...` |
| `aec.py` | ✅ `495a82ca...` |
| `aec1.py` | ✅ `ff4afa8b...` |
| `bargein.py` | ✅ `aea4d037...` |
| `.gitignore` | ✅ `1c5e659c...` |
| `tests/harness/appctl.py` | ✅ `3dc9106a...` |
| `tests/test_aec1.py` | ✅ `ad453769...` |
| `tests/test_gemini_backend.py` | ✅ `332d91ce...` |
| `tests/test_preservation_4_8_resumption_recording_stats.py` | ✅ `7b0a27dd...` |

### Step 2: Key file — ✅ PASS (after fix)

The file arrived as `silver-shift-490819-k0-1b9ed54b2663 2.json` (space + `2`
suffix, likely a macOS/Windows copy artifact) with mode `664`.

**Fixed on EC2:**
- Renamed to `silver-shift-490819-k0-1b9ed54b2663.json`
- `chmod 600`
- Now: `-rw------- 1 ubuntu ubuntu 2382 Sep 29 10:40 silver-shift-490819-k0-1b9ed54b2663.json`

### Step 3: Python and SDK version — ✅ PASS

- **Python:** 3.12.3 (GCC 13.3.0). `audioop` imports with a deprecation warning (expected, it's removed in 3.13).
- **google-genai:** 1.66.0 — matches the expected version for the base URL override and rejected-field list.
- `google.auth` and `requests` also import cleanly.

### Step 4: Test suite — ✅ PASS

```
Ran 251 tests in 72.389s

OK (skipped=2, expected failures=1)
```

Matches the expected result exactly: **251 OK, 2 skipped, 1 expected failure.**

### Step 5: Vertex AI smoke test from EC2 — ⏸️ BLOCKED

`TEST_FILES/_vertex_smoke.py` is **not present** on EC2. The entire `TEST_FILES/`
directory does not exist.

**Action needed:** The operator must copy `TEST_FILES/_vertex_smoke.py` from the
Windows machine to EC2's `TEST_FILES/` directory.

### Step 6: Start the server — ⏸️ WAITING

Blocked by step 5. Per the checklist, should not start the server until the smoke
test passes.

---

## Summary

| Step | Result | Notes |
|---|---|---|
| 1. File hashes | ✅ PASS | All 9 match |
| 2. Key file | ✅ PASS | Renamed + chmod 600 (was wrong name and 664) |
| 3. Python/SDK | ✅ PASS | 3.12.3, genai 1.66.0 |
| 4. Suite | ✅ PASS | 251 OK, 2 skip, 1 xfail |
| 5. Smoke test | ⏸️ BLOCKED | `_vertex_smoke.py` not on EC2 |
| 6. Start server | ⏸️ WAITING | Depends on step 5 |

## Open questions for the operator

1. **Please copy `TEST_FILES/_vertex_smoke.py`** from the Windows machine to
   `/home/ubuntu/AU-Admission-Assist/TEST_FILES/_vertex_smoke.py` on EC2. Once
   done, I'll run the smoke test and proceed to starting the server.

2. The key file arrived with a wrong name (`silver-shift-490819-k0-1b9ed54b2663 2.json`).
   I renamed it and fixed permissions. Please confirm this is correct.

---

*Tasks A-D from section 5 are pending live calls, which require a running server.*
