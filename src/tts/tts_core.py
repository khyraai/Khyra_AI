"""
tts_core.py — Text-to-Speech Orchestration

Provider chain:
    1. Cartesia AI   (primary)   — WebSocket, sonic-3, native Indian language support
    2. Sarvam TTS    (fallback 1) — HTTP POST, bulbul:v1, optimised for Indian languages
    3. ElevenLabs    (fallback 2) — HTTP POST, eleven_multilingual_v2

Two output modes:
    run_tts_collect(text, ...)  → bytes   PCM s16le 16 kHz  — Vobiz telephony
    run_tts_stream(text, ...)   → None    WebSocket chunks  — browser clients

Concurrency model (mirrors stt_core.py):
    [Gate 1] Per-client RPS check     — reject if > N req/sec
    [Gate 2] Per-client semaphore     — max concurrent per client
    [Gate 3] Global semaphore         — hard cap on total in-flight calls

Fault-safe mechanisms:
    - Provider chain: automatic fallback on any provider error
    - Queue timeout: drop request if semaphore slot not free in time
    - Request timeout: per-provider hard deadline
    - Empty / oversized text guards
    - Retry with exponential backoff on primary provider only
    - Cost guardrails: per-session and per-client spending caps
    - Session state cleanup: periodic TTL eviction

Stream-mode fallback strategy:
    Cartesia streams in real-time (WebSocket). If Cartesia fails *before*
    any audio is sent, fallback providers are called in collect mode and
    the collected bytes are forwarded to the client in one shot.
    If Cartesia fails mid-stream (audio already sent), streaming stops
    cleanly — no double-audio from a fallback attempt.
"""

import io
import os
import re
import time
import json
import wave
import base64
import random
import asyncio
import audioop
from threading import Lock
from collections import deque
from typing import Optional

import aiohttp
import websockets
from websockets.connection import State as _WsState

from .tts_metrics import (
    request_started  as _request_started,
    request_finished as _request_finished,
    get_session_spend_inr,
    get_client_spend_inr,
    cleanup_stale_sessions,
)


# ---------------------------------------------------------------------------
# Module-level configuration — all overridable via environment variables
# ---------------------------------------------------------------------------
_CARTESIA_WS_URL   = os.getenv("CARTESIA_TTS_WS_URL",  "wss://api.cartesia.ai/tts/websocket")
_CARTESIA_MODEL_ID = os.getenv("CARTESIA_MODEL_ID",     "sonic-3")
_CARTESIA_VERSION  = os.getenv("CARTESIA_VERSION",      "2025-04-16")
_CARTESIA_SPEED    = float(os.getenv("TTS_SPEED",          "0.9"))

_TTS_SEMAPHORE            = asyncio.Semaphore(int(os.getenv("TTS_MAX_CONCURRENT",                "8")))
_QUEUE_WAIT_TIMEOUT_SEC   = float(os.getenv("TTS_QUEUE_WAIT_TIMEOUT_SEC",     "2.5"))
_REQUEST_TIMEOUT_SEC      = float(os.getenv("TTS_REQUEST_TIMEOUT_SEC",        "30.0"))
_PRIMARY_RETRIES          = int(os.getenv("TTS_PRIMARY_RETRIES",              "1"))
_RETRY_BACKOFF_BASE_SEC   = float(os.getenv("TTS_RETRY_BACKOFF_BASE_SEC",     "0.3"))
_RETRY_BACKOFF_MAX_SEC    = float(os.getenv("TTS_RETRY_BACKOFF_MAX_SEC",      "2.0"))
_RETRY_JITTER_SEC         = float(os.getenv("TTS_RETRY_JITTER_SEC",          "0.1"))
_MAX_CHARS_PER_REQUEST    = int(os.getenv("TTS_MAX_CHARS_PER_REQUEST",        "2000"))
_SESSION_TTL_SEC          = float(os.getenv("TTS_SESSION_TTL_SEC",           "1800"))
_SESSION_CLEANUP_INTERVAL_SEC = float(os.getenv("TTS_SESSION_CLEANUP_INTERVAL_SEC", "60"))
_MAX_TRACKED_SESSIONS     = int(os.getenv("TTS_MAX_TRACKED_SESSIONS",        "5000"))
_MAX_COST_INR_PER_SESSION = float(os.getenv("TTS_MAX_COST_INR_PER_SESSION",  "0"))
_DEFAULT_CLIENT_ID        = (os.getenv("DEFAULT_CLIENT_ID", "default").strip() or "default")
_DEFAULT_CLIENT_MAX_CONCURRENT = int(os.getenv("TTS_DEFAULT_CLIENT_MAX_CONCURRENT", "2"))
_DEFAULT_CLIENT_MAX_RPS   = float(os.getenv("TTS_DEFAULT_CLIENT_MAX_RPS",    "3"))
_DEFAULT_CLIENT_MAX_COST_INR = float(os.getenv("TTS_DEFAULT_CLIENT_MAX_COST_INR", "0"))
_TTS_CLIENT_CONFIG_MAP: dict = {}

_HTTP_SESSION: Optional[aiohttp.ClientSession] = None
_HTTP_SESSION_LOCK   = asyncio.Lock()
_KEY_ROTATION_LOCK   = Lock()
_CARTESIA_KEY_INDEX  = 0
_CLIENT_LIMITERS_LOCK = Lock()
_CLIENT_SEMAPHORES: dict = {}
_CLIENT_RPS_WINDOW: dict = {}
_LAST_SESSION_CLEANUP_TS = 0.0


# ---------------------------------------------------------------------------
# Client config helpers
# ---------------------------------------------------------------------------
def set_tts_client_config_map(config_map: dict):
    global _TTS_CLIENT_CONFIG_MAP
    _TTS_CLIENT_CONFIG_MAP = dict(config_map or {}) if isinstance(config_map, dict) else {}


def _normalize_client_id(client_id: str) -> str:
    cid = (client_id or "").strip()
    return cid if cid else _DEFAULT_CLIENT_ID


