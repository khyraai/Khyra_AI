"""
database.py — Central PostgreSQL database layer.

All tables are created in the khyra_db PostgreSQL instance (Docker).
Connection pooling is handled by pg.py (ThreadedConnectionPool).

Tables
------
clients           — Registry of clinic/doctor config; managed directly in DB
sessions          — Conversation state + memory per call
appointments      — PRIMARY booking table (N8N-managed, doctor-facing)
agent_appointments— SECONDARY fallback (voice-agent-written at booking time)
call_logs         — One row per call: duration, language, outcome, costs
stt_events        — Per-request STT telemetry
tts_events        — Per-request TTS telemetry
llm_events        — Per-request LLM telemetry (tokens, latency, input/output)
cancellations     — Cancellation / reschedule records
audit_log         — Generic change-event trail
reschedules       — Detailed reschedule history (appointment_id soft link)

All tables share session_id as a soft TEXT link (no FK constraint).

Public API
----------
init_db()
upsert_client(cfg: dict)
SessionStore                                — drop-in replacement for utils.py version
check_availability(target_date, target_time, client_id=None) -> bool
insert_appointment(row: dict) -> str
create_appointment(session_id, client_id, state_dict, **kw) -> str
cancel_or_reschedule_appointment(appointment_id, action, **kw)
log_call_start(session_id, client_id, **kw) -> int
log_call_end(session_id, outcome, cost_dict=None)
log_stt_event(event_dict)
log_tts_event(event_dict)
log_llm_event(event_dict)
append_audit(session_id, event_type, entity_type="", entity_id="",
             old_value="", new_value="", client_id="")
"""

import json
import os
import re
import uuid
from datetime import datetime, timedelta
from typing import Optional

import pytz

from pg import get_conn

_IST = pytz.timezone("Asia/Kolkata")

def get_client_max_slots(client_id: str = None, did_number: str = None) -> int:
    """Fetch max_slots_per_30_minutes from DB, using did_number if available, else client_id."""
    try:
        from pg import get_conn
        with get_conn() as cur:
            if did_number:
                cur.execute("SELECT max_slots_per_30_minutes FROM clients WHERE did_number = %s LIMIT 1", (did_number,))
                row = cur.fetchone()
                if row and row["max_slots_per_30_minutes"] is not None:
                    return int(row["max_slots_per_30_minutes"])
                norm = did_number if did_number.startswith("+") else "+" + did_number
                cur.execute("SELECT max_slots_per_30_minutes FROM clients WHERE did_number = %s LIMIT 1", (norm,))
                row = cur.fetchone()
                if row and row["max_slots_per_30_minutes"] is not None:
                    return int(row["max_slots_per_30_minutes"])
            if client_id:
                cur.execute("SELECT max_slots_per_30_minutes FROM clients WHERE client_id = %s LIMIT 1", (client_id,))
                row = cur.fetchone()
                if row and row["max_slots_per_30_minutes"] is not None:
                    return int(row["max_slots_per_30_minutes"])
    except Exception as e:
        print(f"⚠️ [DB] get_client_max_slots error: {e}")
    return 4

_KANNADA_DIGITS = str.maketrans("೦೧೨೩೪೫೬೭೮೯", "0123456789")
_KANNADA_TIME_HINTS = {
    "ಬೆಳಗ್ಗೆ": "AM", "ಮುಂಜಾನೆ": "AM",
    "ಮಧ್ಯಾಹ್ನ": "PM", "ಸಂಜೆ": "PM", "ರಾತ್ರಿ": "PM",
}


