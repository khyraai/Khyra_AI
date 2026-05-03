"""
test_multi_client.py — Multi-client isolation smoke tests.

Run from inside the container (or any env with DATABASE_URL set):
    python src/test_multi_client.py

Two dummy clients (TEST_client_alpha / TEST_client_beta) are inserted,
exercised, then DELETED automatically — win or lose.

Tests
-----
T01  Client upsert + DB persistence (including hours columns)
T02  DID → client config resolution (cache miss → DB fallback)
T03  save_agent_appointment stores correct client_id per client
T04  check_availability — same slot BOOKED for Alpha, AVAILABLE for Beta
T05  verify_appointment_for_cancellation — client isolation
T06  _is_valid_clinic_slot — per-client hours & closed-day rules
T07  build_scheduling_payload — client_id present in output dict
T08  Agent-3 prompts use client-specific clinic/doctor names
T09  agent1 prompt — no hardcoded client data
T10  services column — persisted and nullable
T11  Language switch detection — keyword-based phrases
T12  Welcome language follows client default_language
T13  Security guardrails present in all 5 agent prompts
T14  Agent2 prompt quality — required fields + client isolation
"""

import os
import sys
import uuid

# Ensure src/ is on the path when run from repo root
sys.path.insert(0, os.path.dirname(__file__))

from datetime import date, datetime, timedelta

import pytz

from database import (
    check_availability,
    get_conn,
    init_db,
    save_agent_appointment,
    upsert_client,
    verify_appointment_for_cancellation,
)
import client_config as _cc
from client_config import get_config_by_did
from utils import build_scheduling_payload
from agent3_kn import build_agent3_kn_prompt
from agent3_en import build_agent3_en_prompt

# ---------------------------------------------------------------------------
# Fixtures — two dummy clients
# ---------------------------------------------------------------------------
_IST = pytz.timezone("Asia/Kolkata")

CLIENT_ALPHA = {
    "client_id":                "TEST_client_alpha",
    "did_number":               "+919100000001",
    "clinic_name":              "Alpha Dental Centre",
    "doctor_name":              "Dr. Alpha",
    "doctor_qualifications":    "BDS",
    "address":                  "1 Alpha Street, Bangalore",
    "timings":                  "Mon-Sat 9 AM-1 PM and 5 PM-8 PM",
    "doctor_mobile":            "+91 9000000001",
    "consultation_fee_min":     150,
    "consultation_fee_max":     250,
    "default_language":         "en",
    "emergency_transfer_number": "+910000000001",
    "connection_id":            "cal_alpha",
    # Custom hours: morning starts 09:00, evening 17:00-20:00, Saturday open
    "morning_open":             "09:00",
    "morning_close":            "13:00",
    "evening_open":             "17:00",
    "evening_close":            "20:00",
    "closed_weekdays":          "6",        # Sunday only
}

CLIENT_BETA = {
    "client_id":                "TEST_client_beta",
    "did_number":               "+919100000002",
    "clinic_name":              "Beta Wellness Clinic",
    "doctor_name":              "Dr. Beta",
    "doctor_qualifications":    "MBBS",
    "address":                  "2 Beta Road, Bangalore",
    "timings":                  "Mon-Fri 10 AM-1 PM and 4 PM-7 PM",
    "doctor_mobile":            "+91 9000000002",
    "consultation_fee_min":     300,
    "consultation_fee_max":     500,
    "default_language":         "en",
    "emergency_transfer_number": "+910000000002",
    "connection_id":            "cal_beta",
    # Standard hours, but closed Sat + Sun
    "morning_open":             "10:00",
    "morning_close":            "13:00",
    "evening_open":             "16:00",
    "evening_close":            "19:00",
    "closed_weekdays":          "5,6",      # Saturday + Sunday
}

# ---------------------------------------------------------------------------
# Shared test slot — next Friday 11:00 AM IST
# ---------------------------------------------------------------------------
def _next_weekday(wd: int) -> date:
    today = date.today()
    days_ahead = wd - today.weekday()
    if days_ahead <= 0:
        days_ahead += 7
    return today + timedelta(days=days_ahead)

FRIDAY_DATE   = _next_weekday(4)
SATURDAY_DATE = _next_weekday(5)
SUNDAY_DATE   = _next_weekday(6)

SLOT_DATE = FRIDAY_DATE.strftime("%d %B %Y")
SLOT_TIME = "11:00 AM"
SLOT_ISO  = _IST.localize(
    datetime.combine(FRIDAY_DATE, datetime.strptime(SLOT_TIME, "%I:%M %p").time())
).isoformat()

PATIENT_NAME  = "TEST_Patient_Sharma"
PATIENT_PHONE = "+919876540001"