def _get_client_limits(client_id: str) -> dict:
    cfg_map  = _TTS_CLIENT_CONFIG_MAP
    raw_cfg  = cfg_map.get(client_id, {}) if isinstance(cfg_map, dict) else {}
    if not isinstance(raw_cfg, dict):
        raw_cfg = {}

    cost_limits = raw_cfg.get("cost_limits", {})
    if not isinstance(cost_limits, dict):
        cost_limits = {}

    max_concurrent = int(raw_cfg.get("max_concurrent", _DEFAULT_CLIENT_MAX_CONCURRENT) or _DEFAULT_CLIENT_MAX_CONCURRENT)
    max_rps  = float(raw_cfg.get("max_rps", _DEFAULT_CLIENT_MAX_RPS) or _DEFAULT_CLIENT_MAX_RPS)
    max_cost = float(
        cost_limits.get("max_cost_per_client", raw_cfg.get("max_cost_inr", _DEFAULT_CLIENT_MAX_COST_INR))
        or _DEFAULT_CLIENT_MAX_COST_INR
    )
    return {
        "max_concurrent": max(1, max_concurrent),
        "max_rps":        max(0.0, max_rps),
        "max_cost_inr":   max(0.0, max_cost),
    }


def _get_client_semaphore(client_id: str, max_concurrent: int) -> asyncio.Semaphore:
    key = f"{client_id}:{max(1, int(max_concurrent))}"
    with _CLIENT_LIMITERS_LOCK:
        sem = _CLIENT_SEMAPHORES.get(key)
        if sem is None:
            sem = asyncio.Semaphore(max(1, int(max_concurrent)))
            _CLIENT_SEMAPHORES[key] = sem
    return sem


def _allow_client_rps(client_id: str, max_rps: float) -> bool:
    if max_rps <= 0:
        return True
    allowed_per_sec = max(1, int(max_rps))
    now = time.time()
    with _CLIENT_LIMITERS_LOCK:
        q = _CLIENT_RPS_WINDOW.get(client_id)
        if q is None:
            q = deque()
            _CLIENT_RPS_WINDOW[client_id] = q
        cutoff = now - 1.0
        while q and q[0] < cutoff:
            q.popleft()
        if len(q) >= allowed_per_sec:
            return False
        q.append(now)
    return True


# ---------------------------------------------------------------------------
# Session cleanup
# ---------------------------------------------------------------------------
def _cleanup_session_state(now_ts: float = 0.0):
    global _LAST_SESSION_CLEANUP_TS
    now = float(now_ts or time.time())
    if (now - _LAST_SESSION_CLEANUP_TS) < _SESSION_CLEANUP_INTERVAL_SEC:
        return
    _LAST_SESSION_CLEANUP_TS = now
    stale_cutoff = now - _SESSION_TTL_SEC
    cleanup_stale_sessions(stale_cutoff, _MAX_TRACKED_SESSIONS)


# ---------------------------------------------------------------------------
# Cartesia API key rotation
# ---------------------------------------------------------------------------
def _next_cartesia_key() -> str:
    global _CARTESIA_KEY_INDEX
    primary = os.getenv("CARTESIA_API_KEY", "").strip()
    keys_raw = os.getenv("CARTESIA_API_KEYS", "").strip()
    keys = [k.strip() for k in keys_raw.split(",") if k.strip()]
    if primary:
        keys.insert(0, primary)
    uniq: list = []
    seen: set  = set()
    for k in keys:
        if k not in seen:
            seen.add(k)
            uniq.append(k)
    if not uniq:
        return ""
    with _KEY_ROTATION_LOCK:
        key = uniq[_CARTESIA_KEY_INDEX % len(uniq)]
        _CARTESIA_KEY_INDEX += 1
    return key


# ---------------------------------------------------------------------------
# HTTP session (shared, pooled — for Sarvam TTS and ElevenLabs)
# ---------------------------------------------------------------------------
async def _get_http_session() -> aiohttp.ClientSession:
    global _HTTP_SESSION
    if _HTTP_SESSION and not _HTTP_SESSION.closed:
        return _HTTP_SESSION
    async with _HTTP_SESSION_LOCK:
        if _HTTP_SESSION and not _HTTP_SESSION.closed:
            return _HTTP_SESSION
        connector = aiohttp.TCPConnector(
            limit=int(os.getenv("TTS_HTTP_POOL_LIMIT",    "30")),
            limit_per_host=int(os.getenv("TTS_HTTP_POOL_PER_HOST", "15")),
            ttl_dns_cache=300,
        )
        timeout = aiohttp.ClientTimeout(total=_REQUEST_TIMEOUT_SEC)
        _HTTP_SESSION = aiohttp.ClientSession(connector=connector, timeout=timeout)
        return _HTTP_SESSION


async def close_tts_http_clients():
    global _HTTP_SESSION
    await _cartesia_pool.close_all()
    async with _HTTP_SESSION_LOCK:
        if _HTTP_SESSION and not _HTTP_SESSION.closed:
            await _HTTP_SESSION.close()
        _HTTP_SESSION = None


# ---------------------------------------------------------------------------
# Cost, retry, and text helpers
# ---------------------------------------------------------------------------
def _provider_cost_per_char(provider: str) -> float:
    env_map = {
        "cartesia":   "CARTESIA_COST_INR_PER_CHAR",
        "sarvam_tts": "SARVAM_TTS_COST_INR_PER_CHAR",
        "elevenlabs": "ELEVENLABS_COST_INR_PER_CHAR",
    }
    return float(os.getenv(env_map.get(provider, ""), "0") or "0")


def _estimate_attempt_cost_inr(provider: str, char_count: int, success: bool) -> float:
    if char_count <= 0 or not success:
        return 0.0
    return char_count * _provider_cost_per_char(provider)


def _bump_dict(d: dict, key: str, value: float = 1.0):
    d[key] = d.get(key, 0) + value


def _is_retryable_error(error_type: str) -> bool:
    et = (error_type or "").strip().lower()
    if not et:
        return True
    if et in {"timeout", "network_error", "http_408", "http_425", "http_429"}:
        return True
    if re.match(r"http_5\d\d$", et):
        return True
    if et in {
        "api_key_missing", "provider_unconfigured",
        "http_400", "http_401", "http_403", "http_404",
        "http_409", "http_410", "http_413", "http_415", "http_422",
    }:
        return False
    return False


def _retry_backoff_sec(attempt_index: int) -> float:
    base   = _RETRY_BACKOFF_BASE_SEC * (2 ** max(0, attempt_index))
    jitter = random.uniform(0.0, max(0.0, _RETRY_JITTER_SEC))
    return min(_RETRY_BACKOFF_MAX_SEC, base + jitter)


