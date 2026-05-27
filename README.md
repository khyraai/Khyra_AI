# Khyra AI — Voice Assistant for Healthcare

A production-ready, real-time voice AI platform that handles clinic appointment booking, cancellation, and rescheduling over phone calls. Supports multilingual conversations — currently **English** and **Kannada** — with a pipeline designed to extend to additional Indian languages.

---

## What It Does

- Answers incoming calls and converses naturally with patients in their preferred language
- Books, cancels, and reschedules appointments through voice interaction
- Detects patient language automatically and routes to the correct language agent
- Checks slot availability in real time against a PostgreSQL database
- Falls back to a secondary appointments table if the primary is unreachable
- Syncs confirmed bookings to an N8N workflow for doctor-facing automation
- Logs every STT, TTS, and LLM event for full observability and debugging

---

## Architecture

```
 ┌──────────────┐        WebSocket         ┌─────────────────────────────────────┐
 │  Phone Call  │ ──────────────────────► │          FastAPI Application          │
 │  (Vobiz SIP) │                          │                                       │
 └──────────────┘                          │  ┌─────────────────────────────────┐  │
                                           │  │    Agent 1 — Language Detection  │  │
                                           │  └──────────────┬──────────────────┘  │
                                           │                 │                      │
                                           │        ┌────────┴────────┐            │
                                           │        ▼                 ▼            │
                                           │  ┌──────────┐    ┌──────────────┐    │
                                           │  │ Agent 2  │    │   Agent 3    │    │
                                           │  │(Booking) │    │(Cancel/Rsch.)│    │
                                           │  └────┬─────┘    └──────┬───────┘    │
                                           │       └────────┬─────────┘           │
                                           │                ▼                      │
                                           │  ┌─────────────────────────────────┐  │
                                           │  │     Groq LLM  (Llama 3.3 70B)   │  │
                                           │  └─────────────────────────────────┘  │
                                           │                                       │
                                           │  ┌──────────────┐  ┌──────────────┐  │
                                           │  │  Sarvam STT  │  │ Cartesia TTS │  │
                                           │  └──────────────┘  └──────────────┘  │
                                           │                                       │
                                           │  ┌──────────────┐  ┌──────────────┐  │
                                           │  │  PostgreSQL  │  │ N8N Webhook  │  │
                                           │  │   (Docker)   │  │  (Workflow)  │  │
                                           │  └──────────────┘  └──────────────┘  │
                                           └─────────────────────────────────────┘
```

### Call Flow

