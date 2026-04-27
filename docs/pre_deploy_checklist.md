# Pre-Deployment Checklist — Voice Agent (Doctor Naga Deepti Dental Clinic)

**Target date:** Monday delivery  
**Environment:** Production (Vobiz telephony, SQLite DB, Cartesia TTS, Sarvam STT)

> **How to use this checklist**  
> Every item below has three parts:  
> - **✅ Do** — the exact action or command to run  
> - **👁 Expect** — what a passing result looks like  
> - **🚫 Fail** — what to do if it doesn't match

---

## 1. Environment Variables

**✅ Do:** Open `.env` in the project root and verify each variable is present and non-placeholder.

| Variable | Required | What it should look like |
|---|---|---|
| `GROQ_API_KEYS` | ✅ | `gsk_xxx,gsk_yyy` — at least 2 keys |
| `SARVAM_API_KEYS` | ✅ | `sk_xxx,sk_yyy` |
| `CARTESIA_API_KEYS` | ✅ | `sk_car_xxx,sk_car_yyy` |
| `N8N_WEBHOOK_URL` | ✅ | Full `https://...` URL |
| `VOBIZ_AUTH_ID` | ✅ | `MA_xxxxx` |
| `VOBIZ_AUTH_TOKEN` | ✅ | Long token string |
| `INPUT_MODE` | ✅ | `mic` for browser testing, `vobiz` for live calls |
| `USE_SQLITE` | ✅ | `true` (we are using SQLite) |
| `DEFAULT_CLIENT_ID` | ✅ | `deepti_dental` |

**👁 Expect:** Every variable has a real value — no blanks, no `YOUR_xxx` placeholders.

**🚫 Fail:** If any required variable is missing or placeholder → fill it in from the client credentials sheet before continuing.

---

## 2. Static Code Checks

**✅ Do:** Run this command in the terminal from the project root:
```
venv\Scripts\python.exe -m pytest tests/test_static_checks.py -v
```

**👁 Expect:** Terminal ends with `X passed` — every line shows `PASSED`. The checks verify:
- No agent file has a hardcoded model name like `"llama-3.3-70b-versatile"` — it must use the `LLM_MODEL` variable
- `main.py` uses `asyncio.to_thread` not `threading.Thread`
- Memory trim is exactly `[-12:]`
- `agent2.py` does not exist (it was deleted as dead code)
- Agent-3 prompts contain `CHECK_AVAILABILITY` and `availability_checked` fields
- Default doctor name is `"Doctor Naga Deepti"` in the payload builder

**🚫 Fail:** If any test shows `FAILED` — note the test name, it tells you exactly which file/line is wrong. Fix the source file and re-run.

---

## 3. Clinic Guard Unit Tests

**✅ Do:**
```
venv\Scripts\python.exe -m pytest tests/test_clinic_guard.py -v
```

**👁 Expect:** All tests pass. These verify the hard time-slot guard works correctly:
- Calling `_is_valid_clinic_slot("2026-04-26", "10:00")` → returns `False` (Sunday)
- Calling `_is_valid_clinic_slot("2026-04-27", "14:00")` → returns `False` (lunch gap)
- Calling `_is_valid_clinic_slot("2026-04-27", "10:00")` → returns `True` (valid Monday morning)
- Calling `_is_valid_clinic_slot("2026-04-27", "19:00")` → returns `False` (after 7 PM)
- Garbage input like `"not-a-date"` → returns `True` (fail-open, no crash)

**🚫 Fail:** If guard tests fail, the voice agent will incorrectly accept or reject appointment slots. Check `src/main.py` around the `_is_valid_clinic_slot` function.

---

## 4. Date Format Regression Tests

**✅ Do:**
```
venv\Scripts\python.exe -m pytest tests/test_date_formats.py -v
```

**👁 Expect:** All tests pass. These verify the DB can handle different ways the LLM might express a date:
- `"27 April 2026"` + `"10:00 AM"` → correctly parsed to IST ISO format
- `"2026-04-27"` + `"10:00"` → correctly parsed
- `"27 Apr 2026"` + `"10:00 AM"` → correctly parsed
- A seeded booked slot → `check_availability` returns `False`
- A garbage date like `"xyz"` → returns `True` (fail-open, no crash)

