import os
import time
import base64
import json
import re
import wave
import audioop
from datetime import datetime

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import JSONResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
load_dotenv()

import asyncio

from llm import llm_pool, LLM_MODEL

# -----------------------------------------------------------------------
# Module imports â€” separated concerns
# -----------------------------------------------------------------------
from utils import (
    get_initial_state, parse_llm_json, log_interaction,
    build_scheduling_payload, SessionStore, trigger_vobiz_transfer,
    send_to_n8n_webhook_sync, check_guardrails
)
from agent1 import run_agent1 as _run_agent1
from agent2_kn import run_agent2_kn as _run_agent2_kn
from agent2_en import run_agent2_en as _run_agent2_en
from agent3_kn import run_agent3_kn as _run_agent3_kn
from agent3_en import run_agent3_en as _run_agent3_en
from stt import (
    run_stt_http as _run_stt_http,
    l16_8k_to_pcm16_16k,
    mulaw_8k_to_pcm16_16k,
    pcm16_16k_to_mulaw_8k,
    pcm16_to_wav_bytes,
)
from tts import cartesia_tts_collect, cartesia_tts_chunked, cartesia_tts_stream
from database import check_availability, get_next_available_slot, verify_appointment_for_cancellation, update_appointment_status, reschedule_appointment, log_call_start, log_call_end, log_llm_event, save_agent_appointment
from client_config import get_config_by_did, get_default_config

# -----------------------------
# Create FastAPI App
# -----------------------------
app = FastAPI()

# -----------------------------
# Enable CORS
# -----------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -----------------------------
# Initialize Clients
# -----------------------------
groq_client = llm_pool
sarvam_api_key = os.getenv("SARVAM_API_KEY")

# Initialize Session Store
session_store = SessionStore()

# callUUID → DID mapping (populated by /answer, consumed by start event)
_call_did_map: dict[str, str] = {}


@app.post("/session/clear")
@app.get("/session/clear")
async def clear_session(session_id: str = ""):
    if not session_id:
        return JSONResponse(content={"ok": False, "error": "session_id required"}, status_code=400)
    try:
        await asyncio.to_thread(session_store.clear_session, session_id)
        return {"ok": True}
    except Exception as e:
        return JSONResponse(content={"ok": False, "error": str(e)}, status_code=500)


# -----------------------------------------------------------------------
# Local wrappers â€” bind shared clients so callers need no arguments
# -----------------------------------------------------------------------
async def run_agent1(user_text: str, memory: list) -> dict:
    return await _run_agent1(user_text, memory, groq_client)

def _all_appointment_fields_present(st: dict) -> bool:
    """True when name, age, reason, date AND time are all non-empty."""
    return all(st.get(k) for k in ("name", "age", "reason", "date", "time"))

async def run_agent2(user_text: str, memory: list, state: dict, agent1_context: dict, config: dict = None):
    response, state, parsed = await _run_agent2_kn(user_text, memory, state, agent1_context, groq_client, config)

    # Guard: LLM skipped CHECK_AVAILABILITY but all fields are filled
    if (
        parsed.get("action") != "CHECK_AVAILABILITY"
        and _all_appointment_fields_present(state)
        and not state.get("confirmation_pending")
        and not parsed.get("done")
    ):
        print("[Agent-2-KN] \u26a0\ufe0f All fields present but CHECK_AVAILABILITY skipped \u2014 forcing it")
        parsed["action"] = "CHECK_AVAILABILITY"

    if parsed.get("action") == "CHECK_AVAILABILITY":
        tool_state = await _sanitize_state_for_english_tools(state)
        raw_date = tool_state.get("date", "")
        try:
            if re.match(r"^\d{4}-\d{2}-\d{2}$", raw_date or ""):
                raw_date = datetime.strptime(raw_date, "%Y-%m-%d").strftime("%d %B %Y")
        except Exception:
            pass
        check_time = tool_state.get("time", "")
        print(f"[Agent-2-KN] Intercepting CHECK_AVAILABILITY for {raw_date} {check_time}")

        valid, hours_msg = _is_valid_clinic_slot(raw_date, check_time)
        if not valid:
            print(f"[Agent-2-KN] \u274c Outside clinic hours: {hours_msg}")
            if "Sunday" in hours_msg:
                response = "\u0C95\u0CCD\u0CB7\u0CAE\u0CBF\u0CB8\u0CBF, \u0CAD\u0CBE\u0CA8\u0CC1\u0CB5\u0CBE\u0CB0 \u0C95\u0CCD\u0CB2\u0CBF\u0CA8\u0CBF\u0C95\u0CCD \u0CAE\u0CC1\u0C9A\u0CCD\u0C9A\u0CBF\u0CA6\u0CC6. \u0CA6\u0CAF\u0CB5\u0CBF\u0C9F\u0CCD\u0C9F\u0CC1 \u0CAC\u0CC7\u0CB0\u0CC6 \u0CA6\u0CBF\u0CA8 \u0CB9\u0CC7\u0CB3\u0CBF."
            else:
                response = "\u0C95\u0CCD\u0CB7\u0CAE\u0CBF\u0CB8\u0CBF, \u0C95\u0CCD\u0CB2\u0CBF\u0CA8\u0CBF\u0C95\u0CCD \u0CB8\u0CAE\u0CAF 10 AM\u200D\u2013\u200D1 PM \u0CAE\u0CA4\u0CCD\u0CA4\u0CC1 4 PM\u200D\u2013\u200D7 PM. \u0CA6\u0CAF\u0CB5\u0CBF\u0C9F\u0CCD\u0C9F\u0CC1 \u0CAC\u0CC7\u0CB0\u0CC6 \u0CB8\u0CAE\u0CAF \u0CB9\u0CC7\u0CB3\u0CBF."
            state.pop("time", None)
            state.pop("confirmation_pending", None)
            parsed = {"response": response, "action": None, "handoff": False, "done": False, "state": state}
            return response, state, parsed

        _client_id_kn = config.get("client_id") if config else None
        avail_result = check_availability(raw_date, check_time, client_id=_client_id_kn)
        is_available = avail_result.get('available', True)

        if is_available:
            status_msg = f"System: The slot at {check_time} on {raw_date} is AVAILABLE. Follow step 2: restate date, time, reason and ask 'Is that correct?' — set confirmation_pending=true, done=false."
        else:
            morning_date, morning_time = get_next_available_slot(
                raw_date, check_time,
                clinic_hours={"morning_start": 10, "morning_end": 13, "evening_start": 99, "evening_end": 99},
                client_id=_client_id_kn
            )
            evening_date, evening_time = get_next_available_slot(
                raw_date, check_time,
                clinic_hours={"morning_start": 99, "morning_end": 99, "evening_start": 16, "evening_end": 19},
                client_id=_client_id_kn
            )
            suggestions = []
            if morning_date and morning_time:
                suggestions.append(f"morning: {morning_date} at {morning_time}")
            if evening_date and evening_time:
                suggestions.append(f"evening: {evening_date} at {evening_time}")
            if suggestions:
                status_msg = "System: That slot is BOOKED. Suggest ONLY these alternatives (1 morning, 1 evening) — " + "; ".join(suggestions) + ". Do NOT list any other slots."
            else:
                status_msg = "System: That slot is BOOKED. No alternative slots available in the next 14 days."
        
        memory_with_check = memory + [
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": "Let me check the schedule..."},
            {"role": "user", "content": status_msg}
        ]
        response, state, parsed = await _run_agent2_kn("ದಯವಿಟ್ಟು ಮುಂದುವರಿಯಿರಿ.", memory_with_check, state, agent1_context, groq_client, config)
    return response, state, parsed