# ---------------------------------------------------------------------------
# Schema creation — 10 tables
# ---------------------------------------------------------------------------
_SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS clients (
        client_id               TEXT PRIMARY KEY,
        did_number              TEXT UNIQUE,
        clinic_name             TEXT,
        doctor_name             TEXT,
        doctor_qualifications   TEXT,
        address                 TEXT,
        timings                 TEXT,
        doctor_mobile           TEXT,
        consultation_fee_min    INTEGER DEFAULT 0,
        consultation_fee_max    INTEGER DEFAULT 0,
        default_language        TEXT DEFAULT 'kn',
        emergency_transfer_number TEXT,
        connection_id           TEXT,
        created_at              TEXT DEFAULT (NOW()::TEXT),
        updated_at              TEXT DEFAULT (NOW()::TEXT),
        config_version          INTEGER,
        closed_days             JSONB,
        services                JSONB,
        clinic_type             TEXT,
        google_maps_url         TEXT,
        clinic_email            TEXT,
        clinic_phone            TEXT,
        domain                  VARCHAR(100),
        supported_languages     JSONB,
        session_time            JSONB,
        max_slots_per_30_minutes INTEGER DEFAULT 4
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sessions (
        session_id  TEXT PRIMARY KEY,
        client_id   TEXT,
        state       TEXT,
        memory      TEXT,
        updated_at  TEXT DEFAULT (NOW()::TEXT)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS appointments (
        id               TEXT PRIMARY KEY,
        session_id       TEXT,
        connection_id    TEXT,
        google_event_id  TEXT,
        patient_name     TEXT,
        patient_phone    TEXT,
        start_time       TEXT,
        end_time         TEXT,
        appointment_type TEXT,
        status           TEXT DEFAULT 'confirmed',
        doctor_name      TEXT,
        reason           TEXT,
        booked_via       TEXT DEFAULT 'agent',
        agent_notes      TEXT,
        created_at       TEXT DEFAULT (NOW()::TEXT),
        updated_at       TEXT DEFAULT (NOW()::TEXT),
        sync_status      TEXT,
        sync_error       TEXT,
        retry_count      INTEGER DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_appt_start_time    ON appointments(start_time)",
    "CREATE INDEX IF NOT EXISTS idx_appt_connection_id ON appointments(connection_id)",
    "CREATE INDEX IF NOT EXISTS idx_appt_status        ON appointments(status)",
    "CREATE INDEX IF NOT EXISTS idx_appt_session_id    ON appointments(session_id)",
    """
    CREATE TABLE IF NOT EXISTS agent_appointments (
        id                TEXT PRIMARY KEY,
        session_id        TEXT,
        client_id         TEXT,
        event_type        TEXT DEFAULT 'appointment_create',
        patient_name      TEXT,
        patient_phone     TEXT,
        start_time        TEXT,
        end_time          TEXT,
        previous_datetime TEXT,
        appointment_type  TEXT,
        status            TEXT DEFAULT 'scheduled',
        doctor_name       TEXT,
        reason            TEXT,
        booked_via        TEXT DEFAULT 'voice_assistant',
        agent_notes       TEXT,
        created_at        TEXT DEFAULT (NOW()::TEXT),
        updated_at        TEXT DEFAULT (NOW()::TEXT)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_agent_appt_start_time ON agent_appointments(start_time)",
    "CREATE INDEX IF NOT EXISTS idx_agent_appt_session_id ON agent_appointments(session_id)",
    """
    CREATE TABLE IF NOT EXISTS call_logs (
        id              SERIAL PRIMARY KEY,
        session_id      TEXT UNIQUE,
        client_id       TEXT,
        did_number      TEXT,
        caller_phone    TEXT,
        call_start      TEXT,
        call_end        TEXT,
        duration_sec    REAL DEFAULT 0,
        language        TEXT,
        outcome         TEXT,
        appointment_id  TEXT,
        stt_cost_inr    REAL DEFAULT 0,
        tts_cost_inr    REAL DEFAULT 0,
        llm_cost_inr    REAL DEFAULT 0,
        total_cost_inr  REAL DEFAULT 0,
        total_tokens    INTEGER DEFAULT 0,
        created_at      TEXT DEFAULT (NOW()::TEXT)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_call_logs_client_id  ON call_logs(client_id)",
    "CREATE INDEX IF NOT EXISTS idx_call_logs_session_id ON call_logs(session_id)",
    """
    CREATE TABLE IF NOT EXISTS stt_events (
        id                  SERIAL PRIMARY KEY,
        session_id          TEXT,
        client_id           TEXT,
        ts                  REAL,
        provider            TEXT,
        audio_duration_sec  REAL DEFAULT 0,
        transcript          TEXT,
        language_code       TEXT,
        success             INTEGER DEFAULT 0,
        fallback_used       INTEGER DEFAULT 0,
        retry_count         INTEGER DEFAULT 0,
        total_latency_ms    REAL DEFAULT 0,
        estimated_cost_inr  REAL DEFAULT 0,
        error_type          TEXT,
        created_at          TEXT DEFAULT (NOW()::TEXT)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_stt_session ON stt_events(session_id)",
    "CREATE INDEX IF NOT EXISTS idx_stt_client  ON stt_events(client_id)",
    """
    CREATE TABLE IF NOT EXISTS tts_events (
        id                  SERIAL PRIMARY KEY,
        session_id          TEXT,
        client_id           TEXT,
        ts                  REAL,
        provider            TEXT,
        char_count          INTEGER DEFAULT 0,
        language            TEXT,
        mode                TEXT,
        success             INTEGER DEFAULT 0,
        fallback_used       INTEGER DEFAULT 0,
        retry_count         INTEGER DEFAULT 0,
        total_latency_ms    REAL DEFAULT 0,
        estimated_cost_inr  REAL DEFAULT 0,
        error_type          TEXT,
        created_at          TEXT DEFAULT (NOW()::TEXT)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_tts_session ON tts_events(session_id)",
    "CREATE INDEX IF NOT EXISTS idx_tts_client  ON tts_events(client_id)",
    """
    CREATE TABLE IF NOT EXISTS llm_events (
        id                  SERIAL PRIMARY KEY,
        session_id          TEXT,
        client_id           TEXT,
        ts                  REAL,
        agent               TEXT,
        model               TEXT,
        prompt_tokens       INTEGER DEFAULT 0,
        completion_tokens   INTEGER DEFAULT 0,
        total_tokens        INTEGER DEFAULT 0,
        latency_ms          REAL DEFAULT 0,
        user_input          TEXT,
        llm_response        TEXT,
        success             INTEGER DEFAULT 0,
        error_type          TEXT,
        estimated_cost_inr  REAL DEFAULT 0,
        created_at          TEXT DEFAULT (NOW()::TEXT)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_llm_session ON llm_events(session_id)",
    "CREATE INDEX IF NOT EXISTS idx_llm_client  ON llm_events(client_id)",
    """
    CREATE TABLE IF NOT EXISTS cancellations (
        id              SERIAL PRIMARY KEY,
        appointment_id  TEXT,
        session_id      TEXT,
        client_id       TEXT,
        action          TEXT,
        old_start_time  TEXT,
        new_start_time  TEXT,
        reason          TEXT,
        created_at      TEXT DEFAULT (NOW()::TEXT)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS reschedules (
        id              SERIAL PRIMARY KEY,
        appointment_id  TEXT,
        old_start_time  TEXT,
        new_start_time  TEXT,
        reason          TEXT,
        created_at      TEXT DEFAULT (NOW()::TEXT)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_reschedules_appt_id ON reschedules(appointment_id)",
    """
    CREATE TABLE IF NOT EXISTS audit_log (
        id          SERIAL PRIMARY KEY,
        session_id  TEXT,
        client_id   TEXT,
        event_type  TEXT,
        entity_type TEXT,
        entity_id   TEXT,
        old_value   TEXT,
        new_value   TEXT,
        created_at  TEXT DEFAULT (NOW()::TEXT)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_audit_session ON audit_log(session_id)",
    # ---------------------------------------------------------------------------
    # Column migrations — safe to re-run (ADD COLUMN IF NOT EXISTS)
    # ---------------------------------------------------------------------------
    "ALTER TABLE clients           ADD COLUMN IF NOT EXISTS config_version  INTEGER",
    "ALTER TABLE clients           ADD COLUMN IF NOT EXISTS morning_open    TEXT",
    "ALTER TABLE clients           ADD COLUMN IF NOT EXISTS morning_close   TEXT",
    "ALTER TABLE clients           ADD COLUMN IF NOT EXISTS evening_open    TEXT",
    "ALTER TABLE clients           ADD COLUMN IF NOT EXISTS evening_close   TEXT",
    "ALTER TABLE clients           ADD COLUMN IF NOT EXISTS closed_weekdays TEXT",
    "ALTER TABLE clients           ADD COLUMN IF NOT EXISTS services        TEXT",
    "ALTER TABLE appointments      ADD COLUMN IF NOT EXISTS sync_status    TEXT",
    "ALTER TABLE appointments      ADD COLUMN IF NOT EXISTS sync_error     TEXT",
    "ALTER TABLE appointments      ADD COLUMN IF NOT EXISTS retry_count    INTEGER DEFAULT 0",
    "ALTER TABLE call_logs         ADD COLUMN IF NOT EXISTS total_tokens      INTEGER DEFAULT 0",
    "ALTER TABLE call_logs         ADD COLUMN IF NOT EXISTS transcript        TEXT",
    # N8N fail-safe tracking
    "ALTER TABLE call_logs         ADD COLUMN IF NOT EXISTS n8n_triggered     BOOLEAN DEFAULT FALSE",
    "ALTER TABLE call_logs         ADD COLUMN IF NOT EXISTS n8n_triggered_at  TEXT",
    # Multi-client isolation migrations
    "ALTER TABLE appointments      ADD COLUMN IF NOT EXISTS client_id      TEXT",
    "ALTER TABLE agent_appointments ADD COLUMN IF NOT EXISTS client_id     TEXT",
    "ALTER TABLE agent_appointments ADD COLUMN IF NOT EXISTS event_type    TEXT",
    "ALTER TABLE agent_appointments ADD COLUMN IF NOT EXISTS previous_datetime TEXT",
    "ALTER TABLE reschedules       ADD COLUMN IF NOT EXISTS session_id     TEXT",
    "ALTER TABLE reschedules       ADD COLUMN IF NOT EXISTS client_id      TEXT",
    "CREATE INDEX IF NOT EXISTS idx_appt_client_id        ON appointments(client_id)",
    "CREATE INDEX IF NOT EXISTS idx_agent_appt_client_id  ON agent_appointments(client_id)",
    "CREATE INDEX IF NOT EXISTS idx_reschedules_client_id ON reschedules(client_id)",
    # Tables created externally (dashboard/calendar integration) — ensure they exist
    # IMPORTANT: these must come BEFORE any ALTER TABLE that references them so that
    # a fresh database never hits UndefinedTable on the DROP CONSTRAINT lines below.
    """
    CREATE TABLE IF NOT EXISTS app_users (
        firebase_uid  TEXT PRIMARY KEY,
        client_id     TEXT NOT NULL,
        email         TEXT,
        created_at    TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS calendar_connections (
        id                    SERIAL PRIMARY KEY,
        client_id             TEXT NOT NULL,
        doctor_name           TEXT,
        provider              TEXT DEFAULT 'google',
        google_account_email  TEXT NOT NULL,
        calendar_id           TEXT NOT NULL UNIQUE,
        calendar_name         TEXT,
        refresh_token         TEXT NOT NULL,
        access_token          TEXT,
        token_expiry          TIMESTAMP WITHOUT TIME ZONE,
        watch_channel_id      TEXT,
        watch_resource_id     TEXT,
        watch_expiration      TIMESTAMP WITHOUT TIME ZONE,
        sync_token            TEXT,
        last_sync_time        TIMESTAMP WITHOUT TIME ZONE,
        timezone              TEXT,
        connection_status     TEXT DEFAULT 'active',
        last_error            TEXT,
        created_at            TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP,
        updated_at            TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_calendar_connections_client ON calendar_connections(client_id)",
    # ---------------------------------------------------------------------------
    # Availability table — blocks dates/time-ranges from being booked
    # Created by the dashboard; we just ensure it exists here.
    # ---------------------------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS availability (
        id               character varying(255) PRIMARY KEY,
        client_id        character varying(255),
        doctor_id        character varying(255),
        appointment_id   character varying(255),
        google_event_id  text,
        start_time       timestamptz NOT NULL,
        end_time         timestamptz NOT NULL,
        is_all_day       boolean DEFAULT false,
        event_type       character varying(50),
        reason           character varying(255),
        description      text,
        created_source   character varying(50),
        status           character varying(50) DEFAULT 'active',
        last_synced_at   timestamptz,
        created_at       timestamptz DEFAULT CURRENT_TIMESTAMP,
        updated_at       timestamptz DEFAULT CURRENT_TIMESTAMP
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_availability_client_id  ON availability(client_id)",
    "CREATE INDEX IF NOT EXISTS idx_availability_start_time ON availability(start_time)",
    "CREATE INDEX IF NOT EXISTS idx_availability_status     ON availability(status)",
    # Drop all manually-added client_id FK constraints — none were defined in
    # code; all cause FK violations on any client_id config mismatch.
    # client_id is treated as a soft TEXT label throughout this codebase.
    # These run AFTER all tables are created so they are safe on a fresh DB.
    "ALTER TABLE llm_events          DROP CONSTRAINT IF EXISTS fk_llm_events_client",
    "ALTER TABLE stt_events          DROP CONSTRAINT IF EXISTS fk_stt_events_client",
    "ALTER TABLE tts_events          DROP CONSTRAINT IF EXISTS fk_tts_events_client",
    "ALTER TABLE call_logs           DROP CONSTRAINT IF EXISTS fk_call_logs_client",
    "ALTER TABLE audit_log           DROP CONSTRAINT IF EXISTS fk_audit_log_client",
    "ALTER TABLE cancellations       DROP CONSTRAINT IF EXISTS fk_cancellations_client",
    "ALTER TABLE agent_appointments  DROP CONSTRAINT IF EXISTS fk_agent_appointments_client",
    "ALTER TABLE appointments        DROP CONSTRAINT IF EXISTS fk_appointments_client",
    "ALTER TABLE sessions            DROP CONSTRAINT IF EXISTS fk_sessions_client",
    "ALTER TABLE reschedules         DROP CONSTRAINT IF EXISTS fk_reschedules_client",
    "ALTER TABLE app_users           DROP CONSTRAINT IF EXISTS fk_app_users_client",
    "ALTER TABLE calendar_connections DROP CONSTRAINT IF EXISTS fk_calendar_client",
]


def init_db():
    """Create all tables (idempotent)."""
    with get_conn() as cur:
        for stmt in _SCHEMA_STATEMENTS:
            cur.execute(stmt)
    print("[DB] PostgreSQL schema initialised")


# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------
def upsert_client(cfg: dict):
    """Insert or update a client row from a config dict."""
    if not cfg or not cfg.get("client_id"):
        return
    now = datetime.now().isoformat()
    with get_conn() as cur:
        cur.execute("""
            INSERT INTO clients
                (client_id, did_number, clinic_name, doctor_name, doctor_qualifications,
                 address, timings, doctor_mobile, consultation_fee_min, consultation_fee_max,
                 default_language, emergency_transfer_number, connection_id, created_at, updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT(client_id) DO UPDATE SET
                did_number=EXCLUDED.did_number,
                clinic_name=EXCLUDED.clinic_name,
                doctor_name=EXCLUDED.doctor_name,
                doctor_qualifications=EXCLUDED.doctor_qualifications,
                address=EXCLUDED.address,
                timings=EXCLUDED.timings,
                doctor_mobile=EXCLUDED.doctor_mobile,
                consultation_fee_min=EXCLUDED.consultation_fee_min,
                consultation_fee_max=EXCLUDED.consultation_fee_max,
                default_language=EXCLUDED.default_language,
                emergency_transfer_number=EXCLUDED.emergency_transfer_number,
                connection_id=EXCLUDED.connection_id,
                updated_at=EXCLUDED.updated_at
        """, (
            cfg.get("client_id"),
            cfg.get("_did") or cfg.get("did_number", ""),
            cfg.get("clinic_name", ""),
            cfg.get("doctor_name", ""),
            cfg.get("doctor_qualifications", ""),
            cfg.get("address", ""),
            cfg.get("timings", ""),
            cfg.get("doctor_mobile", ""),
            int(cfg.get("consultation_fee_min", 0) or 0),
            int(cfg.get("consultation_fee_max", 0) or 0),
            cfg.get("default_language", "kn"),
            cfg.get("emergency_transfer_number", ""),
            cfg.get("connection_id", cfg.get("client_id", "")),
            now,
            now,
        ))


# ---------------------------------------------------------------------------
# Sessions  (drop-in replacement for utils.py SessionStore)
# ---------------------------------------------------------------------------
class SessionStore:
    def __init__(self):
        init_db()

    def save_session(self, session_id: str, state: dict, memory: list):
        now = datetime.now().isoformat()
        client_id = state.get("client_id", "") if isinstance(state, dict) else ""
        with get_conn() as cur:
            cur.execute("""
                INSERT INTO sessions (session_id, client_id, state, memory, updated_at)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT(session_id) DO UPDATE SET
                    client_id=EXCLUDED.client_id,
                    state=EXCLUDED.state,
                    memory=EXCLUDED.memory,
                    updated_at=EXCLUDED.updated_at
            """, (session_id, client_id,
                  json.dumps(state, ensure_ascii=False),
                  json.dumps(memory, ensure_ascii=False),
                  now))

    def load_session(self, session_id: str) -> tuple:
        """Returns (state, memory) or (None, None) if not found."""
        with get_conn() as cur:
            cur.execute(
                "SELECT state, memory FROM sessions WHERE session_id = %s",
                (session_id,)
            )
            row = cur.fetchone()
        if row:
            try:
                return json.loads(row["state"]), json.loads(row["memory"])
            except Exception:
                return None, None
        return None, None

    def clear_session(self, session_id: str):
        with get_conn() as cur:
            cur.execute("DELETE FROM sessions WHERE session_id = %s", (session_id,))


# ---------------------------------------------------------------------------
# Appointments — availability check
# ---------------------------------------------------------------------------
def _parse_to_ist_iso(target_date: str, target_time: str) -> str | None:
    """Parse 'DD Month YYYY' + '10:00 AM' → IST ISO-8601 string."""
    datetime_str = f"{target_date} {target_time}"
    for fmt in (
        "%d %B %Y %I:%M %p",
        "%d %B %Y %I %p",
        "%d %B %Y %H:%M",
        "%d %B %Y %H",
        "%Y-%m-%d %I:%M %p",
        "%Y-%m-%d %H:%M",
        "%d %b %Y %I:%M %p",
        "%d %b %Y %H:%M",
    ):
        try:
            naive = datetime.strptime(datetime_str.replace(",", ""), fmt)
            dt = _IST.localize(naive)
            tz = dt.strftime("%z")          # "+0530"
            tz_fmt = tz[:3] + ":" + tz[3:] # "+05:30"
            return dt.strftime("%Y-%m-%d %H:%M:%S") + tz_fmt
        except ValueError:
            continue
    return None


def insert_appointment(row: dict) -> str:
    """Insert a raw appointment row into the PRIMARY appointments table. Returns the id."""
    appt_id = row.get("id") or str(uuid.uuid4())
    now = datetime.now().isoformat()
    with get_conn() as cur:
        cur.execute("""
            INSERT INTO appointments
                (id, session_id, client_id, connection_id, google_event_id, patient_name, patient_phone,
                 start_time, end_time, appointment_type, status, doctor_name,
                 reason, booked_via, agent_notes, created_at, updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT(id) DO UPDATE SET
                session_id=EXCLUDED.session_id,
                client_id=EXCLUDED.client_id,
                connection_id=EXCLUDED.connection_id,
                patient_name=EXCLUDED.patient_name,
                patient_phone=EXCLUDED.patient_phone,
                start_time=EXCLUDED.start_time,
                end_time=EXCLUDED.end_time,
                appointment_type=EXCLUDED.appointment_type,
                status=EXCLUDED.status,
                doctor_name=EXCLUDED.doctor_name,
                reason=EXCLUDED.reason,
                booked_via=EXCLUDED.booked_via,
                agent_notes=EXCLUDED.agent_notes,
                updated_at=EXCLUDED.updated_at
        """, (
            appt_id,
            row.get("session_id", ""),
            row.get("client_id", ""),
            row.get("connection_id", ""),
            row.get("google_event_id", ""),
            row.get("patient_name", ""),
            row.get("patient_phone", ""),
            row.get("start_time", ""),
            row.get("end_time", ""),
            row.get("appointment_type", "consultation"),
            row.get("status", "confirmed"),
            row.get("doctor_name", ""),
            row.get("reason", ""),
            row.get("booked_via", "agent"),
            row.get("agent_notes", ""),
            row.get("created_at", now),
            row.get("updated_at", now),
        ))
    return appt_id


def create_appointment(
    session_id: str,
    client_id: str,
    state_dict: dict,
    *,
    caller_phone: str = "",
    language: str = "kn",
    doctor_name: str = "",
) -> str:
    """Create an appointment from agent state and return appointment id."""
    from client_config import get_config_by_client_id
    cfg = get_config_by_client_id(client_id) or {}
    connection_id = cfg.get("connection_id", client_id)
    resolved_doctor = doctor_name or cfg.get("doctor_name", "")

    raw_date = state_dict.get("date", "")
    raw_time = state_dict.get("time", "")
    iso_start = _parse_to_ist_iso(raw_date, raw_time) if raw_date and raw_time else ""

    appt_id = str(uuid.uuid4())
    now = datetime.now().isoformat()
    with get_conn() as cur:
        cur.execute("""
            INSERT INTO appointments
                (id, session_id, connection_id, patient_name, patient_phone,
                 start_time, appointment_type, status, doctor_name,
                 reason, booked_via, agent_notes, client_id, created_at, updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (
            appt_id,
            session_id,
            connection_id,
            state_dict.get("name", ""),
            caller_phone,
            iso_start,
            state_dict.get("reason", "consultation"),
            "confirmed",
            resolved_doctor,
            state_dict.get("reason", ""),
            "agent",
            f"lang={language} session={session_id}",
            client_id,
            now,
            now,
        ))

    append_audit(session_id, "appointment_created", "appointment", appt_id,
                 old_value="", new_value=json.dumps({"start_time": iso_start}),
                 client_id=client_id)
    return appt_id


def cancel_or_reschedule_appointment(
    appointment_id: str,
    action: str,
    *,
    session_id: str = "",
    client_id: str = "",
    new_start_time: str = "",
    reason: str = "",
):
    """action = 'cancelled' | 'rescheduled'"""
    now = datetime.now().isoformat()
    with get_conn() as cur:
        cur.execute(
            "SELECT start_time, status FROM appointments WHERE id = %s",
            (appointment_id,)
        )
        old_row = cur.fetchone()
        old_start = str(old_row["start_time"]) if (old_row and old_row["start_time"]) else ""
        old_status = old_row["status"] if old_row else ""

        cur.execute("""
            UPDATE appointments
            SET status = %s,
                start_time = COALESCE(%s::TIMESTAMP, start_time),
                updated_at = %s
            WHERE id = %s
        """, (action, new_start_time or None, now, appointment_id))

        cur.execute("""
            INSERT INTO cancellations
                (appointment_id, session_id, client_id, action,
                 old_start_time, new_start_time, reason, created_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
        """, (appointment_id, session_id, client_id, action,
              old_start, new_start_time, reason, now))

    append_audit(session_id, action, "appointment", appointment_id,
                 old_value=json.dumps({"status": old_status, "start_time": old_start}),
                 new_value=json.dumps({"status": action, "start_time": new_start_time or old_start}),
                 client_id=client_id)


# ---------------------------------------------------------------------------
# Call logs
# ---------------------------------------------------------------------------
def log_call_start(
    session_id: str,
    client_id: str,
    *,
    did_number: str = "",
    caller_phone: str = "",
    language: str = "",
) -> int:
    now = datetime.now().isoformat()
    with get_conn() as cur:
        cur.execute("""
            INSERT INTO call_logs
                (session_id, client_id, did_number, caller_phone, call_start, language, created_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT(session_id) DO NOTHING
        """, (session_id, client_id, did_number, caller_phone, now, language, now))
        return 0


def log_call_end(
    session_id: str,
    outcome: str,
    cost_dict: Optional[dict] = None,
    *,
    appointment_id: str = "",
    language: str = "",
    transcript: str = "",
):
    cost_dict = cost_dict or {}
    now = datetime.now().isoformat()
    with get_conn() as cur:
        cur.execute(
            "SELECT call_start FROM call_logs WHERE session_id = %s",
            (session_id,)
        )
        row = cur.fetchone()
        duration = 0.0
        if row and row["call_start"]:
            try:
                start = row["call_start"]
                if isinstance(start, str):
                    start = datetime.fromisoformat(start)
                duration = (datetime.now() - start).total_seconds()
            except Exception:
                pass

        print(f"[DB] log_call_end: session={session_id} outcome={outcome} duration={round(duration,2)}s")
        cur.execute("""
            UPDATE call_logs
            SET call_end=%s, duration_sec=%s, outcome=%s, appointment_id=%s,
                stt_cost_inr=%s, tts_cost_inr=%s, llm_cost_inr=%s, total_cost_inr=%s,
                language=COALESCE(NULLIF(%s, ''), language),
                transcript=%s
            WHERE session_id=%s
        """, (
            now,
            round(duration, 2),
            outcome,
            appointment_id,
            float(cost_dict.get("stt", 0.0)),
            float(cost_dict.get("tts", 0.0)),
            float(cost_dict.get("llm", 0.0)),
            float(cost_dict.get("total", 0.0)),
            language,
            transcript,
            session_id,
        ))
        print(f"[DB] log_call_end: updated {cur.rowcount} row(s)")


# ---------------------------------------------------------------------------
# N8N webhook audit
# ---------------------------------------------------------------------------
def mark_n8n_triggered(session_id: str, success: bool):
    """
    Record whether the n8n webhook was successfully delivered for this call.

    Sets n8n_triggered = success and n8n_triggered_at = now on the call_logs
    row identified by session_id.  Safe to call even if the row does not exist
    yet (UPDATE will simply affect 0 rows and we log a warning).
    """
    now = datetime.now().isoformat()
    status_label = "SUCCESS" if success else "FAILED"
    try:
        with get_conn() as cur:
            cur.execute(
                """
                UPDATE call_logs
                SET n8n_triggered = %s, n8n_triggered_at = %s
                WHERE session_id = %s
                """,
                (success, now, session_id),
            )
            if cur.rowcount == 0:
                print(f"[DB] mark_n8n_triggered: no call_logs row for session={session_id} — skipping")
            else:
                print(f"[DB] mark_n8n_triggered: session={session_id} → {status_label}")
    except Exception as e:
        print(f"[DB] mark_n8n_triggered error: {e}")


# ---------------------------------------------------------------------------
# STT / TTS / LLM event logging
# ---------------------------------------------------------------------------
def log_stt_event(event_dict: dict):
    """Persist one STT request event to stt_events table."""
    try:
        with get_conn() as cur:
            cur.execute("""
                INSERT INTO stt_events
                    (session_id, client_id, ts, provider, audio_duration_sec,
                     transcript, language_code, success, fallback_used, retry_count,
                     total_latency_ms, estimated_cost_inr, error_type, created_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (
                event_dict.get("session_id", "unknown"),
                event_dict.get("client_id", "default"),
                float(event_dict.get("ts", 0.0)),
                event_dict.get("provider", ""),
                float(event_dict.get("audio_duration_sec", 0.0)),
                event_dict.get("transcript", ""),
                event_dict.get("language_code", "") or event_dict.get("detected_lang", ""),
                int(bool(event_dict.get("success"))),
                int(bool(event_dict.get("fallback_used"))),
                int(event_dict.get("retry_count", 0) or 0),
                float(event_dict.get("total_latency_ms", 0.0)),
                float(event_dict.get("estimated_cost_inr", 0.0)),
                event_dict.get("error_type", ""),
                datetime.now().isoformat(),
            ))
    except Exception as e:
        print(f"[DB] log_stt_event error: {e}")


def log_tts_event(event_dict: dict):
    """Persist one TTS request event to tts_events table."""
    try:
        with get_conn() as cur:
            cur.execute("""
                INSERT INTO tts_events
                    (session_id, client_id, ts, provider, char_count, language, mode,
                     success, fallback_used, retry_count, total_latency_ms,
                     estimated_cost_inr, error_type, created_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (
                event_dict.get("session_id", "unknown"),
                event_dict.get("client_id", "default"),
                float(event_dict.get("ts", 0.0)),
                event_dict.get("provider", ""),
                int(event_dict.get("char_count", 0) or 0),
                event_dict.get("language", ""),
                event_dict.get("mode", ""),
                int(bool(event_dict.get("success"))),
                int(bool(event_dict.get("fallback_used"))),
                int(event_dict.get("retry_count", 0) or 0),
                float(event_dict.get("total_latency_ms", 0.0)),
                float(event_dict.get("estimated_cost_inr", 0.0)),
                event_dict.get("error_type", ""),
                datetime.now().isoformat(),
            ))
    except Exception as e:
        print(f"[DB] log_tts_event error: {e}")


def log_llm_event(event_dict: dict):
    """Persist one LLM request event to llm_events table."""
    try:
        with get_conn() as cur:
            cur.execute("""
                INSERT INTO llm_events
                    (session_id, client_id, ts, agent, model,
                     prompt_tokens, completion_tokens, total_tokens,
                     latency_ms, user_input, llm_response,
                     success, error_type, estimated_cost_inr, created_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (
                event_dict.get("session_id", "unknown"),
                event_dict.get("client_id", "default"),
                float(event_dict.get("ts", 0.0)),
                event_dict.get("agent", ""),
                event_dict.get("model", ""),
                int(event_dict.get("prompt_tokens", 0) or 0),
                int(event_dict.get("completion_tokens", 0) or 0),
                int(event_dict.get("total_tokens", 0) or 0),
                float(event_dict.get("latency_ms", 0.0)),
                event_dict.get("user_input", ""),
                event_dict.get("llm_response", ""),
                int(bool(event_dict.get("success"))),
                event_dict.get("error_type", ""),
                float(event_dict.get("estimated_cost_inr", 0.0)),
                datetime.now().isoformat(),
            ))
    except Exception as e:
        print(f"[DB] log_llm_event error: {e}")


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------
def append_audit(
    session_id: str,
    event_type: str,
    entity_type: str = "",
    entity_id: str = "",
    old_value: str = "",
    new_value: str = "",
    client_id: str = "",
):
    try:
        with get_conn() as cur:
            cur.execute("""
                INSERT INTO audit_log
                    (session_id, client_id, event_type, entity_type, entity_id,
                     old_value, new_value, created_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            """, (session_id, client_id, event_type, entity_type, entity_id,
                  old_value, new_value, datetime.now().isoformat()))
    except Exception as e:
        print(f"[DB] append_audit error: {e}")


# ===========================================================================
# Appointment helpers — two-table design
# PRIMARY  : appointments        (N8N-managed, doctor-facing)
# SECONDARY: agent_appointments  (voice-agent fallback, written at booking time)
# ===========================================================================

def _normalize_appointment_datetime(raw: str) -> str:
    """
    Convert various human/Kannada date+time strings to a canonical IST ISO timestamp.
    Returns the original string if parsing fails (so we don't silently drop data).
    """
    if not raw:
        return raw
    s = str(raw).strip()

    if "T" in s and ("+" in s or "Z" in s):
        return s

    s_ascii = s.translate(_KANNADA_DIGITS)

    am_pm_hint = None
    for kn_word, period in _KANNADA_TIME_HINTS.items():
        if kn_word in s_ascii:
            am_pm_hint = period
            s_ascii = s_ascii.replace(kn_word, "")

    for token in ("ಗಂಟೆ", "ಗೆ", "ಗ೦ಟೆ"):
        s_ascii = s_ascii.replace(token, "")
    s_ascii = re.sub(r"\s+", " ", s_ascii).strip()

    if am_pm_hint and not re.search(r"(?i)\b(AM|PM)\b", s_ascii):
        m = re.search(r"(\d{1,2})(?::(\d{2}))?\s*$", s_ascii)
        if m:
            s_ascii = s_ascii[:m.start()] + f"{m.group(1)}:{m.group(2) or '00'} {am_pm_hint}"

    candidates = [
        "%Y-%m-%d %I:%M %p", "%Y-%m-%d %I %p",
        "%Y-%m-%d %H:%M",    "%Y-%m-%d %H",
        "%d %B %Y %I:%M %p", "%d %B %Y %I %p",
        "%d %B %Y %H:%M",
    ]
    naive_dt = None
    for fmt in candidates:
        try:
            naive_dt = datetime.strptime(s_ascii.replace(",", "").strip(), fmt)
            break
        except ValueError:
            continue

    if naive_dt is None:
        print(f"⚠️ [DB] Could not normalize datetime '{raw}', saving as-is.")
        return s

    return _IST.localize(naive_dt).isoformat()


def save_agent_appointment(payload: dict, session_id: str = "", client_id: str = "") -> bool:
    """
    Save an appointment to the SECONDARY agent_appointments table in PostgreSQL.
    This is the agent's own fallback copy — written at booking time.
    Returns True if saved successfully, False otherwise.
    """
    try:
        start_time = _normalize_appointment_datetime(payload.get("start_time", ""))
        end_time   = _normalize_appointment_datetime(payload.get("end_time", ""))
        if start_time and not end_time and "T" in start_time:
            try:
                start_dt = datetime.fromisoformat(start_time)
                end_time = (start_dt + timedelta(minutes=30)).isoformat()
            except Exception:
                pass

        resolved_client_id = client_id or payload.get("client_id", "")
        now = datetime.now().isoformat()
        with get_conn() as cur:
            cur.execute("""
                INSERT INTO agent_appointments (
                    id, session_id, client_id,
                    patient_name, patient_phone, start_time, end_time,
                    appointment_type, status, doctor_name, reason,
                    booked_via, agent_notes, event_type, previous_datetime,
                    created_at, updated_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(id) DO UPDATE SET
                    session_id=EXCLUDED.session_id,
                    client_id=EXCLUDED.client_id,
                    start_time=EXCLUDED.start_time,
                    end_time=EXCLUDED.end_time,
                    status=EXCLUDED.status,
                    event_type=EXCLUDED.event_type,
                    previous_datetime=EXCLUDED.previous_datetime,
                    updated_at=EXCLUDED.updated_at
            """, (
                payload.get("id", ""),
                session_id or payload.get("session_id", ""),
                resolved_client_id,
                payload.get("patient_name", ""),
                payload.get("patient_phone", ""),
                start_time,
                end_time,
                payload.get("appointment_type", "consultation"),
                payload.get("status", "scheduled"),
                payload.get("doctor_name", ""),
                payload.get("reason", ""),
                payload.get("booked_via", "voice_assistant"),
                payload.get("agent_notes", ""),
                payload.get("event_type", ""),
                payload.get("previous_datetime", ""),
                payload.get("created_at", now),
                payload.get("updated_at", now),
            ))
        print(f"✅ [DB] Agent appointment saved: {payload.get('patient_name')} at {start_time}")
        return True
    except Exception as e:
        print(f"❌ [DB] Error saving agent appointment: {e}")
        return False


def get_agent_appointments(start_time_iso: str, client_id: str = None) -> list:
    """
    Get appointments from SECONDARY agent_appointments table matching start_time.
    Used as fallback when the primary appointments table is unreachable.
    """
    try:
        base_timestamp = start_time_iso.split('+')[0] if '+' in start_time_iso else start_time_iso
        ist_timestamp  = base_timestamp + '+05:30'
        with get_conn() as cur:
            if client_id:
                cur.execute("""
                    SELECT * FROM agent_appointments
                    WHERE (start_time = %s OR start_time = %s OR start_time LIKE %s)
                    AND status NOT IN ('cancelled', 'rescheduled')
                    AND client_id = %s
                """, (start_time_iso, ist_timestamp, base_timestamp + '%', client_id))
            else:
                cur.execute("""
                    SELECT * FROM agent_appointments
                    WHERE (start_time = %s OR start_time = %s OR start_time LIKE %s)
                    AND status NOT IN ('cancelled', 'rescheduled')
                """, (start_time_iso, ist_timestamp, base_timestamp + '%'))
            rows = cur.fetchall()
        return [dict(row) for row in rows]
    except Exception as e:
        print(f"❌ [DB] Error querying agent_appointments: {e}")
        return []


def verify_appointment_for_cancellation(
    patient_name: str,
    patient_phone: str,
    target_date: str,
    target_time: str,
) -> dict:
    """
    Verify that an appointment exists for cancellation/rescheduling.
    Checks PRIMARY first; falls back to SECONDARY on DB error OR not found.
    Returns dict with keys: 'exists', 'appointment', 'message', 'source'.
    """
    result = {"exists": False, "appointment": None, "message": "", "source": None}

    iso_timestamp = _parse_to_ist_iso(target_date, target_time)
    if iso_timestamp is None:
        result["message"] = f"Could not parse datetime: {target_date} {target_time}"
        return result

    phone_digits  = re.sub(r"\D", "", patient_phone or "")
    phone_last10  = phone_digits[-10:] if len(phone_digits) >= 10 else phone_digits
    base_ts       = iso_timestamp.split("+")[0]

    print(f"🔍 [DB] Verifying appointment for {patient_name} (…{phone_last10}) at {iso_timestamp}")

    def _query_table(table: str, cur):
        cur.execute(f"""
            SELECT * FROM {table}
            WHERE LOWER(patient_name) LIKE LOWER(%s)
            AND REGEXP_REPLACE(patient_phone, '[^0-9]', '', 'g') LIKE %s
            AND (start_time = %s OR start_time LIKE %s)
            AND status NOT IN ('cancelled', 'rescheduled')
            LIMIT 1
        """, (f"%{patient_name}%", f"%{phone_last10}",
              iso_timestamp, base_ts + "%"))
        return cur.fetchone()

    primary_error = False
    try:
        with get_conn() as cur:
            row = _query_table("appointments", cur)
        if row:
            result.update(exists=True, appointment=dict(row), source="primary",
                          message=f"Appointment found for {patient_name} on {target_date} at {target_time}")
            print(f"✅ [DB] Verified in primary: {result['message']}")
            return result
    except Exception as e:
        print(f"⚠️ [DB] Primary table error: {e}")
        primary_error = True

    try:
        with get_conn() as cur:
            row = _query_table("agent_appointments", cur)
        if row:
            result.update(exists=True, appointment=dict(row), source="secondary",
                          message=f"Appointment found (fallback) for {patient_name} on {target_date}")
            print(f"✅ [DB] Verified in secondary fallback: {result['message']}")
        else:
            result["message"] = (
                f"No appointment found for {patient_name} on {target_date} at {target_time}"
            )
            print(f"❌ [DB] {result['message']}")
    except Exception as e:
        print(f"❌ [DB] Secondary table error: {e}")
        result["message"] = f"Error verifying appointment: {e}"

    return result


def update_appointment_status(appointment_id: str, new_status: str) -> bool:
    """
    Update appointment status. Tries PRIMARY first; falls back to SECONDARY
    on DB error OR if the row does not exist in primary (rowcount == 0).
    """
    now = datetime.now().isoformat()
    try:
        with get_conn() as cur:
            cur.execute("""
                UPDATE appointments
                SET status = %s, updated_at = %s
                WHERE id = %s
            """, (new_status, now, appointment_id))
            rows_updated = cur.rowcount
        if rows_updated > 0:
            print(f"✅ [DB] appointments: {appointment_id} → {new_status}")
            return True
        print(f"⚠️ [DB] Primary: id not found in appointments. Trying secondary...")
    except Exception as e:
        print(f"⚠️ [DB] Primary update failed: {e}. Trying secondary...")

    try:
        with get_conn() as cur:
            cur.execute("""
                UPDATE agent_appointments
                SET status = %s, updated_at = %s
                WHERE id = %s
            """, (new_status, now, appointment_id))
            rows_updated = cur.rowcount
        if rows_updated > 0:
            print(f"✅ [DB] agent_appointments: {appointment_id} → {new_status}")
            return True
        print(f"❌ [DB] id not found in agent_appointments either.")
        return False
    except Exception as e:
        print(f"❌ [DB] Secondary update also failed: {e}")
        return False


def reschedule_appointment(appointment_id: str, new_date: str, new_time: str) -> bool:
    """
    Reschedule an appointment. Tries PRIMARY first; falls back to SECONDARY on error.
    """
    iso_start = _parse_to_ist_iso(new_date, new_time)
    if iso_start is None:
        print(f"❌ [DB] reschedule: could not parse '{new_date} {new_time}'")
        return False

    try:
        new_start_dt = datetime.fromisoformat(iso_start)
        iso_end = (new_start_dt + timedelta(minutes=30)).isoformat()
    except Exception:
        iso_end = ""

    now = datetime.now().isoformat()

    try:
        with get_conn() as cur:
            cur.execute("""
                UPDATE appointments
                SET start_time = %s, end_time = %s, status = 'rescheduled', updated_at = %s
                WHERE id = %s
            """, (iso_start, iso_end, now, appointment_id))
            rows_updated = cur.rowcount
        if rows_updated > 0:
            print(f"✅ [DB] appointments: {appointment_id} rescheduled to {iso_start}")
            return True
        print(f"⚠️ [DB] Primary: id not found in appointments. Trying secondary...")
    except Exception as e:
        print(f"⚠️ [DB] Primary reschedule failed: {e}. Trying secondary...")

    try:
        with get_conn() as cur:
            cur.execute("""
                UPDATE agent_appointments
                SET start_time = %s, end_time = %s, status = 'rescheduled', updated_at = %s
                WHERE id = %s
            """, (iso_start, iso_end, now, appointment_id))
            rows_updated = cur.rowcount
        if rows_updated > 0:
            print(f"✅ [DB] agent_appointments: {appointment_id} rescheduled to {iso_start}")
            return True
        print(f"❌ [DB] id not found in agent_appointments either.")
        return False
    except Exception as e:
        print(f"❌ [DB] Secondary reschedule also failed: {e}")
        return False


def _slot_dt_to_utc(slot_dt) -> datetime:
    """
    Ensure a datetime is UTC-aware. Accepts:
      - timezone-aware datetime (any tz)  → convert to UTC
      - naive datetime                    → assume IST, convert to UTC
    """
    _UTC = pytz.utc
    if slot_dt.tzinfo is None:
        slot_dt = _IST.localize(slot_dt)
    return slot_dt.astimezone(_UTC)


def _fetch_availability_blocks(client_id: str, date_utc_start: datetime, date_utc_end: datetime) -> list:
    """
    Fetch active availability blocks for a client that overlap [date_utc_start, date_utc_end).
    Returns list of dicts with keys: start_time, end_time, is_all_day.
    Timestamps from DB may be timezone-aware (UTC); we normalise them here.
    """
    _UTC = pytz.utc
    try:
        with get_conn() as cur:
            if client_id:
                cur.execute("""
                    SELECT start_time, end_time, is_all_day
                    FROM availability
                    WHERE status = 'active'
                    AND client_id = %s
                    AND start_time < %s
                    AND end_time > %s
                """, (client_id, date_utc_end, date_utc_start))
            else:
                cur.execute("""
                    SELECT start_time, end_time, is_all_day
                    FROM availability
                    WHERE status = 'active'
                    AND start_time < %s
                    AND end_time > %s
                """, (date_utc_end, date_utc_start))
            rows = cur.fetchall()

        blocks = []
        for row in rows:
            st = row["start_time"]
            et = row["end_time"]
            # Normalise to UTC-aware datetime
            if isinstance(st, datetime):
                if st.tzinfo is None:
                    st = _UTC.localize(st)
                else:
                    st = st.astimezone(_UTC)
            if isinstance(et, datetime):
                if et.tzinfo is None:
                    et = _UTC.localize(et)
                else:
                    et = et.astimezone(_UTC)
            blocks.append({"start_time": st, "end_time": et, "is_all_day": bool(row["is_all_day"])})
        return blocks
    except Exception as e:
        print(f"⚠️ [DB] _fetch_availability_blocks error: {e}")
        return []


def _is_slot_blocked_by_availability(slot_utc: datetime, blocks: list) -> bool:
    """
    Given a UTC-aware slot datetime and a list of availability blocks,
    return True if the slot falls within any block (meaning it's UNAVAILABLE).

    Block semantics:
      - is_all_day = True  → entire calendar day of block.start_time (IST date) is blocked
      - is_all_day = False → slot is blocked if start_time <= slot < end_time
    """
    slot_ist = slot_utc.astimezone(_IST)
    for block in blocks:
        if block["is_all_day"]:
            # Block the entire IST calendar date of the block's start
            block_date_ist = block["start_time"].astimezone(_IST).date()
            if slot_ist.date() == block_date_ist:
                return True
        else:
            # Strictly: start_time <= slot < end_time
            if block["start_time"] <= slot_utc < block["end_time"]:
                return True
    return False


def _is_slot_booked(target_date: str, target_time: str, client_id: str = None) -> bool:
    """
    Internal helper — checks the availability table.
    Returns True if the slot is blocked (unavailable), False if it's free.
    """
    iso_timestamp = _parse_to_ist_iso(target_date, target_time)
    if iso_timestamp is None:
        return False

    _UTC = pytz.utc
    try:
        # Build UTC-aware datetime for the slot
        slot_dt = datetime.fromisoformat(iso_timestamp)
        slot_utc = _slot_dt_to_utc(slot_dt)

        # Fetch blocks that overlap the target day (±1 day window to be safe with UTC offsets)
        day_start_utc = slot_utc.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(hours=6)
        day_end_utc   = day_start_utc + timedelta(hours=36)
        blocks = _fetch_availability_blocks(client_id, day_start_utc, day_end_utc)

        return _is_slot_blocked_by_availability(slot_utc, blocks)
    except Exception as e:
        print(f"⚠️ [DB] _is_slot_booked error: {e}")
        return False


def _get_fully_booked_slots(iso_timestamps: list, client_id: str = None) -> set:
    """
    Batch query — returns which of the given ISO start_times are blocked
    according to the availability table.
    """
    if not iso_timestamps:
        return set()

    _UTC = pytz.utc
    try:
        # Parse all slot datetimes to UTC
        slot_utcs = []
        for ts in iso_timestamps:
            try:
                dt = datetime.fromisoformat(ts)
                slot_utcs.append((ts, _slot_dt_to_utc(dt)))
            except Exception:
                pass

        if not slot_utcs:
            return set()

        # Determine overall window for a single DB round-trip
        all_utc = [s for _, s in slot_utcs]
        window_start = min(all_utc) - timedelta(hours=6)
        window_end   = max(all_utc) + timedelta(hours=6)

        blocks = _fetch_availability_blocks(client_id, window_start, window_end)

        blocked = set()
        for original_ts, slot_utc in slot_utcs:
            if _is_slot_blocked_by_availability(slot_utc, blocks):
                blocked.add(original_ts)
        return blocked
    except Exception as e:
        print(f"⚠️ [DB] _get_fully_booked_slots error: {e}")
        return set()


def get_next_available_slot(
    target_date: str,
    target_time: str,
    clinic_hours: dict = None,
    client_id: str = None,
) -> tuple:
    """Find the next available slot. Searches up to 14 days ahead using a single batch query."""
    if clinic_hours is None:
        clinic_hours = {
            "morning_start": 10, "morning_end": 13,
            "evening_start": 16, "evening_end": 19,
        }
    try:
        datetime_str = f"{target_date} {target_time}"
        naive_dt = None
        for fmt in ("%d %B %Y %I:%M %p", "%d %b %Y %I:%M %p"):
            try:
                naive_dt = datetime.strptime(datetime_str.replace(",", ""), fmt)
                break
            except ValueError:
                continue
        if naive_dt is None:
            return (None, None)

        current_dt = _IST.localize(naive_dt)

        # Collect all candidate slots across 2 working days
        all_slots = []
        valid_days = 0
        day_offset = 0
        
        while valid_days < 2 and day_offset < 14:
            check_dt = current_dt + timedelta(days=day_offset)
            if check_dt.weekday() == 6:
                day_offset += 1
                continue
                
            for hour in range(clinic_hours["morning_start"], clinic_hours["morning_end"]):
                for minute in [0, 30]:
                    slot_dt = check_dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
                    if day_offset == 0 and slot_dt <= current_dt:
                        continue
                    all_slots.append(slot_dt)
            for hour in range(clinic_hours["evening_start"], clinic_hours["evening_end"]):
                for minute in [0, 30]:
                    slot_dt = check_dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
                    if day_offset == 0 and slot_dt <= current_dt:
                        continue
                    all_slots.append(slot_dt)
                    
            valid_days += 1
            day_offset += 1

        if not all_slots:
            return (None, None)

        # One batch query instead of up to 140 individual round-trips
        iso_list = [s.isoformat() for s in all_slots]
        booked = _get_fully_booked_slots(iso_list, client_id)

        for slot_dt in all_slots:
            if slot_dt.isoformat() not in booked:
                return (slot_dt.strftime("%d %B %Y"), slot_dt.strftime("%I:%M %p"))
        return (None, None)
    except Exception as e:
        print(f"❌ [DB] get_next_available_slot error: {e}")
        return (None, None)


def get_previous_available_slot(
    target_date: str,
    target_time: str,
    clinic_hours: dict = None,
    client_id: str = None,
) -> tuple:
    """Find the previous available slot on the same day before the target time. Uses a single batch query."""
    if clinic_hours is None:
        clinic_hours = {
            "morning_start": 10, "morning_end": 13,
            "evening_start": 16, "evening_end": 19,
        }
    try:
        datetime_str = f"{target_date} {target_time}"
        naive_dt = None
        for fmt in ("%d %B %Y %I:%M %p", "%d %b %Y %I:%M %p"):
            try:
                naive_dt = datetime.strptime(datetime_str.replace(",", ""), fmt)
                break
            except ValueError:
                continue
        if naive_dt is None:
            return (None, None)

        target_dt = _IST.localize(naive_dt)
        now_dt    = datetime.now(_IST)
        if target_dt.weekday() == 6:
            return (None, None)

        all_slots = []
        for hour in range(clinic_hours["morning_start"], clinic_hours["morning_end"]):
            for minute in [0, 30]:
                all_slots.append(target_dt.replace(hour=hour, minute=minute, second=0, microsecond=0))
        for hour in range(clinic_hours["evening_start"], clinic_hours["evening_end"]):
            for minute in [0, 30]:
                all_slots.append(target_dt.replace(hour=hour, minute=minute, second=0, microsecond=0))

        all_slots = [s for s in all_slots if s < target_dt and s > now_dt]
        all_slots.sort(reverse=True)

        if not all_slots:
            return (None, None)

        # One batch query instead of per-slot round-trips
        iso_list = [s.isoformat() for s in all_slots]
        booked = _get_fully_booked_slots(iso_list, client_id)

        for slot_dt in all_slots:
            if slot_dt.isoformat() not in booked:
                return (slot_dt.strftime("%d %B %Y"), slot_dt.strftime("%I:%M %p"))
        return (None, None)
    except Exception as e:
        print(f"❌ [DB] get_previous_available_slot error: {e}")
        return (None, None)


def check_availability(target_date: str, target_time: str, client_id: str = None) -> dict:
    """
    Checks if a slot is available by querying the availability table.

    Logic:
      1. Parse the requested date+time to a UTC-aware datetime.
      2. Fetch all active availability blocks for the client covering that day.
      3. If any block has is_all_day=True and the IST date matches → UNAVAILABLE.
      4. If any block has start_time <= requested_utc < end_time → UNAVAILABLE.
      5. If unavailable, find next (and previous same-day) free slot.

    Returns:
        Dict with: 'available', 'next_date', 'next_time', 'prev_date', 'prev_time'
    """
    result = {"available": True, "next_date": None, "next_time": None,
              "prev_date": None, "prev_time": None}

    iso_timestamp = _parse_to_ist_iso(target_date, target_time)
    if iso_timestamp is None:
        print(f"⚠️ [DB] Could not parse '{target_date} {target_time}' — defaulting to available")
        return result

    print(f"🔍 [DB] check_availability (availability table): '{target_date} {target_time}' → {iso_timestamp} [client_id={client_id}]")

    try:
        _UTC = pytz.utc
        slot_dt = datetime.fromisoformat(iso_timestamp)
        slot_utc = _slot_dt_to_utc(slot_dt)

        # Fetch blocks covering a ±6-hour window around the slot to catch all UTC offsets
        window_start = slot_utc.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(hours=6)
        window_end   = window_start + timedelta(hours=36)
        blocks = _fetch_availability_blocks(client_id, window_start, window_end)

        if _is_slot_blocked_by_availability(slot_utc, blocks):
            print(f"❌ [DB] AVAILABILITY: {target_date} {target_time} is BLOCKED (UTC={slot_utc.isoformat()})")
            result["available"] = False
        else:
            print(f"✅ [DB] AVAILABILITY: {target_date} {target_time} is FREE (UTC={slot_utc.isoformat()})")

    except Exception as e:
        print(f"⚠️ [DB] check_availability error: {e} — defaulting to available")
        return result

    if not result["available"]:
        next_date, next_time = get_next_available_slot(target_date, target_time, client_id=client_id)
        if next_date:
            result["next_date"] = next_date
            result["next_time"] = next_time
            print(f"✅ [DB] Next available: {next_date} at {next_time}")

        prev_date, prev_time = get_previous_available_slot(target_date, target_time, client_id=client_id)
        if prev_date:
            result["prev_date"] = prev_date
            result["prev_time"] = prev_time
            print(f"✅ [DB] Prev available (same day): {prev_date} at {prev_time}")

    return result
