# Agent Architecture — Technical Reference

> **Component:** `src/agent1.py`, `src/agent2_kn.py`, `src/agent2_en.py`, `src/agent3_kn.py`, `src/agent3_en.py`, routing in `src/main.py`  
> **Last updated:** April 2026

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Agent Directory](#2-agent-directory)
3. [Agent-1 — Intent Router](#3-agent-1--intent-router)
4. [Agent-2 — Conversation Executor](#4-agent-2--conversation-executor)
5. [Agent-3 — Cancel / Reschedule Executor](#5-agent-3--cancelreschedule-executor)
6. [Routing Logic (main.py)](#6-routing-logic-mainpy)
7. [Data Flow — What Goes In, What Comes Out](#7-data-flow--what-goes-in-what-comes-out)
8. [Shared State Object](#8-shared-state-object)
9. [Context Window Management](#9-context-window-management)
10. [Client Differentiation & Multi-Tenancy](#10-client-differentiation--multi-tenancy)
11. [Concurrency Model](#11-concurrency-model)
12. [Availability Check Tool](#12-availability-check-tool)
13. [N8N Webhook Integration](#13-n8n-webhook-integration)
14. [Fallback & Error Handling](#14-fallback--error-handling)
15. [Security Model](#15-security-model)
16. [Environment Variable Reference](#16-environment-variable-reference)

---

## 1. Architecture Overview

```
Caller (phone / browser mic)
         │
         │  Audio → STT → user_text
         ▼
┌──────────────────────────────────────────────┐
│   main.py — WebSocket handler                │
│                                              │
│  ┌────────────────────────────────────────┐  │
│  │  TURN 1 (first utterance)              │  │
│  │                                        │  │
│  │   run_agent1(user_text, memory)        │  │
│  │       │                               │  │
│  │       ├─ intent = "greeting"          │  │  → respond directly, no language lock
│  │       ├─ intent = "system_check"      │  │  → respond directly, no language lock
│  │       ├─ intent = "emergency"         │  │  → TRANSFER_CALL + close websocket
│  │       ├─ intent = "cancel_reschedule" │  │  → hand off to Agent-3
│  │       └─ intent = "appointment"       │  │  → hand off to Agent-2
│  │               or  "enquiry"           │  │
│  └────────────────────────────────────────┘  │
│                                              │
│  ┌────────────────────────────────────────┐  │
│  │  TURN 2+ (subsequent utterances)       │  │
│  │                                        │  │
│  │  language locked from STT detection    │  │
│  │       │                               │  │
│  │       ├─ in_agent3 = True → Agent-3   │  │
│  │       └─ otherwise    → Agent-2        │  │
│  └────────────────────────────────────────┘  │
└──────────────────────────────────────────────┘
         │
         │  response_text  →  TTS  →  audio to caller
```

**Key design principles:**

- **Agent-1 fires only once per session** — on the first meaningful turn to classify intent and detect language.
- **Language is locked after the first turn** — subsequent turns use STT-detected language, not a second Agent-1 call.
- **Routing is fully centralized in `main.py`** — no agent calls another agent directly.
- **Each agent is a pure async function** — no global state, no side-effects beyond returning `(response, state, parsed)`.

---

## 2. Agent Directory

| File | Agent | Role | Language |
|---|---|---|---|
| `src/agent1.py` | Agent-1 | Intent Router | Language-agnostic |
| `src/agent2_kn.py` | Agent-2 KN | Conversation Executor | Kannada |
| `src/agent2_en.py` | Agent-2 EN | Conversation Executor | English |
| `src/agent3_kn.py` | Agent-3 KN | Cancel / Reschedule Executor | Kannada |
| `src/agent3_en.py` | Agent-3 EN | Cancel / Reschedule Executor | English |

All agents call the shared `LLMPool` in `src/llm.py`. No agent calls another agent.

---

## 3. Agent-1 — Intent Router

### Responsibility

Agent-1 is the **coarse-grained router**. Its only job is to classify the first user utterance into an intent and detect the session language. It does not book appointments, answer questions, or manage state.

### Function Signature

```python
# agent1.py
async def run_agent1(user_text: str, memory: list, groq_client) -> dict
```

### Data Received

| Input | Type | Source |
|---|---|---|
| `user_text` | `str` | Transcript from STT |
| `memory` | `list[dict]` | Conversation history (role/content pairs) |
| `groq_client` | `LLMPool` | Shared LLM pool instance from `main.py` |

### Data Returned

```json
{
  "intent": "greeting | appointment | enquiry | emergency | cancel_reschedule | system_check",
  "context": {
    "treatment": "<procedure name or empty string>",
    "query_type": "price | timing | general | none"
  },
  "summary": "<short English summary of the input>",
  "response": "<Kannada greeting text, or empty string>",
  "language": "kn | en | unknown"
}
```

Emergency-only override format:
```json
{
  "intent": "emergency",
  "confidence": 0.95,
  "action": "TRANSFER_CALL",
  "response": "ಡಾಕ್ಟರ್ ಗೆ ಕನೆಕ್ಟ್ ಮಾಡ್ತೀವಿ ಒಂದು ನಿಮಿಷ.",
  "metadata": {
    "reason": "<reason for emergency>",
    "transfer_target": "+918660033297"
  }
}
```

### Intent Classification Rules

| Intent | Trigger Condition |
|---|---|
| `greeting` | Input contains ONLY greeting words (hi, hello, namaskara) with NO request or content |
| `appointment` | Any mention of booking, visiting, pain, name introduction, or time/slot request |
| `enquiry` | Questions about cost, timing, doctor info, or general clinic information |
| `emergency` | Severe / unbearable pain, bleeding, accident, urgent help |
| `cancel_reschedule` | Explicit mention of cancelling or rescheduling |
| `system_check` | Audio/connection test phrases ("can you hear me", "are you there", "hello??") |

**Fallback rule:** When unsure → `enquiry`. Never falls back to `greeting`.

### Language Detection Rules

| Condition | Detected Language |
|---|---|
| Input contains ONLY greeting words | `unknown` — greetings do not lock session language |
| Kannada script or Kanglish words (beku, madbeku, eshtu) | `kn` |
| Predominantly English words and structure | `en` |
| Ambiguous / empty | `kn` (safe default for the clinic's primary market) |

### LLM Parameters

| Parameter | Value |
|---|---|
| Model | `LLM_MODEL` env var (default: `llama-3.3-70b-versatile`) |
| `max_tokens` | 200 |
| `temperature` | 0.1 (near-deterministic routing) |
| `response_format` | `json_object` |
| Timeout | 8 seconds |

### What Agent-1 Does NOT Do

- Does not fill any appointment slots.
- Does not generate a response for `appointment`, `enquiry`, or `cancel_reschedule` — those are empty strings.
- Does not lock the session language (that is done by `main.py` after reading the returned `language` field).
- Does not maintain any state.

---

## 4. Agent-2 — Conversation Executor

Two language variants share the same behavioral contract:

| Variant | File | Called when |
|---|---|---|
| `agent2_kn` | `src/agent2_kn.py` | `session_language == "kn"` |
| `agent2_en` | `src/agent2_en.py` | `session_language == "en"` |

### Responsibility

Agent-2 handles all **appointment booking** and **clinic enquiry** turns. It:

- Fills appointment slots one field at a time (name → age → reason → date → time).
- Enforces the procedure triage flow for complex procedures.
- Triggers an availability check when date and time are collected.
- Confirms the appointment and sets `done = true`.
- Answers clinic enquiries concisely without initiating the appointment flow.

### Function Signature

```python
# agent2_kn.py and agent2_en.py
async def run_agent2_kn(user_text, memory, state, agent1_context, groq_client) -> tuple
async def run_agent2_en(user_text, memory, state, agent1_context, groq_client) -> tuple
```

Returns: `(response_text: str, state: dict, parsed: dict)`

### Data Received

| Input | Type | Description |
|---|---|---|
| `user_text` | `str` | Current user utterance |
| `memory` | `list[dict]` | Last ≤12 turns of conversation (role/content pairs) |
| `state` | `dict` | Accumulated appointment slot state (shared mutable object) |
| `agent1_context` | `dict` | `{treatment, query_type}` extracted by Agent-1 on turn 1 |
| `groq_client` | `LLMPool` | Shared LLM pool |

**Dynamic context message injected at call time (not part of the static system prompt):**

```
CALL CONTEXT — Client: {client_id}. Today: {weekday, DD Month YYYY}. Time: {HH:MM AM/PM}.
Relative dates: "today" = DD Month YYYY; "tomorrow" = ...; "day after tomorrow" = ...
Agent-1 context: {intent, treatment, query_type}
Current state: name: X, age: Y, date: ..., ...
```

This context message is inserted as a `system` role message immediately before the user's utterance so the LLM always has the correct absolute date without being confused by relative terms.

### Data Returned (parsed JSON from LLM)

```json
{
  "response": "<voice-friendly response in the session language>",
  "intent": "enquiry | appointment | emergency",
  "action": "CHECK_AVAILABILITY | TRANSFER_CALL | END_CALL | null",
  "handoff": false,
  "state": {
    "name": "<string or null>",
    "age": "<number or null>",
    "date": "<YYYY-MM-DD or null>",
    "time": "<string or null>",
    "reason": "<string or null>",
    "requested_procedure": "<original procedure if triage redirected to consultation>",
    "visited_before": "<true | false | null>",
    "doctor_advised_procedure": "<true | false | null>",
    "availability_checked": "<true | false | null>",
    "availability_is_available": "<true | false | null>",
    "confirmation_pending": "<true | false | null>"
  },
  "done": false
}
```

### Appointment Slot-Filling Order

Fields are collected strictly in this order, one at a time:

```
1. name
2. age
3. reason  (may trigger procedure triage — see below)
4. date    (relative dates resolved to absolute YYYY-MM-DD in state)
5. time
```

### Procedure Triage Flow

Triggered when `reason` is a complex procedure (root canal, braces, aligners, implants, extraction, wisdom tooth, surgery):

```
User mentions procedure
       │
       ▼
Ask: "Have you visited our clinic before for this issue?"
       │
  ─────┴──────────────
  │                  │
 Yes               No / first visit
  │                  │
  ▼                  ▼
Ask: "Did the doctor     Tell them: doctor must
advise this procedure?"  examine first.
  │                      Offer consultation booking.
  ▼                      Set reason = "consultation"
 Yes → proceed           Keep original in
 No  → consultation      state.requested_procedure
```

State fields written by triage:
- `requested_procedure` — original procedure the user mentioned
- `visited_before` — `true` / `false`
- `doctor_advised_procedure` — `true` / `false`
- `reason` — may be overridden to `"consultation"`

### Clinic Hours Guardrail

Agent-2 will not book outside these windows:
- **Days:** Monday to Saturday only (no Sunday bookings)
- **Morning session:** 10:00 AM to 1:00 PM
- **Evening session:** 4:00 PM to 7:00 PM

If the user requests an invalid slot, the agent politely asks them to choose within these hours.

### Confirmation Protocol

```
All 5 fields collected AND availability confirmed
         │
         ▼
1. Restate: date + time + reason
2. Ask: "Is that correct?" / "ಇದು ಸರಿಯೇ?"
3. Set confirmation_pending = true, done = false
         │
   User confirms (yes)
         │
         ▼
4. Confirm appointment (use patient name)
5. Ask: "Anything else I can help with?" / "ಇನ್ನೇನಾದರೂ ಸಹಾಯ ಬೇಕಾ?"
6. Set confirmation_pending = false, done = true
         │
   User says no further help
         │
         ▼
7. Short thank-you
8. Set action = "END_CALL"
```

### State Merge Logic

After each LLM response, `main.py` / the runner merges only valid new values into the shared state object. Fields are updated only if the new value is:

- Not `None`
- Not an empty string
- Not `"unknown"`, `"n/a"`, `"na"`, `"unk"`
- Not Kannada equivalents: `"ತಿಳಿದಿಲ್ಲ"`, `"ಗೊತ್ತಿಲ್ಲ"`

Boolean values (`true`/`false`) and numeric 0 are always written through.

### Hard Constraints (enforced in prompt)

- **One question per turn** — never ask multiple fields simultaneously.
- **No repeating the patient's name** — used only when first captured and in the final confirmation.
- **No ISO dates in voice responses** — always speak as "Monday, 13 April 2026".
- **No "Dr." abbreviation** — always "Doctor" (English) or "ಡಾಕ್ಟರ್" (Kannada).
- **No confirmation language before all fields are known.**
- **No extra text outside the JSON output.**

### LLM Parameters

| Parameter | Value |
|---|---|
| Model | `LLM_MODEL` env var (default: `llama-3.3-70b-versatile`) |
| `max_tokens` | 600 |
| `temperature` | 0.3 |
| `response_format` | `json_object` |
| Timeout | 15 seconds |

### Agent-2 EN Only: ASCII Sanitization

`agent2_en.py` has a helper `_ensure_english_ascii(text, groq_client)` that auto-transliterates non-ASCII state values (e.g., a name captured in Kannada script during a language-mixed conversation) to plain ASCII before those values are used in tool calls or webhooks.

---

## 5. Agent-3 — Cancel / Reschedule Executor

Two language variants:

| Variant | File | Called when |
|---|---|---|
| `agent3_kn` | `src/agent3_kn.py` | `session_language == "kn"` AND `intent == "cancel_reschedule"` |
| `agent3_en` | `src/agent3_en.py` | `session_language == "en"` AND `intent == "cancel_reschedule"` |

### Responsibility

Agent-3 handles **appointment cancellation** and **rescheduling** only. It does not book new appointments.

### Function Signature

```python
async def run_agent3_kn(user_text, memory, state, context, groq_client) -> tuple
async def run_agent3_en(user_text, memory, state, context, groq_client) -> tuple
```

Returns: `(response_text: str, state: dict, parsed: dict)`

### Data Received

| Input | Type | Description |
|---|---|---|
| `user_text` | `str` | Current user utterance |
| `memory` | `list[dict]` | Last ≤12 turns |
| `state` | `dict` | Agent-3 accumulates its own slot state (separate from Agent-2 state) |
| `context` | `dict` | Agent-1 context (`query_type`, `intent`) |
| `groq_client` | `LLMPool` | Shared LLM pool |

**Dynamic context message injected at call time:**

```
CALL CONTEXT — Client: {client_id}. Today: {weekday, DD Month YYYY}. Time: {HH:MM AM/PM}.
Relative dates: ...
Intent hint: {query_type from agent1_context}
Current state: name: ..., previous_datetime: ..., new_datetime: ...
```

### Required Slot Fields

**Cancellation:**

| Field | Description |
|---|---|
| `name` | Patient's name |
| `previous_datetime` | Date + time of the appointment to cancel (`YYYY-MM-DD HH:MM`) |

**Rescheduling:**

| Field | Description |
|---|---|
| `name` | Patient's name |
| `previous_datetime` | Old appointment date + time (`YYYY-MM-DD HH:MM`) |
| `new_datetime` | New appointment date + time (`YYYY-MM-DD HH:MM`) |

### Data Returned (parsed JSON from LLM)

```json
{
  "response": "<voice-friendly response>",
  "intent": "cancel_reschedule | emergency",
  "event_type": "appointment_cancel | appointment_reschedule",
  "confirmation_status": "tentative | confirmed | unclear",
  "handoff": false,
  "state": {
    "name": "<string or null>",
    "previous_datetime": "<YYYY-MM-DD HH:MM or null>",
    "new_datetime": "<YYYY-MM-DD HH:MM or null>",
    "reason": "<string or null>",
    "age": "<number or null>"
  },
  "done": false
}
```

### State Merge Logic (`_merge_state`)

Agent-3 uses a simpler merge than Agent-2 — it **always overwrites** a field when a valid new value arrives. This is intentional: it allows the user to correct `new_datetime` mid-conversation without the old value persisting.

```python
def _merge_state(state: dict, new_state: dict):
    for k in ["name", "previous_datetime", "new_datetime", "reason", "age"]:
        val = new_state.get(k)
        if val and str(val).strip() and str(val).strip().lower() not in ("", "unknown", "null", "none"):
            state[k] = val  # always overwrite so user can correct new_datetime
```

### N8N Webhook Trigger

When `done = true` and `confirmation_status = "confirmed"`, Agent-3 immediately fires the scheduling payload to the N8N webhook using a **fire-and-forget** pattern:

```python
asyncio.create_task(asyncio.to_thread(send_to_n8n_webhook_sync, scheduling_payload))
```

This does not block the response to the caller. If the webhook fails, it retries up to 3 times with exponential backoff and writes a local fallback log.

### LLM Parameters

| Parameter | Value |
|---|---|
| Model | `LLM_MODEL` env var (default: `llama-3.3-70b-versatile`) |
| `max_tokens` | 600 |
| `temperature` | 0.2 (more conservative than Agent-2 for structured date parsing) |
| `response_format` | `json_object` |
| Timeout | 15 seconds |

---

## 6. Routing Logic (main.py)

### Turn-1 Routing (Vobiz + Browser handlers, symmetric)

```
user_text arrives (turn 1, agent1_ran = False)
    │
    ▼
run_agent1(user_text, memory)
    │
    ├─ intent == "greeting"          → respond with greeting; do NOT lock language; do NOT set agent1_ran
    ├─ intent == "system_check"      → respond "Yes, I can hear you"; do NOT lock language; do NOT set agent1_ran
    ├─ intent == "emergency"         → respond; trigger Vobiz transfer; close websocket
    ├─ intent == "cancel_reschedule" → in_agent3 = True; agent1_ran = True; run Agent-3
    └─ intent == "appointment"       → agent1_ran = True; run Agent-2
       or "enquiry"
```

### Turn-2+ Routing

```
user_text arrives (turn N, agent1_ran = True)
    │
    │  Language derived from STT detected_lang — Agent-1 is NOT called again
    │
    ├─ in_agent3 == True → run Agent-3 (KN or EN)
    └─ otherwise         → run Agent-2 (KN or EN)
```

### Language Lock Mechanism

| Condition | Action |
|---|---|
| Agent-1 returns `language != "unknown"` on turn 1 | `session_language` locked to that value |
| Agent-1 returns `language == "unknown"` (greeting) | `session_language` stays `None` |
| Turn 2+ and `session_language` is still `None` | Derived from STT `detected_lang` (no Agent-1 re-call) |

### Action Intercepts (main.py post-processes Agent responses)

| `action` field | What main.py does |
|---|---|
| `CHECK_AVAILABILITY` | Calls `check_availability(date, time)` from `src/db.py`; injects result as a system message; re-calls the same agent |
| `TRANSFER_CALL` | Calls `trigger_vobiz_transfer(call_sid, metadata)` via `asyncio.to_thread`; closes websocket |
| `END_CALL` | Closes websocket after a 1-second pause |
| `null` | Normal turn, continue conversation |

---

## 7. Data Flow — What Goes In, What Comes Out

### Complete Turn Data Flow

```
[Caller audio]
      │ PCM bytes
      ▼
[STT — run_stt_http()]
      │ (user_text: str, detected_lang: str)
      ▼
[Agent-1 — turn 1 only]
      │ parsed: {intent, context, language, response, summary}
      ▼
[Routing decision in main.py]
      │
      ▼
[Agent-2 or Agent-3]
      │ receives: (user_text, memory[-12:], state, agent1_context, groq_client)
      │ returns:  (response_text, updated_state, parsed_dict)
      ▼
[main.py post-processing]
      │ • Merges state
      │ • Intercepts CHECK_AVAILABILITY → db call → re-call agent
      │ • Intercepts TRANSFER_CALL / END_CALL → close connection
      │ • Appends to memory; trims to last 12 messages
      │ • Builds pending_payload if done == true
      ▼
[TTS — cartesia_tts_collect(response_text)]
      │ PCM bytes
      ▼
[Caller hears response]
```

### Data Never Sent to the LLM

- Raw audio bytes
- Groq API keys
- N8N webhook URL
- Phone number (only passed as context in state; not in the LLM prompt)
- Any PII beyond what the patient volunteers in conversation

---

## 8. Shared State Object

The state is a plain Python `dict` that lives for the lifetime of one WebSocket session. It is initialized by `get_initial_state()` and enriched turn by turn.

### Initial State (`utils.get_initial_state()`)

```python
{
    "name":   None,
    "reason": None,
    "date":   None,
    "time":   None,
    "age":    None,
}
```

### Fields Added at Session Start (by main.py)

```python
state["phone"]     = caller_phone   # from Vobiz query param or browser session
state["client_id"] = client_id      # resolved from phone → CLIENT_PHONE_MAP
```

### Full State Schema (all possible fields across the session)

| Field | Type | Set by | Purpose |
|---|---|---|---|
| `name` | `str` | Agent-2 | Patient name |
| `age` | `int` | Agent-2 | Patient age |
| `reason` | `str` | Agent-2 | Appointment reason |
| `date` | `str` (YYYY-MM-DD) | Agent-2 | Appointment date |
| `time` | `str` | Agent-2 | Appointment time |
| `requested_procedure` | `str` | Agent-2 (triage) | Original procedure before triage redirect |
| `visited_before` | `bool` | Agent-2 (triage) | Whether patient visited clinic before |
| `doctor_advised_procedure` | `bool` | Agent-2 (triage) | Whether doctor advised the procedure |
| `availability_checked` | `bool` | main.py intercept | Whether slot availability was queried |
| `availability_is_available` | `bool` | main.py intercept | Result of slot availability check |
| `confirmation_pending` | `bool` | Agent-2 | Waiting for user to confirm final booking |
| `emergency_transferred` | `bool` | main.py | Guard to prevent double emergency transfer |
| `phone` | `str` | main.py | Caller's phone number |
| `client_id` | `str` | main.py | Tenant identifier |
| `previous_datetime` | `str` (YYYY-MM-DD HH:MM) | Agent-3 | Old appointment datetime for cancel/reschedule |
| `new_datetime` | `str` (YYYY-MM-DD HH:MM) | Agent-3 | New appointment datetime for reschedule |

### Session Persistence

The state and memory are persisted to a local SQLite database (`sessions.db`) via `SessionStore` after every turn:

```python
await asyncio.to_thread(session_store.save_session, session_key, state, memory)
```

On reconnect (e.g., dropped call), the session is recovered:

```python
saved_state, saved_memory = await asyncio.to_thread(session_store.load_session, session_key)
```

Sessions can be cleared via the `POST /session/clear?session_id=<id>` endpoint.

---

## 9. Context Window Management

### Memory Structure

Each turn appends two messages to the `memory` list:

```python
memory.append({"role": "user",      "content": user_text})
memory.append({"role": "assistant", "content": response_text})
```

### Window Size

Memory is hard-capped at **12 messages** (6 complete turns) after every response:

```python
if len(memory) > 12:
    memory = memory[-12:]
```

### Full Message Sequence Sent to LLM per Turn

```
[0]  system  — static agent system prompt   (prompt-cacheable; same every call)
[1..N] user/assistant — last ≤12 memory entries
[N+1]  system  — dynamic CALL CONTEXT message  (client_id, today's date, current state)
[N+2]  user    — current user utterance
```

The static system prompt is separated from the dynamic context message specifically to enable **prompt caching** at the API level — the static portion is identical across all calls and can be cached by the provider, reducing latency and token cost.

---

## 10. Client Differentiation & Multi-Tenancy

### How Clients Are Identified

Every call is associated with a `client_id` resolved at session start from the caller's phone number:

```python
client_id = _resolve_client_id_from_phone(caller_phone)
```

Mapping is configured via environment variables:

```
# Option 1 — JSON map
CLIENT_PHONE_MAP_JSON={"917012345678": "clinic_a", "917087654321": "clinic_b"}

# Option 2 — colon-separated pairs
CLIENT_PHONE_MAP=+917012345678:clinic_a,+917087654321:clinic_b
```

If no match is found, `DEFAULT_CLIENT_ID` is used (defaults to `"default"`).

### Where client_id Flows

| Location | Usage |
|---|---|
| `state["client_id"]` | Stored in session state for full lifetime of call |
| `CALL CONTEXT` system message | Injected into every Agent-2 and Agent-3 LLM call |
| STT call (`run_stt_http`) | `client_id` passed to STT for per-client metrics |
| N8N webhook payload | Included in `booked_via` / agent notes |
| `/llm/metrics?client_id=X` | Filters LLM pool metrics by tenant |

### Per-Client Prompt Customization

Currently the same system prompts are used for all clients. The `client_id` in the `CALL CONTEXT` message allows the LLM to adapt if future client-specific instructions are added to the context. The architecture is ready for full per-client prompt injection without structural changes.

### STT Client Isolation

The STT layer (in `src/stt.py`) maintains separate concurrency semaphores and rate-limit buckets per `client_id`, ensuring one busy client cannot starve another's transcription capacity.

---

## 11. Concurrency Model

### LLM Pool (`src/llm.py`)

All agents share one `LLMPool` singleton (`llm_pool`). The pool provides:

| Feature | Detail |
|---|---|
| **Round-robin key selection** | Cycles across all keys in `GROQ_API_KEYS` in order |
| **Per-key concurrency cap** | `asyncio.Semaphore` per key, default 5 concurrent calls (`LLM_MAX_CONCURRENT_PER_KEY`) |
| **Rate-limit retry** | On HTTP 429, rotates to the next key and retries up to `LLM_MAX_RETRIES` times (default 2) |
| **Retry delay** | `LLM_RETRY_DELAY_SEC` (default 0.3s) between retries |
| **Metrics** | Per-key: requests, latency, tokens, cost; global: totals, rate-limit hits, recent events |

### Agent Call Isolation

Each WebSocket session runs in its own `asyncio` coroutine. Agents are pure `async` functions with no shared mutable state across sessions. The `state` dict and `memory` list are local to each session's handler.

### Thread Safety

- `SessionStore` (SQLite) calls are offloaded with `asyncio.to_thread()` to avoid blocking the event loop.
- N8N webhook delivery uses `asyncio.create_task(asyncio.to_thread(...))` for fire-and-forget behavior.
- The LLM pool's metrics store uses a `threading.Lock` because metrics are written from the async context but may be read from any thread via the `/llm/metrics` HTTP endpoint.
- No `threading.Thread()` is used anywhere in `main.py` — all concurrency goes through the asyncio event loop.

### Simultaneous Sessions

Multiple calls are handled concurrently by FastAPI's event loop. There is no per-session locking — sessions are fully isolated by design.

---

## 12. Availability Check Tool

When Agent-2 has collected both `date` and `time` and `availability_checked` is not yet `true`, it returns `action = "CHECK_AVAILABILITY"`. `main.py` intercepts this and runs:

```python
# main.py — run_agent2() wrapper
is_available = check_availability(date, time)   # db.py call (synchronous)
state["availability_checked"] = True
state["availability_is_available"] = bool(is_available)

# Inject result as a system-level message and re-call the same agent
status_msg = "System: The slot is AVAILABLE." or "System: The slot is already BOOKED."
memory_with_check = memory + [
    {"role": "user",      "content": user_text},
    {"role": "assistant", "content": "Let me check the schedule..."},
    {"role": "user",      "content": status_msg},
]
response, state, parsed = await _run_agent2_kn("ದಯವಿಟ್ಟು ಮುಂದುವರಿಯಿರಿ.", memory_with_check, ...)
```

This means the tool call is **transparent to the caller** — they hear one seamless response that already incorporates the availability result. The agent is called twice in that turn, but only one audio response is produced.

---

## 13. N8N Webhook Integration

### Trigger Points

| Event | Triggering Agent | Trigger Condition |
|---|---|---|
| New appointment | Agent-2 (via `main.py`) | `parsed.get("done") == True` AND state has name + date + time + reason |
| Cancellation | Agent-3 KN / EN | `confirmation_status == "confirmed"` AND `done == True` |
| Rescheduling | Agent-3 KN / EN | `confirmation_status == "confirmed"` AND `done == True` |

### Payload Structure (`build_scheduling_payload` in `utils.py`)

```json
{
  "event_type":        "appointment_create | appointment_cancel | appointment_reschedule",
  "patient_name":      "Raj Kumar",
  "patient_phone":     "+91 9999999999",
  "patient_age":       30,
  "start_time":        "2026-04-25T10:00:00",
  "end_time":          "2026-04-25T10:30:00",
  "appointment_type":  "root canal",      ← requested_procedure if set, else reason
  "status":            "confirmed",
  "doctor_name":       "Doctor Naga Deepti",
  "reason":            "consultation",
  "requested_procedure": "root canal",   ← original procedure (may differ from reason)
  "booked_via":        "voice_assistant",
  "agent_notes":       "Language: en. Confirmation: confirmed",
  "created_at":        "2026-04-20T17:30:00.000000",
  "updated_at":        "2026-04-20T17:30:00.000000",
  "previous_datetime": "2026-04-18T10:00:00"  ← only for cancel/reschedule
}
```

### Delivery & Retry

```
POST {N8N_WEBHOOK_URL} with JSON payload
    │
    ├─ 200/201/202 → success, log "✅ delivered"
    ├─ Other 4xx/5xx → retry (up to 3 attempts)
    │   Backoff: 1s → 2s
    └─ All retries exhausted → write to .logs/n8n_failed_payloads.json
```

Delivery runs in a background thread via `asyncio.create_task(asyncio.to_thread(...))` and does not delay the caller's audio response.

---

## 14. Fallback & Error Handling

### Agent-1 Fallback

If the LLM call times out or throws an exception:

```python
return {"intent": "enquiry", "context": {}, "summary": "fallback", "response": "ಒಂದು ಕ್ಷಣ ದಯವಿಟ್ಟು."}
```

Session continues as an enquiry — the caller still gets a response.

### Agent-2 Fallback (KN / EN)

On timeout (15s):

```
KN: "ಕ್ಷಮಿಸಿ, ಸ್ವಲ್ಪ ತಡವಾಯಿತು. ಮತ್ತೊಮ್ಮೆ ಹೇಳಿ."
EN: "Sorry, that took too long. Could you please repeat that?"
```

State is NOT mutated on a fallback turn — no partial data is written.

### Agent-3 Fallback (KN / EN)

On timeout or error: polite "please repeat" response; `event_type` defaults to `appointment_cancel`, `confirmation_status` to `unclear`.

### Empty TTS Response

If TTS returns empty bytes, the audio send is skipped silently. The caller does not hear a broken response.

### Empty LLM Response / JSON Parse Failure

`parse_llm_json()` in `utils.py` handles malformed JSON by attempting multiple extraction strategies before returning an empty `{}`. The runner then falls back to a default string response.

---

## 15. Security Model

### What the LLM Never Receives

| Sensitive Data | How It Is Protected |
|---|---|
| Groq API keys | Loaded from env; passed only to `LLMPool`; never in prompts |
| N8N webhook URL | Used only in `utils.send_to_n8n_webhook_sync`; never logged or returned to callers |
| SARVAM / STT API keys | Loaded from env; used only in `stt.py` |
| Database credentials | Used only in `db.py` (Supabase client) |
| Other callers' phone numbers | Each session is isolated; no cross-session state |

### LLM Prompt Isolation

Each agent's system prompt is a static string with no runtime interpolation of secrets. The only dynamic injection is the `CALL CONTEXT` system message which contains:
- The current date/time (safe)
- `client_id` (a non-sensitive tenant identifier)
- The accumulated state (patient-provided data only)
- Agent-1's context dict (intent + treatment extracted from the user's own words)

### No PII in Logs Beyond What the Patient Said

Conversation transcripts are printed to server stdout as debug output. No additional PII is introduced — the content is exactly what the patient spoke. API keys, webhook URLs, and internal session keys are not printed.

### State Scoping

The `state` dict is scoped to a single WebSocket session. There is no shared state between sessions. If two callers are on the phone simultaneously, their state objects are entirely separate in-memory dicts.

### Session Store Access Control

The `/session/clear` endpoint requires an explicit `session_id` parameter and only clears that exact session. There is no endpoint that lists, dumps, or bulk-deletes sessions.

### Emergency Transfer Guard

```python
if not state.get("emergency_transferred"):
    state["emergency_transferred"] = True
    await asyncio.to_thread(trigger_vobiz_transfer, call_sid, metadata)
```

The `emergency_transferred` flag ensures the transfer call is made exactly once per session, preventing duplicate transfers if the emergency intent fires twice.

---

## 16. Environment Variable Reference

| Variable | Default | Description |
|---|---|---|
| `LLM_MODEL` | `llama-3.3-70b-versatile` | Groq model used by all agents |
| `GROQ_API_KEYS` | *(required)* | Comma-separated Groq API keys for LLM pool round-robin |
| `GROQ_API_KEY` | *(fallback)* | Single key fallback if `GROQ_API_KEYS` not set |
| `LLM_MAX_CONCURRENT_PER_KEY` | `5` | Max concurrent LLM calls per API key |
| `LLM_MAX_RETRIES` | `2` | Max retries on rate-limit (429) |
| `LLM_RETRY_DELAY_SEC` | `0.3` | Delay between retries (seconds) |
| `LLM_COST_INPUT_PER_1M` | `0.59` | Input token cost in USD per 1M tokens (for metrics) |
| `LLM_COST_OUTPUT_PER_1M` | `0.79` | Output token cost in USD per 1M tokens (for metrics) |
| `DEFAULT_CLIENT_ID` | `default` | Fallback tenant ID when phone not in map |
| `CLIENT_PHONE_MAP_JSON` | *(optional)* | JSON object mapping phone → client_id |
| `CLIENT_PHONE_MAP` | *(optional)* | `phone:client_id` comma-separated pairs |
| `N8N_WEBHOOK_URL` | *(optional)* | Target URL for scheduling payloads |
| `EMERGENCY_TRANSFER_NUMBER` | `+918660033297` | Number to transfer emergency calls to |
| `INPUT_MODE` | `mic` | `mic` for browser UI; `vobiz` for telephony |

---

*End of Agent Architecture Reference*