async def run_agent2_en(user_text: str, memory: list, state: dict, agent1_context: dict, config: dict = None):
    response, state, parsed = await _run_agent2_en(user_text, memory, state, agent1_context, groq_client, config)

    # Guard: LLM skipped CHECK_AVAILABILITY but all fields are filled
    if (
        parsed.get("action") != "CHECK_AVAILABILITY"
        and _all_appointment_fields_present(state)
        and not state.get("confirmation_pending")
        and not parsed.get("done")
    ):
        print("[Agent-2-EN] \u26a0\ufe0f All fields present but CHECK_AVAILABILITY skipped \u2014 forcing it")
        parsed["action"] = "CHECK_AVAILABILITY"

    if parsed.get("action") == "CHECK_AVAILABILITY":
        # Convert ISO date (YYYY-MM-DD) to "DD Month YYYY" if needed
        raw_date = state.get("date", "")
        try:
            if re.match(r"^\d{4}-\d{2}-\d{2}$", raw_date or ""):
                raw_date = datetime.strptime(raw_date, "%Y-%m-%d").strftime("%d %B %Y")
        except Exception:
            pass
        check_date = raw_date
        check_time = state.get("time", "")
        print(f"[Agent-2-EN] Intercepting CHECK_AVAILABILITY for {check_date} {check_time}")

        valid, hours_msg = _is_valid_clinic_slot(check_date, check_time)
        if not valid:
            print(f"[Agent-2-EN] \u274c Outside clinic hours: {hours_msg}")
            response = f"I'm sorry, {hours_msg} Could you suggest a different date or time?"
            state.pop("date", None)
            state.pop("time", None)
            state.pop("confirmation_pending", None)
            parsed = {"response": response, "action": None, "handoff": False, "done": False, "state": state}
            return response, state, parsed

        tool_state = await _sanitize_state_for_english_tools(state)
        _client_id_en = config.get("client_id") if config else None
        avail_result = check_availability(check_date, check_time, client_id=_client_id_en)
        is_available = avail_result.get('available', True)

        if is_available:
            status_msg = f"System: The slot at {check_time} on {check_date} is AVAILABLE. Follow step 2: restate date, time, reason and ask 'Is that correct?' — set confirmation_pending=true, done=false."
        else:
            morning_date, morning_time = get_next_available_slot(
                check_date, check_time,
                clinic_hours={"morning_start": 10, "morning_end": 13, "evening_start": 99, "evening_end": 99},
                client_id=_client_id_en
            )
            evening_date, evening_time = get_next_available_slot(
                check_date, check_time,
                clinic_hours={"morning_start": 99, "morning_end": 99, "evening_start": 16, "evening_end": 19},
                client_id=_client_id_en
            )
            suggestions = []
            if morning_date and morning_time:
                suggestions.append(f"morning: {morning_date} at {morning_time}")
            if evening_date and evening_time:
                suggestions.append(f"evening: {evening_date} at {evening_time}")
            if suggestions:
                status_msg = "System: That slot is BOOKED. Suggest ONLY these alternatives (1 morning, 1 evening) — " + "; ".join(suggestions) + ". Do NOT list any other slots."
            else:
                status_msg = "System: That slot is BOOKED. No alternative slots available in the next 14 days."
        
        memory_with_check = memory + [
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": "Let me check the schedule..."},
            {"role": "user", "content": status_msg}
        ]
        response, state, parsed = await _run_agent2_en("Please proceed based on the availability.", memory_with_check, state, agent1_context, groq_client, config)
    return response, state, parsed

async def run_agent3_kn(user_text: str, memory: list, state: dict, context: dict, config: dict = None):
    response, state, parsed = await _run_agent3_kn(user_text, memory, state, context, groq_client, config)

    # Intercept VERIFY_APPOINTMENT action
    if parsed.get("action") == "VERIFY_APPOINTMENT":
        print(f"[Agent-3-KN] Intercepting VERIFY_APPOINTMENT")
        name = state.get("name", "")
        phone = state.get("phone", "")
        # Normalize phone to +91 format
        phone = _normalize_phone_for_lookup(phone)
        state["phone"] = phone
        prev_date = state.get("previous_date", "")
        prev_time = state.get("previous_time", "")

        # Verify appointment exists
        verify_result = verify_appointment_for_cancellation(name, phone, prev_date, prev_time)
        
        if verify_result['exists']:
            status_msg = f"System: Appointment verified. Found appointment for {name} on {prev_date} at {prev_time}."
            state['verified'] = True
            state['appointment_id'] = verify_result['appointment'].get('id')
        else:
            status_msg = f"System: No appointment found for {name} on {prev_date} at {prev_time} with phone {phone}. Please check the details."
            state['verified'] = False
        
        memory_with_verify = memory + [
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": "Let me verify your appointment..."},
            {"role": "user", "content": status_msg}
        ]
        response, state, parsed = await _run_agent3_kn("ದಯವಿಟ್ಟು ಮುಂದುವರಿಯಿರಿ.", memory_with_verify, state, context, groq_client, config)

    # Intercept CHECK_AVAILABILITY action (reschedule only)
    if parsed.get("action") == "CHECK_AVAILABILITY":
        print(f"[Agent-3-KN] Intercepting CHECK_AVAILABILITY")
        new_date = state.get("new_date", "")
        new_time = state.get("new_time", "")
        print(f"[Agent-3-KN] Checking slot: {new_date} {new_time}")
        avail_result = check_availability(new_date, new_time, client_id=config.get("client_id") if config else None)
        is_available = avail_result.get("available", True)
        if is_available:
            status_msg = f"System: The slot on {new_date} at {new_time} is AVAILABLE."
            state["availability_checked"] = True
            state["availability_is_available"] = True
        else:
            status_msg = f"System: The slot on {new_date} at {new_time} is BOOKED. Please suggest a different date or time."
            state["availability_checked"] = True
            state["availability_is_available"] = False
        memory_with_avail = memory + [
            {"role": "user",      "content": user_text},
            {"role": "assistant", "content": "ಲಭ್ಯತೆ ಪರಿಶೀಲಿಸುತ್ತಿದ್ದೇನೆ..."},
            {"role": "user",      "content": status_msg},
        ]
        response, state, parsed = await _run_agent3_kn("ದಯವಿಟ್ಟು ಮುಂದುವರಿಯಿರಿ.", memory_with_avail, state, context, groq_client, config)

    # When confirmed, update DB
    if parsed.get("done") and parsed.get("confirmation_status") == "confirmed" and state.get("appointment_id"):
        event_type = parsed.get("event_type", "")
        if event_type == "appointment_cancel":
            update_appointment_status(state["appointment_id"], "cancelled")
            print(f"[Agent-3-KN] Appointment {state['appointment_id']} cancelled")
        elif event_type == "appointment_reschedule":
            reschedule_appointment(state["appointment_id"], state.get("new_date", ""), state.get("new_time", ""))
            print(f"[Agent-3-KN] Appointment {state['appointment_id']} rescheduled to {state.get('new_date')} {state.get('new_time')}")
    
    return response, state, parsed