1. **Inbound call** arrives at the Vobiz telephony platform and triggers a WebSocket connection to the FastAPI server.
2. **Agent 1** streams the first audio chunk through Sarvam STT and detects the patient's language (English / Kannada).
3. The session is handed to **Agent 2** (booking) or **Agent 3** (cancel / reschedule) depending on the patient's intent.
4. Each agent runs an LLM conversation loop — STT transcribes patient speech, Groq generates a response, Cartesia synthesises audio, and the response is streamed back over the WebSocket in real time.
5. On booking confirmation, the agent writes the appointment to **PostgreSQL** and fires an **N8N webhook** to notify the clinic's doctor-facing workflow.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Voice / Telephony | Vobiz (WebSocket) |
| Speech-to-Text | Sarvam AI — multilingual Indian languages |
| LLM | Groq — Llama 3.3 70B Versatile |
| Text-to-Speech | Cartesia |
| Backend | FastAPI + Uvicorn (Python 3.11) |
| Database | PostgreSQL 16 |
| Workflow Automation | N8N webhook |
| Containerisation | Docker + Docker Compose |
| Reverse Proxy | Nginx + TLS (Let's Encrypt) |
| Cloud | DigitalOcean (Linux VM) |

---

## Project Structure

```
Khyra_voice_ai/
├── src/
│   ├── main.py              # FastAPI app — WebSocket + HTTP entry point
│   ├── agent1.py            # Language detection agent
│   ├── agent2_en.py         # Booking agent (English)
│   ├── agent2_kn.py         # Booking agent (Kannada)
│   ├── agent3_en.py         # Cancel / reschedule agent (English)
│   ├── agent3_kn.py         # Cancel / reschedule agent (Kannada)
│   ├── database.py          # DB logic — schema, sessions, appointments, telemetry
│   ├── pg.py                # PostgreSQL async connection pool
│   ├── llm.py               # Groq LLM pool with key rotation
│   ├── utils.py             # State management, session store, helpers
│   ├── client_config.py     # Per-clinic config loader
│   ├── client_config.json   # DID → client mapping (excluded from VCS)
│   ├── stt/                 # Speech-to-text module (Sarvam)
│   └── tts/                 # Text-to-speech module (Cartesia)
├── nginx/
│   └── khyra.conf           # Nginx reverse-proxy + WebSocket upgrade config
├── docs/                    # Internal technical documentation
├── Dockerfile               # App container image
├── docker-compose.yml       # App + PostgreSQL services
├── requirements.txt         # Python dependencies
└── .env.example             # Environment variable template
```

---

## Infrastructure & Deployment

### Overview

The production system runs on a single **Linux VM** (DigitalOcean) in the **Bangalore (blr1)** region, containerised with Docker Compose. Nginx sits in front of the application as a reverse proxy and handles TLS termination.

```
Internet
   │
   │  HTTPS / WSS (443)
   ▼
┌─────────────────────────────┐
│         Nginx (TLS)         │  ← Let's Encrypt certificate, auto-renewed
│   Reverse proxy + WS upgrade│
└──────────────┬──────────────┘
               │ HTTP (8000)
               ▼
┌─────────────────────────────┐
│    Docker: khyra_app        │  ← FastAPI + Uvicorn
│    (Dockerfile, port 8000)  │
└──────────────┬──────────────┘
               │ TCP (5432)
               ▼
┌─────────────────────────────┐
│    Docker: khyra_postgres   │  ← PostgreSQL 16-alpine
│    Persistent volume: pgdata│
└─────────────────────────────┘
```

### Services (Docker Compose)

| Service | Image | Purpose |
|---|---|---|
| `app` | Custom (Dockerfile) | FastAPI application |
| `postgres` | `postgres:16-alpine` | Relational database |

Both services restart automatically (`restart: unless-stopped`). App logs are persisted to a named Docker volume (`app_logs`).

### Networking

- All external traffic enters on **port 443 (HTTPS/WSS)** via Nginx.
- Nginx proxies to the app on internal port **8000** with WebSocket upgrade headers.
- PostgreSQL is not exposed externally; it is only reachable within the Docker network.
- Nginx is configured with a 300-second proxy timeout to support long-running voice sessions.

### Deployment Steps (summary)

1. Provision a Linux VM (Ubuntu 24.04 LTS recommended) with Docker installed.
2. Clone the repository to `/opt/khyra` on the server.
3. Copy `.env.example` → `.env` and fill in all API keys and credentials.
4. Set `DATABASE_URL` to use the Docker service name (`postgres`) as the host.
5. Set `SERVER_BASE_URL` to your production HTTPS domain.
6. Configure Nginx using `nginx/khyra.conf` and obtain a TLS certificate (e.g. with Certbot).
7. Run `docker compose up -d --build` to start all services.
8. Configure the Vobiz Answer URL to point to `https://<your-domain>/answer`.

### Health Check

```bash
curl https://<your-domain>/health
```

Expected: `{"message": "Vobiz Voice Assistant Running 🚀"}`

### Updating the Application

```bash
git pull origin main
docker compose up -d --build app
```

---

## Local Development

### Prerequisites
- Python 3.11+
- Docker (for PostgreSQL)
- API keys for Sarvam, Groq, Cartesia, and Vobiz (see `.env.example`)

### 1. Clone and set up environment

```bash
git clone https://github.com/khyraai/Khyra_AI.git
cd Khyra_AI

python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure environment variables

```bash
cp .env.example .env
# Edit .env and fill in your API keys
# Set DATABASE_URL to use localhost for local development
```

### 3. Start PostgreSQL

```bash
docker compose up -d postgres
```

### 4. Run the server

```bash
cd src
uvicorn main:app --host 0.0.0.0 --port 8000
```

All database tables are created automatically on first run.

For local testing with a live telephony connection, use a tunnelling tool (e.g. ngrok) and set `SERVER_BASE_URL` to the generated public URL.

---

## Environment Variables

See `.env.example` for the full list. Key variables:

| Variable | Purpose |
|---|---|
| `SARVAM_API_KEYS` | Comma-separated Sarvam STT API keys |
| `GROQ_API_KEYS` | Comma-separated Groq LLM API keys |
| `CARTESIA_API_KEYS` | Comma-separated Cartesia TTS API keys |
| `DATABASE_URL` | PostgreSQL connection string |
| `N8N_WEBHOOK_URL` | N8N booking webhook endpoint |
| `SERVER_BASE_URL` | Public server URL (tunnel or production domain) |
| `VOBIZ_AUTH_ID` / `VOBIZ_AUTH_TOKEN` | Vobiz telephony credentials |
| `LLM_MODEL` | Groq model ID (default: `llama-3.3-70b-versatile`) |
| `MAX_BOOKINGS_PER_SLOT` | Concurrency limit per appointment slot |
| `CLIENT_PHONE_MAP_JSON` | JSON map of DID numbers to client IDs |

---

## Observability

Every call session generates structured logs covering:
- STT transcript events (with latency)
- LLM request/response events (with token counts and latency)
- TTS synthesis events (with chunk timings)
- DB operation results (booking writes, slot checks)
- Session lifecycle events (connect, disconnect, language detected)

Logs are written to `.logs/` and persisted via a Docker volume in production.

---

## License

Proprietary — All rights reserved. © Khyra AI