# ---------------------------------------------------------------------------
# Result tracking
# ---------------------------------------------------------------------------
_results: list[tuple[str, bool]] = []


def check(label: str, condition: bool, detail: str = "") -> bool:
    status = "\u2705 PASS" if condition else "\u274c FAIL"
    print(f"  {status}  {label}")
    if detail:
        print(f"         {detail}")
    _results.append((label, condition))
    return condition


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _insert_appt(table: str, client_id: str, appt_id: str = None) -> str:
    """Insert a minimal test appointment row directly with client_id."""
    appt_id = appt_id or f"TEST_{uuid.uuid4().hex[:12]}"
    now = datetime.now().isoformat()
    with get_conn() as cur:
        if table == "appointments":
            cur.execute("""
                INSERT INTO appointments
                    (id, session_id, connection_id, patient_name, patient_phone,
                     start_time, end_time, appointment_type, status, doctor_name,
                     reason, booked_via, agent_notes, client_id, created_at, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(id) DO NOTHING
            """, (
                appt_id, "TEST_sess", client_id,
                PATIENT_NAME, PATIENT_PHONE,
                SLOT_ISO, SLOT_ISO,
                "consultation", "scheduled", "Dr. Test",
                "toothache", "test", "test",
                client_id, now, now,
            ))
        else:  # agent_appointments
            cur.execute("""
                INSERT INTO agent_appointments
                    (id, session_id, client_id, patient_name, patient_phone,
                     start_time, end_time, appointment_type, status, doctor_name,
                     reason, booked_via, agent_notes, created_at, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(id) DO NOTHING
            """, (
                appt_id, "TEST_sess", client_id,
                PATIENT_NAME, PATIENT_PHONE,
                SLOT_ISO, SLOT_ISO,
                "consultation", "scheduled", "Dr. Test",
                "toothache", "test", "test",
                now, now,
            ))
    return appt_id


def _delete_test_data():
    with get_conn() as cur:
        cur.execute("DELETE FROM appointments      WHERE session_id = 'TEST_sess' OR id LIKE 'TEST_%'")
        cur.execute("DELETE FROM agent_appointments WHERE session_id = 'TEST_sess' OR id LIKE 'TEST_%'")
        cur.execute("DELETE FROM clients            WHERE client_id LIKE 'TEST_%'")
    print("\n\U0001f9f9  Cleanup: all TEST_ rows deleted.\n")


# ---------------------------------------------------------------------------
# Inline _is_valid_clinic_slot so we don't trigger main.py's heavy imports
# ---------------------------------------------------------------------------
def _is_valid_clinic_slot(date_str: str, time_str: str, client_cfg: dict = None):
    if not date_str or not time_str:
        return True, ""
    date_obj = None
    for fmt in ("%Y-%m-%d", "%d %B %Y", "%d %b %Y", "%B %d %Y"):
        try:
            date_obj = datetime.strptime(str(date_str).strip().replace(",", ""), fmt)
            break
        except ValueError:
            continue
    if date_obj is None:
        return True, ""
    time_obj = None
    for fmt in ("%H:%M", "%I:%M %p", "%I %p", "%H"):
        try:
            time_obj = datetime.strptime(str(time_str).strip(), fmt)
            break
        except ValueError:
            continue
    if time_obj is None:
        return True, ""
    cfg = client_cfg or {}
    try:
        closed_days = {
            int(d.strip())
            for d in str(cfg.get("closed_weekdays", "6")).split(",")
            if d.strip()
        }
    except Exception:
        closed_days = {6}
    if date_obj.weekday() in closed_days:
        return False, f"The centre is closed on {date_obj.strftime('%A')}s."

    def _hm(hm, default):
        try:
            h, m = str(hm).split(":")
            return int(h) * 60 + int(m)
        except Exception:
            return default

    m_open  = _hm(cfg.get("morning_open",  "10:00"), 600)
    m_close = _hm(cfg.get("morning_close", "13:00"), 780)
    e_open  = _hm(cfg.get("evening_open",  "16:00"), 960)
    e_close = _hm(cfg.get("evening_close", "19:00"), 1140)
    minutes = time_obj.hour * 60 + time_obj.minute
    if m_open <= minutes < m_close or e_open <= minutes < e_close:
        return True, ""
    return False, "Outside clinic hours."