def _sanitize_text(text: str) -> str:
    """Trim whitespace, sanitize emojis/markdown/annotations, and truncate."""
    t = (text or "").strip()
    if not t:
        return ""
    
    # 1. Remove stage directions in asterisks or brackets (e.g., *sigh*, [laughs])
    t = re.sub(r'\*[^*]+\*', '', t)
    t = re.sub(r'\[[^\]]+\]', '', t)
    
    # 2. Remove remaining markdown formatting (asterisks, underscores, hashes, tildes)
    t = re.sub(r'[*_#~]', '', t)
    
    # 3. Remove emojis (basic unicode range for emojis and pictographs)
    t = re.sub(r'[\U00010000-\U0010ffff]', '', t)
    
    t = t.strip()
    # 4. Truncate to TTS_MAX_CHARS_PER_REQUEST
    if len(t) > _MAX_CHARS_PER_REQUEST:
        t = t[:_MAX_CHARS_PER_REQUEST]
    return t


def _cartesia_language_code(language: str) -> str:
    """Map short language tag to Cartesia language code."""
    lang = (language or "kn").strip().lower()
    if lang.startswith("kn"):
        return "kn"
    return "en"


def _cartesia_voice_for_language(language: str) -> str:
    """Select Cartesia voice ID based on language."""
    lang = (language or "kn").strip().lower()
    if lang.startswith("kn"):
        return os.getenv("CARTESIA_VOICE_ID_KN", os.getenv("CARTESIA_VOICE_ID", ""))
    return os.getenv("CARTESIA_VOICE_ID_EN", os.getenv("CARTESIA_VOICE_ID", ""))


def _wav_bytes_to_pcm16_16k(wav_bytes: bytes) -> bytes:
    """Decode WAV (any sample rate / channel count) → PCM s16le mono 16 kHz."""
    try:
        buf = io.BytesIO(wav_bytes)
        with wave.open(buf, "rb") as wf:
            n_channels = wf.getnchannels()
            sampwidth  = wf.getsampwidth()
            framerate  = wf.getframerate()
            frames     = wf.readframes(wf.getnframes())
        if n_channels > 1:
            frames = audioop.tomono(frames, sampwidth, 0.5, 0.5)
        if sampwidth != 2:
            frames = audioop.lin2lin(frames, sampwidth, 2)
        if framerate != 16000:
            frames, _ = audioop.ratecv(frames, 2, 1, framerate, 16000, None)
        return frames
    except Exception as exc:
        print(f"[TTS] WAV→PCM conversion error: {exc}")
        return b""


# ---------------------------------------------------------------------------
# Cartesia persistent WebSocket connection pool
# ---------------------------------------------------------------------------
class _CartesiaPool:
    """One persistent WebSocket per API key; per-connection Lock serialises use.

    Design:
    - _conns[key]        — live ws or None (replaced on reconnect)
    - _use_locks[key]    — one Lock per conn; only one synthesis at a time
    - _reconn_locks[key] — one Lock per key; prevents concurrent reconnects
    - _dict_lock         — guards dict mutations (held only for brief dict ops)

    Round-robin still works: _next_cartesia_key() picks key[i % n] as before;
    each key owns a persistent connection so no handshake overhead after warm-up.
    Dead connections (network error / idle timeout) are transparently reconnected
    on next use.  WebSocket-level pings (every 20 s) prevent server-side eviction.
    """

    _CONNECT_TIMEOUT = 12.0   # max seconds to open a fresh WebSocket
    _PING_INTERVAL   = 20     # WS ping every N seconds (keeps idle conn alive)
    _PING_TIMEOUT    = 10     # seconds to wait for pong before treating as dead

    def __init__(self):
        self._conns:        dict = {}
        self._use_locks:    dict = {}
        self._reconn_locks: dict = {}
        self._dict_lock          = asyncio.Lock()

    # ── per-key structure init (called once per key, lazily) ────────────────
    async def _ensure(self, key: str) -> None:
        async with self._dict_lock:
            if key not in self._use_locks:
                self._use_locks[key]    = asyncio.Lock()
                self._reconn_locks[key] = asyncio.Lock()

    # ── return live WebSocket, reconnecting transparently if dead ────────────
    async def get_conn(self, key: str):
        await self._ensure(key)

        # fast path — existing live connection (no lock needed for a read)
        ws = self._conns.get(key)
        if ws is not None and ws.state == _WsState.OPEN:
            return ws

        # slow path — reconnect, serialised per key
        async with self._reconn_locks[key]:
            ws = self._conns.get(key)       # re-check under reconnect lock
            if ws is not None and ws.state == _WsState.OPEN:
                return ws
            ws = await self._open(key)
            self._conns[key] = ws
        return ws

    async def _open(self, key: str):
        uri = (
            f"{_CARTESIA_WS_URL}"
            f"?api_key={key}"
            f"&cartesia_version={_CARTESIA_VERSION}"
        )
        return await websockets.connect(
            uri,
            open_timeout=self._CONNECT_TIMEOUT,
            ping_interval=self._PING_INTERVAL,
            ping_timeout=self._PING_TIMEOUT,
        )

    # ── synthesis lock for this key (serialises sends on one connection) ────
    async def use_lock(self, key: str) -> asyncio.Lock:
        await self._ensure(key)
        return self._use_locks[key]

    # ── call after any synthesis error to force fresh connect next time ─────
    def mark_dead(self, key: str) -> None:
        ws = self._conns.pop(key, None)
        if ws and ws.state != _WsState.CLOSED:
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    loop.create_task(ws.close())
            except Exception:
                pass

    # ── graceful shutdown ────────────────────────────────────────────────────
    async def close_all(self) -> None:
        async with self._dict_lock:
            items = list(self._conns.items())
            self._conns.clear()
        for _, ws in items:
            if ws and ws.state != _WsState.CLOSED:
                try:
                    await asyncio.wait_for(ws.close(), timeout=2.0)
                except Exception:
                    pass


_cartesia_pool = _CartesiaPool()


