"""
utils.py — Shared utilities for the Voice Assistant.

Exports:
    get_initial_state()          — fresh appointment state dict
    parse_llm_json()             — robust JSON parser for LLM output
    log_interaction()            — CSV interaction logger
    build_scheduling_payload()   — builds structured scheduling event payload
    send_to_n8n_webhook_sync()   — placeholder for sending data to n8n
"""

import os
import csv
import json
import re
from datetime import datetime


# -----------------------------------------------------------------------
# Security Guardrails — pre-LLM filter
# -----------------------------------------------------------------------

_JAILBREAK_RE = re.compile(
    r"ignore\s+.{0,25}(instructions?|rules?|context|constraints?)"
    r"|forget\s+(your\s+|all\s+|the\s+)?(rules?|instructions?|role|context|previous|prior)"
    r"|you\s+are\s+now\s+(a\s+|an\s+)?"
    r"|new\s+(persona|role|instructions?|rules?|identity)"
    r"|from\s+now\s+on\s+you\s+(are|will|must|should)"
    r"|act\s+as\s+(if\s+)?(you\s+are|a\s+|an\s+)"
    r"|pretend\s+(to\s+be|you\s+are|you\s+were)"
    r"|roleplay\s+as"
    r"|simulate\s+(being|a\s+|an\s+)"
    r"|\bDAN\b"
    r"|jailbreak"
    r"|do\s+anything\s+now"
    r"|override\s+(your\s+|all\s+)?(rules?|instructions?|constraints?|guidelines?)"
    r"|without\s+(any\s+)?(restrictions?|constraints?|limits?|rules?|guidelines?)",
    re.IGNORECASE,
)

_META_RE = re.compile(
    r"who\s+(built|made|created|trained|developed|programmed|coded|wrote|designed)\s+you"
    r"|what\s+(ai|model|llm|language\s+model|system|version|technology|tech)\s+are\s+you"
    r"|are\s+you\s+(chat\s*gpt|gpt[\s\-]?[0-9]*|openai|claude|anthropic|gemini|llama|groq|mistral|an?\s+ai|an?\s+artificial)"
    r"|which\s+(company|organization|team)\s+(made|built|created|trained|owns)\s+you"
    r"|what\s+(is|are)\s+you\s+(running\s+on|powered\s+by|built\s+on|made\s+of)"
    r"|who\s+(is|are)\s+your\s+(creator|maker|developer|owner|company)"
    r"|your\s+(underlying\s+)?(model|architecture|training|ai|technology)",
    re.IGNORECASE,
)

_OUT_OF_SCOPE_RE = re.compile(
    r"(write|compose|draft)\s+(a\s+|an\s+)?(poem|song|story|essay|code|program|script|email\s+for)"
    r"|(solve|calculate|compute)\s+.{0,25}(equation|math|formula|integral)"
    r"|(what\s+is|explain|tell\s+me\s+about)\s+.{0,30}(weather|cricket|politics|stock\s+market|bitcoin|crypto|recipe\s+for|history\s+of)",
    re.IGNORECASE,
)

# Polite deflection responses keyed by language
_GUARDRAIL_RESPONSES = {
    "kn": {
        "jailbreak": "ನಾನು ದಿವ್ಯ, ಕ್ಲಿನಿಕ್‌ನ ರಿಸೆಪ್ಷನಿಸ್ಟ್. ನನ್ನ ಕೆಲಸ ಅಪಾಯಿಂಟ್ಮೆಂಟ್ ಮತ್ತು ಕ್ಲಿನಿಕ್ ಮಾಹಿತಿಗೆ ಸೀಮಿತವಾಗಿದೆ. ನಿಮಗೆ ಹೇಗೆ ಸಹಾಯ ಮಾಡಲಿ?",
        "meta":      "ನಾನು ದಿವ್ಯ, ಕ್ಲಿನಿಕ್‌ನ ರಿಸೆಪ್ಷನಿಸ್ಟ್. ನಿಮಗೆ ಅಪಾಯಿಂಟ್ಮೆಂಟ್ ಅಥವಾ ಕ್ಲಿನಿಕ್ ಬಗ್ಗೆ ಏನಾದರೂ ಸಹಾಯ ಬೇಕಾ?",
        "scope":     "ಕ್ಷಮಿಸಿ, ಅದು ನನ್ನ ಕ್ಷೇತ್ರದ ಹೊರಗೆ. ಕ್ಲಿನಿಕ್ ಅಥವಾ ಅಪಾಯಿಂಟ್ಮೆಂಟ್ ಬಗ್ಗೆ ಸಹಾಯ ಮಾಡಬಲ್ಲೆ.",
    },
    "en": {
        "jailbreak": "I'm Divya, the clinic receptionist. I can only help with appointments and clinic enquiries. How may I assist you?",
        "meta":      "I'm Divya, the clinic receptionist. I'm not able to share information about the technology behind this service. Can I help you with an appointment?",
        "scope":     "I'm sorry, that's outside what I can help with. I can assist with clinic appointments and enquiries.",
    },
}


