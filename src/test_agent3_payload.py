"""
test_agent3_payload.py — Unit + regression tests for Agent-3 n8n payloads.

Covers:
  U01  build_scheduling_payload for appointment_cancel — correct fields
  U02  build_scheduling_payload for appointment_reschedule — correct fields
  U03  Cancel: start_time == previous appointment, no new_date/time required
  U04  Reschedule: start_time == new slot; previous_datetime == old slot
  U05  event_type key is present and correct in both payloads
  U06  status mapped correctly (cancelled / rescheduled)
  U07  client_id carried through in payload
  U08  patient_name and patient_phone carried through
  U09  previous_datetime absent for cancel (same as start_time is fine, field present)
  U10  Regression — appointment_create still works after the Agent-3 block added

  DB01 reschedules table has client_id column
  DB02 reschedules table has session_id column
  DB03 cancellations table has client_id column

Usage (inside container):
    python /app/src/test_agent3_payload.py
"""

import os, sys, json

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql://khyra:khyra_secret@postgres:5432/khyra_db"
)

from database import init_db
init_db()

from utils import build_scheduling_payload
from pg import get_conn

# ═══════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════

_results = []

def check(label: str, ok: bool, detail: str = ""):
    _results.append((label, ok))
    icon = "  ✅ PASS" if ok else "  ❌ FAIL"
    print(f"{icon}  {label}")
    if detail:
        print(f"         {detail}")

# ── Shared fixture states ────────────────────────────────────────────────────

CANCEL_A3_STATE = {
    "name":          "Ravi Kumar",
    "phone":         "+919876543210",
    "previous_date": "2025-06-10",
    "previous_time": "10:30 AM",
    "new_date":      None,
    "new_time":      None,
    "verified":      True,
    "reason":        "feeling better",
    "age":           35,
    "date":          "2025-06-10",   # mapped by main.py for cancel
    "time":          "10:30 AM",
}

RESCHEDULE_A3_STATE = {
    "name":          "Priya Sharma",
    "phone":         "+919123456780",
    "previous_date": "2025-06-10",
    "previous_time": "10:30 AM",
    "new_date":      "2025-06-15",
    "new_time":      "04:00 PM",
    "verified":      True,
    "reason":        "conflict",
    "age":           28,
    "date":          "2025-06-15",   # mapped by main.py for reschedule
    "time":          "04:00 PM",
}

CREATE_STATE = {
    "name":   "Anand Rao",
    "phone":  "+919000001111",
    "date":   "2025-06-20",
    "time":   "11:00 AM",
    "reason": "toothache",
    "age":    45,
}

CLIENT_ID   = "TEST_agent3"
CALLER_PHONE = "+919876543210"
AGENT1_CTX   = {"intent": "cancel_reschedule", "client_id": CLIENT_ID}


# ═══════════════════════════════════════════════════════════════════
# Unit Tests — Payload Builder
# ═══════════════════════════════════════════════════════════════════

print("=" * 65)
print("  Agent-3 Payload Unit + Regression Tests")
print("=" * 65)

# ── U01: Cancel payload has required top-level keys ──────────────────
print("\n── U01–U09: Payload builder — cancel & reschedule ──")

cancel_prev_iso = "2025-06-10 10:30 AM"

cancel_payload = build_scheduling_payload(
    event_type="appointment_cancel",
    state=CANCEL_A3_STATE,
    phone=CALLER_PHONE,
    previous_datetime_iso=cancel_prev_iso,
    confirmation_status="confirmed",
    language="en",
    agent1_context=AGENT1_CTX,
    client_id=CLIENT_ID,
)

required_keys = ["id", "client_id", "event_type", "patient_name", "patient_phone",
                 "start_time", "end_time", "status", "previous_datetime",
                 "appointment_type", "booked_via", "created_at"]

missing = [k for k in required_keys if k not in cancel_payload]
check("U01: Cancel payload has all required keys", not missing,
      detail=f"Missing: {missing}" if missing else "")

# ── U02: Reschedule payload has required keys ─────────────────────────
reschedule_prev_iso = "2025-06-10 10:30 AM"

reschedule_payload = build_scheduling_payload(
    event_type="appointment_reschedule",
    state=RESCHEDULE_A3_STATE,
    phone=CALLER_PHONE,
    previous_datetime_iso=reschedule_prev_iso,
    confirmation_status="confirmed",
    language="en",
    agent1_context=AGENT1_CTX,
    client_id=CLIENT_ID,
)

missing_r = [k for k in required_keys if k not in reschedule_payload]
check("U02: Reschedule payload has all required keys", not missing_r,
      detail=f"Missing: {missing_r}" if missing_r else "")

# ── U03: Cancel start_time = old appointment slot ─────────────────────
check("U03: Cancel start_time contains previous date",
      "2025-06-10" in (cancel_payload.get("start_time") or ""),
      detail=f"start_time={cancel_payload.get('start_time')}")

# ── U04: Reschedule start_time = new slot, previous_datetime = old ───
check("U04a: Reschedule start_time contains new date",
      "2025-06-15" in (reschedule_payload.get("start_time") or ""),
      detail=f"start_time={reschedule_payload.get('start_time')}")