def _normalize_phone_for_lookup(phone: str) -> str:
    """Normalize phone number to +91XXXXXXXXXX format for database lookup."""
    if not phone:
        return phone
    digits = re.sub(r"\D", "", str(phone))
    # If 10 digits and doesn't start with country code, add +91
    if len(digits) == 10 and not digits.startswith("91"):
        return f"+91{digits}"
    # If already 12 digits starting with 91, add +
    if len(digits) == 12 and digits.startswith("91"):
        return f"+{digits}"
    return phone


async def run_agent3_en(user_text: str, memory: list, state: dict, context: dict, config: dict = None):
    response, state, parsed = await _run_agent3_en(user_text, memory, state, context, groq_client, config)

    # Intercept VERIFY_APPOINTMENT action
    if parsed.get("action") == "VERIFY_APPOINTMENT":
        print(f"[Agent-3-EN] Intercepting VERIFY_APPOINTMENT")
        name = state.get("name", "")
        phone = state.get("phone", "")
        # Normalize phone to +91 format
        phone = _normalize_phone_for_lookup(phone)
        state["phone"] = phone
        prev_date = state.get("previous_date", "")
        prev_time = state.get("previous_time", "")

        # Verify appointment exists
        verify_result = verify_appointment_for_cancellation(name, phone, prev_date, prev_time)
        
        if verify_result['exists']:
            status_msg = f"System: Appointment verified. Found appointment for {name} on {prev_date} at {prev_time}."
            state['verified'] = True
            state['appointment_id'] = verify_result['appointment'].get('id')
        else:
            status_msg = f"System: No appointment found for {name} on {prev_date} at {prev_time} with phone {phone}. Please check the details."
            state['verified'] = False
        
        memory_with_verify = memory + [
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": "Let me verify your appointment..."},
            {"role": "user", "content": status_msg}
        ]
        response, state, parsed = await _run_agent3_en("Please proceed based on the verification result.", memory_with_verify, state, context, groq_client, config)

    # Intercept CHECK_AVAILABILITY action (reschedule only)
    if parsed.get("action") == "CHECK_AVAILABILITY":
        print(f"[Agent-3-EN] Intercepting CHECK_AVAILABILITY")
        new_date = state.get("new_date", "")
        new_time = state.get("new_time", "")
        print(f"[Agent-3-EN] Checking slot: {new_date} {new_time}")
        avail_result = check_availability(new_date, new_time, client_id=config.get("client_id") if config else None)
        is_available = avail_result.get("available", True)
        if is_available:
            status_msg = f"System: The slot on {new_date} at {new_time} is AVAILABLE."
            state["availability_checked"] = True
            state["availability_is_available"] = True
        else:
            status_msg = f"System: The slot on {new_date} at {new_time} is BOOKED. Please suggest a different date or time."
            state["availability_checked"] = True
            state["availability_is_available"] = False
        memory_with_avail = memory + [
            {"role": "user",      "content": user_text},
            {"role": "assistant", "content": "Let me check availability for that slot..."},
            {"role": "user",      "content": status_msg},
        ]
        response, state, parsed = await _run_agent3_en("Please proceed based on the availability result.", memory_with_avail, state, context, groq_client, config)

    # When confirmed, update appointment status in database
    if parsed.get("done") and parsed.get("confirmation_status") == "confirmed":
        event_type = parsed.get("event_type", "")
        if event_type in ("appointment_cancel", "appointment_reschedule") and state.get("appointment_id"):
            if event_type == "appointment_cancel":
                update_appointment_status(state["appointment_id"], "cancelled")
                print(f"[Agent-3-EN] Appointment {state['appointment_id']} cancelled")
            elif event_type == "appointment_reschedule":
                reschedule_appointment(state["appointment_id"], state.get("new_date", ""), state.get("new_time", ""))
                print(f"[Agent-3-EN] Appointment {state['appointment_id']} rescheduled to {state.get('new_date')} {state.get('new_time')}")
    
    return response, state, parsed

async def run_stt_http(audio_bytes: bytes, filename: str = "audio.wav", language_code: str = "unknown", *, session_id: str = "", client_id: str = "default"):
    return await _run_stt_http(audio_bytes, sarvam_api_key, filename, language_code=language_code, session_id=session_id, client_id=client_id)

def _normalize_streaming_lang_to_en_or_kn(detected_lang: str, transcript: str) -> str:
    dl = (detected_lang or "").strip().lower()
    if dl.startswith("kn"):
        return "kn-IN"
    if dl.startswith("en"):
        return "en-IN"
    if re.search(r"[\u0C80-\u0CFF]", transcript or ""):
        return "kn-IN"
    return "en-IN"

async def _ensure_english_value(text: str) -> str:
    if text is None:
        return ""
    s = str(text).strip()
    if not s:
        return ""
    if s.isascii():
        return s
    try:
        prompt = (
            "Convert the following value to plain English (ASCII only). "
            "If it is a person's name, output the English transliteration. "
            "Return ONLY the converted text, no quotes, no extra words.\n\n"
            f"Value: {s}"
        )
        resp = await groq_client.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=40,
            temperature=0.0,
            stream=False,
        )
        out = (resp.choices[0].message.content or "").strip()
        return out if out.isascii() else "".join(ch for ch in out if ord(ch) < 128).strip()
    except Exception:
        return "".join(ch for ch in s if ord(ch) < 128).strip()

async def _ensure_english_time(text: str) -> str:
    """Convert any regional/Kannada time expression to English HH:MM AM/PM format."""
    if text is None:
        return ""
    s = str(text).strip()
    if not s:
        return ""
    if s.isascii():
        return s
    try:
        prompt = (
            "Convert the following time expression to English HH:MM AM/PM format (e.g. '10:00 AM', '4:30 PM'). "
            "Rules: ಬೆಳಿಗ್ಗೆ/ಬೆಳಗ್ಗೆ = morning (AM), ಮಧ್ಯಾಹ್ನ = noon (PM after 12), "
            "ಸಂಜೆ/ರಾತ್ರಿ = evening/night (PM). "
            "Return ONLY the time in HH:MM AM/PM format, nothing else.\n\n"
            f"Time: {s}"
        )
        resp = await groq_client.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=20,
            temperature=0.0,
            stream=False,
        )
        out = (resp.choices[0].message.content or "").strip()
        return out if out.isascii() else "".join(ch for ch in out if ord(ch) < 128).strip()
    except Exception:
        return "".join(ch for ch in s if ord(ch) < 128).strip()

async def _sanitize_state_for_english_tools(state: dict) -> dict:
    safe = dict(state or {})
    for k in ("name", "reason", "date"):
        if safe.get(k) is not None:
            safe[k] = await _ensure_english_value(safe.get(k))
    if safe.get("time") is not None:
        safe["time"] = await _ensure_english_time(safe.get("time"))
    return safe

