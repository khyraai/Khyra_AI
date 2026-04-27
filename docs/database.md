# Database — Technical Reference

> **Engine:** SQLite (WAL mode, `PRAGMA foreign_keys=ON`)  
> **File:** `.logs/app.db`  
> **Schema owner:** `src/database.py` — `init_db()` (idempotent `CREATE TABLE IF NOT EXISTS`)  
> **Last updated:** April 2026

---

## Table of Contents

1. [Overview](#1-overview)
2. [Configuration & Connection Model](#2-configuration--connection-model)
3. [Table: clients](#3-table-clients)
4. [Table: sessions](#4-table-sessions)
5. [Table: appointments](#5-table-appointments)
6. [Table: call_logs](#6-table-call_logs)
7. [Table: stt_events](#7-table-stt_events)
8. [Table: tts_events](#8-table-tts_events)
9. [Table: cancellations](#9-table-cancellations)
10. [Table: audit_log](#10-table-audit_log)
11. [Entity Relationships](#11-entity-relationships)
12. [Public API Reference](#12-public-api-reference)

---

## 1. Overview

The database is the **single source of truth for all runtime state** in the voice assistant system. It covers four distinct concerns:

| Concern | Tables |
|---|---|
| **Tenant registry** | `clients` |
| **Active call state** | `sessions` |
| **Business records** | `appointments`, `cancellations` |
| **Observability & cost** | `call_logs`, `stt_events`, `tts_events`, `audit_log` |

All writes go through plain SQLite connections opened with `check_same_thread=False` in WAL (Write-Ahead Logging) mode. This allows concurrent reads from the FastAPI HTTP endpoints while a WebSocket session writes. There is no ORM — all queries are raw SQL executed via Python's `sqlite3` module.

---

## 2. Configuration & Connection Model

| Setting | Value |
|---|---|
| **DB path** | `.logs/app.db` (relative to project root) |
| **Journal mode** | `WAL` — non-blocking concurrent reads |
| **Foreign keys** | `ON` (enforced per connection) |
| **Row factory** | `sqlite3.Row` — rows accessible as dicts |
| **Thread model** | Per-call connections (`_conn()` helper); no shared connection pool |
| **Initialisation** | `init_db()` called at server startup; also upserts all clients from `client_config.json` |

```python
# Every public function opens and closes its own connection
def _conn() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    return con
```

---

## 3. Table: `clients`

### Purpose

Acts as a **local registry mirror** of `src/client_config.json`. One row per clinic (tenant). Populated at startup via `upsert_client()` and kept in sync whenever `init_db()` is called. Serves as the authoritative in-database record of which clinics the system is configured for, enabling SQL joins and per-client filtering without needing to parse the JSON file at query time.

### What we are trying to achieve

- Provide a queryable, indexed record of every onboarded clinic so that call logs, appointments, and STT/TTS events can be filtered and grouped by tenant.
- Store the clinic profile (name, doctor, address, timings, fees) so it can be cross-referenced in analytics without hitting the config file at runtime.
- Track the `connection_id` used to scope appointment availability checks per clinic.

### Schema

| Column | Type | Nullable / Default | Description |
|---|---|---|---|
| `client_id` | `TEXT` | **PRIMARY KEY** | Unique tenant identifier (e.g. `"deepti_dental"`). Used throughout the system to associate every event with a clinic. |
| `did_number` | `TEXT` | UNIQUE, nullable | The DID (Direct Inward Dialing) phone number assigned to this clinic. Callers ring this number; the system routes the call to the correct client. |
| `clinic_name` | `TEXT` | nullable | Full display name of the clinic (e.g. `"Doctor Deepti's Dental and Orthodontic Centre"`). Used in agent prompts and reports. |
| `doctor_name` | `TEXT` | nullable | Full name of the primary doctor (e.g. `"Doctor Naga Deepti"`). Injected into agent prompts and appointment records. |
| `doctor_qualifications` | `TEXT` | nullable | Credentials string (e.g. `"MDS — Orthodontics and Dentofacial Orthopaedics"`). Used in enquiry responses. |
| `address` | `TEXT` | nullable | Physical clinic address. Provided to callers who ask for directions. |
| `timings` | `TEXT` | nullable | Free-text description of clinic hours (e.g. `"Monday to Saturday — 10:00 AM to 1:00 PM and 4:00 PM to 7:00 PM. Closed on Sunday."`). Enforced as a guardrail in the booking agent. |
| `doctor_mobile` | `TEXT` | nullable | Direct mobile number for the doctor. Used in emergency transfer scenarios. |
| `consultation_fee_min` | `INTEGER` | DEFAULT `0` | Minimum consultation fee in INR. Provided to callers asking about cost. |
| `consultation_fee_max` | `INTEGER` | DEFAULT `0` | Maximum consultation fee in INR. Upper bound for fee range given to callers. |
| `default_language` | `TEXT` | DEFAULT `'kn'` | Session language if none is detected from STT. `"kn"` for Kannada, `"en"` for English. |
| `emergency_transfer_number` | `TEXT` | nullable | Phone number to transfer the call to in emergency situations. |
| `connection_id` | `TEXT` | nullable | Logical identifier used to scope appointment availability checks (e.g. `"deepti_dental"`). May differ from `client_id` if the clinic has multiple lines. |
| `created_at` | `TEXT` | DEFAULT `datetime('now')` | ISO-8601 timestamp of when the row was first inserted. |
| `updated_at` | `TEXT` | DEFAULT `datetime('now')` | ISO-8601 timestamp of the last upsert. Updated every time `upsert_client()` runs at startup. |

### Indexes

| Index | Columns | Purpose |
|---|---|---|
| PRIMARY KEY | `client_id` | Fast lookups by tenant ID across all joined queries. |
| UNIQUE | `did_number` | Prevents duplicate DID registrations; enables O(1) lookup when a call arrives on a DID. |

### Lifecycle

- **Created / Updated:** `upsert_client(cfg)` — called during `init_db()` for every entry in `client_config.json`. Uses `INSERT … ON CONFLICT DO UPDATE` so re-runs are always safe.
- **Never deleted** at runtime — client rows are permanent for the lifetime of the deployment.

---

## 4. Table: `sessions`

### Purpose

Stores the **live conversation state and memory** for every active (or recently active) call. This is the database-backed replacement for the original in-memory `SessionStore` in `utils.py`. One row per unique `session_id`; the row is upserted on every conversation turn.

### What we are trying to achieve

- Survive server restarts and dropped WebSocket connections without losing conversation progress. On reconnect, the session is restored from this table so the caller does not have to start over.
- Allow the system to pick up where it left off: the patient's name, appointment reason, and collected slot fields are all preserved in the `state` JSON.
- Provide a lightweight conversation history (`memory`) so the LLM has context about the last several turns without needing to re-process the full call.

### Schema

| Column | Type | Nullable / Default | Description |
|---|---|---|---|
| `session_id` | `TEXT` | **PRIMARY KEY** | Unique call session identifier. Generated by `main.py` per WebSocket connection (e.g. a UUID or Vobiz call SID). |
| `client_id` | `TEXT` | nullable | The tenant (`client_id`) associated with this session. Extracted from `state["client_id"]` at save time. Enables per-client session filtering. |
| `state` | `TEXT` | nullable | JSON-serialised Python dict containing the full slot-filling state: `name`, `age`, `date`, `time`, `reason`, `phone`, `availability_checked`, `confirmation_pending`, etc. See the Agent Architecture doc for the complete field list. |
| `memory` | `TEXT` | nullable | JSON-serialised list of `{"role": "user"/"assistant", "content": "..."}` dicts representing the last ≤12 conversation turns. Passed directly to the LLM on the next turn. |
| `updated_at` | `TEXT` | DEFAULT `datetime('now')` | ISO-8601 timestamp of the last save. Used to identify stale sessions for cleanup. |

### Lifecycle

- **Created / Updated:** `SessionStore.save_session(session_id, state, memory)` — called after every conversation turn via `asyncio.to_thread`.
- **Read:** `SessionStore.load_session(session_id)` — called at session start to check for a resumable session.
- **Deleted:** `SessionStore.clear_session(session_id)` — called via `POST /session/clear` endpoint.

---

## 5. Table: `appointments`

### Purpose

The **primary business record** for the system. One row per appointment booking. Mirrors the Supabase `appointments` table schema exactly so rows can be synced bi-directionally. Used locally for availability checking (slot conflict detection) and as a fallback when Supabase is unavailable.

### What we are trying to achieve

- Give the booking agents a source of truth to check whether a requested time slot is already taken before confirming it to the caller.
- Maintain a local record of every appointment so the clinic can see upcoming bookings without relying on an external service.
- Support status lifecycle: `confirmed` → `cancelled` or `rescheduled` — enabling the system to correctly treat cancelled slots as free again.
- Provide the data needed to build appointment reports and dashboards.

### Schema

| Column | Type | Nullable / Default | Description |
|---|---|---|---|
| `id` | `TEXT` | **PRIMARY KEY** | UUID appointment identifier. Generated by the system at booking time (`uuid.uuid4()`). Also used as the Google Calendar event reference. |
| `connection_id` | `TEXT` | nullable | Scopes the appointment to a specific clinic (`connection_id` from `clients`). All availability checks filter by this to prevent cross-clinic slot collisions. |
| `google_event_id` | `TEXT` | nullable | Google Calendar event ID returned by the N8N webhook after the event is created in Google Calendar. Populated on sync; empty until then. |
| `patient_name` | `TEXT` | nullable | Full name of the patient as captured by the booking agent. |
| `patient_phone` | `TEXT` | nullable | Caller's phone number. Used to identify the patient and for follow-up communication. |
| `start_time` | `TEXT` | nullable | ISO-8601 datetime with IST timezone offset (e.g. `"2026-04-25T10:00:00+05:30"`). The primary slot identifier for availability checking. |
| `end_time` | `TEXT` | nullable | ISO-8601 datetime of appointment end. Currently set to 30 minutes after `start_time`. |
| `appointment_type` | `TEXT` | DEFAULT `'consultation'` | The procedure or visit type (e.g. `"consultation"`, `"root canal"`, `"braces"`). Reflects the triage-resolved reason, not the original patient request. |
| `status` | `TEXT` | DEFAULT `'confirmed'` | Current lifecycle state: `confirmed`, `cancelled`, or `rescheduled`. Availability checks exclude rows where status is `cancelled` or `rescheduled`. |
| `doctor_name` | `TEXT` | nullable | Doctor assigned to the appointment. Resolved from `client_config` at booking time. |
| `reason` | `TEXT` | nullable | Free-text reason as stated by the patient. May differ from `appointment_type` (e.g. reason = `"tooth pain"`, type = `"consultation"`). |
| `booked_via` | `TEXT` | DEFAULT `'agent'` | Channel through which the appointment was booked. Always `"agent"` for calls routed through this system. |
| `agent_notes` | `TEXT` | nullable | Metadata string appended by the booking agent: `"lang=kn session=<session_id>"`. Used for debugging and audit. |
| `created_at` | `TEXT` | DEFAULT `datetime('now')` | ISO-8601 timestamp of when the booking was made. |
| `updated_at` | `TEXT` | DEFAULT `datetime('now')` | ISO-8601 timestamp of the last status change (cancel, reschedule). |

### Indexes

| Index | Columns | Purpose |
|---|---|---|
| `idx_appt_start_time` | `start_time` | Fast slot-conflict lookup: `WHERE start_time = ? AND status NOT IN (...)`. Core of the availability check query. |
| `idx_appt_connection_id` | `connection_id` | Fast per-clinic listing: `WHERE connection_id = ?`. Used by `list_appointments()` and all reporting queries. |
| `idx_appt_status` | `status` | Efficient filtering of active vs. cancelled appointments. |

### Lifecycle

- **Created:** `create_appointment()` or `insert_appointment()` — triggered when the booking agent sets `done = true` and `main.py` fires the N8N webhook.
- **Updated:** `cancel_or_reschedule_appointment()` — sets `status` to `cancelled` or `rescheduled` and updates `start_time` for reschedules.
- **Read:** `check_availability()` — queries `start_time` + `status` for conflict detection. `list_appointments()` — returns all rows for a clinic sorted by `start_time`.

---

## 6. Table: `call_logs`

### Purpose

Records **one row per call** from start to finish. Captures the full lifecycle of a phone call: when it started, how long it lasted, what language was used, what outcome was reached, and the total cost (STT + TTS + LLM) incurred.

### What we are trying to achieve

- Give the operations team a per-call record they can review to understand call volume, duration trends, and success rates.
- Track per-call AI costs (STT, TTS, LLM individually) to enable billing reconciliation and cost-per-call reporting.
- Link a call to its resulting appointment (via `appointment_id`) so the booking conversion rate can be calculated.
- Provide the raw data for daily/weekly cost dashboards in `src/live.html`.

### Schema

| Column | Type | Nullable / Default | Description |
|---|---|---|---|
| `id` | `INTEGER` | **PRIMARY KEY AUTOINCREMENT** | Internal surrogate key. |
| `session_id` | `TEXT` | UNIQUE, nullable | The WebSocket session identifier. Links this call log to `sessions`, `stt_events`, `tts_events`, and `audit_log` rows for the same call. |
| `client_id` | `TEXT` | nullable | The clinic/tenant this call belongs to. Enables per-client cost and volume reporting. |
| `did_number` | `TEXT` | nullable | The DID number the caller dialled. Useful for verifying call routing and per-DID analytics. |
| `caller_phone` | `TEXT` | nullable | The caller's phone number. Used for identifying repeat callers and linking to patient records. |
| `call_start` | `TEXT` | nullable | ISO-8601 timestamp when the WebSocket session was opened. Written by `log_call_start()`. |
| `call_end` | `TEXT` | nullable | ISO-8601 timestamp when the session was closed. Written by `log_call_end()`. |
| `duration_sec` | `REAL` | DEFAULT `0` | Call duration in seconds, computed as `call_end − call_start`. Used for average handle time (AHT) metrics. |
| `language` | `TEXT` | nullable | Session language locked during the call (`"kn"` or `"en"`). Populated at call end. |
| `outcome` | `TEXT` | nullable | Final call result. Examples: `"appointment_booked"`, `"cancelled"`, `"enquiry"`, `"emergency_transfer"`, `"abandoned"`. |
| `appointment_id` | `TEXT` | nullable | UUID of the appointment created during this call (if any). Foreign key to `appointments.id`. Null for enquiry-only or abandoned calls. |
| `stt_cost_inr` | `REAL` | DEFAULT `0` | Estimated STT cost in Indian Rupees for all transcription calls in this session. |
| `tts_cost_inr` | `REAL` | DEFAULT `0` | Estimated TTS cost in INR for all synthesis calls in this session. |
| `llm_cost_inr` | `REAL` | DEFAULT `0` | Estimated LLM cost in INR for all agent LLM calls in this session. |
| `total_cost_inr` | `REAL` | DEFAULT `0` | Sum of `stt_cost_inr + tts_cost_inr + llm_cost_inr`. The headline cost-per-call figure. |
| `created_at` | `TEXT` | DEFAULT `datetime('now')` | ISO-8601 timestamp of row creation (same as `call_start`). |

### Indexes

| Index | Columns | Purpose |
|---|---|---|
| `idx_call_logs_client_id` | `client_id` | Per-tenant call volume and cost queries. |
| `idx_call_logs_session_id` | `session_id` | Fast lookup when `log_call_end()` needs to read `call_start` to compute duration. |

### Lifecycle

- **Created:** `log_call_start(session_id, client_id, ...)` — called at the moment the WebSocket connection is established.
- **Updated:** `log_call_end(session_id, outcome, cost_dict, ...)` — called when the session closes, filling in `call_end`, `duration_sec`, `outcome`, `appointment_id`, and all cost columns.

---

## 7. Table: `stt_events`

### Purpose

Records **one row per STT (Speech-to-Text) transcription request**. Each time the system sends an audio chunk to a provider (Sarvam, Deepgram, Groq, etc.) and receives a transcript, a row is written here.

### What we are trying to achieve

- Track the reliability of each STT provider: success rate, fallback rate, retry counts, and error types.
- Measure per-request latency to identify performance regressions or provider degradation.
- Accumulate cost estimates per session and per client so total STT spend can be attributed accurately.
- Provide the raw data for the live STT operations dashboard at `src/live.html`.

### Schema

| Column | Type | Nullable / Default | Description |
|---|---|---|---|
| `id` | `INTEGER` | **PRIMARY KEY AUTOINCREMENT** | Internal surrogate key. |
| `session_id` | `TEXT` | nullable | The call session this transcription belongs to. Links to `call_logs.session_id`. |
| `client_id` | `TEXT` | nullable | Tenant identifier. Enables per-client STT reliability and cost breakdowns. |
| `ts` | `REAL` | nullable | Unix epoch timestamp (float) of when the STT request was initiated. Used for precise ordering and latency graphs. |
| `provider` | `TEXT` | nullable | Which STT provider handled the request (e.g. `"sarvam"`, `"deepgram"`, `"groq"`, `"openai"`). Populated even on failure, so provider error rates can be tracked. |
| `audio_duration_sec` | `REAL` | DEFAULT `0` | Duration of the audio chunk submitted for transcription, in seconds. Used for cost estimation (most providers charge per second). |
| `transcript` | `TEXT` | nullable | The text returned by the STT provider. Empty string on failure. Stored for debugging transcript quality issues. |
| `language_code` | `TEXT` | nullable | Language code detected or used by the STT provider (e.g. `"kn-IN"`, `"en-IN"`). Populated from `language_code` or `detected_lang` in the event dict. |
| `success` | `INTEGER` | DEFAULT `0` | Boolean flag (`0` / `1`). `1` if the request returned a usable transcript; `0` if it failed or returned empty. |
| `fallback_used` | `INTEGER` | DEFAULT `0` | Boolean flag (`0` / `1`). `1` if a fallback provider was used after the primary provider failed. |
| `retry_count` | `INTEGER` | DEFAULT `0` | Number of retry attempts before the final result. Used to track transient failure rates per provider. |
| `total_latency_ms` | `REAL` | DEFAULT `0` | End-to-end latency in milliseconds from request send to transcript received. |
| `estimated_cost_inr` | `REAL` | DEFAULT `0` | Estimated cost for this single transcription call in INR, calculated from `audio_duration_sec` × provider rate. |
| `error_type` | `TEXT` | nullable | Short error classifier if `success = 0` (e.g. `"timeout"`, `"rate_limit"`, `"empty_transcript"`, `"api_error"`). Null on success. |
| `created_at` | `TEXT` | DEFAULT `datetime('now')` | ISO-8601 timestamp of row insertion. |

### Indexes

| Index | Columns | Purpose |
|---|---|---|
| `idx_stt_session` | `session_id` | Retrieve all STT events for a call (e.g. to sum costs for `log_call_end`). |
| `idx_stt_client` | `client_id` | Per-tenant STT cost and reliability aggregations. |

### Lifecycle

- **Created:** `log_stt_event(event_dict)` — called by `src/stt/stt_core.py` after every transcription attempt, whether successful or not.
- **Never updated or deleted** — each row is an immutable telemetry record.

---

## 8. Table: `tts_events`

### Purpose

Records **one row per TTS (Text-to-Speech) synthesis request**. Each time the system sends text to a provider (Cartesia, Sarvam TTS, ElevenLabs) and receives audio, a row is written here.

### What we are trying to achieve

- Mirror the observability goals of `stt_events` for the synthesis side: provider reliability, latency, fallback frequency, and cost.
- Track the `char_count` of every synthesis job as the primary cost driver for TTS (providers charge per character).
- Distinguish between `collect` (full audio buffered) and `stream` (audio streamed in real time) modes to understand latency differences.
- Feed the live TTS ops dashboard in `src/live.html`.

### Schema

| Column | Type | Nullable / Default | Description |
|---|---|---|---|
| `id` | `INTEGER` | **PRIMARY KEY AUTOINCREMENT** | Internal surrogate key. |
| `session_id` | `TEXT` | nullable | The call session this synthesis belongs to. Links to `call_logs.session_id`. |
| `client_id` | `TEXT` | nullable | Tenant identifier. Enables per-client TTS cost and reliability breakdowns. |
| `ts` | `REAL` | nullable | Unix epoch timestamp (float) of when the TTS request was initiated. |
| `provider` | `TEXT` | nullable | Which TTS provider handled the request (e.g. `"cartesia"`, `"sarvam"`, `"elevenlabs"`). |
| `char_count` | `INTEGER` | DEFAULT `0` | Number of characters in the text submitted for synthesis. The primary cost unit for TTS billing. |
| `language` | `TEXT` | nullable | Language of the synthesised text (e.g. `"kn"`, `"en"`). Used to track language distribution across calls. |
| `mode` | `TEXT` | nullable | Synthesis mode: `"collect"` (full audio returned before playback) or `"stream"` (audio piped to caller in real time). |
| `success` | `INTEGER` | DEFAULT `0` | Boolean flag (`0` / `1`). `1` if the provider returned audio bytes; `0` on failure. |
| `fallback_used` | `INTEGER` | DEFAULT `0` | Boolean flag (`0` / `1`). `1` if a fallback provider was used after the primary failed. |
| `retry_count` | `INTEGER` | DEFAULT `0` | Number of retry attempts before the final result. |
| `total_latency_ms` | `REAL` | DEFAULT `0` | End-to-end latency in milliseconds from request send to first audio byte received. |
| `estimated_cost_inr` | `REAL` | DEFAULT `0` | Estimated cost for this synthesis call in INR, calculated from `char_count` × provider rate. |
| `error_type` | `TEXT` | nullable | Short error classifier if `success = 0` (e.g. `"timeout"`, `"rate_limit"`, `"ws_error"`). Null on success. |
| `created_at` | `TEXT` | DEFAULT `datetime('now')` | ISO-8601 timestamp of row insertion. |

### Indexes

| Index | Columns | Purpose |
|---|---|---|
| `idx_tts_session` | `session_id` | Retrieve all TTS events for a call to sum costs. |
| `idx_tts_client` | `client_id` | Per-tenant TTS cost and reliability aggregations. |

### Lifecycle

- **Created:** `log_tts_event(event_dict)` — called by `src/tts/tts_core.py` after every synthesis attempt.
- **Never updated or deleted** — each row is an immutable telemetry record.

---

## 9. Table: `cancellations`

### Purpose

An **append-only audit trail** for every appointment cancellation and reschedule action. Complements the status change on the `appointments` row by preserving the full before/after snapshot at the time of the action.

### What we are trying to achieve

- Retain a permanent record of why and when each appointment was cancelled or rescheduled, even if the `appointments` row is later modified again.
- Allow support staff to reconstruct the history of an appointment: what time it was originally booked for, what it was changed to, and which session/caller made the change.
- Provide the raw data for cancellation-rate and reschedule-rate metrics.

### Schema

| Column | Type | Nullable / Default | Description |
|---|---|---|---|
| `id` | `INTEGER` | **PRIMARY KEY AUTOINCREMENT** | Internal surrogate key. |
| `appointment_id` | `TEXT` | nullable | UUID of the appointment that was cancelled or rescheduled. Foreign key to `appointments.id`. |
| `session_id` | `TEXT` | nullable | The call session during which the action was taken. Links to `call_logs.session_id` for cross-referencing. |
| `client_id` | `TEXT` | nullable | Tenant identifier. Enables per-clinic cancellation rate reporting. |
| `action` | `TEXT` | nullable | The action taken: `"cancelled"` or `"rescheduled"`. Matches the new `status` written to `appointments`. |
| `old_start_time` | `TEXT` | nullable | The appointment's `start_time` before the action was taken. Preserved here even if the `appointments` row is later updated. |
| `new_start_time` | `TEXT` | nullable | The new `start_time` for rescheduled appointments. Empty for pure cancellations. |
| `reason` | `TEXT` | nullable | Reason given by the patient for cancelling or rescheduling (as captured by Agent-3). |
| `created_at` | `TEXT` | DEFAULT `datetime('now')` | ISO-8601 timestamp of when the cancellation/reschedule was recorded. |

### Lifecycle

- **Created:** `cancel_or_reschedule_appointment(appointment_id, action, ...)` — a row is inserted here atomically alongside the `UPDATE` on the `appointments` table, in the same database transaction.
- **Never updated or deleted** — append-only by design.

---

## 10. Table: `audit_log`

### Purpose

A **generic, append-only event log** for any significant state change in the system. While `cancellations` is domain-specific to appointment actions, `audit_log` captures all entity mutation events in a uniform structure — appointment creation, cancellation, reschedule, and any future event types added to the system.

### What we are trying to achieve

- Provide a tamper-evident trail of changes for compliance, debugging, and support escalations.
- Record the `old_value` and `new_value` of any change so that disputes can be resolved without guessing what the state was before an action.
- Serve as a universal fallback audit mechanism: any component can call `append_audit()` with any event type without needing a dedicated table.

### Schema

| Column | Type | Nullable / Default | Description |
|---|---|---|---|
| `id` | `INTEGER` | **PRIMARY KEY AUTOINCREMENT** | Internal surrogate key. |
| `session_id` | `TEXT` | nullable | The call session during which the event occurred. Links to `call_logs.session_id`. |
| `client_id` | `TEXT` | nullable | Tenant identifier. Enables per-clinic audit filtering. |
| `event_type` | `TEXT` | nullable | The type of event recorded. Current values: `"appointment_created"`, `"cancelled"`, `"rescheduled"`. Extensible. |
| `entity_type` | `TEXT` | nullable | The kind of object affected. Current value: `"appointment"`. Extensible to any future entity. |
| `entity_id` | `TEXT` | nullable | The primary key of the affected entity (e.g. the appointment UUID). |
| `old_value` | `TEXT` | nullable | JSON-serialised snapshot of the entity's relevant fields *before* the change (e.g. `{"status": "confirmed", "start_time": "..."}`). Empty string for creation events. |
| `new_value` | `TEXT` | nullable | JSON-serialised snapshot of the entity's relevant fields *after* the change (e.g. `{"status": "cancelled", "start_time": "..."}`). |
| `created_at` | `TEXT` | DEFAULT `datetime('now')` | ISO-8601 timestamp of when the event was recorded. |

### Indexes

| Index | Columns | Purpose |
|---|---|---|
| `idx_audit_session` | `session_id` | Retrieve the full audit trail for a single call. |

### Lifecycle

- **Created:** `append_audit(session_id, event_type, entity_type, entity_id, old_value, new_value, client_id)` — called from:
  - `create_appointment()` → event `"appointment_created"`
  - `cancel_or_reschedule_appointment()` → event `"cancelled"` or `"rescheduled"`
- **Never updated or deleted** — append-only by design.

---

## 11. Entity Relationships

```
clients
  │
  ├── client_id ──────────────────────────────────────────────────────────┐
  │                                                                        │
  ├── connection_id ─────────────────────────────────┐                    │
  │                                                  │                    │
  ▼                                                  ▼                    │
appointments                                  (scoping filter             │
  │  id ──────────────┐                       on appointments)            │
  │  connection_id    │                                                    │
  │                   │                                                    │
  ▼                   ▼                                                    │
cancellations    call_logs                                                 │
  appointment_id   session_id ──────────────────────────────────────────┐ │
                   client_id ───────────────────────────────────────────┘─┘
                   appointment_id ──────────────────► appointments.id
                        │
             ┌──────────┴──────────┐
             ▼                     ▼
         stt_events            tts_events
           session_id            session_id
           client_id             client_id
             │
             └──────────────────► audit_log
                                    session_id
                                    client_id
                                    entity_id → appointments.id
```

**Key relationships:**

- `session_id` is the **horizontal key** that links `sessions`, `call_logs`, `stt_events`, `tts_events`, and `audit_log` rows belonging to the same call.
- `client_id` is the **vertical key** that partitions all tables by tenant.
- `appointments.id` is referenced by `cancellations.appointment_id`, `call_logs.appointment_id`, and `audit_log.entity_id`.
- `appointments.connection_id` links to `clients.connection_id` (not a formal FK; enforced by application logic).

---

## 12. Public API Reference

All functions are in `src/database.py`.

| Function | Table(s) | Description |
|---|---|---|
| `init_db()` | All | Creates all tables (idempotent). Upserts clients from `client_config.json`. |
| `upsert_client(cfg)` | `clients` | Insert or update a client row from a config dict. |
| `SessionStore.save_session(session_id, state, memory)` | `sessions` | Persist call state and memory after each turn. |
| `SessionStore.load_session(session_id)` | `sessions` | Retrieve `(state, memory)` for a session, or `(None, None)`. |
| `SessionStore.clear_session(session_id)` | `sessions` | Delete a session row. |
| `check_availability(target_date, target_time, connection_id)` | `appointments` | Return `True` if the slot is free, `False` if booked. |
| `insert_appointment(row)` | `appointments` | Insert a raw appointment dict. Returns `id`. |
| `list_appointments(connection_id)` | `appointments` | Return all appointments for a clinic ordered by `start_time`. |
| `create_appointment(session_id, client_id, state_dict, ...)` | `appointments`, `audit_log` | Create an appointment from agent state. Returns `appointment_id`. |
| `cancel_or_reschedule_appointment(appointment_id, action, ...)` | `appointments`, `cancellations`, `audit_log` | Update appointment status and write a cancellation record. |
| `log_call_start(session_id, client_id, ...)` | `call_logs` | Open a call log row. Returns `id`. |
| `log_call_end(session_id, outcome, cost_dict, ...)` | `call_logs` | Close the call log with duration, outcome, and costs. |
| `log_stt_event(event_dict)` | `stt_events` | Append one STT telemetry row. |
| `log_tts_event(event_dict)` | `tts_events` | Append one TTS telemetry row. |
| `append_audit(session_id, event_type, ...)` | `audit_log` | Append one audit event. |