# ===========================================================================
# T01 — Client upsert & DB persistence
# ===========================================================================
def t01_client_upsert():
    print("\n\u2500\u2500 T01: Client upsert & DB persistence \u2500\u2500")
    upsert_client(CLIENT_ALPHA)
    upsert_client(CLIENT_BETA)

    with get_conn() as cur:
        cur.execute(
            "SELECT * FROM clients WHERE client_id IN (%s, %s)",
            ("TEST_client_alpha", "TEST_client_beta"),
        )
        rows = {r["client_id"]: dict(r) for r in cur.fetchall()}

    check("Alpha row inserted",           "TEST_client_alpha" in rows)
    check("Beta row inserted",            "TEST_client_beta"  in rows)
    check("Alpha clinic_name correct",    rows.get("TEST_client_alpha", {}).get("clinic_name") == "Alpha Dental Centre")
    check("Beta clinic_name correct",     rows.get("TEST_client_beta",  {}).get("clinic_name") == "Beta Wellness Clinic")
    check("Alpha morning_open stored",    rows.get("TEST_client_alpha", {}).get("morning_open")  == "09:00")
    check("Alpha evening_open stored",    rows.get("TEST_client_alpha", {}).get("evening_open")  == "17:00")
    check("Beta closed_weekdays stored",  rows.get("TEST_client_beta",  {}).get("closed_weekdays") == "5,6")


# ===========================================================================
# T02 — DID → client config resolution (cache-miss → DB fallback)
# ===========================================================================
def t02_did_resolution():
    print("\n\u2500\u2500 T02: DID \u2192 client config resolution \u2500\u2500")

    # Force cache miss so every lookup hits the DB
    _cc._did_to_config.clear()
    _cc._id_to_config.clear()
    _cc._cached_configs.clear()

    cfg_alpha = get_config_by_did("+919100000001")
    cfg_beta  = get_config_by_did("+919100000002")
    cfg_miss  = get_config_by_did("+919999999999")

    check("Alpha DID resolves to TEST_client_alpha",
          cfg_alpha is not None and cfg_alpha.get("client_id") == "TEST_client_alpha",
          detail=f"got client_id={cfg_alpha.get('client_id') if cfg_alpha else None}")
    check("Beta DID resolves to TEST_client_beta",
          cfg_beta is not None and cfg_beta.get("client_id") == "TEST_client_beta",
          detail=f"got client_id={cfg_beta.get('client_id') if cfg_beta else None}")
    check("Unknown DID returns None",
          cfg_miss is None,
          detail=f"got {cfg_miss}")
    check("Alpha config cached after DB lookup",
          _cc._did_to_config.get("+919100000001") is not None)
    check("Alpha morning_open persisted through DB round-trip",
          (cfg_alpha or {}).get("morning_open") == "09:00",
          detail=f"got morning_open={( cfg_alpha or {}).get('morning_open')}")


# ===========================================================================
# T03 — save_agent_appointment stores correct client_id
# ===========================================================================
def t03_appointment_save():
    print("\n\u2500\u2500 T03: save_agent_appointment stores correct client_id \u2500\u2500")
    id_alpha = f"TEST_{uuid.uuid4().hex[:10]}"
    id_beta  = f"TEST_{uuid.uuid4().hex[:10]}"

    base = {
        "patient_name":     PATIENT_NAME,
        "patient_phone":    PATIENT_PHONE,
        "start_time":       SLOT_ISO,
        "end_time":         SLOT_ISO,
        "appointment_type": "consultation",
        "status":           "scheduled",
        "doctor_name":      "Dr. Test",
        "reason":           "toothache",
        "booked_via":       "test",
        "agent_notes":      "test",
    }

    ok_a = save_agent_appointment(
        {**base, "id": id_alpha, "client_id": "TEST_client_alpha"},
        session_id="TEST_sess",
        client_id="TEST_client_alpha",
    )
    ok_b = save_agent_appointment(
        {**base, "id": id_beta, "client_id": "TEST_client_beta"},
        session_id="TEST_sess",
        client_id="TEST_client_beta",
    )

    check("Alpha appointment saved (return True)", ok_a)
    check("Beta appointment saved (return True)",  ok_b)

    with get_conn() as cur:
        cur.execute(
            "SELECT id, client_id FROM agent_appointments WHERE id IN (%s, %s)",
            (id_alpha, id_beta),
        )
        rows = {r["id"]: dict(r) for r in cur.fetchall()}

    check("Alpha row has client_id=TEST_client_alpha",
          rows.get(id_alpha, {}).get("client_id") == "TEST_client_alpha",
          detail=f"got {rows.get(id_alpha, {}).get('client_id')}")
    check("Beta row has client_id=TEST_client_beta",
          rows.get(id_beta, {}).get("client_id") == "TEST_client_beta",
          detail=f"got {rows.get(id_beta, {}).get('client_id')}")
    check("Both rows present (no mixing)", len(rows) == 2)


