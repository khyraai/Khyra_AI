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

from groq import AsyncGroq

# -----------------------------------------------------------------------
# Module imports â€” separated concerns
# -----------------------------------------------------------------------
from utils import (
    get_initial_state, parse_llm_json, log_interaction,
    build_scheduling_payload, SessionStore, trigger_vobiz_transfer,
    send_to_n8n_webhook_sync
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
from tts import cartesia_tts_collect, cartesia_tts_stream
from database import check_availability, verify_appointment_for_cancellation, update_appointment_status, reschedule_appointment

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
groq_client = AsyncGroq(api_key=os.getenv("GROQ_API_KEY"))
sarvam_api_key = os.getenv("SARVAM_API_KEY")

# Initialize Session Store
session_store = SessionStore()


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

async def run_agent2(user_text: str, memory: list, state: dict, agent1_context: dict):
    response, state, parsed = await _run_agent2_kn(user_text, memory, state, agent1_context, groq_client)
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
        avail_result = check_availability(raw_date, check_time)
        is_available = avail_result.get('available', True)
        
        if is_available:
            status_msg = "System: The slot is AVAILABLE."
        else:
            next_date = avail_result.get('next_date')
            next_time = avail_result.get('next_time')
            prev_date = avail_result.get('prev_date')
            prev_time = avail_result.get('prev_time')
            suggestions = []
            if prev_date and prev_time:
                suggestions.append(f"earlier slot: {prev_date} at {prev_time}")
            if next_date and next_time:
                suggestions.append(f"next slot: {next_date} at {next_time}")
            if suggestions:
                status_msg = f"System: The slot is already BOOKED. Suggest these alternatives â€” " + "; ".join(suggestions) + "."
            else:
                status_msg = "System: The slot is already BOOKED. No alternative slots available."
        
        memory_with_check = memory + [
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": "Let me check the schedule..."},
            {"role": "user", "content": status_msg}
        ]
        response, state, parsed = await _run_agent2_kn("à²¦à²¯à²µà²¿à²Ÿà³à²Ÿà³ à²®à³à²‚à²¦à³à²µà²°à²¿à²¯à²¿à²°à²¿.", memory_with_check, state, agent1_context, groq_client)
    return response, state, parsed

async def run_agent2_en(user_text: str, memory: list, state: dict, agent1_context: dict):
    response, state, parsed = await _run_agent2_en(user_text, memory, state, agent1_context, groq_client)
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
        tool_state = await _sanitize_state_for_english_tools(state)
        avail_result = check_availability(check_date, check_time)
        is_available = avail_result.get('available', True)
        
        if is_available:
            status_msg = "System: The slot is AVAILABLE."
        else:
            next_date = avail_result.get('next_date')
            next_time = avail_result.get('next_time')
            prev_date = avail_result.get('prev_date')
            prev_time = avail_result.get('prev_time')
            suggestions = []
            if prev_date and prev_time:
                suggestions.append(f"earlier slot: {prev_date} at {prev_time}")
            if next_date and next_time:
                suggestions.append(f"next slot: {next_date} at {next_time}")
            if suggestions:
                status_msg = f"System: The slot is already BOOKED. Suggest these alternatives â€” " + "; ".join(suggestions) + "."
            else:
                status_msg = "System: The slot is already BOOKED. No alternative slots available."
        
        memory_with_check = memory + [
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": "Let me check the schedule..."},
            {"role": "user", "content": status_msg}
        ]
        response, state, parsed = await _run_agent2_en("Please proceed based on the availability.", memory_with_check, state, agent1_context, groq_client)
    return response, state, parsed

async def run_agent3_kn(user_text: str, memory: list, state: dict, context: dict):
    response, state, parsed = await _run_agent3_kn(user_text, memory, state, context, groq_client)
    
    # Intercept VERIFY_APPOINTMENT action
    if parsed.get("action") == "VERIFY_APPOINTMENT":
        print(f"[Agent-3-KN] Intercepting VERIFY_APPOINTMENT")
        name = state.get("name", "")
        phone = state.get("phone", "")
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
        response, state, parsed = await _run_agent3_kn("ದಯವಿಟ್ಟು ಮುಂದುವರಿಯಿರಿ.", memory_with_verify, state, context, groq_client)

    # Intercept CHECK_AVAILABILITY action (reschedule only)
    if parsed.get("action") == "CHECK_AVAILABILITY":
        print(f"[Agent-3-KN] Intercepting CHECK_AVAILABILITY")
        new_date = state.get("new_date", "")
        new_time = state.get("new_time", "")
        print(f"[Agent-3-KN] Checking slot: {new_date} {new_time}")
        avail_result = check_availability(new_date, new_time)
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
        response, state, parsed = await _run_agent3_kn("ದಯವಿಟ್ಟು ಮುಂದುವರಿಯಿರಿ.", memory_with_avail, state, context, groq_client)

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


async def run_agent3_en(user_text: str, memory: list, state: dict, context: dict):
    response, state, parsed = await _run_agent3_en(user_text, memory, state, context, groq_client)
    
    # Intercept VERIFY_APPOINTMENT action
    if parsed.get("action") == "VERIFY_APPOINTMENT":
        print(f"[Agent-3-EN] Intercepting VERIFY_APPOINTMENT")
        name = state.get("name", "")
        phone = state.get("phone", "")
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
        response, state, parsed = await _run_agent3_en("Please proceed based on the verification result.", memory_with_verify, state, context, groq_client)

    # Intercept CHECK_AVAILABILITY action (reschedule only)
    if parsed.get("action") == "CHECK_AVAILABILITY":
        print(f"[Agent-3-EN] Intercepting CHECK_AVAILABILITY")
        new_date = state.get("new_date", "")
        new_time = state.get("new_time", "")
        print(f"[Agent-3-EN] Checking slot: {new_date} {new_time}")
        avail_result = check_availability(new_date, new_time)
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
        response, state, parsed = await _run_agent3_en("Please proceed based on the availability result.", memory_with_avail, state, context, groq_client)

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

async def run_stt_http(audio_bytes: bytes, filename: str = "audio.wav"):
    return await _run_stt_http(audio_bytes, sarvam_api_key, filename)

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
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=40,
            temperature=0.0,
            stream=False,
        )
        out = (resp.choices[0].message.content or "").strip()
        return out if out.isascii() else "".join(ch for ch in out if ord(ch) < 128).strip()
    except Exception:
        return "".join(ch for ch in s if ord(ch) < 128).strip()

