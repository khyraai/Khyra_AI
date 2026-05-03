"""
test_isolation.py — Multi-client isolation verification.

Inserts two DEMO clients, verifies full isolation across:
  - DID routing (each DID resolves to correct client)
  - Welcome greeting (language + clinic name per client)
  - Booking isolation (agent_appointments tagged per client_id)
  - appointments table client_id
  - session client_id
  - build_scheduling_payload client_id
  - Cross-query isolation (client A cannot see client B's bookings)
  - DB column presence (reschedules, appointments, agent_appointments)

All TEST_ rows are cleaned up on exit.

Run inside container:
    python src/test_isolation.py
"""

import os, sys, uuid
sys.path.insert(0, os.path.dirname(__file__))

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql://khyra:khyra_secret@postgres:5432/khyra_db"
)

from datetime import datetime, date, timedelta
import pytz

from database import (
    init_db, get_conn, upsert_client, save_agent_appointment,
    insert_appointment, log_call_start,
)
import client_config as _cc
from client_config import get_config_by_did, get_config_by_client_id, get_default_config
from utils import build_scheduling_payload

# ── Init DB (runs migrations including new client_id columns) ─────────────────
init_db()

# ── Demo clients ──────────────────────────────────────────────────────────────
DEMO_DEEPTI = {
    "client_id":                "DEMO_deepti_dental",
    "did_number":               "+919999100001",  # fictional test DID
    "clinic_name":              "Doctor Deepti's Dental and Orthodontic Centre",
    "doctor_name":              "Doctor Naga Deepti",
    "doctor_qualifications":    "MDS — Orthodontics",
    "address":                  "39, 3rd Cross, Dwarakanagar, Hoskerehalli, Bangalore",
    "timings":                  "Mon-Sat 10 AM-1 PM and 4 PM-7 PM",
    "doctor_mobile":            "+91 9187471874",
    "consultation_fee_min":     200,
    "consultation_fee_max":     300,
    "default_language":         "kn",
    "emergency_transfer_number": "+918660033297",
    "connection_id":            "deepti_dental",
    "morning_open":             "10:00",
    "morning_close":            "13:00",
    "evening_open":             "16:00",
    "evening_close":            "19:00",
    "closed_weekdays":          "6",
}

DEMO_SECOND = {
    "client_id":                "DEMO_second_clinic",
    "did_number":               "+919000001111",
    "clinic_name":              "Second Demo Clinic",
    "doctor_name":              "Dr. Rajesh Kumar",
    "doctor_qualifications":    "MBBS, MD",
    "address":                  "12 MG Road, Bangalore",
    "timings":                  "Mon-Fri 9 AM-12 PM and 5 PM-8 PM",
    "doctor_mobile":            "+91 9000001111",
    "consultation_fee_min":     300,
    "consultation_fee_max":     500,
    "default_language":         "en",
    "emergency_transfer_number": "+919000001112",
    "connection_id":            "second_demo",
    "morning_open":             "09:00",
    "morning_close":            "12:00",
    "evening_open":             "17:00",
    "evening_close":            "20:00",
    "closed_weekdays":          "5,6",
}

_IST = pytz.timezone("Asia/Kolkata")
_results: list[tuple[str, bool]] = []

def _next_weekday(wd: int) -> date:
    today = date.today()
    days_ahead = wd - today.weekday()
    if days_ahead <= 0:
        days_ahead += 7
    return today + timedelta(days=days_ahead)

SLOT_DATE = _next_weekday(0)  # next Monday
SLOT_ISO  = _IST.localize(
    datetime.combine(SLOT_DATE, datetime.strptime("11:00 AM", "%I:%M %p").time())
).isoformat()
SLOT_DATE_STR = SLOT_DATE.strftime("%d %B %Y")

def check(label: str, ok: bool, detail: str = "") -> bool:
    icon = "  ✅ PASS" if ok else "  ❌ FAIL"
    print(f"{icon}  {label}")
    if detail:
        print(f"         {detail}")
    _results.append((label, ok))
    return ok


# ── Cleanup ───────────────────────────────────────────────────────────────────
def _cleanup():
    with get_conn() as cur:
        cur.execute("DELETE FROM agent_appointments WHERE client_id LIKE 'DEMO_%' OR id LIKE 'DEMO_%'")
        cur.execute("DELETE FROM appointments      WHERE client_id LIKE 'DEMO_%' OR id LIKE 'DEMO_%'")
        cur.execute("DELETE FROM sessions          WHERE client_id LIKE 'DEMO_%' OR session_id LIKE 'DEMO_%'")
        cur.execute("DELETE FROM call_logs         WHERE client_id LIKE 'DEMO_%' OR session_id LIKE 'DEMO_%'")
        cur.execute("DELETE FROM clients           WHERE client_id LIKE 'DEMO_%'")
    print("\n🧹  Cleanup complete — all DEMO_ rows deleted.\n")