# ===========================================================================
# T04 — check_availability: same slot BOOKED for Alpha, AVAILABLE for Beta
# ===========================================================================
def t04_availability_isolation():
    print("\n\u2500\u2500 T04: check_availability \u2014 client isolation \u2500\u2500")

    # Book the slot for Alpha only (in primary appointments table)
    _insert_appt("appointments", "TEST_client_alpha")

    avail_alpha = check_availability(SLOT_DATE, SLOT_TIME, client_id="TEST_client_alpha")
    avail_beta  = check_availability(SLOT_DATE, SLOT_TIME, client_id="TEST_client_beta")
    avail_global = check_availability(SLOT_DATE, SLOT_TIME)  # no filter

    check("Slot BOOKED for Alpha (correct client)",
          avail_alpha["available"] is False,
          detail=f"available={avail_alpha['available']}")
    check("Slot AVAILABLE for Beta (different client, same time)",
          avail_beta["available"] is True,
          detail=f"available={avail_beta['available']}")
    check("Global query (no client_id) sees it as BOOKED",
          avail_global["available"] is False,
          detail=f"available={avail_global['available']}")


# T05-specific patient — isolated from T03 to avoid cross-slot contamination
_T05_PATIENT_NAME  = "TEST_T05_Unique_Ravi"
_T05_PATIENT_PHONE = "+919800000099"


def _insert_appt_t05(table: str, client_id: str) -> str:
    """Same as _insert_appt but with T05-specific patient details."""
    appt_id = f"TEST_{uuid.uuid4().hex[:12]}"
    now = datetime.now().isoformat()
    with get_conn() as cur:
        if table == "appointments":
            cur.execute("""
                INSERT INTO appointments
                    (id, session_id, connection_id, patient_name, patient_phone,
                     start_time, end_time, appointment_type, status, doctor_name,
                     reason, booked_via, agent_notes, client_id, created_at, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(id) DO NOTHING
            """, (
                appt_id, "TEST_sess", client_id,
                _T05_PATIENT_NAME, _T05_PATIENT_PHONE,
                SLOT_ISO, SLOT_ISO,
                "consultation", "scheduled", "Dr. Test",
                "toothache", "test", "test",
                client_id, now, now,
            ))
        else:
            cur.execute("""
                INSERT INTO agent_appointments
                    (id, session_id, client_id, patient_name, patient_phone,
                     start_time, end_time, appointment_type, status, doctor_name,
                     reason, booked_via, agent_notes, created_at, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(id) DO NOTHING
            """, (
                appt_id, "TEST_sess", client_id,
                _T05_PATIENT_NAME, _T05_PATIENT_PHONE,
                SLOT_ISO, SLOT_ISO,
                "consultation", "scheduled", "Dr. Test",
                "toothache", "test", "test",
                now, now,
            ))
    return appt_id


# ===========================================================================
# T05 — verify_appointment_for_cancellation: client isolation
# ===========================================================================
def t05_verify_isolation():
    print("\n\u2500\u2500 T05: verify_appointment_for_cancellation \u2014 client isolation \u2500\u2500")

    # T05-specific patient belongs ONLY to Alpha — Beta has zero rows for this patient
    _insert_appt_t05("appointments",      "TEST_client_alpha")
    _insert_appt_t05("agent_appointments", "TEST_client_alpha")

    res_alpha = verify_appointment_for_cancellation(
        _T05_PATIENT_NAME, _T05_PATIENT_PHONE, SLOT_DATE, SLOT_TIME,
        client_id="TEST_client_alpha",
    )
    res_beta = verify_appointment_for_cancellation(
        _T05_PATIENT_NAME, _T05_PATIENT_PHONE, SLOT_DATE, SLOT_TIME,
        client_id="TEST_client_beta",
    )
    res_no_filter = verify_appointment_for_cancellation(
        _T05_PATIENT_NAME, _T05_PATIENT_PHONE, SLOT_DATE, SLOT_TIME,
    )

    check("Alpha finds its OWN appointment",
          res_alpha["exists"] is True,
          detail=f"source={res_alpha.get('source')}")
    check("Beta CANNOT find Alpha's appointment (client isolation)",
          res_beta["exists"] is False,
          detail=f"msg={res_beta.get('message')}")
    check("No-filter query finds the appointment (global view)",
          res_no_filter["exists"] is True,
          detail=f"source={res_no_filter.get('source')}")