# ---------------------------------------------------------------------------
# Provider: Cartesia — collect mode (returns bytes)
# ---------------------------------------------------------------------------
async def _cartesia_attempt_collect(text: str, language: str, api_key: str) -> tuple:
    """Cartesia WebSocket TTS — collect all PCM chunks into one bytes object.

    Uses the persistent connection pool (_cartesia_pool).  The per-key use_lock
    ensures only one synthesis at a time per connection; get_conn() reconnects
    transparently if the idle connection was evicted by the server.

    Returns: (ok, pcm_bytes, error_type, error_msg, timed_out)
    """
    if not api_key:
        return False, b"", "api_key_missing", "no Cartesia API key configured", False

    import uuid
    context_id = str(uuid.uuid4())
    voice_id   = _cartesia_voice_for_language(language)
    pcm_chunks: list = []

    payload = json.dumps({
        "context_id":    context_id,
        "model_id":      _CARTESIA_MODEL_ID,
        "transcript":    text,
        "voice":         {"mode": "id", "id": voice_id},
        "language":      _cartesia_language_code(language),
        "output_format": {"container": "raw", "encoding": "pcm_s16le", "sample_rate": 16000},
        "generation_config": {"speed": _CARTESIA_SPEED},
        "add_timestamps": False,
    })

    use_lock = await _cartesia_pool.use_lock(api_key)

    async def _do_collect():
        ws = await _cartesia_pool.get_conn(api_key)
        await ws.send(payload)
        async for raw_msg in ws:
            try:
                msg = json.loads(raw_msg)
            except Exception:
                continue
            msg_type = msg.get("type", "")
            if msg_type == "chunk":
                b64 = msg.get("data", "")
                if b64:
                    pcm_chunks.append(base64.b64decode(b64))
            elif msg_type == "done":
                break
            elif msg_type == "error":
                raise RuntimeError(str(msg.get("message", msg)))

    try:
        async with use_lock:
            await asyncio.wait_for(_do_collect(), timeout=_REQUEST_TIMEOUT_SEC)
    except asyncio.TimeoutError:
        _cartesia_pool.mark_dead(api_key)
        return False, b"", "timeout", "", True
    except Exception as exc:
        _cartesia_pool.mark_dead(api_key)
        return False, b"", "network_error", str(exc), False

    pcm = b"".join(pcm_chunks)
    if not pcm:
        return False, b"", "empty_response", "no audio chunks received from Cartesia", False
    return True, pcm, "", "", False


# ---------------------------------------------------------------------------
# Provider: Cartesia — stream mode (sends chunks directly to browser WS)
# ---------------------------------------------------------------------------
async def _cartesia_attempt_stream(
    text: str,
    language: str,
    api_key: str,
    safe_send_bytes,
    safe_send_text,
    stt_start_time: float = 0.0,
) -> tuple:
    """Cartesia WebSocket TTS — true streaming to browser.

    Uses the persistent connection pool (_cartesia_pool).  The per-key use_lock
    ensures only one stream at a time per connection; reconnects transparently
    if the connection was evicted by the server between turns.

    Returns: (ok, sent_any_audio, error_type, error_msg, timed_out)
    sent_any_audio=True means audio_start was already sent; caller must not
    attempt another provider as that would produce duplicate/garbled audio.
    """
    if not api_key:
        return False, False, "api_key_missing", "no Cartesia API key configured", False

    import uuid
    context_id     = str(uuid.uuid4())
    voice_id       = _cartesia_voice_for_language(language)
    sent_any_audio = False

    payload = json.dumps({
        "context_id":    context_id,
        "model_id":      _CARTESIA_MODEL_ID,
        "transcript":    text,
        "voice":         {"mode": "id", "id": voice_id},
        "language":      _cartesia_language_code(language),
        "output_format": {"container": "raw", "encoding": "pcm_s16le", "sample_rate": 16000},
        "generation_config": {"speed": _CARTESIA_SPEED},
        "add_timestamps": False,
    })

    use_lock = await _cartesia_pool.use_lock(api_key)

    async def _do_stream():
        nonlocal sent_any_audio
        ws = await _cartesia_pool.get_conn(api_key)
        await ws.send(payload)
        async for raw_msg in ws:
            try:
                msg = json.loads(raw_msg)
            except Exception:
                continue
            msg_type = msg.get("type", "")
            if msg_type == "chunk":
                b64 = msg.get("data", "")
                if not b64:
                    continue
                chunk = base64.b64decode(b64)
                if not sent_any_audio:
                    sent_any_audio = True
                    await safe_send_text(json.dumps({"type": "audio_start", "sample_rate": 16000}))
                    if stt_start_time:
                        ttfa = time.time() - stt_start_time
                        print(f"[TTS][Cartesia] TTFA {ttfa:.3f}s (STT→first audio)")
                await safe_send_bytes(chunk)
            elif msg_type == "done":
                break
            elif msg_type == "error":
                raise RuntimeError(str(msg.get("message", msg)))

    try:
        async with use_lock:
            await asyncio.wait_for(_do_stream(), timeout=_REQUEST_TIMEOUT_SEC)
        return True, sent_any_audio, "", "", False
    except asyncio.TimeoutError:
        _cartesia_pool.mark_dead(api_key)
        return False, sent_any_audio, "timeout", "", True
    except Exception as exc:
        _cartesia_pool.mark_dead(api_key)
        return False, sent_any_audio, "network_error", str(exc), False


# ---------------------------------------------------------------------------
# Provider: Sarvam TTS — HTTP POST (fallback 1)
# ---------------------------------------------------------------------------
async def _sarvam_tts_attempt(text: str, language: str) -> tuple:
    """Sarvam TTS via HTTP POST.

    Returns: (ok, pcm_bytes, error_type, error_msg, timed_out)
    """
    key = os.getenv("SARVAM_API_KEY", "").strip()
    if not key:
        return False, b"", "api_key_missing", "no SARVAM_API_KEY configured", False

    lang_code = "kn-IN" if (language or "").lower().startswith("kn") else "en-IN"
    speaker   = (
        os.getenv("SARVAM_TTS_SPEAKER_KN", "kavya")
        if lang_code == "kn-IN"
        else os.getenv("SARVAM_TTS_SPEAKER_EN", "kavya")
    )
    url     = "https://api.sarvam.ai/text-to-speech"
    headers = {"api-subscription-key": key, "Content-Type": "application/json"}
    payload = {
        "inputs":               [text],
        "target_language_code": lang_code,
        "speaker":              speaker,
        "model":                os.getenv("SARVAM_TTS_MODEL", "bulbul:v2"),
        "speech_sample_rate":   16000,
        "enable_preprocessing": True,
    }

    session = await _get_http_session()
    try:
        async with session.post(url, headers=headers, json=payload) as resp:
            body = await resp.text()
            if resp.status == 200:
                data   = await resp.json(content_type=None)
                audios = data.get("audios", [])
                if not audios:
                    return False, b"", "empty_response", "no audios field in Sarvam TTS response", False
                wav_bytes = base64.b64decode(audios[0])
                pcm_bytes = _wav_bytes_to_pcm16_16k(wav_bytes)
                if not pcm_bytes:
                    return False, b"", "conversion_error", "WAV→PCM conversion failed", False
                return True, pcm_bytes, "", "", False
            timed_out = resp.status == 408
            return False, b"", f"http_{resp.status}", body[:400], timed_out
    except asyncio.TimeoutError:
        return False, b"", "timeout", "", True
    except Exception as exc:
        return False, b"", "network_error", str(exc), False