# ═══════════════════════════════════════════════════════════════════════
# T01 — DB schema has client_id in all required tables
# ═══════════════════════════════════════════════════════════════════════
def t01_schema():
    print("\n── T01: DB schema — client_id columns present ──")
    checks = [
        ("appointments",       "client_id"),
        ("agent_appointments", "client_id"),
        ("reschedules",        "client_id"),
        ("reschedules",        "session_id"),
        ("call_logs",          "client_id"),
        ("sessions",           "client_id"),
        ("stt_events",         "client_id"),
        ("tts_events",         "client_id"),
        ("llm_events",         "client_id"),
        ("cancellations",      "client_id"),
        ("audit_log",          "client_id"),
    ]
    with get_conn() as cur:
        for table, col in checks:
            cur.execute("""
                SELECT 1 FROM information_schema.columns
                WHERE table_name=%s AND column_name=%s
            """, (table, col))
            exists = cur.fetchone() is not None
            check(f"{table}.{col} column exists", exists)


# ═══════════════════════════════════════════════════════════════════════
# T02 — Client upsert + DB persistence
# ═══════════════════════════════════════════════════════════════════════
def t02_client_upsert():
    print("\n── T02: Client upsert ──")
    upsert_client(DEMO_DEEPTI)
    upsert_client(DEMO_SECOND)
    with get_conn() as cur:
        cur.execute(
            "SELECT client_id, clinic_name, default_language, did_number FROM clients "
            "WHERE client_id IN (%s, %s)",
            ("DEMO_deepti_dental", "DEMO_second_clinic"),
        )
        rows = {r["client_id"]: dict(r) for r in cur.fetchall()}
    check("Deepti row inserted",  "DEMO_deepti_dental"  in rows)
    check("Second row inserted",  "DEMO_second_clinic"  in rows)
    check("Deepti language = kn", rows.get("DEMO_deepti_dental", {}).get("default_language") == "kn")
    check("Second language = en", rows.get("DEMO_second_clinic", {}).get("default_language") == "en")
    _d_did = rows.get("DEMO_deepti_dental", {}).get("did_number", "")
    _s_did = rows.get("DEMO_second_clinic", {}).get("did_number", "")
    check("Deepti DID stored",    _d_did.lstrip("+") == "919999100001",
          detail=f"stored: '{_d_did}'")
    check("Second DID stored",    _s_did.lstrip("+") == "919000001111",
          detail=f"stored: '{_s_did}'")


# ═══════════════════════════════════════════════════════════════════════
# T03 — DID routing (cache cleared → DB fallback)
# ═══════════════════════════════════════════════════════════════════════
def t03_did_routing():
    print("\n── T03: DID → client resolution (DB fallback) ──")
    _cc._did_to_config.clear()
    _cc._id_to_config.clear()
    _cc._cached_configs.clear()

    cfg_deepti = get_config_by_did("+919999100001")
    cfg_second = get_config_by_did("+919000001111")
    cfg_miss   = get_config_by_did("+919999999999")

    check("Deepti DID resolves", cfg_deepti is not None,
          detail=f"client_id={cfg_deepti.get('client_id') if cfg_deepti else None}")
    check("Deepti DID → correct client_id",
          (cfg_deepti or {}).get("client_id") == "DEMO_deepti_dental")
    check("Second DID resolves", cfg_second is not None,
          detail=f"client_id={cfg_second.get('client_id') if cfg_second else None}")
    check("Second DID → correct client_id",
          (cfg_second or {}).get("client_id") == "DEMO_second_clinic")
    check("Unknown DID returns None", cfg_miss is None)
    check("Deepti DID cached after DB lookup",
          _cc._did_to_config.get("+919999100001") is not None)