def _is_valid_clinic_slot(date_str: str, time_str: str):
    """
    Validate that (date, time) falls within clinic operating hours.
    
    Hours:
      - Closed all day Sunday.
      - Monâ€“Sat: 10:00â€“12:59 (morning) and 16:00â€“18:59 (evening).
      - 13:00â€“15:59 lunch gap, 19:00+ closed.
    
    Returns:
        (ok: bool, msg: str) â€” msg is empty when valid; otherwise a short
        explanation suitable to relay to the user.
    
    Fail-open: if either input is empty/unparseable, returns (True, "")
    so callers don't reject legitimate bookings due to format edge cases.
    """
    from datetime import datetime  # noqa: needed for exec() in test context
    if not date_str or not time_str:
        return True, ""
    
    # ---- Parse date (accept ISO + human formats) -------------------------
    date_obj = None
    for fmt in ("%Y-%m-%d", "%d %B %Y", "%d %b %Y", "%B %d %Y", "%b %d %Y"):
        try:
            date_obj = datetime.strptime(str(date_str).strip().replace(",", ""), fmt)
            break
        except ValueError:
            continue
    if date_obj is None:
        return True, ""  # fail-open on unparseable date
    
    # ---- Parse time (accept HH:MM, h:MM AM/PM, h AM/PM) ------------------
    time_obj = None
    raw_time = str(time_str).strip()
    for fmt in ("%H:%M", "%I:%M %p", "%I %p", "%H"):
        try:
            time_obj = datetime.strptime(raw_time, fmt)
            break
        except ValueError:
            continue
    if time_obj is None:
        # Date was valid but time is unrecognisable — ask the user to clarify
        return False, (
            "I didn't catch a valid time. "
            "Our centre hours are 10:00 AM–1:00 PM and 4:00 PM–7:00 PM. "
            "What time works for you?"
        )
    
    # ---- Sunday closed ---------------------------------------------------
    # Python: Monday=0 ... Sunday=6
    if date_obj.weekday() == 6:
        return False, "The centre is closed on Sundays. Please pick another day."
    
    # ---- Operating windows ----------------------------------------------
    minutes = time_obj.hour * 60 + time_obj.minute
    morning_ok = (10 * 60) <= minutes <= (12 * 60 + 59)   # 10:00â€“12:59
    evening_ok = (16 * 60) <= minutes <= (18 * 60 + 59)   # 16:00â€“18:59

    if morning_ok or evening_ok:
        return True, ""

    return False, (
        "Our centre hours are 10:00 AM–1:00 PM and 4:00 PM–7:00 PM. "
        "Please choose a time within those windows."
    )


# =======================================================================
# SHARED: State, Prompt, LLM, TTS helpers — now delegated to modules
# (see utils.py / agent1.py / agent2.py / stt.py / tts.py)
# =======================================================================


# =======================================================================
# VOBIZ CALL CONTROL
# =======================================================================

def _hangup_vobiz_call(call_uuid: str):
    """Tell Vobiz to hang up the actual phone call via REST API."""
    import requests as _requests
    auth_id    = os.getenv("VOBIZ_AUTH_ID", "")
    auth_token = os.getenv("VOBIZ_AUTH_TOKEN", "")
    api_base   = os.getenv("VOBIZ_API_BASE", "https://api.vobiz.ai/api/v1").rstrip("/")

    if not auth_id or not auth_token:
        print("[HANGUP] VOBIZ_AUTH_ID / VOBIZ_AUTH_TOKEN not set — cannot hangup.")
        return
    if not call_uuid or call_uuid in ("unknown", "browser_default"):
        print(f"[HANGUP] Skipping — invalid call_uuid: {call_uuid}")
        return

    url = f"{api_base}/Account/{auth_id}/Call/{call_uuid}/"
    headers = {
        "X-Auth-ID": auth_id,
        "X-Auth-Token": auth_token,
        "Content-Type": "application/json",
    }
    try:
        resp = _requests.delete(url, headers=headers, timeout=3)
        if resp.status_code in (204, 202, 200):
            print(f"[HANGUP] ✅ Call {call_uuid} ended via API (HTTP {resp.status_code})")
        else:
            print(f"[HANGUP] ❌ HTTP {resp.status_code}: {resp.text}")
    except Exception as e:
        print(f"[HANGUP] ❌ Error: {e}")


def _split_first_sentence(text: str) -> tuple[str, str]:
    """Split text into (first_sentence, remainder) for pipelined TTS.

    Returns the first complete sentence and the rest of the text so Sarvam
    receives shorter input for the first chunk, reducing TTFA by ~40-60%.
    Falls back to (text, "") if no sentence boundary is found.
    """
    if not text or not text.strip():
        return text, ""
    t = text.strip()
    # Sentence-ending punctuation; skip decimal numbers and common abbrevs
    _ABBREVS = {"dr", "mr", "mrs", "ms", "prof", "sr", "jr", "st", "no", "vs"}
    for i, ch in enumerate(t):
        if ch not in ".!?":
            continue
        if ch == ".":
            # skip decimals: digit.digit
            if i > 0 and i < len(t) - 1 and t[i-1].isdigit() and t[i+1].isdigit():
                continue
            # skip abbreviations: short word before dot
            word_start = i - 1
            while word_start >= 0 and t[word_start].isalpha():
                word_start -= 1
            word = t[word_start+1:i].lower()
            if word in _ABBREVS:
                continue
        first = t[:i+1].strip()
        rest  = t[i+1:].strip()
        if len(first) >= 15 and rest:
            return first, rest
    return t, ""


# =======================================================================
# VOBIZ TELEPHONY ENDPOINTS
# =======================================================================

@app.post("/answer")
async def vobiz_answer(request: Request):
    """
    Vobiz Answer URL.
    Configure your Vobiz number's Answer URL to:
        https://<your-ngrok-domain>/answer
    """
    body     = await request.body()
    body_str = body.decode()
    print(f"[Vobiz] /answer â€” {body_str[:200]}")

    # Parse form data to extract caller (From) and called DID (To)
    import urllib.parse
    parsed_body = urllib.parse.parse_qs(body_str)
    caller_phone = parsed_body.get("From", [""])[0]
    did_number   = parsed_body.get("To", [""])[0]
    call_uuid    = parsed_body.get("CallUUID", [""])[0]

    # Hangup signal — return empty OK
    if "CallStatus=completed" in body_str or "CallStatus=busy" in body_str:
        return Response(content="OK", media_type="text/plain")

    # Store callUUID → DID so the WebSocket start event can resolve client config
    if call_uuid and did_number:
        _call_did_map[call_uuid] = did_number

    host     = request.headers.get("host", "localhost:8000")
    scheme   = "wss" if ("ngrok" in host or "khyraai" in host) else "ws"

    params = {}
    if caller_phone:
        params["phone"] = caller_phone
    stream_url = f"{scheme}://{host}/vobiz-stream"
    if params:
        stream_url += "?" + urllib.parse.urlencode(params)
    print(f"[Vobiz] Caller: {caller_phone}  DID: {did_number}  CallUUID: {call_uuid}")

    xml_response = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Stream bidirectional="true" keepCallAlive="true">{stream_url}</Stream>
    <Wait length="3600" />