# ---------------------------------------------------------------------------
# Provider: ElevenLabs — HTTP POST (fallback 2)
# ---------------------------------------------------------------------------
async def _elevenlabs_attempt(text: str, language: str) -> tuple:
    """ElevenLabs TTS via HTTP POST, requesting PCM 16 kHz output directly.

    Returns: (ok, pcm_bytes, error_type, error_msg, timed_out)
    """
    key      = os.getenv("ELEVENLABS_API_KEY", "").strip()
    voice_id = os.getenv("ELEVENLABS_VOICE_ID", "").strip()
    if not key or not voice_id:
        return False, b"", "provider_unconfigured", "ELEVENLABS_API_KEY or ELEVENLABS_VOICE_ID not set", False

    url     = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}?output_format=pcm_16000"
    headers = {"xi-api-key": key, "Content-Type": "application/json"}
    payload = {
        "text":     text,
        "model_id": os.getenv("ELEVENLABS_MODEL_ID", "eleven_multilingual_v2"),
    }

    session = await _get_http_session()
    try:
        async with session.post(url, headers=headers, json=payload) as resp:
            if resp.status == 200:
                pcm_bytes = await resp.read()
                if not pcm_bytes:
                    return False, b"", "empty_response", "no PCM data in ElevenLabs response", False
                return True, pcm_bytes, "", "", False
            body      = await resp.text()
            timed_out = resp.status == 408
            return False, b"", f"http_{resp.status}", body[:400], timed_out
    except asyncio.TimeoutError:
        return False, b"", "timeout", "", True
    except Exception as exc:
        return False, b"", "network_error", str(exc), False


# ---------------------------------------------------------------------------
# Internal: run the collect provider chain (shared by both public functions)
# ---------------------------------------------------------------------------
async def _collect_provider_chain(
    text: str,
    language: str,
    char_count: int,
    *,
    skip_primary: bool = False,
) -> tuple:
    """Try providers in order, return (ok, pcm_bytes, chosen_provider, fallback_used,
    retries, timed_out, error_type, error_msg, provider_latency_ms, provider_attempt_cost_inr).
    """
    provider_chain = ["cartesia", "sarvam_tts", "elevenlabs"]
    chosen_provider        = "none"
    retries                = 0
    fallback_used          = False
    timed_out              = False
    pcm_bytes              = b""
    error_type             = ""
    error_msg              = ""
    provider_latency_ms    = 0.0
    provider_attempt_cost  = {}

    for p in provider_chain:
        if p == "cartesia" and skip_primary:
            error_type = "primary_skipped"
            continue

        provider_ok = False
        attempts    = (1 + max(0, _PRIMARY_RETRIES)) if p == "cartesia" else 1

        for idx in range(attempts):
            attempt_t0 = time.time()

            if p == "cartesia":
                api_key = _next_cartesia_key()
                ok, pcm, et, em, to = await _cartesia_attempt_collect(text, language, api_key)
            elif p == "sarvam_tts":
                ok, pcm, et, em, to = await _sarvam_tts_attempt(text, language)
            elif p == "elevenlabs":
                ok, pcm, et, em, to = await _elevenlabs_attempt(text, language)
            else:
                break

            provider_latency_ms += max(0.0, (time.time() - attempt_t0) * 1000.0)
            attempt_cost = _estimate_attempt_cost_inr(p, char_count, ok)
            if attempt_cost > 0:
                _bump_dict(provider_attempt_cost, p, attempt_cost)

            if idx > 0:
                retries += 1

            if ok:
                chosen_provider = p
                provider_ok     = True
                pcm_bytes       = pcm
                if p != "cartesia":
                    fallback_used = True
                break

            if et == "provider_unconfigured":
                break

            error_type = et or "provider_error"
            error_msg  = em or ""
            timed_out  = timed_out or bool(to)

            if not _is_retryable_error(error_type):
                break

            if idx < (attempts - 1):
                await asyncio.sleep(_retry_backoff_sec(idx))

        if provider_ok:
            break

    return (
        bool(pcm_bytes),
        pcm_bytes,
        chosen_provider,
        fallback_used,
        retries,
        timed_out,
        error_type,
        error_msg,
        provider_latency_ms,
        provider_attempt_cost,
    )


# ---------------------------------------------------------------------------
# Concurrency gate prologue — shared by both public entry points
# ---------------------------------------------------------------------------
async def _acquire_gates(resolved_client_id: str, client_limits: dict, started: float):
    """Acquire per-client and global semaphores.

    Returns (client_sem, client_acquired, acquired, queue_wait_ms, gate_error_type)
    gate_error_type is non-empty if gates could not be acquired.
    """
    client_max_concurrent = int(client_limits["max_concurrent"])
    client_max_rps        = float(client_limits["max_rps"])

    if not _allow_client_rps(resolved_client_id, client_max_rps):
        return None, False, False, 0.0, "client_rps_limited"

    client_sem      = _get_client_semaphore(resolved_client_id, client_max_concurrent)
    client_acquired = False
    acquired        = False
    acquire_t0      = time.time()

    try:
        await asyncio.wait_for(client_sem.acquire(), timeout=_QUEUE_WAIT_TIMEOUT_SEC)
        client_acquired = True
        elapsed          = time.time() - acquire_t0
        remaining        = max(0.05, _QUEUE_WAIT_TIMEOUT_SEC - elapsed)
        await asyncio.wait_for(_TTS_SEMAPHORE.acquire(), timeout=remaining)
        acquired = True
    except asyncio.TimeoutError:
        gate_error = "queue_timeout" if client_acquired else "client_queue_timeout"
        queue_wait = round((time.time() - acquire_t0) * 1000.0, 2)
        return client_sem, client_acquired, acquired, queue_wait, gate_error

    queue_wait_ms = round((time.time() - acquire_t0) * 1000.0, 2)
    return client_sem, client_acquired, acquired, queue_wait_ms, ""