async def _sanitize_state_for_english_tools(state: dict) -> dict:
    safe = dict(state or {})
    for k in ("name", "reason", "date", "time"):
        if safe.get(k) is not None:
            safe[k] = await _ensure_english_value(safe.get(k))
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
        return True, ""  # fail-open on unparseable time
    
    # ---- Sunday closed ---------------------------------------------------
    # Python: Monday=0 ... Sunday=6
    if date_obj.weekday() == 6:
        return False, "The clinic is closed on Sundays. Please pick another day."
    
    # ---- Operating windows ----------------------------------------------
    minutes = time_obj.hour * 60 + time_obj.minute
    morning_ok = (10 * 60) <= minutes <= (12 * 60 + 59)   # 10:00â€“12:59
    evening_ok = (16 * 60) <= minutes <= (18 * 60 + 59)   # 16:00â€“18:59
    
    if morning_ok or evening_ok:
        return True, ""
    
    return False, (
        "Our clinic hours are 10:00 AMâ€“1:00 PM and 4:00 PMâ€“7:00 PM. "
        "Please choose a time within those windows."
    )

# =======================================================================
# SHARED: State, Prompt, LLM, TTS helpers â€” now delegated to modules
# (see utils.py / agent1.py / agent2.py / stt.py / tts.py)
# =======================================================================


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

    # Parse form data to safely extract the caller number (typically in 'From')
    import urllib.parse
    parsed_body = urllib.parse.parse_qs(body_str)
    caller_phone = parsed_body.get("From", [""])[0]

    # Hangup signal â€” return empty OK
    if "CallStatus=completed" in body_str or "CallStatus=busy" in body_str:
        return Response(content="OK", media_type="text/plain")

    host     = request.headers.get("host", "localhost:8000")
    scheme   = "wss" if ("ngrok" in host or request.url.scheme == "https") else "ws"
    
    stream_url = f"{scheme}://{host}/vobiz-stream"
    if caller_phone:
        stream_url += f"?phone={urllib.parse.quote(caller_phone)}"
        print(f"[Vobiz] Caller Phone: {caller_phone}")

    xml_response = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Stream bidirectional="true" keepCallAlive="true">{stream_url}</Stream>
    <Wait length="3600" />