# ===========================================================================
# T06 — _is_valid_clinic_slot: per-client hours & closed-day rules
# ===========================================================================
def t06_clinic_hours():
    print("\n\u2500\u2500 T06: _is_valid_clinic_slot \u2014 per-client hours \u2500\u2500")
    fri  = FRIDAY_DATE.strftime("%d %B %Y")
    sat  = SATURDAY_DATE.strftime("%d %B %Y")
    sun  = SUNDAY_DATE.strftime("%d %B %Y")

    # 9:30 AM — valid for Alpha (09:00 open), invalid for default (10:00 open)
    ok_alpha_930,   _ = _is_valid_clinic_slot(fri, "9:30 AM", CLIENT_ALPHA)
    ok_default_930, _ = _is_valid_clinic_slot(fri, "9:30 AM", {})
    check("Alpha 9:30 AM VALID (morning_open=09:00)",          ok_alpha_930   is True)
    check("Default 9:30 AM INVALID (morning_open=10:00)",      ok_default_930 is False)

    # Saturday — open for Alpha (closed_weekdays=6), closed for Beta (5,6)
    ok_alpha_sat, _    = _is_valid_clinic_slot(sat, "11:00 AM", CLIENT_ALPHA)
    ok_beta_sat, msg_b = _is_valid_clinic_slot(sat, "11:00 AM", CLIENT_BETA)
    check("Alpha Saturday OPEN (closed_weekdays=6 only)",      ok_alpha_sat is True)
    check("Beta Saturday CLOSED (closed_weekdays=5,6)",        ok_beta_sat  is False,
          detail=msg_b)

    # Sunday — closed for both
    ok_alpha_sun, _ = _is_valid_clinic_slot(sun, "11:00 AM", CLIENT_ALPHA)
    ok_beta_sun,  _ = _is_valid_clinic_slot(sun, "11:00 AM", CLIENT_BETA)
    check("Alpha Sunday CLOSED",  ok_alpha_sun is False)
    check("Beta Sunday CLOSED",   ok_beta_sun  is False)

    # Evening: Alpha 5:30 PM valid (17:00 open); Beta 5:30 PM valid (16:00 open)
    ok_alpha_eve, _ = _is_valid_clinic_slot(fri, "5:30 PM", CLIENT_ALPHA)
    ok_beta_eve,  _ = _is_valid_clinic_slot(fri, "5:30 PM", CLIENT_BETA)
    check("Alpha 5:30 PM VALID (evening_open=17:00)",          ok_alpha_eve is True)
    check("Beta 5:30 PM VALID (evening_open=16:00)",           ok_beta_eve  is True)

    # Lunch gap 2:00 PM — outside hours for both
    ok_alpha_lunch, _ = _is_valid_clinic_slot(fri, "2:00 PM", CLIENT_ALPHA)
    ok_beta_lunch,  _ = _is_valid_clinic_slot(fri, "2:00 PM", CLIENT_BETA)
    check("Alpha 2:00 PM INVALID (lunch gap)",                 ok_alpha_lunch is False)
    check("Beta 2:00 PM INVALID (lunch gap)",                  ok_beta_lunch  is False)

    # Alpha after-hours 8:30 PM
    ok_alpha_late, _ = _is_valid_clinic_slot(fri, "8:30 PM", CLIENT_ALPHA)
    check("Alpha 8:30 PM INVALID (after evening_close=20:00)", ok_alpha_late is False)


# ===========================================================================
# T07 — build_scheduling_payload includes client_id
# ===========================================================================
def t07_scheduling_payload():
    print("\n\u2500\u2500 T07: build_scheduling_payload \u2014 client_id in output \u2500\u2500")

    import unittest.mock as mock
    with mock.patch("utils.save_agent_appointment", return_value=True) as mock_save:
        payload = build_scheduling_payload(
            event_type="appointment_create",
            state={
                "name":     "Test Patient",
                "date":     SLOT_DATE,
                "time":     SLOT_TIME,
                "doctor":   "Dr. Alpha",
                "reason":   "cavity",
                "age":      30,
                "call_sid": "TEST_sess",
            },
            phone=PATIENT_PHONE,
            language="en",
            client_id="TEST_client_alpha",
        )

    check("Payload client_id = TEST_client_alpha",
          payload.get("client_id") == "TEST_client_alpha",
          detail=f"got {payload.get('client_id')}")
    check("Payload patient_name correct",
          payload.get("patient_name") == "Test Patient")
    check("Payload start_time is non-empty ISO string",
          bool(payload.get("start_time")) and "T" in str(payload.get("start_time", "")))
    check("Payload status = 'scheduled'",
          payload.get("status") == "scheduled")
    check("save_agent_appointment called once (via mock)",
          mock_save.call_count == 1)
    call_kwargs = mock_save.call_args
    check("save_agent_appointment received client_id=TEST_client_alpha",
          call_kwargs is not None and "TEST_client_alpha" in str(call_kwargs),
          detail=str(call_kwargs))