def check_guardrails(text: str, lang: str = "en") -> tuple:
    """
    Pre-LLM security check.

    Returns (blocked: bool, response_text: str).
    If blocked=True, the caller should speak response_text and skip LLM.
    lang: "kn" | "en"
    """
    t = (text or "").strip()
    if not t:
        return False, ""

    lang_key = "kn" if str(lang).lower().startswith("kn") else "en"
    responses = _GUARDRAIL_RESPONSES[lang_key]

    if _JAILBREAK_RE.search(t):
        print(f"[GUARDRAIL] Jailbreak attempt blocked: {t[:120]}")
        return True, responses["jailbreak"]

    if _META_RE.search(t):
        print(f"[GUARDRAIL] Meta question blocked: {t[:120]}")
        return True, responses["meta"]

    if _OUT_OF_SCOPE_RE.search(t):
        print(f"[GUARDRAIL] Out-of-scope request blocked: {t[:120]}")
        return True, responses["scope"]

    return False, ""


# -----------------------------------------------------------------------
# State Factory
# -----------------------------------------------------------------------
def get_initial_state() -> dict:
    return {
        "name":   None,
        "doctor": "Doctor Naga Deepti - MDS - Orthodontics and Dentofacial Orthopaedics",
        "reason": None,
        "date":   None,
        "time":   None,
        "age":    None,   # collected during appointment flow
    }


# -----------------------------------------------------------------------
# Scheduling Payload Builder
# -----------------------------------------------------------------------
def build_scheduling_payload(
    *,
    event_type: str,          # "appointment_create" | "appointment_cancel" | "appointment_reschedule"
    state: dict,
    phone: str = "",
    previous_datetime_iso: str = None,
    confirmation_status: str = "confirmed",
    notes: str = "",
    language: str = "kn",
    agent1_context: dict = None,
    client_id: str = "",
) -> dict:
    """
    Build the strict scheduling event payload.
    Does NOT send anywhere — caller should log / store / forward.

    Returns the payload dict and also prints it for debugging.
    """
    if agent1_context is None:
        agent1_context = {}

    # Resolve datetime_iso from state date + time if possible
    datetime_iso = None
    if state.get("date") and state.get("time"):
        # Attempt a best-effort ISO parse; keep raw string on failure
        try:
            # normalise "DD Month YYYY HH:MM AM/PM" style inputs
            raw = f"{state['date']} {state['time']}"
            import pytz
            ist_tz = pytz.timezone("Asia/Kolkata")
            for fmt in ("%d %B %Y %I:%M %p", "%d %B %Y %H:%M", "%Y-%m-%d %I:%M %p", "%Y-%m-%d %H:%M"):
                try:
                    naive_dt = datetime.strptime(raw, fmt)
                    # Localize to IST to ensure consistent timezone format
                    localized_dt = ist_tz.localize(naive_dt)
                    datetime_iso = localized_dt.isoformat()
                    break
                except ValueError:
                    continue
        except Exception:
            pass
        if not datetime_iso:
            datetime_iso = f"{state.get('date', '')} {state.get('time', '')}".strip()

    # Coerce age to int safely
    raw_age = state.get("age")
    try:
        age_int = int(raw_age) if raw_age not in (None, "", "unknown") else None
    except (ValueError, TypeError):
        age_int = None

    from datetime import timedelta
    # Calculate end_time (assume 30 mins for now)
    end_time_iso = None
    if datetime_iso:
        try:
            st = datetime.fromisoformat(datetime_iso)
            end_time_iso = (st + timedelta(minutes=30)).isoformat()
        except:
            pass
            
    # Resolve status
    mapped_status = {
        "appointment_create": "scheduled",
        "appointment_cancel": "cancelled",
        "appointment_reschedule": "rescheduled",
        "emergency_handoff": "emergency_transferred",
    }.get(event_type, "new")

    import uuid
    from datetime import timezone
    now_ts = datetime.now(timezone.utc).astimezone().isoformat()

    appt_type = (
        state.get("requested_procedure")
        or state.get("reason")
        or "consultation"
    ) or "consultation"

    raw_doctor = state.get("doctor", "") or ""
    doctor_name = "Doctor Naga Deepti" if not raw_doctor or raw_doctor == "Doctor Naga Deepti - MDS - Orthodontics and Dentofacial Orthopaedics" else raw_doctor

    resolved_client_id = client_id or state.get("client_id", "")

    payload = {
        "id": str(uuid.uuid4()),
        "event_type": event_type,
        "session_id": state.get("call_sid", ""),
        "client_id": resolved_client_id,
        "connection_id": state.get("connection_id", state.get("call_sid", "")),
        "google_event_id": "",
        "patient_name": state.get("name", ""),
        "patient_phone": (lambda p: ("+" + p) if p and not p.startswith("+") else p)(
            (phone or state.get("phone", "") or "").strip()
        ),
        "start_time": datetime_iso or "",
        "end_time": end_time_iso or "",
        "appointment_type": appt_type,
        "status": mapped_status,
        "doctor_name": doctor_name,
        "reason": state.get("reason", ""),
        "requested_procedure": state.get("requested_procedure", ""),
        "previous_datetime": previous_datetime_iso or "",
        "booked_via": "voice_assistant",
        "agent_notes": notes or f"Language: {language}. Confirmation: {confirmation_status}",
        "created_at": now_ts,
        "updated_at": now_ts
    }

    print(f"[SCHEDULING PAYLOAD]\n{json.dumps(payload, indent=2, ensure_ascii=False)}")

    return payload


