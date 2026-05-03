"""
test_did_welcome.py — DID-swap welcome routing test.

Scenario A:
  DID +919100001111  →  Deepti's clinic (in DB)
  DID +919100002222  →  Dummy Test Clinic (in DB)

  Calling +919100001111  →  "Hello, welcome to Doctor Deepti's Dental..."
  Calling +919100002222  →  "Hello, welcome to Dummy Test Clinic..."

Scenario B (swap the DID):
  UPDATE DB: Deepti's DID cleared → ""
  UPDATE DB: Dummy's DID set      → +919100001111

  Calling +919100001111  →  "Hello, welcome to Dummy Test Clinic..."
  Calling +919100002222  →  None (no match expected)

Uses only the DB path (DIDs not in client_config.json) so cache is
fully controlled by this script.
"""

import os, sys

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql://khyra:khyra_secret@postgres:5432/khyra_db"
)

from database import init_db
init_db()

from pg import get_conn
import client_config as _cc

# ── Test constants ────────────────────────────────────────────────────────────

DID_A   = "+919100001111"   # DID we'll move between clients
DID_B   = "+919100002222"   # DID that stays with Dummy (Scenario A only)

DEEPTI  = {
    "client_id":                "TEST_did_deepti",
    "did_number":               DID_A,
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
    "connection_id":            "TEST_did_deepti",
    "morning_open":             "10:00",
    "morning_close":            "13:00",
    "evening_open":             "16:00",
    "evening_close":            "19:00",
    "closed_weekdays":          "6",
}

DUMMY   = {
    "client_id":                "TEST_did_dummy",
    "did_number":               DID_B,
    "clinic_name":              "Dummy Test Clinic",
    "doctor_name":              "Dr. Dummy",
    "doctor_qualifications":    "MBBS",
    "address":                  "1 Test Lane, Testville",
    "timings":                  "Mon-Fri 9 AM-5 PM",
    "doctor_mobile":            "+91 9000000099",
    "consultation_fee_min":     100,
    "consultation_fee_max":     200,
    "default_language":         "en",
    "emergency_transfer_number": "+910000000099",
    "connection_id":            "TEST_did_dummy",
    "morning_open":             "09:00",
    "morning_close":            "13:00",
    "evening_open":             "16:00",
    "evening_close":            "19:00",
    "closed_weekdays":          "6",
}

_results = []

def check(label: str, ok: bool, detail: str = ""):
    _results.append((label, ok))
    icon = "  ✅ PASS" if ok else "  ❌ FAIL"
    print(f"{icon}  {label}")
    if detail:
        print(f"         {detail}")

# ── Helpers ───────────────────────────────────────────────────────────────────

def _upsert(cfg: dict):
    from datetime import datetime
    now = datetime.now().isoformat()
    with get_conn() as cur:
        cur.execute("""
            INSERT INTO clients
                (client_id, did_number, clinic_name, doctor_name, doctor_qualifications,
                 address, timings, doctor_mobile, consultation_fee_min, consultation_fee_max,
                 default_language, emergency_transfer_number, connection_id,
                 morning_open, morning_close, evening_open, evening_close, closed_weekdays,
                 created_at, updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT(client_id) DO UPDATE SET
                did_number=EXCLUDED.did_number,
                clinic_name=EXCLUDED.clinic_name,
                updated_at=EXCLUDED.updated_at
        """, (
            cfg["client_id"], cfg["did_number"], cfg["clinic_name"],
            cfg["doctor_name"], cfg["doctor_qualifications"],
            cfg["address"], cfg["timings"], cfg["doctor_mobile"],
            cfg["consultation_fee_min"], cfg["consultation_fee_max"],
            cfg["default_language"], cfg["emergency_transfer_number"],
            cfg["connection_id"],
            cfg["morning_open"], cfg["morning_close"],
            cfg["evening_open"], cfg["evening_close"],
            cfg["closed_weekdays"],
            now, now,
        ))


