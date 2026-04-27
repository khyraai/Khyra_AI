# Speech-to-Text (STT) — Technical Reference

> **Component:** `src/stt/stt_core.py` + `/ws-stt-only` endpoint in `src/main.py`  
> **Last updated:** April 2026

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Provider Chain & APIs](#2-provider-chain--apis)
3. [Fallback Strategy](#3-fallback-strategy)
4. [Retry Policy](#4-retry-policy)
5. [Fail-Safe & Fail-Proof Mechanisms](#5-fail-safe--fail-proof-mechanisms)
6. [Concurrency Model](#6-concurrency-model)
7. [Client Isolation & Multi-Tenancy](#7-client-isolation--multi-tenancy)
8. [Onboarding a New Client](#8-onboarding-a-new-client)
9. [Rate Limits](#9-rate-limits)
10. [Transcript Filtering & Quality Gates](#10-transcript-filtering--quality-gates)
11. [Silence Detection & Audio Pipeline](#11-silence-detection--audio-pipeline)
12. [Latency Profile](#12-latency-profile)
13. [Scalability & Reliability (NFRs)](#13-scalability--reliability-nfrs)
14. [Observability & Metrics](#14-observability--metrics)
15. [Environment Variable Reference](#15-environment-variable-reference)

---

## 1. Architecture Overview

```
Browser / Vobiz Telephony
         │
         │  WebSocket (PCM 16kHz s16le)
         ▼
  ┌─────────────────────────┐
  │   main.py — WebSocket   │   /ws-stt-only  (browser mic mode)
  │   handler               │   /ws           (Vobiz telephony mode)
  │                         │
  │  ┌─────────────────┐    │
  │  │ RMS Silence     │    │   Accumulates raw PCM frames.
  │  │ Detector        │    │   Fires when silence > 450 ms.
  │  └────────┬────────┘    │
  │           │ audio bytes │
  │  ┌────────▼────────┐    │
  │  │ _dispatch_stt() │    │   Trims trailing silence, applies
  │  │                 │    │   language + word + hallucination gates.
  │  └────────┬────────┘    │
  └───────────┼─────────────┘
              │  WAV bytes
              ▼
  ┌───────────────────────────────────────────┐
  │   stt_core.py — run_stt_http()            │
  │                                           │
  │  ┌──────────────────────────────────────┐ │
  │  │ Client RPS gate (per-client, 4/sec)  │ │
  │  └──────────────────────────────────────┘ │
  │  ┌──────────────────────────────────────┐ │
  │  │ Per-client semaphore (max 3)         │ │
  │  └──────────────────────────────────────┘ │
  │  ┌──────────────────────────────────────┐ │
  │  │ Global semaphore (max 10)            │ │
  │  └──────────────────────────────────────┘ │
  │  ┌──────────────────────────────────────┐ │
  │  │ Global Sarvam RPM gate (110/min)     │ │
  │  └──────────────────────────────────────┘ │
  │                                           │
  │  Provider chain:                          │
  │    1. Sarvam AI  (primary)                │
  │    2. Deepgram   (fallback 1)             │
  │    3. Groq Whisper (fallback 2)           │
  └───────────────────────────────────────────┘
```

**Two entry points:**

| Entry point | Mode | Used by |
|---|---|---|
| `WebSocket /ws-stt-only` | Browser mic / STT-only | Web clients, front-end demos |
| `WebSocket /ws` | Vobiz telephony | Inbound/outbound phone calls |

Both paths share the same `run_stt_http()` core in `stt_core.py`.

---

## 2. Provider Chain & APIs

### Primary — Sarvam AI `saaras:v3`

| Property | Value |
|---|---|
| Endpoint | `POST https://api.sarvam.ai/speech-to-text` |
| Auth header | `api-subscription-key: <key>` |
| Body | `multipart/form-data` — fields: `model`, `language_code`, `mode`, `file` |
| Language codes | `unknown`, `en-IN`, `kn-IN` |
| Key rotation | 2 keys, round-robin via `SARVAM_API_KEYS` |
| Response field | `transcript` |
| Why primary | Best accuracy for Indian English + Kannada |

### Fallback 1 — Deepgram `nova-3`

| Property | Value |
|---|---|
| Endpoint | `POST https://api.deepgram.com/v1/listen?model=nova-3&smart_format=true[&language=en\|kn]` |
| Auth header | `Authorization: Token <key>` |
| Body | Raw WAV bytes, `Content-Type: audio/wav` |
| Language | Passed as `language=en` or `language=kn` when session has locked; auto-detect otherwise |
| Response path | `results.channels[0].alternatives[0].transcript` |
| Why fallback 1 | Very fast, high accuracy for English; handles Sarvam 429s/timeouts |

### Fallback 2 — Groq Whisper `whisper-large-v3-turbo`

| Property | Value |
|---|---|
| Endpoint | `POST https://api.groq.com/openai/v1/audio/transcriptions` |
| Auth header | `Authorization: Bearer <GROQ_API_KEY>` |
| Body | `multipart/form-data` — fields: `model`, `file`, `response_format=json`, optional `language` |
| Language | `kn` or `en` when session locked |
| Response field | `text` |
| Why fallback 2 | OpenAI Whisper-compatible; excellent multilingual including Kannada; Groq runs it at very low latency |

Override Groq model via `GROQ_WHISPER_MODEL=whisper-large-v3` for higher accuracy at slightly more latency.

---

## 3. Fallback Strategy

The fallback is **automatic and transparent** — no intervention required.

```
Sarvam attempt 1
  ├─ HTTP 200 → ✅ return transcript
  └─ 429 / 5xx / timeout → retry once (with backoff)
       ├─ HTTP 200 → ✅ return transcript
       └─ still failing → Deepgram attempt
            ├─ HTTP 200 → ✅ return transcript (fallback_used=True in metrics)
            └─ failing → Groq Whisper attempt
                 ├─ HTTP 200 → ✅ return transcript
                 └─ failing → return "" (silent drop, no crash)

Special case: Global Sarvam RPM window full (110 req/min)
  → Sarvam is skipped entirely for that call
  → Falls directly to Deepgram without consuming a Sarvam attempt
```

Retryable errors that trigger the fallback chain: `429`, `408`, `5xx`, `timeout`, `network_error`.  
Non-retryable errors that skip to next provider immediately: `400`, `401`, `403`, `422`.

---

## 4. Retry Policy

Retries apply **only to the primary provider (Sarvam)**.

| Parameter | Default | Env var |
|---|---|---|
| Retry attempts on Sarvam | 1 (2 total attempts) | `STT_PRIMARY_RETRIES=1` |
| Backoff base | 250 ms | `STT_RETRY_BACKOFF_BASE_SEC=0.25` |
| Backoff max | 1.5 s | `STT_RETRY_BACKOFF_MAX_SEC=1.5` |
| Jitter | ±80 ms | `STT_RETRY_JITTER_SEC=0.08` |

Backoff formula: `min(max_backoff, base × 2^attempt + jitter)`  
Deepgram and Groq are each tried once — no retries on fallbacks to keep latency predictable.

---

## 5. Fail-Safe & Fail-Proof Mechanisms

### 5.1 Global Sarvam RPM Gate
A sliding 60-second window tracks all outgoing Sarvam requests across all clients.  
- Limit: **110 requests/min** (55 per key × 2 keys)  
- When full: Sarvam is bypassed and Deepgram is called directly  
- **No call is dropped** — it is redirected, not discarded  
- Configurable: `SARVAM_API_RPM_LIMIT=110`

### 5.2 Per-Client RPS Gate
Each client has an independent 1-second sliding window.  
- Default: **4 requests/sec per client**  
- Excess requests are dropped immediately with `client_rps_limited` error logged  
- Prevents one client from flooding the provider on behalf of others

### 5.3 Queue Timeout
If all semaphore slots are occupied, new requests wait up to **1.5 seconds**.  
After that, the request is dropped with `queue_timeout` logged — no hang, no crash.

### 5.4 Request Timeout
Each HTTP call to any provider has a hard **8-second timeout**.  
On timeout, the error is classified as `timeout` and the fallback chain continues.

### 5.5 Duplicate Transcript Filter
Within any single session, the same transcript is suppressed if it appears again within **3.5 seconds** of the previous identical result. Prevents double-sends from near-boundary silence detections.

### 5.6 Noise Filter
Transcripts shorter than **3 characters** (after stripping punctuation) are dropped.  
Transcripts that match a noisy-punctuation pattern (e.g., `!!!!!!`) are dropped.

### 5.7 Language Gate
Only English (ASCII) and Kannada (Unicode block U+0C80–U+0CFF) script characters are accepted.  
Any transcript containing Tamil, Hindi, Telugu, or other scripts is silently dropped.  
This runs in `main.py` after the STT call, before the transcript is forwarded downstream.

### 5.8 Hallucination Guard
For single-word transcripts:
- Word must be in the `_SHORT_VALID` whitelist (e.g., "yes", "no", "hello", "okay", "ಹೌದು")
- Trimmed audio duration must be ≥ 250 ms  
→ Short noise bursts that Sarvam/Whisper hallucinate as "Hello" or "Thank you" are dropped

### 5.9 Cost Guardrails
Optional spending caps prevent runaway API bills:
- Per-session cost cap: `STT_MAX_COST_INR_PER_SESSION` (default 0 = disabled)
- Per-client cost cap: configured per client in `STT_CLIENT_CONFIG_JSON`

### 5.10 Session State Cleanup
Stale session entries (duplicate filter cache, cost tracking) are pruned every **60 seconds**.  
Sessions inactive for **30 minutes** are evicted. Max tracked sessions: **5000**.

---

## 6. Concurrency Model

The system uses **two-level asyncio semaphore gating** — no threads, fully async.

```
Request arrives
    │
    ├─ [Gate 1] Per-client RPS check (sync, O(1) deque) ──► reject if > 4/sec
    │
    ├─ [Gate 2] Per-client asyncio.Semaphore(3) ──────────► wait up to 1.5s or drop
    │
    └─ [Gate 3] Global asyncio.Semaphore(10) ─────────────► wait up to remaining budget or drop
```

| Semaphore | Default | Env var | Purpose |
|---|---|---|---|
| Per-client | 3 | `STT_DEFAULT_CLIENT_MAX_CONCURRENT=3` | Prevents one client monopolising all slots |
| Global | 10 | `STT_MAX_CONCURRENT=10` | Hard cap on total in-flight HTTP calls |

The HTTP connection pool (`aiohttp.TCPConnector`) has:
- Total connections: **50** (`STT_HTTP_POOL_LIMIT`)
- Connections per host: **20** (`STT_HTTP_POOL_PER_HOST`)
- DNS TTL cache: 300 seconds

A single `aiohttp.ClientSession` is shared across all requests (pooled, reused, thread-safe for async).

---

## 7. Client Isolation & Multi-Tenancy

Every STT call carries a `client_id`. This is used to:
1. Select the correct per-client semaphore
2. Apply per-client RPS limits
3. Track per-client cost
4. Filter metrics by client via `/stt/metrics?client_id=<id>`

**How `client_id` is resolved:**

| Channel | Resolution method |
|---|---|
| Vobiz telephony | Caller phone number → `CLIENT_PHONE_MAP` / `CLIENT_PHONE_MAP_JSON` → `client_id` |
| Browser STT-only | URL query param `?client_id=<id>` |
| REST upload (`/stt/upload`) | Always `DEFAULT_CLIENT_ID` |

Clients that are not in any map fall back to `DEFAULT_CLIENT_ID` ("default") and receive the default limits.

**Per-client limits** are injected at startup via `STT_CLIENT_CONFIG_JSON`. Each client can have:
- `max_concurrent` — override for their semaphore size
- `max_rps` — override for their request rate
- `max_cost_inr` — spending cap in INR

---

## 8. Onboarding a New Client

### Step 1 — Assign a client ID
Choose a stable, lowercase identifier: e.g., `"clinic_abc"`, `"hospital_xyz"`.

### Step 2 — Map phone numbers to the client (Vobiz only)
In `.env`, add the phone → client mapping using either format:

```env
# Simple key:value pairs (comma-separated)
CLIENT_PHONE_MAP=+919876543210:clinic_abc,+918888888888:clinic_abc

# Or as JSON (more readable for many numbers)
CLIENT_PHONE_MAP_JSON={"9876543210": "clinic_abc", "8888888888": "clinic_abc"}
```

Phone numbers are normalised (leading `+`, digits only) before lookup — format does not need to be exact.

For browser/WebSocket clients, the front-end must pass `?client_id=clinic_abc` in the WebSocket URL.

### Step 3 — Set per-client limits (optional)
If the new client needs different concurrency or rate limits from the defaults, add to `.env`:

```env
STT_CLIENT_CONFIG_JSON={
  "clinic_abc": {
    "max_concurrent": 5,
    "max_rps": 8,
    "cost_limits": {"max_cost_per_client": 500.0}
  }
}
```

If this step is skipped, the client inherits global defaults:
- `max_concurrent = 3`
- `max_rps = 4 req/sec`
- No cost cap

### Step 4 — Restart the server
No code changes are required. All configuration is read from environment variables at startup.

### Does anything else need to change?
No. The language lock, session tracking, duplicate filtering, fallback chain, and transcript gates all work automatically for the new client from the first call.

---

## 9. Rate Limits

### Sarvam API (provider-side)
| Constraint | Value | Mechanism |
|---|---|---|
| Global requests/min | **110** | Sliding 60s window across all clients (`SARVAM_API_RPM_LIMIT`) |
| Keys in rotation | 2 | Round-robin via `SARVAM_API_KEYS` |
| Per-key effective budget | ~55 RPM | Standard Sarvam tier |

When the 110 RPM window is full, the call is routed to Deepgram automatically.

### Per-client (application-side)
| Constraint | Default | Configurable |
|---|---|---|
| Max concurrent calls | 3 | `STT_DEFAULT_CLIENT_MAX_CONCURRENT` or per-client config |
| Max requests/sec | 4 | `STT_DEFAULT_CLIENT_MAX_RPS` or per-client config |

### System-wide
| Constraint | Default |
|---|---|
| Total in-flight HTTP calls | 10 (`STT_MAX_CONCURRENT`) |
| Queue wait before drop | 1.5 s (`STT_QUEUE_WAIT_TIMEOUT_SEC`) |
| HTTP request hard timeout | 8 s (`STT_REQUEST_TIMEOUT_SEC`) |

---

## 10. Transcript Filtering & Quality Gates

All filtering runs in `main.py` **after** the STT HTTP call returns, before forwarding downstream.

```
STT returns text
     │
     ├─ Empty / < 3 chars ──────────────────────────► drop (noise)
     │
     ├─ Noisy punctuation pattern ─────────────────► drop
     │
     ├─ Duplicate within 3.5s window ──────────────► drop
     │
     ├─ Non EN/KN script characters ──────────────► drop (language gate)
     │
     └─ Single word?
           ├─ Word NOT in whitelist ──────────────► drop
           └─ Word in whitelist, audio < 250ms ──► drop (hallucination guard)
                    │
                    ▼
               ✅ Forward transcript downstream
```

**`_SHORT_VALID` whitelist** (single words always allowed if audio ≥ 250ms):
`hello, hi, hey, yes, yeah, yep, yup, no, nope, okay, ok, sure, right, correct, exactly, good, great, fine, alright, done, thanks, bye, please, sorry, hmm, oh, ah, uh, um, well, wait, pardon, what, when, where, why, how, who, which` + Kannada equivalents: `ಹೌದು, ಇಲ್ಲ, ಸರಿ, ಆಗಲಿ, ಹಾ, ಹೂಂ, ಏನು, ಯಾರು`

Any transcript with **2+ words passes all word-count gates automatically**.

---

## 11. Silence Detection & Audio Pipeline

Used in `/ws-stt-only` (browser mic mode). Simple RMS-based, no external VAD library.

```
Incoming PCM frames (10ms @ 16kHz s16le)
     │
     ├─ RMS ≥ 100 ──► update last_speech_ts, append to buffer
     └─ RMS < 100 ──► silence; if (now - last_speech_ts) > 450ms → fire STT
                                  │
                                  ▼
                        _trim_silence(buffer)
                          removes trailing frames below RMS threshold,
                          keeps 80ms tail after last speech frame
                                  │
                                  ▼
                        len(trimmed) ≥ 200ms worth of bytes?
                          No → drop (too short to contain speech)
                          Yes → convert PCM → WAV → dispatch to run_stt_http()
```

**Tuning knobs:**

| Param | Default | Env var | Effect |
|---|---|---|---|
| Silence threshold | 450 ms | `STT_ONLY_SILENCE_MS` | Time of quiet before STT fires |
| RMS speech threshold | 100 | `STT_ONLY_RMS_SPEECH` | Minimum energy level to count as speech |
| Min audio gate | 200 ms | `STT_ONLY_MIN_AUDIO_MS` | Shortest audio sent to any provider |

---

## 12. Latency Profile

End-to-end latency = silence detection delay + audio trim + semaphore wait + HTTP round-trip + filtering.

| Component | Typical value | Notes |
|---|---|---|
| Silence detection lag | ~450 ms | Configured; user controls the trade-off |
| Audio trim + WAV encoding | < 2 ms | Local, in-process |
| Semaphore wait | 0 – 50 ms | Near-zero under normal load (slots available) |
| Sarvam HTTP round-trip | 300 – 700 ms | Depends on audio length and Sarvam infra |
| Deepgram HTTP round-trip | 200 – 500 ms | Slightly faster than Sarvam |
| Groq Whisper round-trip | 300 – 800 ms | Groq optimises for throughput |
| Post-processing (filtering) | < 1 ms | Pure in-process |
| **Total (happy path)** | **~750 ms – 1.2 s** | Silence threshold dominates |

> **Note:** The 450ms silence threshold is intentional — it is the primary latency lever. Reducing it below 350ms increases the chance of splitting mid-sentence utterances.

---

## 13. Scalability & Reliability (NFRs)

### Scalability
- **Horizontal**: Multiple server instances can run in parallel. The Sarvam RPM gate is per-process — each instance maintains its own window. For true multi-instance RPM enforcement, a shared Redis counter would be needed (not yet implemented).
- **Vertical**: The `STT_MAX_CONCURRENT` and `STT_DEFAULT_CLIENT_MAX_CONCURRENT` knobs scale linearly. A machine with higher network bandwidth and lower latency can safely run with higher concurrency values.
- **Client growth**: Adding a new client requires only `.env` changes. No database, no code change.

### Reliability
- **Zero single point of failure at provider level**: 3-provider chain ensures a transcript is returned even if Sarvam is fully down.
- **No blocking on provider failure**: Timeouts (8s) and queue timeouts (1.5s) bound worst-case latency regardless of provider health.
- **Graceful degradation**: Under heavy load, excess requests are dropped with a log entry — they do not block or delay in-flight calls.
- **Session state is in-memory**: No database dependency for the STT path. Session cleanup runs periodically to prevent unbounded memory growth.

### Availability
- **Provider uptime dependency**: Sarvam, Deepgram, and Groq are all external SaaS. Failures are mitigated by the fallback chain but not eliminated.
- **Process restart recovery**: All state is in-memory. On restart, sessions start fresh — no data loss for the STT pipeline itself.

### Throughput capacity (current configuration)
| Metric | Value |
|---|---|
| Max simultaneous STT calls | 10 |
| Max calls/sec (all clients) | ~10 (limited by 8s timeout × 10 slots) |
| Max Sarvam calls/min | 110 |
| Max clients before contention | ~3–5 (at 4 RPS each, within 110 RPM budget) |

### Security
- API keys are in `.env` only — never logged, never included in responses.
- Client IDs are resolved server-side from phone numbers — callers cannot spoof a different client.
- The STT endpoint accepts only binary WebSocket frames; text frames are ignored.

---

## 14. Observability & Metrics

### Endpoint: `GET /stt/metrics`
Returns a JSON snapshot of the STT system. Supports filtering by client:

```
GET /stt/metrics                      → system-wide stats
GET /stt/metrics?client_id=clinic_abc → filtered to one client
GET /stt/metrics?session_id=<sid>     → session cost lookup
```

**Fields returned:**
- `active_calls` — current vobiz + browser call count
- `total_requests`, `success_rate` — overall and per-client
- `fallback_rate` — % of calls that fell back from Sarvam
- `avg_latency_ms`, `p95_latency_ms` — provider latency distribution
- `estimated_cost_inr` — accumulated cost
- `recent_events` — last N request event objects (with provider, latency, error, client, session)

### Log lines emitted
| Prefix | Meaning |
|---|---|
| `[STT][HTTP] provider=sarvam text='...'` | Successful transcript |
| `[STT][FILTER] Dropped transcript: '...'` | Noise/duplicate filter |
| `[STT-ONLY][DROP] non-EN/KN script` | Language gate |
| `[STT-ONLY][DROP] single non-whitelisted word` | Word gate |
| `[STT-ONLY][DROP] likely hallucination` | Hallucination guard |
| `[STT][RPM] Global Sarvam RPM limit reached` | Sarvam bypassed, Deepgram used |
| `[STT][DROP] queue timeout` | Semaphore queue full, chunk skipped |

---

## 15. Environment Variable Reference

### API Keys
| Variable | Required | Description |
|---|---|---|
| `SARVAM_API_KEY` | Yes | Primary Sarvam key (also used by Sarvam LLM client) |
| `SARVAM_API_KEYS` | Recommended | Comma-separated pool of Sarvam keys for STT rotation |
| `DEEPGRAM_API_KEY` | Recommended | Deepgram API key (fallback 1) |
| `GROQ_API_KEY` | Yes (LLM) | Also used for Groq Whisper STT (fallback 2) |

### STT Concurrency & Rate Limiting
| Variable | Default | Description |
|---|---|---|
| `STT_MAX_CONCURRENT` | `10` | Global in-flight HTTP calls cap |
| `STT_DEFAULT_CLIENT_MAX_CONCURRENT` | `3` | Per-client concurrent cap |
| `STT_DEFAULT_CLIENT_MAX_RPS` | `4` | Per-client requests/sec |
| `SARVAM_API_RPM_LIMIT` | `110` | Global Sarvam requests/60s window |
| `STT_QUEUE_WAIT_TIMEOUT_SEC` | `1.5` | Max wait for a semaphore slot |
| `STT_REQUEST_TIMEOUT_SEC` | `8.0` | Hard HTTP timeout per attempt |

### STT Quality & Retry
| Variable | Default | Description |
|---|---|---|
| `STT_PRIMARY_RETRIES` | `1` | Extra attempts on Sarvam before fallback |
| `STT_RETRY_BACKOFF_BASE_SEC` | `0.25` | Retry backoff base delay |
| `STT_RETRY_BACKOFF_MAX_SEC` | `1.5` | Retry backoff ceiling |
| `STT_DUPLICATE_WINDOW_SEC` | `3.5` | Suppress identical transcript within N seconds |
| `STT_MIN_TRANSCRIPT_CHARS` | `3` | Drop transcripts shorter than this |
| `GROQ_WHISPER_MODEL` | `whisper-large-v3-turbo` | Override Groq model |

### STT-Only Silence Detection
| Variable | Default | Description |
|---|---|---|
| `STT_ONLY_SILENCE_MS` | `450` | Silence duration before STT fires |
| `STT_ONLY_RMS_SPEECH` | `100` | RMS energy level to count as speech |
| `STT_ONLY_MIN_AUDIO_MS` | `200` | Drop audio shorter than this before sending |

### Session & Cost
| Variable | Default | Description |
|---|---|---|
| `STT_SESSION_TTL_SEC` | `1800` | Inactive session eviction (30 min) |
| `STT_MAX_TRACKED_SESSIONS` | `5000` | Cap on in-memory session state entries |
| `STT_MAX_COST_INR_PER_SESSION` | `0` | Session cost cap (0 = disabled) |
| `STT_DEFAULT_CLIENT_MAX_COST_INR` | `0` | Default client cost cap (0 = disabled) |

### Client Mapping
| Variable | Format | Description |
|---|---|---|
| `DEFAULT_CLIENT_ID` | string | Fallback client ID if no match found |
| `CLIENT_PHONE_MAP` | `+91xxx:client_id,...` | Phone-to-client map (simple format) |
| `CLIENT_PHONE_MAP_JSON` | JSON object | Phone-to-client map (JSON format) |
| `STT_CLIENT_CONFIG_JSON` | JSON object | Per-client concurrency/cost overrides |

### HTTP Pool
| Variable | Default | Description |
|---|---|---|
| `STT_HTTP_POOL_LIMIT` | `50` | Total aiohttp connection pool size |
| `STT_HTTP_POOL_PER_HOST` | `20` | Per-host connection limit |