def _release_gates(client_sem, client_acquired: bool, acquired: bool):
    if acquired:
        _TTS_SEMAPHORE.release()
    if client_acquired and client_sem is not None:
        client_sem.release()


# ---------------------------------------------------------------------------
# Public entry point 1: collect mode (Vobiz telephony)
# ---------------------------------------------------------------------------
async def run_tts_collect(
    text: str,
    *,
    client_id: str = "default",
    session_id: str = "",
    language: str = "kn",
    stt_start_time: float = 0.0,
) -> bytes:
    """Collect all TTS audio as PCM s16le 16 kHz bytes.

    Used by Vobiz telephony handler. Returns empty bytes on total failure —
    never raises, never hangs beyond TTS_REQUEST_TIMEOUT_SEC.
    """
    _cleanup_session_state()

    text = _sanitize_text(text)
    if not text:
        return b""

    resolved_client_id = _normalize_client_id(client_id)
    client_limits      = _get_client_limits(resolved_client_id)
    char_count         = len(text)
    started            = time.time()

    request_event = {
        "ts":          round(started, 3),
        "client_id":   resolved_client_id,
        "session_id":  (session_id or "unknown"),
        "char_count":  char_count,
        "language":    language,
        "mode":        "collect",
        "provider":    "none",
        "retry_count": 0,
        "fallback_used": False,
        "success":     False,
        "queue_rejected": False,
        "timed_out":   False,
        "error_type":  "",
        "error":       "",
    }
    _request_started()

    client_sem = None
    client_acquired = False
    acquired        = False

    try:
        client_sem, client_acquired, acquired, queue_wait_ms, gate_err = await _acquire_gates(
            resolved_client_id, client_limits, started
        )
        if gate_err:
            request_event.update({
                "queue_rejected":   True,
                "error_type":       gate_err,
                "error":            f"gate blocked: {gate_err}",
                "total_latency_ms": round((time.time() - started) * 1000.0, 2),
                "queue_wait_ms":    queue_wait_ms,
                "estimated_cost_inr": 0.0,
            })
            _request_finished(request_event)
            if gate_err == "client_rps_limited":
                print(f"[TTS][DROP] client_rps_limited client={resolved_client_id}")
            else:
                print(f"[TTS][DROP] {gate_err} — request skipped")
            return b""

        # Cost guardrails
        session_spend = get_session_spend_inr((session_id or "unknown").strip())
        client_spend  = get_client_spend_inr(resolved_client_id)
        client_max_cost = float(client_limits["max_cost_inr"])

        if _MAX_COST_INR_PER_SESSION > 0 and session_spend >= _MAX_COST_INR_PER_SESSION:
            request_event.update({"error_type": "cost_guardrail_exceeded", "total_latency_ms": 0.0, "estimated_cost_inr": 0.0})
            _request_finished(request_event)
            return b""

        if client_max_cost > 0 and client_spend >= client_max_cost:
            request_event.update({"error_type": "client_cost_guardrail_exceeded", "total_latency_ms": 0.0, "estimated_cost_inr": 0.0})
            _request_finished(request_event)
            return b""

        ok, pcm, chosen, fallback_used, retries, timed_out, et, em, prov_lat_ms, attempt_costs = (
            await _collect_provider_chain(text, language, char_count)
        )

        cost_inr         = float(sum(attempt_costs.values()))
        total_latency_ms = round((time.time() - started) * 1000.0, 2)

        if ok:
            print(f"[TTS][COLLECT] provider={chosen} chars={char_count} lang={language} pcm={len(pcm)}B latency={total_latency_ms:.0f}ms")

        request_event.update({
            "provider":           chosen,
            "retry_count":        retries,
            "fallback_used":      fallback_used,
            "success":            ok,
            "timed_out":          timed_out,
            "error_type":         et,
            "error":              (em or "")[:500],
            "queue_wait_ms":      queue_wait_ms,
            "api_latency_ms":     round(prov_lat_ms, 2),
            "total_latency_ms":   total_latency_ms,
            "estimated_cost_inr": round(cost_inr, 8),
            "provider_attempt_cost_inr": {k: round(v, 8) for k, v in attempt_costs.items()},
            "pcm_bytes":          len(pcm),
        })
        _request_finished(request_event)
        return pcm

    except Exception as exc:
        request_event.update({
            "error_type": "exception", "error": str(exc)[:500],
            "total_latency_ms": round((time.time() - started) * 1000.0, 2),
            "estimated_cost_inr": 0.0,
        })
        _request_finished(request_event)
        print(f"[TTS][COLLECT] Exception: {exc}")
        return b""
    finally:
        _release_gates(client_sem, client_acquired, acquired)


# ---------------------------------------------------------------------------
# Provider: Sarvam Bulbul v3 HTTP streaming (primary chunked provider)
# ---------------------------------------------------------------------------
async def _sarvam_tts_stream_chunked(
    text: str,
    language: str,
    min_chunk_ms: int = 300,
):
    """Sarvam Bulbul v3 HTTP streaming TTS — async generator yielding PCM s16le 16kHz.

    Uses /text-to-speech/stream endpoint with linear16 codec at 16kHz.
    Yields nothing on any error so caller can fall back to Cartesia/ElevenLabs.
    """
    key = os.getenv("SARVAM_API_KEY", "").strip()
    if not key:
        return

    lang_code = "kn-IN" if (language or "").lower().startswith("kn") else "en-IN"
    speaker = (
        os.getenv("SARVAM_TTS_SPEAKER_KN", "kavya")
        if lang_code == "kn-IN"
        else os.getenv("SARVAM_TTS_SPEAKER_EN", "kavya")
    )

    url     = "https://api.sarvam.ai/text-to-speech/stream"
    headers = {"api-subscription-key": key, "Content-Type": "application/json"}
    payload = {
        "text":                 text,
        "target_language_code": lang_code,
        "speaker":              speaker,
        "model":                os.getenv("SARVAM_TTS_MODEL", "bulbul:v3"),
        "output_audio_codec":   "linear16",
        "speech_sample_rate":   16000,
        "pace":                 float(os.getenv("SARVAM_TTS_PACE", "0.95")),
        "enable_preprocessing": True,
    }

    # min_chunk_bytes at 16kHz PCM s16le (2 bytes/sample)
    min_chunk_bytes = max(4000, int(min_chunk_ms * 16000 * 2 / 1000))
    buffer: list   = []
    buf_bytes: int = 0

    session = await _get_http_session()
    try:
        async with session.post(url, headers=headers, json=payload) as resp:
            if resp.status != 200:
                body = await resp.text()
                print(f"[TTS][SARVAM-STREAM] HTTP {resp.status}: {body[:200]}")
                return
            async for chunk in resp.content.iter_chunked(8192):
                if not chunk:
                    continue
                buffer.append(chunk)
                buf_bytes += len(chunk)
                if buf_bytes >= min_chunk_bytes:
                    yield b"".join(buffer)
                    buffer  = []
                    buf_bytes = 0
    except asyncio.TimeoutError:
        print("[TTS][SARVAM-STREAM] Timeout")
        return
    except Exception as exc:
        print(f"[TTS][SARVAM-STREAM] Exception: {exc}")
        return

    if buffer:
        yield b"".join(buffer)