# ═══════════════════════════════════════════════════════════════════════
# T04 — Welcome text per client language
# ═══════════════════════════════════════════════════════════════════════
def t04_welcome_language():
    print("\n── T04: Welcome greeting per client language ──")

    def _welcome_for(cfg: dict) -> str:
        lang = cfg.get("default_language", "en")
        name = cfg.get("clinic_name", "the clinic")
        if lang == "kn":
            return f"ನಮಸ್ಕಾರ, {name} ಗೆ ಸ್ವಾಗತ. ನಾನು ನಿಮಗೆ ಹೇಗೆ ಸಹಾಯ ಮಾಡಲಿ?"
        return f"Hello, welcome to {name}. How may I assist you?"

    cfg_deepti = get_config_by_did("+919999100001")
    cfg_second = get_config_by_did("+919000001111")

    w_deepti = _welcome_for(cfg_deepti)
    w_second = _welcome_for(cfg_second)

    check("Deepti welcome is in Kannada", "ನಮಸ್ಕಾರ" in w_deepti,
          detail=w_deepti)
    check("Deepti welcome contains clinic name",
          "Doctor Deepti" in w_deepti, detail=w_deepti)
    check("Second welcome is in English", w_second.startswith("Hello"),
          detail=w_second)
    check("Second welcome contains clinic name",
          "Second Demo Clinic" in w_second, detail=w_second)
    check("Welcomes are different", w_deepti != w_second)


# ═══════════════════════════════════════════════════════════════════════
# T05 — booking isolation: agent_appointments
# ═══════════════════════════════════════════════════════════════════════
def t05_booking_isolation():
    print("\n── T05: Booking isolation — agent_appointments ──")

    id_deepti = f"DEMO_{uuid.uuid4().hex[:10]}"
    id_second = f"DEMO_{uuid.uuid4().hex[:10]}"

    base = {
        "patient_name":  "Demo Patient",
        "patient_phone": "+919876540000",
        "start_time":    SLOT_ISO,
        "end_time":      SLOT_ISO,
        "appointment_type": "consultation",
        "status":        "scheduled",
        "doctor_name":   "Demo Doctor",
        "reason":        "checkup",
        "booked_via":    "test",
        "agent_notes":   "isolation test",
    }

    ok_d = save_agent_appointment(
        {**base, "id": id_deepti}, session_id="DEMO_sess_d", client_id="DEMO_deepti_dental"
    )
    ok_s = save_agent_appointment(
        {**base, "id": id_second}, session_id="DEMO_sess_s", client_id="DEMO_second_clinic"
    )

    check("Deepti booking saved", ok_d)
    check("Second booking saved", ok_s)

    with get_conn() as cur:
        cur.execute(
            "SELECT id, client_id FROM agent_appointments WHERE id IN (%s, %s)",
            (id_deepti, id_second),
        )
        rows = {r["id"]: dict(r) for r in cur.fetchall()}

    check("Deepti row has correct client_id",
          rows.get(id_deepti, {}).get("client_id") == "DEMO_deepti_dental",
          detail=f"got {rows.get(id_deepti, {}).get('client_id')}")
    check("Second row has correct client_id",
          rows.get(id_second, {}).get("client_id") == "DEMO_second_clinic",
          detail=f"got {rows.get(id_second, {}).get('client_id')}")
    check("No row mixing (both present)", len(rows) == 2)

    # Cross-query: querying by client_id only returns own data
    with get_conn() as cur:
        cur.execute(
            "SELECT id FROM agent_appointments WHERE client_id = %s AND id IN (%s, %s)",
            ("DEMO_deepti_dental", id_deepti, id_second),
        )
        deepti_only = [r["id"] for r in cur.fetchall()]

    check("Client A query only returns Client A rows",
          deepti_only == [id_deepti],
          detail=f"got {deepti_only}")


# ═══════════════════════════════════════════════════════════════════════
# T06 — appointments table client_id
# ═══════════════════════════════════════════════════════════════════════
def t06_appointments_table():
    print("\n── T06: Primary appointments table — client_id ──")
    id_d = f"DEMO_{uuid.uuid4().hex[:10]}"
    id_s = f"DEMO_{uuid.uuid4().hex[:10]}"
    now  = datetime.now().isoformat()
    base = dict(
        session_id="DEMO_sess", connection_id="demo",
        patient_name="Demo Patient", patient_phone="+919876540000",
        start_time=SLOT_ISO, end_time=SLOT_ISO,
        appointment_type="consultation", status="confirmed",
        doctor_name="Demo Dr", reason="checkup",
        booked_via="test", agent_notes="", created_at=now, updated_at=now,
    )
    insert_appointment({**base, "id": id_d, "client_id": "DEMO_deepti_dental"})
    insert_appointment({**base, "id": id_s, "client_id": "DEMO_second_clinic"})

    with get_conn() as cur:
        cur.execute(
            "SELECT id, client_id FROM appointments WHERE id IN (%s, %s)",
            (id_d, id_s),
        )
        rows = {r["id"]: dict(r) for r in cur.fetchall()}

    check("Deepti appt has client_id in appointments",
          rows.get(id_d, {}).get("client_id") == "DEMO_deepti_dental")
    check("Second appt has client_id in appointments",
          rows.get(id_s, {}).get("client_id") == "DEMO_second_clinic")