check("U04b: Reschedule previous_datetime contains old date",
      "2025-06-10" in (reschedule_payload.get("previous_datetime") or ""),
      detail=f"previous_datetime={reschedule_payload.get('previous_datetime')}")

check("U04c: Reschedule start_time does NOT contain old date",
      "2025-06-10" not in (reschedule_payload.get("start_time") or ""),
      detail=f"start_time={reschedule_payload.get('start_time')}")

# ── U05: event_type key present and correct ───────────────────────────
check("U05a: Cancel event_type == 'appointment_cancel'",
      cancel_payload.get("event_type") == "appointment_cancel",
      detail=f"Got: {cancel_payload.get('event_type')}")

check("U05b: Reschedule event_type == 'appointment_reschedule'",
      reschedule_payload.get("event_type") == "appointment_reschedule",
      detail=f"Got: {reschedule_payload.get('event_type')}")

# ── U06: status mapped correctly ──────────────────────────────────────
check("U06a: Cancel status == 'cancelled'",
      cancel_payload.get("status") == "cancelled",
      detail=f"Got: {cancel_payload.get('status')}")

check("U06b: Reschedule status == 'rescheduled'",
      reschedule_payload.get("status") == "rescheduled",
      detail=f"Got: {reschedule_payload.get('status')}")

# ── U07: client_id carried through ───────────────────────────────────
check("U07a: Cancel client_id correct",
      cancel_payload.get("client_id") == CLIENT_ID,
      detail=f"Got: {cancel_payload.get('client_id')}")

check("U07b: Reschedule client_id correct",
      reschedule_payload.get("client_id") == CLIENT_ID,
      detail=f"Got: {reschedule_payload.get('client_id')}")

# ── U08: patient_name and patient_phone correct ───────────────────────
check("U08a: Cancel patient_name == 'Ravi Kumar'",
      cancel_payload.get("patient_name") == "Ravi Kumar")

check("U08b: Cancel patient_phone correct",
      CALLER_PHONE in (cancel_payload.get("patient_phone") or ""),
      detail=f"Got: {cancel_payload.get('patient_phone')}")

check("U08c: Reschedule patient_name == 'Priya Sharma'",
      reschedule_payload.get("patient_name") == "Priya Sharma")

# ── U09: previous_datetime present and non-empty in both ─────────────
check("U09a: Cancel previous_datetime is set",
      bool(cancel_payload.get("previous_datetime")),
      detail=f"Got: {cancel_payload.get('previous_datetime')}")

check("U09b: Reschedule previous_datetime is set",
      bool(reschedule_payload.get("previous_datetime")),
      detail=f"Got: {reschedule_payload.get('previous_datetime')}")

# ── U10: Regression — appointment_create still works ─────────────────
print("\n── U10: Regression — appointment_create payload unchanged ──")

create_payload = build_scheduling_payload(
    event_type="appointment_create",
    state=CREATE_STATE,
    phone="+919000001111",
    confirmation_status="confirmed",
    language="en",
    agent1_context={"intent": "appointment"},
    client_id=CLIENT_ID,
)

check("U10a: Create event_type == 'appointment_create'",
      create_payload.get("event_type") == "appointment_create",
      detail=f"Got: {create_payload.get('event_type')}")

check("U10b: Create status == 'scheduled'",
      create_payload.get("status") == "scheduled",
      detail=f"Got: {create_payload.get('status')}")

check("U10c: Create start_time contains booking date",
      "2025-06-20" in (create_payload.get("start_time") or ""),
      detail=f"start_time={create_payload.get('start_time')}")

check("U10d: Create previous_datetime is empty",
      not create_payload.get("previous_datetime"),
      detail=f"Got: {create_payload.get('previous_datetime')}")

check("U10e: Create patient_name == 'Anand Rao'",
      create_payload.get("patient_name") == "Anand Rao")

# ═══════════════════════════════════════════════════════════════════
# DB Schema Tests
# ═══════════════════════════════════════════════════════════════════

print("\n── DB01–DB03: Database schema verification ──")

def _get_columns(table: str) -> list:
    with get_conn() as cur:
        cur.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = %s AND table_schema = 'public'
        """, (table,))
        return [row["column_name"] for row in cur.fetchall()]

reschedule_cols   = _get_columns("reschedules")
cancellation_cols = _get_columns("cancellations")

check("DB01: reschedules has client_id column",
      "client_id" in reschedule_cols,
      detail=f"Columns: {reschedule_cols}")

check("DB02: reschedules has session_id column",
      "session_id" in reschedule_cols,
      detail=f"Columns: {reschedule_cols}")

check("DB03: cancellations has client_id column",
      "client_id" in cancellation_cols,
      detail=f"Columns: {cancellation_cols}")

# ═══════════════════════════════════════════════════════════════════
# Summary
# ═══════════════════════════════════════════════════════════════════

passed = sum(1 for _, ok in _results if ok)
failed = sum(1 for _, ok in _results if not ok)
total  = len(_results)

print()
print("=" * 65)
if failed == 0:
    print(f"  RESULT: {passed}/{total} passed — ✅ all green")
else:
    print(f"  RESULT: {passed}/{total} passed  |  {failed} FAILED")
    print()
    print("  Failed checks:")
    for label, ok in _results:
        if not ok:
            print(f"    ❌ {label}")
print("=" * 65)

sys.exit(0 if failed == 0 else 1)