# -----------------------------------------------------------------------
# LLM JSON Parser
# -----------------------------------------------------------------------
def parse_llm_json(text: str) -> dict:
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r'\{.*\}', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass
    return {
        "response": "ಕ್ಷಮಿಸಿ, ಮತ್ತೊಮ್ಮೆ ಹೇಳಿ.",
        "state": {},
        "missing_fields": ["name", "doctor", "reason", "date", "time"],
        "done": False
    }


# -----------------------------------------------------------------------
# CSV Interaction Logger
# -----------------------------------------------------------------------
LOG_DIR  = os.path.join(os.path.dirname(__file__), "..", ".logs")
LOG_FILE = os.path.join(LOG_DIR, "voice_log.csv")
if not os.path.exists(LOG_DIR):
    os.makedirs(LOG_DIR)

LOG_HEADERS = [
    "timestamp", "user_text", "assistant_text",
    "detected_lang", "tts_lang",
    "stt_time", "llm_time", "tts_time", "total_time", "tts_chars"
]

def log_interaction(user_text, assistant_text, detected_lang, tts_lang,
                    stt_time, llm_time, tts_time, total_time):
    file_exists = os.path.isfile(LOG_FILE)
    with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=LOG_HEADERS)
        if not file_exists:
            writer.writeheader()
        writer.writerow({
            "timestamp":      datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "user_text":      user_text,
            "assistant_text": assistant_text,
            "detected_lang":  detected_lang,
            "tts_lang":       tts_lang,
            "stt_time":       round(stt_time, 3),
            "llm_time":       round(llm_time, 3),
            "tts_time":       round(tts_time, 3),
            "total_time":     round(total_time, 3),
            "tts_chars":      len(assistant_text)
        })
    print(f"📋 Logged interaction to {LOG_FILE}")

# -----------------------------------------------------------------------
# Session Persistence
# -----------------------------------------------------------------------
from database import SessionStore