</Response>
"""
    print(f"[Vobiz] Returning XML with stream_url={stream_url}")
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
    print(f"\n[Vobiz] ðŸ“ž Call connected | Caller: {caller_phone}")

    state = get_initial_state()
    state["phone"] = caller_phone
    memory = []

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
            user_text, detected_lang = await run_stt_http(wav_bytes)
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

            if user_text.strip().lower() in NOISE_TRANSCRIPTS or len(user_text.strip()) <= 2:
                print(f"[Vobiz][STT] Noise transcript dropped: '{user_text}'")
                return

            if not call_active:
                return

            t1 = time.time()
            effective_lang = "en" if str(detected_lang).lower().startswith("en") else "kn"
            print(f"[Vobiz][LANG LOCKED from STT] {detected_lang} → {effective_lang}")

            if not agent1_ran:
                agent1_parsed = await run_agent1(user_text, memory)
                intent = agent1_parsed.get("intent", "enquiry")
                agent1_context = agent1_parsed.get("context", {})
                print(f" [VOBIZ ROUTER] Intent: {intent} | Summary: {agent1_parsed.get('summary')}")

                raw_lang = agent1_parsed.get("language", "unknown")
                if session_language is None and raw_lang != "unknown":
                    session_language = raw_lang

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
                        response_text, agent3_state, parsed = await run_agent3_en(user_text, memory, agent3_state, agent1_context)
                    else:
                        response_text, agent3_state, parsed = await run_agent3_kn(user_text, memory, agent3_state, agent1_context)
                elif intent == "emergency":
                    agent1_ran = True
                    parsed = agent1_parsed
                    response_text = (
                        "This sounds like an emergency. Please come to the clinic immediately or call us directly. We will inform the doctor right away."
                        if effective_lang == "en"
                        else "à²‡à²¦à³ à²¤à³à²°à³à²¤à³ à²¸à²®à²¸à³à²¯à³†à²¯à²‚à²¤à³† à²•à²¾à²£à³à²¤à³à²¤à²¿à²¦à³†. à²¦à²¯à²µà²¿à²Ÿà³à²Ÿà³ à²¤à²•à³à²·à²£ à²•à³à²²à²¿à²¨à²¿à²•à³â€Œà²—à³† à²¬à²¨à³à²¨à²¿ à²…à²¥à²µà²¾ à²¨à³‡à²°à²µà²¾à²—à²¿ à²¨à²®à³à²®à²¨à³à²¨à³ à²¸à²‚à²ªà²°à³à²•à²¿à²¸à²¿. à²¨à²¾à²µà³ à²¤à²•à³à²·à²£ à²µà³ˆà²¦à³à²¯à²°à²¨à³à²¨à³ à²¤à²¿à²³à²¿à²¸à³à²¤à³à²¤à³‡à²µà³†."
                    )
                else:
                    print(f"[VOBIZ ROUTER] Agent-2 ({effective_lang}) | Intent: {intent}")
                    if effective_lang == "en":
                        response_text, state, parsed = await run_agent2_en(user_text, memory, state, agent1_context)
                    else:
                        response_text, state, parsed = await run_agent2(user_text, memory, state, agent1_context)
                    agent1_ran = True
            else:
                if session_language is None:
                    agent1_parsed = await run_agent1(user_text, memory)
                    raw_lang = agent1_parsed.get("language", "unknown")
                    if raw_lang != "unknown":
                        session_language = raw_lang
                effective_lang = session_language if session_language else effective_lang

                if in_agent3:
                    print(f"[VOBIZ ROUTER] Agent-3 ({effective_lang}) | continuation")
                    if effective_lang == "en":
                        response_text, agent3_state, parsed = await run_agent3_en(user_text, memory, agent3_state, agent1_context)
                    else:
                        response_text, agent3_state, parsed = await run_agent3_kn(user_text, memory, agent3_state, agent1_context)
                else:
                    if effective_lang == "en":
                        response_text, state, parsed = await run_agent2_en(user_text, memory, state, agent1_context)
                    else:
                        response_text, state, parsed = await run_agent2(user_text, memory, state, agent1_context)

            llm_time = time.time() - t1
            print(f"[Vobiz][LLM] '{response_text}' in {llm_time:.3f}s")

            if not call_active:
                return

            memory.append({"role": "user", "content": user_text})
            memory.append({"role": "assistant", "content": response_text})
            if len(memory) > 12:
                memory = memory[-12:]

            if (
                pending_payload is None
                and not pending_payload_sent
                and parsed.get("done")
                and state.get("name")
                and state.get("date")
                and state.get("time")
                and state.get("reason")
            ):
                safe_state = await _sanitize_state_for_english_tools(state)
                pending_payload = build_scheduling_payload(
                    event_type="appointment_create",
                    state=safe_state,
                    phone=caller_phone,
                    confirmation_status="confirmed",
                    language="en",
                    agent1_context=agent1_context,
                )

            if not response_text.strip():
                print("[Vobiz][LLM] Empty response â€” using fallback")
                response_text = "Sorry, could you please repeat that?" if effective_lang == "en" else "à²•à³à²·à²®à²¿à²¸à²¿, à²®à²¤à³à²¤à³Šà²®à³à²®à³† à²¹à³‡à²³à²¿."

            t2 = time.time()
            tts_pcm = await cartesia_tts_collect(response_text, language=effective_lang)
            tts_time = time.time() - t2
            print(f"[Vobiz][TTS] {len(tts_pcm)}B PCM in {tts_time:.3f}s")

            log_interaction(
                user_text=user_text, assistant_text=response_text,
                detected_lang=detected_lang, tts_lang=effective_lang,
                stt_time=round(stt_time, 3), llm_time=round(llm_time, 3),
                tts_time=round(tts_time, 3),
                total_time=round(stt_time + llm_time + tts_time, 3)
            )

            if not tts_pcm:
                print("[Vobiz][TTS] Empty â€” skipping send")
                return

            if "mulaw" in vobiz_encoding or "ulaw" in vobiz_encoding:
                pcm_8k, _ = audioop.ratecv(tts_pcm, 2, 1, 16000, 8000, None)
                out_audio = audioop.lin2ulaw(pcm_8k, 2)
                content_type = "audio/x-mulaw"
            else:
                out_audio, _ = audioop.ratecv(tts_pcm, 2, 1, 16000, 8000, None)
                content_type = "audio/x-l16"

            if call_active:
                frame = json.dumps({
                    "event": "playAudio",
                    "media": {
                        "contentType": content_type,
                        "sampleRate": 8000,
                        "payload": base64.b64encode(out_audio).decode(),
                    },
                })
                await websocket.send_text(frame)
                print(f"[Vobiz] Sent {len(out_audio)}B ({content_type}) via playAudio")

            if parsed.get("action") == "END_CALL":
                call_active = False
                try:
                    await asyncio.sleep(1.0)
                    await websocket.close()
                except Exception:
                    pass
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
                call_sid = info.get("callId", info.get("callSid", "unknown"))
                stream_sid = info.get("streamId", info.get("streamSid", "unknown"))
                fmt = info.get("mediaFormat", {})
                vobiz_encoding = fmt.get("encoding", "audio/x-mulaw").lower()
                print(f"[Vobiz] Stream started â€” callSid={call_sid} encoding={vobiz_encoding}")

                session_key = f"vobiz_{call_sid}"
                saved_state, saved_memory = await asyncio.to_thread(session_store.load_session, session_key)
                if saved_state:
                    state = saved_state
                    memory = saved_memory
                    print(f"[Vobiz] Recovered session for {session_key}")

                if not greeted and call_active:
                    greeted = True
                    try:
                        welcome_text = "Hello, welcome to Doctor Deepti's Dental and Orthodontic Clinic. How may I assist you?"
                        tts_pcm = await cartesia_tts_collect(welcome_text, language="en")
                        if tts_pcm:
                            out_audio, _ = audioop.ratecv(tts_pcm, 2, 1, 16000, 8000, None)
                            frame = json.dumps({
                                "event": "playAudio",
                                "media": {
                                    "contentType": "audio/x-l16",
                                    "sampleRate": 8000,
                                    "payload": base64.b64encode(out_audio).decode(),
                                },
                            })
                            is_speaking = True
                            await websocket.send_text(frame)
                            print(f"[Vobiz] âœ… Sent welcome greeting via playAudio ({len(out_audio)}B)")
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
            except Exception as e:
                print(f"[Vobiz] Webhook send error: {e}")

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