# ===========================================================================
# T08 — Agent-3 prompts use client-specific clinic/doctor names
# ===========================================================================
def t08_prompt_isolation():
    print("\n\u2500\u2500 T08: Agent-3 prompts use client-specific names \u2500\u2500")

    prompt_kn = build_agent3_kn_prompt(
        state={},
        context={"client_id": "TEST_client_alpha"},
        config=CLIENT_ALPHA,
    )
    prompt_en = build_agent3_en_prompt(
        state={},
        context={"client_id": "TEST_client_beta"},
        config=CLIENT_BETA,
    )

    check("KN prompt contains Alpha clinic name",
          "Alpha Dental Centre" in prompt_kn)
    check("KN prompt contains Alpha doctor name",
          "Dr. Alpha" in prompt_kn)
    check("EN prompt contains Beta clinic name",
          "Beta Wellness Clinic" in prompt_en)
    check("EN prompt contains Beta doctor name",
          "Dr. Beta" in prompt_en)
    check("KN prompt does NOT contain Beta's clinic name",
          "Beta Wellness" not in prompt_kn)
    check("EN prompt does NOT contain Alpha's clinic name",
          "Alpha Dental" not in prompt_en)
    check("KN prompt has no hardcoded city 'Bangalore'",
          "Bangalore" not in prompt_kn)
    check("EN prompt has no hardcoded city 'Bangalore'",
          "Bangalore" not in prompt_en)


# ===========================================================================
# T09 — agent1 prompt contains NO hardcoded client-specific data
# ===========================================================================
def t09_agent1_prompt_clean():
    print("\n\u2500\u2500 T09: agent1 prompt \u2014 no hardcoded client data \u2500\u2500")
    from agent1 import build_agent1_prompt

    prompt_alpha = build_agent1_prompt(config=CLIENT_ALPHA)
    prompt_beta  = build_agent1_prompt(config=CLIENT_BETA)
    prompt_empty = build_agent1_prompt(config={})

    check("Alpha prompt contains Alpha clinic name in greeting",
          "Alpha Dental Centre" in prompt_alpha)
    check("Beta prompt contains Beta clinic name in greeting",
          "Beta Wellness Clinic" in prompt_beta)
    check("Alpha prompt contains Alpha emergency number",
          "+910000000001" in prompt_alpha)
    check("Beta prompt contains Beta emergency number",
          "+910000000002" in prompt_beta)
    check("Alpha prompt has no Bangalore",        "Bangalore" not in prompt_alpha)
    check("Beta prompt has no Bangalore",         "Bangalore" not in prompt_beta)
    check("Empty-config prompt falls back to 'the clinic'", "the clinic" in prompt_empty)
    check("Alpha prompt has no Doctor Deepti",    "Deepti" not in prompt_alpha)
    check("Alpha prompt has no 9187471874",       "9187471874" not in prompt_alpha)
    check("Alpha prompt has no 8660033297",       "8660033297" not in prompt_alpha)


# ===========================================================================
# T10 — services column: persisted and nullable
# ===========================================================================
def t10_services_column():
    print("\n\u2500\u2500 T10: services column \u2500\u2500")
    # Upsert Alpha with services
    cfg = dict(CLIENT_ALPHA)
    cfg["services"] = "Teeth cleaning, Root canal, Braces, Aligners"
    upsert_client(cfg)
    with get_conn() as cur:
        cur.execute("SELECT services FROM clients WHERE client_id = %s", ("TEST_client_alpha",))
        row = cur.fetchone()
    svc = row["services"] if row else None
    check("Alpha services column stored correctly",
          svc == "Teeth cleaning, Root canal, Braces, Aligners",
          detail=f"got {svc}")

    # Beta has no services key — should be NULL
    upsert_client(CLIENT_BETA)  # no 'services' key
    with get_conn() as cur:
        cur.execute("SELECT services FROM clients WHERE client_id = %s", ("TEST_client_beta",))
        row = cur.fetchone()
    svc_beta = row["services"] if row else "no row"
    check("Beta services column is NULL when not provided",
          row is not None and svc_beta is None,
          detail=f"got {svc_beta}")

    # Update Alpha to clear services
    cfg2 = dict(CLIENT_ALPHA)
    cfg2["services"] = None
    upsert_client(cfg2)
    with get_conn() as cur:
        cur.execute("SELECT services FROM clients WHERE client_id = %s", ("TEST_client_alpha",))
        row = cur.fetchone()
    svc_cleared = row["services"] if row else "no row"
    check("Alpha services clears to NULL on update",
          row is not None and svc_cleared is None,
          detail=f"got {svc_cleared}")