# ---------------------------------------------------------------------------
# Public entry point 1b: chunked collect mode (Vobiz incremental streaming)
# ---------------------------------------------------------------------------
async def run_tts_collect_chunked(
    text: str,
    *,
    language: str = "kn",
    min_chunk_ms: int = 300,
):
    """Async generator: yields PCM s16le 16kHz chunks buffered to ~min_chunk_ms.

    Provider order:
        1. Sarvam Bulbul v3  (primary  — HTTP streaming, no credits issue)
        2. Cartesia WebSocket (fallback — code preserved, commented out)
        3. run_tts_collect()  (final fallback — Cartesia collect / ElevenLabs)
    """
    text = _sanitize_text(text)
    if not text:
        return

    # ── Primary: Sarvam Bulbul v3 HTTP streaming ────────────────────────────
    yielded_any = False
    try:
        async for chunk in _sarvam_tts_stream_chunked(text, language, min_chunk_ms=min_chunk_ms):
            yield chunk
            yielded_any = True
    except Exception as exc:
        print(f"[TTS][CHUNKED] Sarvam stream exception: {exc}")

    if yielded_any:
        return

    print("[TTS][CHUNKED] Sarvam stream yielded nothing — trying Cartesia fallback")

    # ── Fallback: Cartesia WebSocket ─────────────────────────────────────────
    # [CARTESIA FALLBACK — uncomment this entire block to use Cartesia as primary instead]
    # api_key = _next_cartesia_key()
    # if not api_key:
    #     pcm = await run_tts_collect(text, language=language)
    #     if pcm:
    #         yield pcm
    #     return
    #
    # min_chunk_bytes = max(4000, int(min_chunk_ms * 16000 * 2 / 1000))
    # voice_id = _cartesia_voice_for_language(language)
    # uri = (
    #     f"{_CARTESIA_WS_URL}"
    #     f"?api_key={api_key}"
    #     f"&cartesia_version={_CARTESIA_VERSION}"
    # )
    # import uuid as _uuid
    # cart_payload = json.dumps({
    #     "context_id":    str(_uuid.uuid4()),
    #     "model_id":      _CARTESIA_MODEL_ID,
    #     "transcript":    text,
    #     "voice":         {"mode": "id", "id": voice_id},
    #     "language":      _cartesia_language_code(language),
    #     "output_format": {"container": "raw", "encoding": "pcm_s16le", "sample_rate": 16000},
    #     "generation_config": {"speed": _CARTESIA_SPEED},
    #     "add_timestamps": False,
    # })
    # cart_buffer: list  = []
    # cart_buf_bytes: int = 0
    # cart_yielded: bool  = False
    # try:
    #     async with websockets.connect(uri, open_timeout=5.0, close_timeout=2.0) as ws:
    #         await ws.send(cart_payload)
    #         async for raw_msg in ws:
    #             try:
    #                 msg = json.loads(raw_msg)
    #             except Exception:
    #                 continue
    #             msg_type = msg.get("type", "")
    #             if msg_type == "chunk":
    #                 chunk = base64.b64decode(msg.get("data", ""))
    #                 if chunk:
    #                     cart_buffer.append(chunk)
    #                     cart_buf_bytes += len(chunk)
    #                     if cart_buf_bytes >= min_chunk_bytes:
    #                         yield b"".join(cart_buffer)
    #                         cart_yielded = True
    #                         cart_buffer    = []
    #                         cart_buf_bytes = 0
    #             elif msg_type == "error":
    #                 print(f"[TTS][CHUNKED] Cartesia error: {msg.get('message', msg)}")
    #                 break
    #             elif msg_type == "done":
    #                 break
    # except Exception as exc:
    #     print(f"[TTS][CHUNKED] Cartesia exception: {exc}")
    # if cart_buffer:
    #     yield b"".join(cart_buffer)
    #     cart_yielded = True
    # if cart_yielded:
    #     return
    # print("[TTS][CHUNKED] Cartesia also yielded nothing")

    # ── Final fallback: collect mode (Cartesia collect → ElevenLabs) ────────
    pcm = await run_tts_collect(text, language=language)
    if pcm:
        yield pcm