def _set_did(client_id: str, new_did: str):
    """Update a client's DID directly in DB."""
    from datetime import datetime
    with get_conn() as cur:
        cur.execute(
            "UPDATE clients SET did_number=%s, updated_at=%s WHERE client_id=%s",
            (new_did, datetime.now().isoformat(), client_id)
        )


def _clear_cache():
    """Flush client_config.py in-memory caches so next lookup hits DB."""
    _cc._cached_configs.clear()
    _cc._did_to_config.clear()
    _cc._id_to_config.clear()


def _welcome_for(did: str) -> str:
    """Simulate what the WebSocket handler sends as welcome text."""
    _clear_cache()
    cfg = _cc.get_config_by_did(did)
    if cfg is None:
        return None
    clinic = cfg.get("clinic_name", "the clinic")
    return f"Hello, welcome to {clinic}. How may I assist you?"


def _cleanup():
    with get_conn() as cur:
        cur.execute(
            "DELETE FROM clients WHERE client_id IN (%s, %s)",
            ("TEST_did_deepti", "TEST_did_dummy")
        )
    print("🧹  Cleanup: TEST_did_* rows deleted.")


# ── Test runner ───────────────────────────────────────────────────────────────

print("=" * 64)
print("  DID-Swap Welcome Routing Test")
print(f"  DID_A = {DID_A}  |  DID_B = {DID_B}")
print("=" * 64)

try:
    _cleanup()   # clear any leftovers

    # ── Insert both clients ───────────────────────────────────────────────────
    _upsert(DEEPTI)   # DID_A → Deepti
    _upsert(DUMMY)    # DID_B → Dummy
    print("\n── Scenario A: Deepti owns DID_A, Dummy owns DID_B ──")

    welcome_a1 = _welcome_for(DID_A)
    welcome_b1 = _welcome_for(DID_B)
    print(f"  DID_A welcome : {welcome_a1}")
    print(f"  DID_B welcome : {welcome_b1}")

    check("DID_A → Deepti's clinic welcome",
          welcome_a1 and "Deepti" in welcome_a1,
          detail=welcome_a1 or "None")
    check("DID_B → Dummy clinic welcome",
          welcome_b1 and "Dummy" in welcome_b1,
          detail=welcome_b1 or "None")
    check("DID_A welcome does NOT say 'Dummy'",
          welcome_a1 and "Dummy" not in welcome_a1)
    check("DID_B welcome does NOT say 'Deepti'",
          welcome_b1 and "Deepti" not in welcome_b1)

    # ── Swap DID: clear Deepti, give DID_A to Dummy ──────────────────────────
    print("\n── Scenario B: DID_A moved from Deepti → Dummy ──")
    _set_did("TEST_did_deepti", "")     # Deepti loses DID_A
    _set_did("TEST_did_dummy",  DID_A)  # Dummy gains DID_A

    welcome_a2 = _welcome_for(DID_A)
    welcome_b2 = _welcome_for(DID_B)   # DID_B now orphaned
    print(f"  DID_A welcome : {welcome_a2}")
    print(f"  DID_B welcome : {welcome_b2}")

    check("DID_A now → Dummy clinic welcome (after swap)",
          welcome_a2 and "Dummy" in welcome_a2,
          detail=welcome_a2 or "None")
    check("DID_A no longer says 'Deepti' (after swap)",
          welcome_a2 and "Deepti" not in welcome_a2)
    check("DID_B now → no match (orphaned DID)",
          welcome_b2 is None,
          detail=str(welcome_b2))

finally:
    _cleanup()

# ── Summary ───────────────────────────────────────────────────────────────────
passed = sum(1 for _, ok in _results if ok)
failed = sum(1 for _, ok in _results if not ok)
total  = len(_results)

print("=" * 64)
if failed == 0:
    print(f"  Results: {passed}/{total} passed  — all green ✅")
else:
    print(f"  Results: {passed}/{total} passed  |  {failed} FAILED")
    print()
    print("  Failed checks:")
    for label, ok in _results:
        if not ok:
            print(f"    ❌ {label}")
print("=" * 64)

sys.exit(0 if failed == 0 else 1)