# ===========================================================================
# T11 — Language switch detection (keyword-based, no main.py import)
# ===========================================================================
def t11_language_switch_detection():
    print("\n\u2500\u2500 T11: Language switch detection \u2500\u2500")

    # Inline the same logic as _detect_language_switch_request in main.py
    _KN = [
        "speak in kannada", "kannada lo", "kannada madiri", "kannada maat",
        "in kannada", "kannada aagi", "switch to kannada", "change to kannada",
        "talk in kannada", "please speak kannada", "kannada li",
    ]
    _EN = [
        "speak in english", "in english", "switch to english", "change to english",
        "talk in english", "english lo", "english madiri", "please speak english",
        "english maat",
    ]
    def _detect(text):
        t = text.lower()
        if any(s in t for s in _KN): return "kn"
        if any(s in t for s in _EN): return "en"
        return None

    check("'speak in kannada' detects kn",          _detect("please speak in kannada") == "kn")
    check("'kannada madiri maatadi' detects kn",    _detect("kannada madiri maatadi") == "kn")
    check("'kannada lo maatadi' detects kn",        _detect("kannada lo maatadi") == "kn")
    check("'switch to kannada' detects kn",         _detect("can you switch to kannada") == "kn")
    check("'switch to english' detects en",         _detect("switch to english please") == "en")
    check("'english lo' detects en",                _detect("english lo maatadi") == "en")
    check("'in english' detects en",                _detect("can you speak in english") == "en")
    check("Normal booking phrase → None",           _detect("I want to book an appointment") is None)
    check("Kanglish 'appointment beku' → None",     _detect("appointment beku") is None)
    check("Greeting 'hello' → None",                _detect("hello") is None)
    check("'kannada' in name shouldn't trigger",    _detect("my name is Nanda") is None)


# ===========================================================================
# T12 — Welcome language follows client default_language
# ===========================================================================
def t12_welcome_language():
    print("\n\u2500\u2500 T12: Welcome language per client default_language \u2500\u2500")

    # Insert a KN-default client
    kn_cfg = dict(CLIENT_ALPHA)
    kn_cfg["client_id"]       = "TEST_kn_welcome_client"
    kn_cfg["did_number"]      = "+919100000099"
    kn_cfg["default_language"] = "kn"
    upsert_client(kn_cfg)
    _cc._did_to_config.clear()  # flush in-memory cache so DB is re-queried
    cfg = get_config_by_did("+919100000099")

    lang    = (cfg or {}).get("default_language", "en")
    clinic  = (cfg or {}).get("clinic_name", "the clinic")
    welcome = (
        f"Hello, welcome to {clinic}. How may I assist you?"
        if lang == "en"
        else f"\u0ca8\u0cae\u0cb8\u0ccd\u0c95\u0cbe\u0cb0, {clinic} \u0c97\u0cc6 \u0cb8\u0ccd\u0cb5\u0cbe\u0c97\u0ca4. \u0ca8\u0cbe\u0ca8\u0cc1 \u0ca6\u0cbf\u0cb5\u0ccd\u0caf. \u0c8f\u0ca8\u0cc1 \u0cb8\u0cb9\u0cbe\u0caf \u0cac\u0cc7\u0c95\u0cbf\u0ca4\u0ccd\u0ca4\u0cc1?"
    )

    check("KN client default_language stored as 'kn'",
          lang == "kn", detail=f"got {lang}")
    check("KN client welcome text is Kannada (contains ನಮಸ್ಕಾರ)",
          "\u0ca8\u0cae\u0cb8\u0ccd\u0c95\u0cbe\u0cb0" in welcome)
    check("KN client welcome text does NOT contain 'Hello'",
          "Hello" not in welcome)

    # EN-default client (CLIENT_BETA already has default_language='en')
    en_lang = CLIENT_BETA.get("default_language", "en")
    en_welcome = (
        f"Hello, welcome to {CLIENT_BETA['clinic_name']}. How may I assist you?"
        if en_lang == "en"
        else "KANNADA"
    )
    check("EN client welcome text starts with 'Hello'",
          en_welcome.startswith("Hello"))