**🚫 Fail:** If date parsing fails, the availability check will always return "available" even for booked slots. Fix is in `src/database.py` → `_parse_to_ist_iso()`.

---

## 5. N8N Webhook Payload Tests

**✅ Do:**
```
venv\Scripts\python.exe -m pytest tests/test_webhook_payload.py -v
```

**👁 Expect:** All tests pass. These verify the JSON payload sent to N8N is always correct:
- `doctor_name` is exactly `"Doctor Naga Deepti"` (not `"Dr. Naga Deepti"` or with MDS suffix)
- `booked_via` is `"voice_assistant"`
- `start_time` is ISO format, `end_time` is 30 min later
- For a reschedule/cancel call, `previous_datetime` is present in the payload
- An all-null state (no user data collected yet) does not crash the builder

**🚫 Fail:** If payload tests fail, N8N may receive malformed data and fail to create calendar events. Fix is in `src/utils.py` → `build_scheduling_payload()`.

---

## 6. Regression Suite

**✅ Do:**
```
venv\Scripts\python.exe -m pytest tests/test_regression.py -v
```

**👁 Expect:** `52 passed, 0 failed`. This is the core safety net — it covers:
- Client config loading from `client_config.json` (DID lookup works)
- SQLite DB init and availability checks
- Agent-2 prompts contain the clinic fee range and doctor name
- `parse_llm_json` handles bad LLM output without crashing
- Date formats and clinic guard work correctly (re-checked here as regression)

**🚫 Fail:** A regression means something previously working is now broken. The test name will tell you exactly which component. Fix it before moving on.

---

## 7. Persistence Test

**✅ Do:**
```
venv\Scripts\python.exe tests/test_persistence.py
```

**👁 Expect:** Terminal prints `✅ Session Store Test Passed!` and no errors. This verifies:
- A session state (`name`, `doctor`, etc.) saved to SQLite can be loaded back identically
- Doctor name stored is `"Doctor Naga Deepti"` (not old `"Dr. Dipti"`)
- After `clear_session`, loading returns empty data

**🚫 Fail:** Session store is broken — caller state won't survive between turns of a conversation. Fix is in `src/database.py` → `SessionStore`.

---

## 8. Full Iteration Suite (requires Groq API keys)

**✅ Do:**
```
venv\Scripts\python.exe tests/test_iteration.py
```
*This will take ~5–10 minutes as it calls the real LLM.*

**👁 Expect:** A summary at the end showing ≥95% pass rate. Key things it checks with real LLM calls:
- Agent-1 correctly classifies "book", "cancel", "enquiry", "emergency" intents
- Agent-2 EN greeting does NOT say `"Dr."` — must say `"Doctor"`
- Agent-2 KN responds with Kannada characters `(ಕ)`
- When a Sunday date is injected mid-booking → agent verbally rejects it
- When reschedule fields are all filled → `action = "CHECK_AVAILABILITY"` is triggered

**🚫 Fail:** Note which specific test failed. If it's an LLM behaviour test, it may occasionally fail due to LLM variability — re-run once more. If it fails consistently, the agent prompt needs fixing.

---

## 9. Agent-3 Availability Flow Tests

**✅ Do:**
```
venv\Scripts\python.exe tests/test_agent3_availability.py
```

**👁 Expect:** ≥9/10 pass. These test that the cancel/reschedule agent (Agent-3) triggers the availability check at the right time:
- User says "reschedule my appointment" + provides name + old date + new date → Agent-3 responds with `action = "CHECK_AVAILABILITY"`
- User says "cancel" → Agent-3 does NOT trigger `CHECK_AVAILABILITY` (no new slot needed for cancel)
- After a `"System: AVAILABLE"` message is injected → agent asks for confirmation
- After a `"System: BOOKED"` message is injected → agent asks for a different time