</Response>
"""
    print(f"[Vobiz] Returning XML with stream_url={stream_url}")
    return Response(content=xml_response, media_type="application/xml")


@app.post("/hangup")
@app.get("/hangup")
async def vobiz_hangup(request: Request):
    """Vobiz Hangup URL — acknowledge hangup events."""
    return Response(content="OK", media_type="text/plain")


@app.post("/test-answer")
@app.get("/test-answer")
async def vobiz_test_answer(request: Request):
    """Debug endpoint — returns a plain <Speak> to verify Vobiz executes XML."""
    xml_response = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Speak>Hello, this is a test. Vobiz is executing XML correctly.</Speak>
</Response>
"""
    print("[Vobiz] /test-answer hit")
    return Response(content=xml_response, media_type="application/xml")


@app.post("/transfer/emergency")
@app.get("/transfer/emergency")
async def transfer_emergency(request: Request):
    default_number = os.getenv("EMERGENCY_TRANSFER_NUMBER", "+918660033297")
    number = request.query_params.get("number", default_number)
    
    caller_id_attr = ""
    try:
        if request.method == "POST":
            form = await request.form()
            to_number = form.get("To", "")
            if to_number:
                caller_id_attr = f' callerId="{to_number}"'
    except Exception:
        pass

    xml_response = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Dial{caller_id_attr}>
        <Number>{number}</Number>
    </Dial>