# ===========================================================================
# T13 — Security guardrails present in all 5 agent prompts
# ===========================================================================
def t13_security_guardrails():
    print("\n\u2500\u2500 T13: Security guardrails in all agent prompts \u2500\u2500")
    from agent1    import build_agent1_prompt
    from agent2_en import build_agent2_en_prompt
    from agent2_kn import build_agent2_kn_prompt
    from agent3_en import build_agent3_en_prompt
    from agent3_kn import build_agent3_kn_prompt

    prompts = [
        ("agent1",    build_agent1_prompt(config=CLIENT_ALPHA)),
        ("agent2_en", build_agent2_en_prompt(config=CLIENT_ALPHA, state={}, agent1_context={})),
        ("agent2_kn", build_agent2_kn_prompt(config=CLIENT_ALPHA, state={}, agent1_context={})),
        ("agent3_en", build_agent3_en_prompt(state={}, context={}, config=CLIENT_ALPHA)),
        ("agent3_kn", build_agent3_kn_prompt(state={}, context={}, config=CLIENT_ALPHA)),
    ]

    for name, prompt in prompts:
        check(f"{name}: has SECURITY & SCOPE CONSTRAINTS section",
              "SECURITY & SCOPE CONSTRAINTS" in prompt)
        check(f"{name}: blocks jailbreak attempts",
              "jailbreak" in prompt)
        check(f"{name}: blocks roleplay",
              "roleplay" in prompt)
        check(f"{name}: blocks 'who built you' questions",
              "who built you" in prompt)


# ===========================================================================
# T14 — Agent2 prompts: required fields and no hardcoded city
# ===========================================================================
def t14_agent2_prompt_quality():
    print("\n\u2500\u2500 T14: Agent2 prompt quality checks \u2500\u2500")
    from agent2_en import build_agent2_en_prompt
    from agent2_kn import build_agent2_kn_prompt

    en_alpha = build_agent2_en_prompt(config=CLIENT_ALPHA, state={}, agent1_context={})
    en_beta  = build_agent2_en_prompt(config=CLIENT_BETA,  state={}, agent1_context={})
    kn_alpha = build_agent2_kn_prompt(config=CLIENT_ALPHA, state={}, agent1_context={})

    # Client isolation in agent2
    check("agent2_en Alpha prompt has Alpha clinic name",  "Alpha Dental Centre" in en_alpha)
    check("agent2_en Beta prompt has Beta clinic name",    "Beta Wellness Clinic" in en_beta)
    check("agent2_en Alpha prompt has NO Beta clinic name","Beta Wellness" not in en_alpha)
    check("agent2_kn Alpha prompt has Alpha clinic name",  "Alpha Dental Centre" in kn_alpha)

    # Required-fields guardrail present
    for label, prompt in [("agent2_en Alpha", en_alpha), ("agent2_kn Alpha", kn_alpha)]:
        check(f"{label}: NEVER confirm without all required fields",
              "ALL required fields" in prompt or "NEVER confirm an appointment unless ALL" in prompt)
        check(f"{label}: requires name field",   "name" in prompt)
        check(f"{label}: requires age field",    "age"  in prompt)
        check(f"{label}: requires reason field", "reason" in prompt)
        check(f"{label}: requires date field",   "date"  in prompt)
        check(f"{label}: requires time field",   "time"  in prompt)

    # No hardcoded DOCTOR names from other clients in agent2 prompts
    check("agent2_en Alpha has no Beta doctor name", "Dr. Beta" not in en_alpha)
    check("agent2_kn Alpha has no Beta doctor name", "Dr. Beta" not in kn_alpha)


# ===========================================================================
# Main runner
# ===========================================================================
if __name__ == "__main__":
    print("=" * 64)
    print("  Khyra AI — Multi-Client Isolation Test Suite")
    print(f"  Slot under test : {SLOT_DATE} {SLOT_TIME}")
    print(f"  Saturday        : {SATURDAY_DATE.strftime('%d %B %Y')}")
    print(f"  Sunday          : {SUNDAY_DATE.strftime('%d %B %Y')}")
    print("=" * 64)

    try:
        init_db()
        _delete_test_data()  # wipe any leftovers from a previous failed run

        t01_client_upsert()
        t02_did_resolution()
        t03_appointment_save()
        t04_availability_isolation()
        t05_verify_isolation()
        t06_clinic_hours()
        t07_scheduling_payload()
        t08_prompt_isolation()
        t09_agent1_prompt_clean()
        t10_services_column()
        t11_language_switch_detection()
        t12_welcome_language()
        t13_security_guardrails()
        t14_agent2_prompt_quality()

    finally:
        _delete_test_data()

    # ── Summary ──────────────────────────────────────────────────────────
    passed = sum(1 for _, ok in _results if ok)
    failed = sum(1 for _, ok in _results if not ok)
    total  = len(_results)

    print("=" * 64)
    print(f"  Results: {passed}/{total} passed", end="")
    if failed:
        print(f"  |  {failed} FAILED")
        print("\n  Failed checks:")
        for label, ok in _results:
            if not ok:
                print(f"    \u274c {label}")
    else:
        print("  — all green \u2705")
    print("=" * 64)

    sys.exit(0 if failed == 0 else 1)