**🚫 Fail:** If CHECK_AVAILABILITY isn't triggering, check `src/agent3_en.py` or `src/agent3_kn.py` prompts — the `CHECK_AVAILABILITY` rule section.

---

## 10. Edge Case Tests

**✅ Do:**
```
venv\Scripts\python.exe tests/test_edge_cases.py
```

**👁 Expect:** ≥25/28 pass. This checks the system doesn't crash under unusual conditions:
- Sending empty string, emoji-only, or 2000-char input → agent returns a dict, no exception
- Sending `"I'm having severe bleeding"` mid-booking → Agent-1 returns `intent = emergency`
- Sending `"I'm bleeding, need help!"` to Agent-2 → response has `action = TRANSFER_CALL`
- Booking the same slot twice → second `check_availability` returns `False` (slot taken)
- Two different session IDs store different patient names without mixing up

**🚫 Fail:** Edge case crashes indicate fragile code. Note the test name — it tells you exactly which scenario failed.

---

## 11. NFR Tests (latency)

**✅ Do:**
```
venv\Scripts\python.exe tests/test_nfr.py
```

**👁 Expect:** All hard caps respected — terminal shows latency numbers like:
```
[LATENCY] Agent-1 p50=1.8s  (SLA=3s  cap=15s)
[LATENCY] Agent-2-EN p50=3.2s  (SLA=5s  cap=20s)
```
- If p50 exceeds the SLA but not the cap → warning printed, test still **passes**
- If p50 exceeds the hard cap → test **fails** (unacceptable for production)
- 6 concurrent LLM requests fire simultaneously and all return without exception

**🚫 Fail:** If latency exceeds hard cap, the phone call will feel laggy. Check Groq API status or reduce prompt length.

---

## 12. Concurrency Tests

**✅ Do:**
```
venv\Scripts\python.exe tests/test_concurrency.py
```