</Response>
"""
    print(f"[Vobiz] /transfer/emergency â€” returning XML, dialing {number}")
    return Response(content=xml_response, media_type="application/xml")

@app.post("/")
async def vobiz_root_callback(request: Request):
    """Fallback endpoint to catch whatever Vobiz is POSTing and why."""
    body = await request.body()
    try:
        data = (await request.form())
        print(f"ðŸš¨ [VOBIZ POST /] Callback Received! Data: {dict(data)}")
    except Exception:
        print(f"ðŸš¨ [VOBIZ POST /] Callback Received! Raw body: {body}")
    xml_response = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'
    return Response(content=xml_response, media_type="text/plain")


@app.websocket("/vobiz-stream")
async def vobiz_stream(websocket: WebSocket):
    await websocket.accept()
    call_sid   = "unknown"
    stream_sid = "unknown"
    call_active = True

    caller_phone = websocket.query_params.get("phone", "unknown")
    did_number   = ""       # resolved from Vobiz start event
    client_cfg   = get_default_config()
    client_id    = client_cfg.get("client_id", "default")
    print(f"\n[Vobiz] Call connected | Caller: {caller_phone} | DID: TBD (awaiting start event)")

    state = get_initial_state()
    state["phone"]         = caller_phone
    state["client_id"]     = client_id
    state["did_number"]    = did_number
    state["connection_id"] = client_cfg.get("connection_id", client_id)
    memory = []
    transcript_log = []

    session_key = "vobiz_call"
    vobiz_encoding = "audio/x-mulaw"

    session_language = None
    agent1_ran = False
    agent1_context = {}
    in_agent3 = False
    agent3_state = {}

    greeted = False

    pending_payload = None
    pending_payload_sent = False

    audio_buffer = bytearray()
    is_speaking  = False

    voiced_bytes = 0
    next_stt_allowed_ts = 0.0
    stt_backoff_secs = 0.0

    BYTES_PER_SEC    = 16000
    FLUSH_AFTER_SECS = 2.0
    SILENCE_SECS     = 0.65
    MIN_SPEECH_SECS  = 0.35
    MIN_VOICED_SECS  = 0.25

    FLUSH_BYTES   = int(BYTES_PER_SEC * FLUSH_AFTER_SECS)
    SILENCE_BYTES = int(BYTES_PER_SEC * SILENCE_SECS)
    MIN_BYTES     = int(BYTES_PER_SEC * MIN_SPEECH_SECS)
    MIN_VOICED_BYTES = int(BYTES_PER_SEC * MIN_VOICED_SECS)

    SILENCE_RMS_THR  = 800

    NOISE_TRANSCRIPTS = {
        "à²šà²°à²¿", "à²šà²°à²¿.", "à²šà²°à²¿ à²šà²¾à²°à³.", "à²šà²°à²¿ à²šà²¾à²°à³", "okay", "ok", "ok.",
        "hmm", "hmm.", ".", "..", "...", "à²¸à²°à²¿", "à²¸à²°à²¿.", "à²¹à²¾à²‚", "à²¹à²¾à²‚.",
        "thank you", "thanks", "yes", "no",
    }

    silence_run = 0

    def _silence_rms_threshold() -> int:
        try:
            enc = (vobiz_encoding or "").lower()
        except Exception:
            enc = ""
        if "audio/x-l16" in enc or "l16" in enc:
            return 250
        return SILENCE_RMS_THR

    def chunk_is_silent(raw_chunk: bytes) -> bool:
        try:
            if "mulaw" in vobiz_encoding or "ulaw" in vobiz_encoding:
                pcm = audioop.ulaw2lin(raw_chunk, 2)
            else:
                pcm = raw_chunk
            return audioop.rms(pcm, 2) < _silence_rms_threshold()
        except Exception:
            return False

    async def process_audio():
        nonlocal audio_buffer, is_speaking, memory, state, silence_run
        nonlocal session_language, agent1_ran, agent1_context, in_agent3, agent3_state
        nonlocal voiced_bytes, next_stt_allowed_ts, stt_backoff_secs, call_active
        nonlocal pending_payload, pending_payload_sent

        buf_snapshot = bytes(audio_buffer)
        voiced_snapshot = voiced_bytes
        audio_buffer = bytearray()
        silence_run = 0
        voiced_bytes = 0

        now_ts = time.time()
        if now_ts < next_stt_allowed_ts:
            return

        if voiced_snapshot < MIN_VOICED_BYTES:
            print(f"[Vobiz] Too little voiced audio ({voiced_snapshot}B) â€” skipping")
            return

        try:
            buf_rms = audioop.rms(buf_snapshot, 2)
            if buf_rms < max(150, int(_silence_rms_threshold() * 0.8)):
                print(f"[Vobiz] RMS={buf_rms} noise â€” skip")
                return
            print(f"[Vobiz] RMS={buf_rms} â€” sending to STT")
        except Exception:
            pass

        is_speaking = True
        parsed = {}
        effective_lang = "en"
        response_text = ""
        detected_lang = "en-IN"
        stt_time = 0.0
        llm_time = 0.0

        try:
            if "mulaw" in vobiz_encoding or "ulaw" in vobiz_encoding:
                pcm_16k = mulaw_8k_to_pcm16_16k(buf_snapshot)
            else:
                pcm_16k = l16_8k_to_pcm16_16k(buf_snapshot)

            wav_bytes = pcm16_to_wav_bytes(pcm_16k, 16000)

            t0 = time.time()
            user_text, detected_lang = await run_stt_http(
                wav_bytes,
                language_code=session_language if session_language else "unknown",
                session_id=session_key,
                client_id=client_id,
            )
            stt_time = time.time() - t0
            print(f"[Vobiz][STT] '{user_text}' ({detected_lang}) in {stt_time:.3f}s")

            if user_text.strip():
                stt_backoff_secs = 0.0
                next_stt_allowed_ts = time.time() + 0.8
            else:
                stt_backoff_secs = min(8.0, (stt_backoff_secs * 2.0) if stt_backoff_secs else 1.5)
                next_stt_allowed_ts = time.time() + stt_backoff_secs

            if not user_text.strip():
                return

            stripped = user_text.strip()
            is_numeric = stripped.replace(".", "").replace("।", "").isdigit()
            if stripped.lower() in NOISE_TRANSCRIPTS or (len(stripped) <= 2 and not is_numeric):
                print(f"[Vobiz][STT] Noise transcript dropped: '{user_text}'")
                return

            if not call_active:
                return

            t1 = time.time()
            # ── Language lock: use session_language if already set; only use STT on first turn ──
            if session_language:
                effective_lang = session_language
                print(f"[Vobiz][LANG] Session locked to {session_language} (ignoring STT {detected_lang})")
            else:
                effective_lang = "en" if str(detected_lang).lower().startswith("en") else "kn"
                session_language = effective_lang
                print(f"[Vobiz][LANG] First turn — locked to {session_language} from STT {detected_lang}")

            # ── Security guardrail pre-check (before any LLM call) ──────────
            _blocked, _guard_response = check_guardrails(user_text, lang=effective_lang)
            if _blocked:
                print(f"[GUARDRAIL] Blocked — responding directly without LLM")
                async for chunk in cartesia_tts_chunked(_guard_response, language=effective_lang):
                    if not call_active:
                        break
                    out_chunk, _ = audioop.ratecv(chunk, 2, 1, 16000, 8000, None)
                    await websocket.send_text(json.dumps({
                        "event": "playAudio",
                        "media": {
                            "contentType": "audio/x-l16",
                            "sampleRate": 8000,
                            "payload": base64.b64encode(out_chunk).decode(),
                        },
                    }))
                return
            # ────────────────────────────────────────────────────────────────

            if not agent1_ran:
                agent1_parsed = await run_agent1(user_text, memory)
                intent = agent1_parsed.get("intent", "enquiry")
                agent1_context = agent1_parsed.get("context", {})
                print(f" [VOBIZ ROUTER] Intent: {intent} | Summary: {agent1_parsed.get('summary')}")

                raw_lang = agent1_parsed.get("language", "kn")
                if raw_lang in ("kn", "en"):
                    session_language = raw_lang
                    print(f"[Vobiz][LANG] Agent-1 override → {session_language}")

                effective_lang = session_language if session_language else effective_lang

                if intent == "greeting":
                    response_text = agent1_parsed.get(
                        "response",
                        "Hello! Welcome to Doctor Deepti's Dental and Orthodontic Clinic. How can I help you today?" if effective_lang == "en" else "à²¨à²®à²¸à³à²•à²¾à²°, à²¡à²¾à²•à³à²Ÿà²°à³ à²¦à³€à²ªà³à²¤à²¿ à²…à²µà²° à²¡à³†à²‚à²Ÿà²²à³ à²®à²¤à³à²¤à³ à²†à²°à³à²¥à³Šà²¡à²¾à²‚à²Ÿà²¿à²•à³ à²•à³à²²à²¿à²¨à²¿à²•à³ à²—à³† à²¸à³à²µà²¾à²—à²¤. à²¨à²¾à²¨à³ à²¨à²¿à²®à²—à³† à²¹à³‡à²—à³† à²¸à²¹à²¾à²¯ à²®à²¾à²¡à²²à²¿?"
                    )
                    parsed = agent1_parsed
                elif intent == "system_check":
                    agent1_ran = True
                    parsed = agent1_parsed
                    response_text = agent1_parsed.get("response", "")
                elif intent == "cancel_reschedule":
                    in_agent3 = True
                    agent1_ran = True
                    print(f"[VOBIZ ROUTER] Agent-3 ({effective_lang}) | Intent: cancel_reschedule")
                    if effective_lang == "en":
                        response_text, agent3_state, parsed = await run_agent3_en(user_text, memory, agent3_state, agent1_context, config=client_cfg)
                    else:
                        response_text, agent3_state, parsed = await run_agent3_kn(user_text, memory, agent3_state, agent1_context, config=client_cfg)
                elif intent == "emergency":
                    agent1_ran = True
                    parsed = agent1_parsed
                    parsed["action"] = "TRANSFER_CALL"
                    parsed["metadata"] = agent1_parsed.get("metadata", {})
                    if not parsed["metadata"]:
                        parsed["metadata"] = {
                            "reason": "Emergency assistance requested",
                            "transfer_target": "+918660033297"
                        }
                    # Use Agent-1's own short response; fallback to short message
                    agent1_response = agent1_parsed.get("response", "").strip()
                    response_text = agent1_response if agent1_response else (
                        "Please hold, connecting you now."
                        if effective_lang == "en"
                        else "\u0CA1\u0CBE\u0C95\u0CCD\u0C9F\u0CB0\u0CCD \u0C97\u0CC6 \u0C95\u0CA8\u0CC6\u0C95\u0CCD\u0C9F\u0CCD \u0CAE\u0CBE\u0CA1\u0CCD\u0CA4\u0CC0\u0CB5\u0CBF."
                    )
                else:
                    print(f"[VOBIZ ROUTER] Agent-2 ({effective_lang}) | Intent: {intent}")
                    if effective_lang == "en":
                        response_text, state, parsed = await run_agent2_en(user_text, memory, state, agent1_context, config=client_cfg)
                    else:
                        response_text, state, parsed = await run_agent2(user_text, memory, state, agent1_context, config=client_cfg)
                    agent1_ran = True
            else:
                if in_agent3:
                    print(f"[VOBIZ ROUTER] Agent-3 ({effective_lang}) | continuation")
                    if effective_lang == "en":
                        response_text, agent3_state, parsed = await run_agent3_en(user_text, memory, agent3_state, agent1_context, config=client_cfg)
                    else:
                        response_text, agent3_state, parsed = await run_agent3_kn(user_text, memory, agent3_state, agent1_context, config=client_cfg)
                else:
                    if effective_lang == "en":
                        response_text, state, parsed = await run_agent2_en(user_text, memory, state, agent1_context, config=client_cfg)
                    else:
                        response_text, state, parsed = await run_agent2(user_text, memory, state, agent1_context, config=client_cfg)

                    # Agent-2 handoff → switch to Agent-3 for cancel/reschedule
                    if parsed.get("handoff"):
                        in_agent3 = True
                        agent1_context["intent"] = "cancel_reschedule"
                        print(f"[VOBIZ ROUTER] Agent-2 handoff → Agent-3 ({effective_lang})")

            # ── Language switch: re-lock session if LLM detected an explicit switch ──
            _lang_switch = parsed.get("language_switch")
            if _lang_switch in ("kn", "en") and _lang_switch != session_language:
                print(f"[LANG SWITCH] User requested switch: {session_language} → {_lang_switch}")
                session_language = _lang_switch
                effective_lang   = _lang_switch

            llm_time = time.time() - t1
            print(f"[Vobiz][LLM] '{response_text}' in {llm_time:.3f}s")

            agent_name = (f"agent3_{effective_lang}") if in_agent3 else (f"agent2_{effective_lang}")
            asyncio.create_task(asyncio.to_thread(log_llm_event, {
                "session_id":   session_key,
                "client_id":    client_id,
                "ts":           t1,
                "agent":        agent_name,
                "model":        LLM_MODEL,
                "latency_ms":   round(llm_time * 1000, 2),
                "user_input":   user_text,
                "llm_response": response_text,
                "success":      bool(response_text),
            }))

            if not call_active:
                return

            memory.append({"role": "user", "content": user_text})
            memory.append({"role": "assistant", "content": response_text})
            transcript_log.append({"speaker": "user", "text": user_text})
            transcript_log.append({"speaker": "bot",  "text": response_text})
            if len(memory) > 12:
                memory = memory[-12:]

            # ── Agent-2: new appointment ──────────────────────────────────
            if (
                not in_agent3
                and not pending_payload_sent
                and not pending_payload
                and parsed.get("done")
                and state.get("name")
                and state.get("date")
                and state.get("time")
                and state.get("reason")
            ):
                safe_state = await _sanitize_state_for_english_tools(state)
                safe_state["call_sid"] = session_key
                pending_payload = build_scheduling_payload(
                    event_type="appointment_create",
                    state=safe_state,
                    phone=caller_phone,
                    confirmation_status="confirmed",
                    language=effective_lang,
                    agent1_context=agent1_context,
                    client_id=client_id,
                )

            # ── Agent-3: cancel or reschedule ─────────────────────────────
            if (
                in_agent3
                and not pending_payload_sent
                and parsed.get("done")
                and parsed.get("confirmation_status") == "confirmed"
                and agent3_state.get("name")
                and agent3_state.get("previous_date")
            ):
                _evt = parsed.get("event_type", "appointment_cancel")
                _a3  = dict(agent3_state)
                _a3["call_sid"] = session_key
                _prev_iso = f"{_a3.get('previous_date', '')} {_a3.get('previous_time', '')}".strip()

                if _evt == "appointment_reschedule":
                    # start_time = new slot; previous_datetime = old slot
                    _a3["date"] = _a3.get("new_date", "")
                    _a3["time"] = _a3.get("new_time", "")
                else:
                    # cancel: start_time = the appointment being cancelled
                    _a3["date"] = _a3.get("previous_date", "")
                    _a3["time"] = _a3.get("previous_time", "")

                pending_payload = build_scheduling_payload(
                    event_type=_evt,
                    state=_a3,
                    phone=caller_phone,
                    previous_datetime_iso=_prev_iso or None,
                    confirmation_status="confirmed",
                    language=effective_lang,
                    agent1_context=agent1_context,
                    client_id=client_id,
                )
                print(f"[Agent-3] Queued {_evt} payload for n8n")

            # Guard: detect non-Kannada Indic script in Kannada responses
            if effective_lang == "kn" and response_text.strip():
                kannada_chars = sum(1 for ch in response_text if '\u0C80' <= ch <= '\u0CFF')
                indic_chars = sum(1 for ch in response_text if '\u0900' <= ch <= '\u0DFF')
                if indic_chars > 0 and kannada_chars < indic_chars * 0.5:
                    print(f"[Vobiz][LLM] ⚠️ Non-Kannada script detected — using fallback")
                    if parsed.get("action") == "END_CALL" or parsed.get("done"):
                        response_text = "\u0CA7\u0CA8\u0CCD\u0CAF\u0CB5\u0CBE\u0CA6\u0C97\u0CB3\u0CC1, \u0CB6\u0CC1\u0CAD \u0CA6\u0CBF\u0CA8!"  # ಧನ್ಯವಾದಗಳು, ಶುಭ ದಿನ!
                        parsed["action"] = "END_CALL"
                        parsed["done"] = True
                    else:
                        response_text = "\u0C95\u0CCD\u0CB7\u0CAE\u0CBF\u0CB8\u0CBF, \u0CAE\u0CA4\u0CCD\u0CA4\u0CC7\u0CAE\u0CCD\u0CAE\u0CC6 \u0CB9\u0CC7\u0CB3\u0CBF."  # ಕ್ಷಮಿಸಿ, ಮತ್ತೇಮ್ಮೆ ಹೇಳಿ.

            t2 = time.time()
            tts_total_bytes = 0

            first_sent, remainder = _split_first_sentence(response_text)
            tts_parts = [first_sent, remainder] if remainder else [response_text]

            for tts_part in tts_parts:
                if not tts_part or not call_active:
                    break
                async for chunk in cartesia_tts_chunked(tts_part, language=effective_lang):
                    tts_total_bytes += len(chunk)
                    if not call_active:
                        break
                    chunk = chunk[:len(chunk) & ~1]
                    if not chunk:
                        continue
                    out_chunk, _ = audioop.ratecv(chunk, 2, 1, 16000, 8000, None)
                    frame = json.dumps({
                        "event": "playAudio",
                        "media": {
                            "contentType": "audio/x-l16",
                            "sampleRate": 8000,
                            "payload": base64.b64encode(out_chunk).decode(),
                        },
                    })
                    await websocket.send_text(frame)

            tts_time = time.time() - t2
            print(f"[Vobiz][TTS] {tts_total_bytes}B PCM in {tts_time:.3f}s")

            log_interaction(
                user_text=user_text, assistant_text=response_text,
                detected_lang=detected_lang, tts_lang=effective_lang,
                stt_time=round(stt_time, 3), llm_time=round(llm_time, 3),
                tts_time=round(tts_time, 3),
                total_time=round(stt_time + llm_time + tts_time, 3)
            )

            if not tts_total_bytes:
                print("[Vobiz][TTS] Empty - skipping send")
                return

            if parsed.get("action") == "END_CALL":
                call_active = False
                try:
                    await asyncio.sleep(1.5)
                    # Tell Vobiz to hang up the actual phone call
                    _hangup_vobiz_call(call_sid)
                    await websocket.close()
                except Exception:
                    pass
                return

            # Handle emergency transfer
            if parsed.get("action") == "TRANSFER_CALL":
                call_active = False
                print(f"[Vobiz] Emergency transfer requested for call {call_sid}")
                try:
                    # Play emergency message first
                    await asyncio.sleep(1.5)
                    # Transfer the call
                    transfer_metadata = parsed.get("metadata", {}) if isinstance(parsed.get("metadata"), dict) else {}
                    trigger_vobiz_transfer(call_sid, transfer_metadata)
                    await websocket.close()
                except Exception as e:
                    print(f"[Vobiz] Transfer error: {e}")
                return

        except Exception as e:
            import traceback
            print(f"[Vobiz][ERROR] process_audio: {e}")
            traceback.print_exc()
        finally:
            is_speaking = False
            if session_key and session_key != "unknown":
                await asyncio.to_thread(session_store.save_session, session_key, state, memory)

    try:
        while call_active:
            try:
                message = await asyncio.wait_for(websocket.receive_text(), timeout=30)
            except asyncio.TimeoutError:
                print("[Vobiz] 30s idle â€” closing")
                break

            try:
                data = json.loads(message)
            except Exception:
                continue

            event = data.get("event", "")
            if event != "media":
                print(f"[Vobiz WebSocket] Raw Event: {message[:250]}")

            if event == "start":
                info = data.get("start", {})
                call_sid   = info.get("callId",   info.get("callSid",   "unknown"))
                stream_sid = info.get("streamId", info.get("streamSid", "unknown"))
                fmt = info.get("mediaFormat", {})
                vobiz_encoding = fmt.get("encoding", "audio/x-mulaw").lower()
                _actual_bps = 8000 if ("mulaw" in vobiz_encoding or "ulaw" in vobiz_encoding) else 16000
                FLUSH_BYTES      = int(_actual_bps * FLUSH_AFTER_SECS)
                SILENCE_BYTES    = int(_actual_bps * SILENCE_SECS)
                MIN_BYTES        = int(_actual_bps * MIN_SPEECH_SECS)
                MIN_VOICED_BYTES = int(_actual_bps * MIN_VOICED_SECS)

                # ── DID resolution: map from /answer, then start event fields ─
                did_number = (
                    _call_did_map.pop(call_sid, None)
                    or info.get("to")
                    or info.get("calledNumber")
                    or info.get("destination")
                    or info.get("did")
                    or info.get("To")
                    or ""
                )
                if did_number:
                    resolved_cfg = get_config_by_did(did_number)
                    if resolved_cfg:
                        client_cfg = resolved_cfg
                        client_id  = client_cfg.get("client_id", "default")
                        state["client_id"]     = client_id
                        state["did_number"]    = did_number
                        state["connection_id"] = client_cfg.get("connection_id", client_id)
                print(f"[Vobiz] Stream started — callSid={call_sid} DID={did_number} client={client_id} encoding={vobiz_encoding}")

                session_key = f"vobiz_{call_sid}"
                saved_state, saved_memory = await asyncio.to_thread(session_store.load_session, session_key)
                if saved_state:
                    state = saved_state
                    state.setdefault("client_id", client_id)
                    state.setdefault("connection_id", client_cfg.get("connection_id", client_id))
                    memory = saved_memory
                    print(f"[Vobiz] Recovered session for {session_key}")

                # Log call start with client/DID context
                try:
                    await asyncio.to_thread(
                        log_call_start, session_key, client_id,
                        did_number=did_number, caller_phone=caller_phone,
                        language=client_cfg.get("default_language", "en"),
                    )
                except Exception as _lce:
                    print(f"[Vobiz] log_call_start error: {_lce}")

                if not greeted and call_active:
                    greeted = True
                    try:
                        welcome_lang  = client_cfg.get("default_language", "en")
                        clinic_name   = client_cfg.get("clinic_name", "our clinic")
                        if welcome_lang == "kn":
                            welcome_text = f"ನಮಸ್ಕಾರ, {clinic_name} ಗೆ ಸ್ವಾಗತ. ನಾನು ನಿಮಗೆ ಹೇಗೆ ಸಹಾಯ ಮಾಡಲಿ?"
                        else:
                            welcome_text = f"Hello, welcome to {clinic_name}. How may I assist you?"
                        welcome_bytes = 0
                        async for chunk in cartesia_tts_chunked(welcome_text, language=welcome_lang):
                            welcome_bytes += len(chunk)
                            out_chunk, _ = audioop.ratecv(chunk, 2, 1, 16000, 8000, None)
                            frame = json.dumps({
                                "event": "playAudio",
                                "media": {
                                    "contentType": "audio/x-l16",
                                    "sampleRate": 8000,
                                    "payload": base64.b64encode(out_chunk).decode(),
                                },
                            })
                            is_speaking = True
                            await websocket.send_text(frame)
                        if welcome_bytes:
                            print(f"[Vobiz] Sent welcome greeting via playAudio ({welcome_bytes}B)")
                            transcript_log.append({"speaker": "bot", "text": welcome_text})
                            asyncio.create_task(asyncio.to_thread(log_llm_event, {
                                "session_id":   session_key,
                                "client_id":    client_id,
                                "ts":           time.time(),
                                "agent":        "greeting",
                                "model":        "",
                                "latency_ms":   0.0,
                                "user_input":   "",
                                "llm_response": welcome_text,
                                "success":      True,
                            }))
                    except Exception as e:
                        print(f"[Vobiz] Welcome greeting error: {e}")
                    finally:
                        is_speaking = False

            elif event == "media":
                if is_speaking:
                    continue

                payload_b64 = data.get("media", {}).get("payload", "")
                if not payload_b64:
                    continue

                chunk = base64.b64decode(payload_b64)
                audio_buffer.extend(chunk)

                if chunk_is_silent(chunk):
                    silence_run += len(chunk)
                else:
                    silence_run = 0
                    voiced_bytes += len(chunk)

                buffer_full = len(audio_buffer) >= FLUSH_BYTES
                silence_enough = (
                    silence_run >= SILENCE_BYTES
                    and voiced_bytes >= MIN_VOICED_BYTES
                    and len(audio_buffer) >= MIN_BYTES
                )
                has_some_silence = silence_run >= int(SILENCE_BYTES * 0.4)

                if (buffer_full and has_some_silence and voiced_bytes >= MIN_VOICED_BYTES) and not is_speaking:
                    asyncio.create_task(process_audio())
                elif silence_enough and not is_speaking:
                    asyncio.create_task(process_audio())

            elif event == "stop":
                print("[Vobiz] Stream stop received")
                call_active = False
                if len(audio_buffer) >= MIN_BYTES and not is_speaking:
                    await process_audio()
                break

            elif event == "mark":
                pass

    except WebSocketDisconnect:
        print("[Vobiz] Caller disconnected")
    except Exception as e:
        print(f"[Vobiz] Stream error: {e}")
    finally:
        call_active = False
        print(f"[Vobiz] Call ended â€” callSid={call_sid}")

        if pending_payload and not pending_payload_sent:
            try:
                pending_payload_sent = True
                asyncio.create_task(asyncio.to_thread(send_to_n8n_webhook_sync, pending_payload))
                asyncio.create_task(asyncio.to_thread(
                    save_agent_appointment, pending_payload,
                    session_key, client_id,
                ))
            except Exception as e:
                print(f"[Vobiz] Webhook send error: {e}")

        try:
            if call_sid and call_sid != "unknown":
                outcome = "booked" if (state.get("appointment_id") or pending_payload) else "enquiry"
                await asyncio.to_thread(
                    log_call_end, session_key, outcome,
                    language=session_language or "",
                    appointment_id=state.get("appointment_id", ""),
                    transcript=json.dumps(transcript_log, ensure_ascii=False),
                )
        except Exception as e:
            print(f"[Vobiz] log_call_end error: {e}")

        try:
            if session_key and session_key != "unknown":
                await asyncio.to_thread(session_store.clear_session, session_key)
        except Exception as e:
            print(f"[Vobiz] Session clear error for {session_key}: {e}")


# =======================================================================
# HEALTH CHECK
# =======================================================================

@app.get("/health")
def health():
    return {"message": "Vobiz Voice Assistant Running 🚀"}