# ---------------------------------------------------------------------------
# Public entry point 2: stream mode (browser WebSocket)
# ---------------------------------------------------------------------------
async def run_tts_stream(
    text: str,
    safe_send_bytes,
    safe_send_text,
    *,
    client_id: str = "default",
    session_id: str = "",
    language: str = "kn",
    stt_start_time: float = 0.0,
) -> None:
    """Stream TTS audio to a browser WebSocket client.

    Strategy:
      1. Try Cartesia in streaming mode (true low-latency streaming).
      2. If Cartesia fails *before* sending any audio, fall back to
         Sarvam TTS / ElevenLabs (collect bytes, then send in one shot).
      3. If Cartesia fails mid-stream (audio already sent), stop cleanly.
      4. Always emit 'audio_end' in the finally block.

    Never raises. Never hangs beyond TTS_REQUEST_TIMEOUT_SEC per attempt.
    """
    _cleanup_session_state()

    text = _sanitize_text(text)
    audio_started = False

    if not text:
        try:
            await safe_send_text(json.dumps({"type": "audio_end"}))
        except Exception:
            pass
        return

    resolved_client_id = _normalize_client_id(client_id)
    client_limits      = _get_client_limits(resolved_client_id)
    char_count         = len(text)
    started            = time.time()

    request_event = {
        "ts":          round(started, 3),
        "client_id":   resolved_client_id,
        "session_id":  (session_id or "unknown"),
        "char_count":  char_count,
        "language":    language,
        "mode":        "stream",
        "provider":    "none",
        "retry_count": 0,
        "fallback_used": False,
        "success":     False,
        "queue_rejected": False,
        "timed_out":   False,
        "error_type":  "",
        "error":       "",
    }
    _request_started()

    client_sem      = None
    client_acquired = False
    acquired        = False

    try:
        client_sem, client_acquired, acquired, queue_wait_ms, gate_err = await _acquire_gates(
            resolved_client_id, client_limits, started
        )
        if gate_err:
            request_event.update({
                "queue_rejected":   True,
                "error_type":       gate_err,
                "error":            f"gate blocked: {gate_err}",
                "total_latency_ms": round((time.time() - started) * 1000.0, 2),
                "queue_wait_ms":    queue_wait_ms,
                "estimated_cost_inr": 0.0,
            })
            _request_finished(request_event)
            if gate_err == "client_rps_limited":
                print(f"[TTS][DROP] client_rps_limited client={resolved_client_id}")
            else:
                print(f"[TTS][DROP] {gate_err} — request skipped")
            return

        # Cost guardrails
        session_spend   = get_session_spend_inr((session_id or "unknown").strip())
        client_spend    = get_client_spend_inr(resolved_client_id)
        client_max_cost = float(client_limits["max_cost_inr"])

        if _MAX_COST_INR_PER_SESSION > 0 and session_spend >= _MAX_COST_INR_PER_SESSION:
            request_event.update({"error_type": "cost_guardrail_exceeded", "total_latency_ms": 0.0, "estimated_cost_inr": 0.0})
            _request_finished(request_event)
            return

        if client_max_cost > 0 and client_spend >= client_max_cost:
            request_event.update({"error_type": "client_cost_guardrail_exceeded", "total_latency_ms": 0.0, "estimated_cost_inr": 0.0})
            _request_finished(request_event)
            return

        provider_attempt_cost: dict = {}
        chosen_provider = "none"
        fallback_used   = False
        retries         = 0
        timed_out       = False
        error_type      = ""
        error_msg       = ""
        prov_lat_ms     = 0.0

        # ---- Attempt 1+: Cartesia streaming (primary) ----
        cartesia_attempts = 1 + max(0, _PRIMARY_RETRIES)
        cartesia_ok       = False

        for idx in range(cartesia_attempts):
            api_key    = _next_cartesia_key()
            attempt_t0 = time.time()
            ok, sent_any, et, em, to = await _cartesia_attempt_stream(
                text, language, api_key, safe_send_bytes, safe_send_text, stt_start_time
            )
            prov_lat_ms += max(0.0, (time.time() - attempt_t0) * 1000.0)

            attempt_cost = _estimate_attempt_cost_inr("cartesia", char_count, ok or sent_any)
            if attempt_cost > 0:
                _bump_dict(provider_attempt_cost, "cartesia", attempt_cost)

            if idx > 0:
                retries += 1

            if ok:
                cartesia_ok     = True
                audio_started   = sent_any
                chosen_provider = "cartesia"
                break

            if sent_any:
                # Mid-stream failure — audio already sent, cannot fall back
                audio_started = True
                error_type    = et or "stream_interrupted"
                error_msg     = em
                timed_out     = bool(to)
                print(f"[TTS][STREAM] Cartesia mid-stream failure (sent_any=True): {et}")
                break

            error_type = et or "provider_error"
            error_msg  = em
            timed_out  = bool(to)

            if not _is_retryable_error(error_type):
                break

            if idx < (cartesia_attempts - 1):
                await asyncio.sleep(_retry_backoff_sec(idx))

        if not cartesia_ok and not audio_started:
            # Cartesia failed before any audio — try fallback providers
            print(f"[TTS][STREAM] Cartesia failed ({error_type}), trying fallback providers")
            for p in ["sarvam_tts", "elevenlabs"]:
                attempt_t0 = time.time()
                if p == "sarvam_tts":
                    ok, pcm, et, em, to = await _sarvam_tts_attempt(text, language)
                else:
                    ok, pcm, et, em, to = await _elevenlabs_attempt(text, language)
                prov_lat_ms += max(0.0, (time.time() - attempt_t0) * 1000.0)

                attempt_cost = _estimate_attempt_cost_inr(p, char_count, ok)
                if attempt_cost > 0:
                    _bump_dict(provider_attempt_cost, p, attempt_cost)

                if ok and pcm:
                    await safe_send_text(json.dumps({"type": "audio_start", "sample_rate": 16000}))
                    audio_started   = True
                    await safe_send_bytes(pcm)
                    chosen_provider = p
                    fallback_used   = True
                    error_type      = ""
                    error_msg       = ""
                    break

                if et == "provider_unconfigured":
                    continue

                error_type = et or "provider_error"
                error_msg  = em
                timed_out  = timed_out or bool(to)

        cost_inr         = float(sum(provider_attempt_cost.values()))
        total_latency_ms = round((time.time() - started) * 1000.0, 2)
        success          = audio_started and (cartesia_ok or fallback_used or chosen_provider != "none")

        if success:
            print(f"[TTS][STREAM] provider={chosen_provider} chars={char_count} lang={language} latency={total_latency_ms:.0f}ms")

        request_event.update({
            "provider":           chosen_provider,
            "retry_count":        retries,
            "fallback_used":      fallback_used,
            "success":            success,
            "timed_out":          timed_out,
            "error_type":         error_type,
            "error":              (error_msg or "")[:500],
            "queue_wait_ms":      queue_wait_ms,
            "api_latency_ms":     round(prov_lat_ms, 2),
            "total_latency_ms":   total_latency_ms,
            "estimated_cost_inr": round(cost_inr, 8),
            "provider_attempt_cost_inr": {k: round(v, 8) for k, v in provider_attempt_cost.items()},
        })
        _request_finished(request_event)

    except Exception as exc:
        request_event.update({
            "error_type": "exception", "error": str(exc)[:500],
            "total_latency_ms": round((time.time() - started) * 1000.0, 2),
            "estimated_cost_inr": 0.0,
        })
        _request_finished(request_event)
        print(f"[TTS][STREAM] Exception: {exc}")
    finally:
        try:
            await safe_send_text(json.dumps({"type": "audio_end"}))
        except Exception:
            pass
        _release_gates(client_sem, client_acquired, acquired)