# ═══════════════════════════════════════════════════════════════════════
# T07 — build_scheduling_payload includes client_id
# ═══════════════════════════════════════════════════════════════════════
def t07_payload_client_id():
    print("\n── T07: build_scheduling_payload — client_id in output ──")
    state = {
        "name": "Demo Patient", "age": "30", "reason": "checkup",
        "date": SLOT_DATE_STR, "time": "11:00 AM",
        "doctor": "Demo Dr",
        "client_id": "DEMO_deepti_dental",
        "connection_id": "deepti_dental",
        "call_sid": "DEMO_sess_payload",
    }
    payload = build_scheduling_payload(
        event_type="appointment_create",
        state=state,
        phone="+919876540000",
        client_id="DEMO_deepti_dental",
    )
    check("Payload contains client_id",
          payload.get("client_id") == "DEMO_deepti_dental",
          detail=f"got {payload.get('client_id')}")
    check("Payload contains connection_id",
          bool(payload.get("connection_id")))
    check("Payload contains start_time",
          bool(payload.get("start_time")))


# ═══════════════════════════════════════════════════════════════════════
# T08 — call_logs tagged with client_id + DID
# ═══════════════════════════════════════════════════════════════════════
def t08_call_logs():
    print("\n── T08: call_logs — client_id + DID logged ──")
    sess_d = f"DEMO_call_{uuid.uuid4().hex[:8]}"
    sess_s = f"DEMO_call_{uuid.uuid4().hex[:8]}"

    log_call_start(sess_d, "DEMO_deepti_dental",
                   did_number="+919999100001", caller_phone="+919876540001", language="kn")
    log_call_start(sess_s, "DEMO_second_clinic",
                   did_number="+919000001111", caller_phone="+919876540002", language="en")

    with get_conn() as cur:
        cur.execute(
            "SELECT session_id, client_id, did_number, language FROM call_logs "
            "WHERE session_id IN (%s, %s)",
            (sess_d, sess_s),
        )
        rows = {r["session_id"]: dict(r) for r in cur.fetchall()}

    check("Deepti call log saved", sess_d in rows)
    check("Second call log saved", sess_s in rows)
    check("Deepti call has correct client_id",
          rows.get(sess_d, {}).get("client_id") == "DEMO_deepti_dental")
    check("Second call has correct client_id",
          rows.get(sess_s, {}).get("client_id") == "DEMO_second_clinic")
    check("Deepti call DID correct",
          rows.get(sess_d, {}).get("did_number") == "+919999100001")
    check("Deepti call language = kn",
          rows.get(sess_d, {}).get("language") == "kn")
    check("Second call language = en",
          rows.get(sess_s, {}).get("language") == "en")


# ═══════════════════════════════════════════════════════════════════════
# T09 — get_default_config fallback
# ═══════════════════════════════════════════════════════════════════════
def t09_default_config():
    print("\n── T09: get_default_config never returns None ──")
    cfg = get_default_config()
    check("get_default_config returns a dict", isinstance(cfg, dict))
    check("Default config has client_id", bool(cfg.get("client_id")))
    check("Default config has clinic_name", bool(cfg.get("clinic_name")))


# ═══════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    print("=" * 60)
    print("  Multi-Client Isolation Test")
    print("=" * 60)

    try:
        t01_schema()
        t02_client_upsert()
        t03_did_routing()
        t04_welcome_language()
        t05_booking_isolation()
        t06_appointments_table()
        t07_payload_client_id()
        t08_call_logs()
        t09_default_config()
    finally:
        _cleanup()

    # Summary
    passed = sum(1 for _, ok in _results if ok)
    failed = sum(1 for _, ok in _results if not ok)
    print("=" * 60)
    print(f"  RESULT: {passed} passed, {failed} failed out of {len(_results)}")
    print("=" * 60)
    if failed:
        print("\nFailed tests:")
        for label, ok in _results:
            if not ok:
                print(f"  ❌  {label}")
        sys.exit(1)
    else:
        print("\n  ✅ All isolation checks passed.\n")
