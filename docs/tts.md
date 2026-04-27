# Text-to-Speech (TTS) — Technical Reference

> **Component:** `src/tts/tts_core.py` + backward-compat shims in `src/tts/__init__.py`  
> **Last updated:** April 2026

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Provider Chain & APIs](#2-provider-chain--apis)
3. [Fallback Strategy](#3-fallback-strategy)
4. [Retry Policy](#4-retry-policy)
5. [Fail-Safe Mechanisms](#5-fail-safe-mechanisms)
6. [Concurrency Model](#6-concurrency-model)
7. [Client Isolation & Multi-Tenancy](#7-client-isolation--multi-tenancy)
8. [Onboarding a New Client](#8-onboarding-a-new-client)
9. [Rate Limits](#9-rate-limits)
10. [Text Quality Gates](#10-text-quality-gates)
11. [Output Modes](#11-output-modes)
12. [Latency Profile](#12-latency-profile)
13. [Scalability & Reliability (NFRs)](#13-scalability--reliability-nfrs)
14. [Observability & Metrics](#14-observability--metrics)
15. [Environment Variable Reference](#15-environment-variable-reference)

---

## 1. Architecture Overview

```
LLM Response Text
        │
        ▼
┌─────────────────────────────────────────────────────────┐
│  tts_core.py — run_tts_collect() / run_tts_stream()     │
│                                                         │
│  ┌────────────────────────────────────────────────────┐ │
│  │ Text sanitize + empty / oversized guard            │ │
│  └────────────────────────────────────────────────────┘ │
│  ┌────────────────────────────────────────────────────┐ │
│  │ [Gate 1] Per-client RPS check (3 req/sec default)  │ │
│  └────────────────────────────────────────────────────┘ │
│  ┌────────────────────────────────────────────────────┐ │
│  │ [Gate 2] Per-client semaphore (max 2)              │ │
│  └────────────────────────────────────────────────────┘ │
│  ┌────────────────────────────────────────────────────┐ │
│  │ [Gate 3] Global semaphore (max 8)                  │ │
│  └────────────────────────────────────────────────────┘ │
│  ┌────────────────────────────────────────────────────┐ │
│  │ Session + client cost guardrails                   │ │
│  └────────────────────────────────────────────────────┘ │
│                                                         │
│  Provider chain:                                        │
│    1. Cartesia AI   (primary)   — WebSocket             │
│    2. Sarvam TTS    (fallback 1) — HTTP POST            │
│    3. ElevenLabs    (fallback 2) — HTTP POST            │
└─────────────────────────────────────────────────────────┘
        │
        ├── collect mode → PCM bytes → Vobiz telephony
        └── stream mode  → WS chunks → browser client
```

**Two entry points:**

| Function | Mode | Used by |
|---|---|---|
| `run_tts_collect(text, ...)` | Collects all PCM bytes | Vobiz telephony (`cartesia_tts_collect` shim) |
| `run_tts_stream(text, ssb, sst, ...)` | Streams chunks to browser WS | Browser `/ws` handler (`cartesia_tts_stream` shim) |

Both share the same concurrency gates, provider chain, and metrics layer.  
`main.py` requires **no changes** — the old `cartesia_tts_collect` and `cartesia_tts_stream` names are re-exported as shims from `src/tts/__init__.py`.

---

## 2. Provider Chain & APIs

### Primary — Cartesia AI `sonic-3`

| Property | Value |
|---|---|
| Endpoint | `wss://api.cartesia.ai/tts/websocket` |
| Auth | `?api_key=<key>` query param |
| Protocol | WebSocket — send JSON request, receive `chunk` / `done` / `error` messages |
| Output | Raw PCM s16le 16 kHz in base64-encoded `data` field of each `chunk` message |
| Language codes | `kn` (Kannada), `en` (English) |
| Voice IDs | Per-language, configurable via `CARTESIA_VOICE_ID_KN` / `CARTESIA_VOICE_ID_EN` |
| Key rotation | Pool via `CARTESIA_API_KEYS` (comma-separated), round-robin |
| Why primary | Lowest TTFA (Time To First Audio), native Kannada support, streaming protocol |

### Fallback 1 — Sarvam TTS `bulbul:v1`

| Property | Value |
|---|---|
| Endpoint | `POST https://api.sarvam.ai/text-to-speech` |
| Auth header | `api-subscription-key: <key>` |
| Body | JSON — `inputs`, `target_language_code`, `speaker`, `model`, `speech_sample_rate` |
| Language codes | `kn-IN`, `en-IN` |
| Speaker | `SARVAM_TTS_SPEAKER_KN` (default `amol`), `SARVAM_TTS_SPEAKER_EN` (default `meera`) |
| Response | JSON `{"audios": ["<base64_WAV>"]}` — decoded and resampled to 16 kHz PCM |
| Why fallback 1 | Purpose-built for Indian languages; HTTP (not WebSocket) for simplicity in fallback |

### Fallback 2 — ElevenLabs `eleven_multilingual_v2`

| Property | Value |
|---|---|
| Endpoint | `POST https://api.elevenlabs.io/v1/text-to-speech/{voice_id}?output_format=pcm_16000` |
| Auth header | `xi-api-key: <key>` |
| Body | JSON `{"text": "...", "model_id": "eleven_multilingual_v2"}` |
| Response | Raw PCM s16le 16 kHz bytes directly (no conversion needed) |
| Why fallback 2 | High-quality multilingual; HTTP; returns 16 kHz PCM natively |

---

## 3. Fallback Strategy

The fallback is **automatic and transparent** — no intervention required.

```
Cartesia attempt 1
  ├─ WebSocket OK → ✅ return audio
  └─ error / timeout → retry once (with backoff)
       ├─ WebSocket OK → ✅ return audio
       └─ still failing → Sarvam TTS attempt
            ├─ HTTP 200 → ✅ return audio (fallback_used=True in metrics)
            └─ failing → ElevenLabs attempt
                 ├─ HTTP 200 → ✅ return audio
                 └─ failing → return b"" (silent empty, no crash)
```

### Stream mode fallback nuance

In `run_tts_stream`, Cartesia is tried first in **true streaming mode** (chunks sent as they arrive).

```
Cartesia stream attempt
  ├─ ok=True, sent_any_audio=True → ✅ done, no fallback needed
  ├─ ok=False, sent_any_audio=False → ❌ failed before any audio
  │    └─ try Sarvam TTS collect → send collected bytes in one shot
  │         └─ still failing → try ElevenLabs collect → send
  └─ ok=False, sent_any_audio=True → ⚠️ mid-stream failure
       └─ DO NOT fall back (partial audio already sent — duplicate audio risk)
            └─ send audio_end and stop cleanly
```

Retryable errors: `429`, `408`, `5xx`, `timeout`, `network_error`  
Non-retryable (skip to next provider immediately): `400`, `401`, `403`, `422`, `provider_unconfigured`

---

## 4. Retry Policy

Retries apply **only to the primary provider (Cartesia)**.

| Parameter | Default | Env var |
|---|---|---|
| Retry attempts on Cartesia | 1 (2 total attempts) | `TTS_PRIMARY_RETRIES=1` |
| Backoff base | 300 ms | `TTS_RETRY_BACKOFF_BASE_SEC=0.3` |
| Backoff max | 2.0 s | `TTS_RETRY_BACKOFF_MAX_SEC=2.0` |
| Jitter | ±100 ms | `TTS_RETRY_JITTER_SEC=0.1` |

Sarvam TTS and ElevenLabs are each tried once — no retries on fallbacks.

---

## 5. Fail-Safe Mechanisms

### 5.1 Empty Text Guard
If `text` is empty or whitespace after strip: returns `b""` immediately, no API call.  
For stream mode: sends `audio_end` then returns.

### 5.2 Oversized Text Truncation
Text longer than `TTS_MAX_CHARS_PER_REQUEST` (default 2000) is **truncated**, not rejected.  
This prevents unexpectedly long LLM responses from causing timeouts or oversized API bills.

### 5.3 Per-Client RPS Gate
Each client has an independent 1-second sliding window.
- Default: **3 req/sec per client**
- Excess requests are dropped with `client_rps_limited` logged
- Prevents one active session from starving other clients

### 5.4 Queue Timeout
If a semaphore slot is not available within **2.5 seconds**, the request is dropped with `queue_timeout` logged.  
No hang, no crash — returns `b""` (collect) or sends `audio_end` (stream).

### 5.5 Request Timeout
Each provider call (WebSocket or HTTP) has a hard **30-second timeout**.  
On timeout, classified as `timeout` and the fallback chain continues.

### 5.6 Mid-Stream Failure Guard
If Cartesia starts streaming then errors partway through:
- `audio_end` is sent to the client to close the audio frame correctly
- **No fallback attempt is made** — avoids delivering garbled/duplicate audio

### 5.7 Cost Guardrails
Optional spending caps prevent runaway API bills:
- Per-session cap: `TTS_MAX_COST_INR_PER_SESSION` (default 0 = disabled)
- Per-client cap: configured per client in `TTS_CLIENT_CONFIG_JSON`
- Cost is estimated per character synthesised using `*_COST_INR_PER_CHAR` env vars

### 5.8 Session State Cleanup
Stale session cost entries are pruned every **60 seconds**.  
Sessions inactive for **30 minutes** are evicted. Max tracked sessions: **5000**.

### 5.9 audio_end Guarantee
`run_tts_stream` always sends `{"type": "audio_end"}` in a `finally` block — guaranteed even if an unexpected exception is thrown, ensuring the browser client is never left waiting for audio that will never come.

---

## 6. Concurrency Model

Fully async — no threads. Two-level asyncio semaphore gating (mirrors STT architecture).

```
Request arrives
    │
    ├─ [Gate 1] Per-client RPS check (sync, O(1) deque) ──► reject if > 3/sec
    │
    ├─ [Gate 2] Per-client asyncio.Semaphore(2) ─────────► wait up to 2.5s or drop
    │
    └─ [Gate 3] Global asyncio.Semaphore(8) ─────────────► wait up to remaining budget or drop
```

| Semaphore | Default | Env var | Purpose |
|---|---|---|---|
| Per-client | 2 | `TTS_DEFAULT_CLIENT_MAX_CONCURRENT=2` | Prevents one client monopolising all slots |
| Global | 8 | `TTS_MAX_CONCURRENT=8` | Hard cap on total in-flight TTS calls |

The HTTP connection pool (`aiohttp.TCPConnector`) is used **only for fallback providers**:
- Total connections: **30** (`TTS_HTTP_POOL_LIMIT`)
- Connections per host: **15** (`TTS_HTTP_POOL_PER_HOST`)

Cartesia uses per-call WebSocket connections (no pooling — each synthesis is an independent context).

---

## 7. Client Isolation & Multi-Tenancy

Every TTS call carries a `client_id`. Used to:
1. Select the correct per-client semaphore
2. Apply per-client RPS limits
3. Track per-client cost
4. Filter metrics by client via `/tts/metrics?client_id=<id>`

**How `client_id` is resolved** (same as STT):

| Channel | Resolution |
|---|---|
| Vobiz telephony | From caller phone number via `CLIENT_PHONE_MAP` |
| Browser WS | Passed down from the existing session context |
| Collect default | `DEFAULT_CLIENT_ID` ("default") |

**Per-client limits** via `TTS_CLIENT_CONFIG_JSON`:

```env
TTS_CLIENT_CONFIG_JSON={
  "clinic_abc": {
    "max_concurrent": 4,
    "max_rps": 5,
    "cost_limits": {"max_cost_per_client": 200.0}
  }
}
```

Clients not in the map fall back to global defaults.

---

## 8. Onboarding a New Client

### Step 1 — Assign a client ID
Same as STT — use the same `client_id` value for both pipelines.

### Step 2 — Map phone numbers (Vobiz only)
Already done via `CLIENT_PHONE_MAP` for STT — TTS uses the same resolved `client_id`.

### Step 3 — Set per-client TTS limits (optional)
```env
TTS_CLIENT_CONFIG_JSON={"clinic_abc": {"max_concurrent": 4, "max_rps": 5}}
```
If omitted, global defaults apply (2 concurrent, 3 req/sec).

### Step 4 — Restart
No code changes required. All limits are read from env vars at startup.

---

## 9. Rate Limits

### Cartesia API (provider-side)
Cartesia does not publish a public QPM limit. The `CARTESIA_API_KEYS` pool and round-robin rotation distribute load across multiple API keys if needed.

### Per-client (application-side)
| Constraint | Default | Configurable |
|---|---|---|
| Max concurrent TTS calls | 2 | `TTS_DEFAULT_CLIENT_MAX_CONCURRENT` or per-client config |
| Max requests/sec | 3 | `TTS_DEFAULT_CLIENT_MAX_RPS` or per-client config |

### System-wide
| Constraint | Default |
|---|---|
| Total in-flight TTS calls | 8 (`TTS_MAX_CONCURRENT`) |
| Queue wait before drop | 2.5 s (`TTS_QUEUE_WAIT_TIMEOUT_SEC`) |
| Hard request timeout | 30 s (`TTS_REQUEST_TIMEOUT_SEC`) |
| Max chars per request | 2000 (`TTS_MAX_CHARS_PER_REQUEST`) |

---

## 10. Text Quality Gates

All gates run **before** any provider call.

```
LLM response text arrives
     │
     ├─ Empty / whitespace ───────────────────────────► return b"" (stream: send audio_end)
     │
     ├─ Length > TTS_MAX_CHARS_PER_REQUEST ──────────► truncate to limit (do not reject)
     │
     └─ ✅ Pass — call provider chain
```

No content filtering (language, noise) is applied at the TTS layer — LLM output is trusted to be well-formed. Text cleaning (e.g., removing markdown) is the responsibility of the agent layer before calling TTS.

---

## 11. Output Modes

### Collect mode — `run_tts_collect()`
Used by Vobiz telephony. Blocks until all PCM bytes are received, then returns them.  
Caller converts to the telephony codec (µ-law 8 kHz) as needed.

```python
pcm = await run_tts_collect(response_text, language="en", client_id=client_id)
```

### Stream mode — `run_tts_stream()`
Used by browser WebSocket handler. Sends PCM chunks as they arrive from Cartesia.  
Browser receives: `audio_start` → binary PCM chunks → `audio_end`.

```python
await run_tts_stream(
    response_text, safe_send_bytes, safe_send_text,
    language="kn", client_id=client_id, stt_start_time=stt_ts
)
```

Protocol events sent to browser:

| Event (JSON) | Meaning |
|---|---|
| `{"type": "audio_start", "sample_rate": 16000}` | First audio chunk incoming |
| Binary frame | PCM s16le 16 kHz chunk |
| `{"type": "audio_end"}` | Synthesis complete (or failed — always sent) |

---

## 12. Latency Profile

End-to-end TTS latency = semaphore wait + provider connection + synthesis time.

| Component | Typical value | Notes |
|---|---|---|
| Semaphore wait | 0 – 20 ms | Near-zero under normal load |
| Cartesia WS connect | 50 – 150 ms | Per call; no persistent pool |
| Cartesia TTFA (stream) | 250 – 600 ms | Time to first audio chunk |
| Cartesia total (collect) | 600 ms – 2 s | Depends on text length |
| Sarvam TTS HTTP round-trip | 500 ms – 1.5 s | HTTP POST, response includes full WAV |
| ElevenLabs HTTP round-trip | 400 ms – 1.2 s | HTTP POST, returns PCM directly |
| WAV→PCM conversion | < 2 ms | In-process; only needed for Sarvam |
| **Total happy path (stream)** | **~300 – 800 ms TTFA** | Cartesia streaming dominates |
| **Total happy path (collect)** | **~700 ms – 2 s** | Full synthesis before return |

> **TTFA** (Time To First Audio) in stream mode is the key latency metric for perceived responsiveness. Cartesia's streaming protocol delivers the first audio chunk well before synthesis is complete.

---

## 13. Scalability & Reliability (NFRs)

### Scalability
- **Horizontal**: Multiple server instances share no state. Each maintains its own semaphores and metrics. Key pool and client config are read from env vars at startup.
- **Vertical**: `TTS_MAX_CONCURRENT` and `TTS_DEFAULT_CLIENT_MAX_CONCURRENT` scale linearly with available network bandwidth.
- **Client growth**: No code changes required for new clients — env var config only.

### Reliability
- **Zero single point of failure at provider level**: 3-provider chain.
- **No blocking on Cartesia failure**: 30s timeout + fallback chain bounds worst-case latency.
- **audio_end guarantee**: Browser client is never left in a broken state after a TTS call.
- **Session state is in-memory**: No database dependency; cleanup runs periodically.

### Availability
- **Provider uptime dependency**: Cartesia, Sarvam, and ElevenLabs are external SaaS.
- **Process restart recovery**: All state is in-memory. On restart, cost tracking resets — no data loss for the audio pipeline.

### Throughput capacity (current configuration)
| Metric | Value |
|---|---|
| Max simultaneous TTS calls | 8 |
| Max clients before contention | ~4 (at 3 RPS each, within semaphore budget) |
| Max text synthesised/min | Bounded by provider rate limits (not currently gated) |

---

## 14. Observability & Metrics

### Endpoint: `GET /tts/metrics`

```
GET /tts/metrics                      → system-wide stats
GET /tts/metrics?client_id=clinic_abc → filtered to one client
```

**Fields returned** (from `get_tts_metrics_snapshot()`):
- `active_tts_requests` — current in-flight count
- `total_requests`, `success_rate`, `error_rate` — overall and per-client
- `fallback_rate` — % of calls that used a fallback provider
- `avg_latency_ms` — rolling average over last 300 requests
- `cost_inr_total`, `cost_per_char` — accumulated cost and efficiency
- `provider_usage`, `provider_errors` — per-provider breakdowns
- `client_requests`, `client_cost_inr` — per-client accounting
- `recent_events` — last 200 request event objects

### Log lines emitted

| Prefix | Meaning |
|---|---|
| `[TTS][COLLECT] provider=cartesia chars=42 ...` | Successful collect |
| `[TTS][STREAM] provider=cartesia chars=42 ...` | Successful stream |
| `[TTS][Cartesia] TTFA 0.312s` | Time-to-first-audio measurement |
| `[TTS][STREAM] Cartesia mid-stream failure` | Stream interrupted after audio started |
| `[TTS][STREAM] Cartesia failed (...), trying fallback` | Fallback triggered |
| `[TTS][DROP] client_rps_limited` | RPS gate rejected request |
| `[TTS][DROP] queue_timeout` | Semaphore full, request dropped |
| `[TTS] WAV→PCM conversion error` | Sarvam TTS WAV decode failure |

---

## 15. Environment Variable Reference

### API Keys
| Variable | Required | Description |
|---|---|---|
| `CARTESIA_API_KEY` | Yes | Primary Cartesia key |
| `CARTESIA_API_KEYS` | Recommended | Comma-separated key pool for rotation |
| `SARVAM_API_KEY` | Recommended | Reused from STT config — also drives TTS fallback 1 |
| `ELEVENLABS_API_KEY` | Optional | ElevenLabs key for fallback 2 |
| `ELEVENLABS_VOICE_ID` | Optional | ElevenLabs voice ID (required if key is set) |

### TTS Concurrency & Rate Limiting
| Variable | Default | Description |
|---|---|---|
| `TTS_MAX_CONCURRENT` | `8` | Global in-flight TTS calls cap |
| `TTS_DEFAULT_CLIENT_MAX_CONCURRENT` | `2` | Per-client concurrent cap |
| `TTS_DEFAULT_CLIENT_MAX_RPS` | `3` | Per-client requests/sec |
| `TTS_QUEUE_WAIT_TIMEOUT_SEC` | `2.5` | Max wait for a semaphore slot |
| `TTS_REQUEST_TIMEOUT_SEC` | `30.0` | Hard timeout per provider attempt |
| `TTS_MAX_CHARS_PER_REQUEST` | `2000` | Truncation limit before provider call |

### TTS Retry
| Variable | Default | Description |
|---|---|---|
| `TTS_PRIMARY_RETRIES` | `1` | Extra attempts on Cartesia before fallback |
| `TTS_RETRY_BACKOFF_BASE_SEC` | `0.3` | Retry backoff base delay |
| `TTS_RETRY_BACKOFF_MAX_SEC` | `2.0` | Retry backoff ceiling |

### Provider Config
| Variable | Default | Description |
|---|---|---|
| `CARTESIA_MODEL_ID` | `sonic-3` | Cartesia synthesis model |
| `CARTESIA_VOICE_ID` | `7c6219d2-...` | Default voice (used if language-specific not set) |
| `CARTESIA_VOICE_ID_KN` | — | Cartesia voice ID for Kannada |
| `CARTESIA_VOICE_ID_EN` | — | Cartesia voice ID for English |
| `SARVAM_TTS_MODEL` | `bulbul:v2` | Sarvam TTS model |
| `SARVAM_TTS_SPEAKER_KN` | `karun` | Sarvam voice for Kannada |
| `SARVAM_TTS_SPEAKER_EN` | `anushka` | Sarvam voice for English |
| `ELEVENLABS_MODEL_ID` | `eleven_multilingual_v2` | ElevenLabs model |

### Cost Tracking
| Variable | Default | Description |
|---|---|---|
| `CARTESIA_COST_INR_PER_CHAR` | `0` | Cost rate for Cartesia (0 = untracked) |
| `SARVAM_TTS_COST_INR_PER_CHAR` | `0` | Cost rate for Sarvam TTS |
| `ELEVENLABS_COST_INR_PER_CHAR` | `0` | Cost rate for ElevenLabs |
| `TTS_MAX_COST_INR_PER_SESSION` | `0` | Session spending cap (0 = disabled) |
| `TTS_DEFAULT_CLIENT_MAX_COST_INR` | `0` | Default per-client spending cap |

### Session & State
| Variable | Default | Description |
|---|---|---|
| `TTS_SESSION_TTL_SEC` | `1800` | Inactive session eviction (30 min) |
| `TTS_MAX_TRACKED_SESSIONS` | `5000` | Cap on in-memory session state entries |

### HTTP Pool (fallback providers)
| Variable | Default | Description |
|---|---|---|
| `TTS_HTTP_POOL_LIMIT` | `30` | Total aiohttp connection pool size |
| `TTS_HTTP_POOL_PER_HOST` | `15` | Per-host connection limit |

### Client Mapping
| Variable | Format | Description |
|---|---|---|
| `TTS_CLIENT_CONFIG_JSON` | JSON object | Per-client concurrency/cost overrides |
| `DEFAULT_CLIENT_ID` | string | Shared with STT — fallback client ID |
