# Khyra AI — Voice Assistant for Healthcare

A production-ready, real-time voice assistant that handles clinic appointment booking, cancellation, and rescheduling over phone calls. Supports both **English** and **Kannada**.

---

## What It Does

- Answers incoming calls and converses naturally with patients
- Books, cancels, and reschedules appointments via voice
- Checks slot availability in real time against a PostgreSQL database
- Falls back to a secondary appointments table if the primary is unreachable
- Syncs confirmed bookings to N8N for doctor-facing workflow automation
- Logs every STT, TTS, and LLM event for observability

---

## Stack

| Layer | Technology |
|---|---|
| Voice / Telephony | Vobiz (WebSocket) |
| Speech-to-Text | Sarvam AI (Kannada + English) |
| LLM | Groq — Llama 3.3 70B |
| Text-to-Speech | Cartesia |
| Backend | FastAPI + Uvicorn |
| Database | PostgreSQL 16 (Docker) |
| Automation | N8N webhook |

---

## Project Structure

```
src/
├── main.py              # FastAPI app — WebSocket entry point
├── agent1.py            # Language detection agent
├── agent2_en.py         # Booking agent (English)
├── agent2_kn.py         # Booking agent (Kannada)
├── agent3_en.py         # Cancel / reschedule agent (English)
├── agent3_kn.py         # Cancel / reschedule agent (Kannada)
├── database.py          # All DB logic — schema, sessions, appointments, telemetry
├── pg.py                # PostgreSQL connection pool
├── llm.py               # Groq LLM pool with key rotation
├── utils.py             # State, session store, helpers
├── client_config.py     # Clinic config loader
├── client_config.json   # Clinic config (DID → client mapping)
├── stt/                 # Speech-to-text module
└── tts/                 # Text-to-speech module
```

---

## How to Run

### Prerequisites
- Python 3.11+
- Docker (for PostgreSQL)
- API keys for Sarvam, Groq, Cartesia, Vobiz (see `.env.example`)

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
# Edit .env and fill in your real API keys
```

### 3. Start PostgreSQL

```bash
docker compose up -d
```

### 4. Run the server

```bash
cd src
uvicorn main:app --host 0.0.0.0 --port 8000
```

The server starts, connects to PostgreSQL, and initialises all 10 tables automatically on first run.

---

## Environment Variables

See `.env.example` for the full list. Key variables:

| Variable | Purpose |
|---|---|
| `SARVAM_API_KEYS` | Comma-separated STT API keys |
| `GROQ_API_KEYS` | Comma-separated LLM API keys |
| `CARTESIA_API_KEYS` | Comma-separated TTS API keys |
| `DATABASE_URL` | PostgreSQL connection string |
| `N8N_WEBHOOK_URL` | N8N booking webhook |
| `SERVER_BASE_URL` | Public server URL (ngrok or production) |
| `VOBIZ_AUTH_ID` / `VOBIZ_AUTH_TOKEN` | Telephony credentials |