# -----------------------------------------------------------------------
# Telephony Control
# -----------------------------------------------------------------------
def trigger_vobiz_transfer(call_uuid: str, metadata: dict):
    """
    Calls the Vobiz Transfer API to redirect a live call.

    Flow:
      1. POST to Vobiz API with aleg_url pointing at our /transfer/emergency endpoint.
      2. Vobiz fetches /transfer/emergency, receives XML, and dials the emergency number.

    Required .env vars:
        VOBIZ_AUTH_ID         — Vobiz Auth ID
        VOBIZ_AUTH_TOKEN      — Vobiz Auth Token
        VOBIZ_API_BASE        — API base  (default: https://api.vobiz.ai/api/v1)
        SERVER_BASE_URL       — Public base URL of this server (e.g. ngrok domain)
        EMERGENCY_TRANSFER_NUMBER — Override default number (+918660033297)
    """
    import requests as _requests
    import os

    auth_id    = os.getenv("VOBIZ_AUTH_ID", "")
    auth_token = os.getenv("VOBIZ_AUTH_TOKEN", "")
    api_base   = os.getenv("VOBIZ_API_BASE", "https://api.vobiz.ai/api/v1").rstrip("/")
    server_url = os.getenv("SERVER_BASE_URL", "").rstrip("/")
    target     = metadata.get("transfer_target") or os.getenv("EMERGENCY_TRANSFER_NUMBER", "+918660033297")
    reason     = metadata.get("reason", "Emergency detected")

    if not auth_id or not auth_token:
        print("🚨 [TRANSFER][ERROR] VOBIZ_AUTH_ID / VOBIZ_AUTH_TOKEN not set in .env — cannot transfer call.")
        return
    if not server_url:
        print("🚨 [TRANSFER][ERROR] SERVER_BASE_URL not set in .env — cannot build aleg_url.")
        return
    if not call_uuid or call_uuid in ("unknown", "browser_default"):
        print(f"🚨 [TRANSFER][MOCK] Simulated transfer for browser/unknown. Target: {target} | Reason: {reason}")
        return

    # Build the aleg_url pointing at our /transfer/emergency endpoint.
    # Append the number as a query param so the XML can use it dynamically.
    import urllib.parse
    aleg_url = f"{server_url}/transfer/emergency?number={urllib.parse.quote(target)}"
    
    # Correct Vobiz Endpoint: POST /api/v1/Account/{auth_id}/Call/{call_uuid}/
    transfer_url = f"{api_base}/Account/{auth_id}/Call/{call_uuid}/"

    payload = {
        "legs":         "aleg",
        "aleg_url":     aleg_url,
        "aleg_method":  "POST",
    }
    
    # Correct Vobiz Authentication Headers (No Basic Auth)
    headers = {
        "X-Auth-ID": auth_id,
        "X-Auth-Token": auth_token,
        "Content-Type": "application/json",
    }

    print(f"🚨 [TRANSFER] Initiating — call_uuid={call_uuid} | target={target} | reason={reason}")
    print(f"🚨 [TRANSFER] POST {transfer_url}")
    print(f"🚨 [TRANSFER] aleg_url={aleg_url}")

    try:
        resp = _requests.post(
            transfer_url,
            json=payload,
            headers=headers,
            timeout=3,
        )
        # Vobiz accepts with 202
        if resp.status_code == 202:
            print(f"✅ [TRANSFER] Success — HTTP 202: Call transfer initiated.")
        else:
            print(f"❌ [TRANSFER] Failed — HTTP {resp.status_code}: {resp.text}")
    except _requests.exceptions.Timeout:
        print("❌ [TRANSFER][ERROR] Vobiz API call timed out after 3 seconds.")
    except _requests.exceptions.ConnectionError as e:
        print(f"❌ [TRANSFER][ERROR] Network error contacting Vobiz API: {e}")
    except Exception as e:
        print(f"❌ [TRANSFER][ERROR] Unexpected error: {e}")

# -----------------------------------------------------------------------
# N8N Webhook Placeholder
# -----------------------------------------------------------------------
def send_to_n8n_webhook_sync(payload: dict) -> bool:
    """
    Send payload to the n8n webhook URL (synchronous, runs in a thread).

    Returns:
        True  — HTTP 200 / 201 / 202 received (n8n accepted the payload).
        False — URL not configured, HTTP error, or any exception.
    """
    from dotenv import load_dotenv
    import requests
    load_dotenv()

    webhook_url = os.getenv("N8N_WEBHOOK_URL", "")

    if not webhook_url:
        print("🟡 [N8N] Webhook URL not set (N8N_WEBHOOK_URL). Payload logged but not sent.")
        return False

    try:
        print(f"🌐 [N8N] Sending payload to {webhook_url}...")
        resp = requests.post(webhook_url, json=payload, timeout=5)
        if resp.status_code in (200, 201, 202):
            print(f"✅ [N8N] Payload delivered successfully! (HTTP {resp.status_code})")
            return True
        else:
            print(f"❌ [N8N] Delivery failed — HTTP {resp.status_code}: {resp.text[:200]}")
            return False
    except requests.exceptions.Timeout:
        print("❌ [N8N] Request timed out after 5s.")
        return False
    except Exception as e:
        print(f"❌ [N8N] Request error: {e}")
        return False