**👁 Expect:** ≥8/9 pass. Verifies the system handles multiple simultaneous calls:
- Two patients in parallel sessions → each gets their own state (names don't mix)
- 5 concurrent Agent-1 calls → all return valid intent dicts
- 10 concurrent session-store saves → no corruption, no lock errors
- 6 simultaneous LLM requests → pool handles them without deadlock

**🚫 Fail:** Concurrency failures in production = patients hearing each other's data. Must be fixed before Vobiz go-live.

---

## 13. Browser Smoke Tests (manual, mic mode)

**✅ Do:**
1. Start the server: `venv\Scripts\python.exe src/main.py`
2. Open browser → `http://localhost:PORT/`
3. Click **Start** and speak each scenario below
4. After each booking, open `http://localhost:PORT/database` → **Appointments tab** to verify

| # | What you say | What you expect to hear | What to check in DB |
|---|---|---|---|
| **SM-01** | *"I want to book an appointment. My name is Arjun, I'm 30, I have a toothache, how about Monday 10 AM?"* | Agent asks for confirmation, then says "Appointment confirmed" | `status = scheduled`, `doctor_name = Doctor Naga Deepti` |
| **SM-02** | *"ನನಗೆ ಅಪಾಯಿಂಟ್ಮೆಂಟ್ ಬೇಕು"* | Agent responds in Kannada, asks for name | Kannada characters visible in terminal logs |
| **SM-03** | *"Book for this Sunday at 10 AM"* | Agent says "we are closed on Sundays, please choose Monday to Saturday" | No appointment created |
| **SM-04** | *"Book for Monday at 2 PM"* | Agent says clinic hours are 10 AM–1 PM or 4 PM–7 PM | No appointment created |
| **SM-05** | *"I'm having severe chest pain and bleeding, I need help!"* | Agent stops talking, call transfer fires | Terminal logs `TRANSFER_CALL` |

**👁 Expect for DB check:** Open `http://localhost:PORT/database` → Appointments tab → newest row at top shows:
- `patient_name = Arjun`
- `status = scheduled`
- `doctor_name = Doctor Naga Deepti`
- `booked_via = voice_assistant`

**🚫 Fail:** If DB row not visible after booking → N8N or the webhook fired but DB write failed. Check terminal for `[DB ERROR]` lines.

---

## 14. Vobiz Live Call Tests (manual, production phone)

**✅ Do:** Set `INPUT_MODE=vobiz` in `.env`, restart server, call the production DID from a real phone.

| # | Call scenario | Step by step | Expected outcome |
|---|---|---|---|
| **VC-01 Full KN booking** | Call DID, speak Kannada | 1. Hear greeting in Kannada within 3 s<br>2. Say "ಅಪಾಯಿಂಟ್ಮೆಂಟ್ ಬೇಕು"<br>3. Give name, age, reason, date (Monday 10 AM), time<br>4. Confirm | N8N execution log shows 1 new run · DB row with `status=scheduled` |
| **VC-02 Sunday rejection** | Mid-booking say Sunday | After providing name + reason, say "This Sunday 10 AM" | Agent verbally says closed on Sunday, asks for Mon–Sat |
| **VC-03 Off-hours rejection** | Say 2 PM slot | After name + reason, say "Monday 2 PM" | Agent says clinic open 10 AM–1 PM and 4 PM–7 PM |
| **VC-04 Reschedule** | Call again, say reschedule | 1. Say "I need to reschedule my appointment"<br>2. Give name + previous date<br>3. Give new date Monday 10 AM<br>4. Confirm | N8N fires `event_type = appointment_reschedule` · DB row updated |
| **VC-05 Cancel** | Call again, say cancel | 1. Say "I want to cancel my appointment"<br>2. Give name + appointment date<br>3. Confirm cancel | N8N fires `event_type = appointment_cancel` |
| **VC-06 Emergency** | Say emergency phrase | Mid-booking say "I'm having severe bleeding right now" | Call transfers to emergency number — you hear the line dial |

**After each call:** Check `http://localhost:PORT/database` (Appointments + Call Logs tabs) and N8N execution history.

**👁 Expect:** All 6 scenarios behave as described. N8N execution history shows no errors (green checkmarks).

**🚫 Fail:** If Vobiz call doesn't connect → check `SERVER_BASE_URL` in `.env` (must be current ngrok URL). If N8N doesn't fire → check `N8N_WEBHOOK_URL`.

---

## 15. Post-Call Metrics Check

**✅ Do:** After completing the Vobiz tests, open these URLs in the browser:

| URL | What to check |
|---|---|
| `http://localhost:PORT/llm/metrics` | `success` count > 0, no key at 100% error rate |
| `http://localhost:PORT/stt/metrics` | Primary provider (Sarvam) handling most calls |
| `http://localhost:PORT/database` | Appointments tab shows all test bookings · Call Logs tab shows one row per call |

**👁 Expect:** All metrics endpoints return JSON. LLM success rate ≥ 99%. No provider fully down.

**🚫 Fail:** If STT error rate is high → check Sarvam API key. If LLM errors → check Groq key rotation in `.env`.

---

## 16. Rollback Plan

If anything fails after going live on Vobiz:

1. In `.env` set `INPUT_MODE=mic` → restart server → Vobiz calls stop being processed
2. Notify client: "Brief maintenance, back in X minutes"
3. Fix the issue → re-run only the affected test gate
4. Set `INPUT_MODE=vobiz` → restart → confirm Vobiz resumes

---

## Sign-Off

| Gate | Status | Notes |
|---|---|---|
| 1. Env variables | ⬜ | |
| 2. Static checks | ⬜ | |
| 3. Clinic guard | ⬜ | |
| 4. Date formats | ⬜ | |
| 5. Webhook payload | ⬜ | |
| 6. Regression suite | ⬜ | |
| 7. Persistence | ⬜ | |
| 8. Iteration (LLM) | ⬜ | |
| 9. Agent-3 availability | ⬜ | |
| 10. Edge cases | ⬜ | |
| 11. NFR / latency | ⬜ | |
| 12. Concurrency | ⬜ | |
| 13. Browser smoke | ⬜ | |
| 14. Vobiz live calls | ⬜ | |
| 15. Metrics check | ⬜ | |

**All 15 gates ✅ → APPROVED FOR CLIENT DELIVERY**
