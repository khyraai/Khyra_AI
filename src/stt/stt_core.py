"""
stt.py — Speech-to-Text + Audio Codec Helpers + STT Metrics Layer
"""

import io
import os
import re
import time
import random
import asyncio
import wave
import audioop
from threading import Lock
from collections import deque
from typing import Optional

import aiohttp
from .stt_metrics import (
    request_started as _request_started,
    request_finished as _request_finished,
    get_session_spend_inr,
    get_client_spend_inr,
    cleanup_stale_sessions,
)


def _normalize_lang_to_en_or_kn(*, detected_lang: str, transcript: str) -> str:
    dl = (detected_lang or "").strip().lower()
    if dl.startswith("kn"):
        return "kn-IN"
    if dl.startswith("en"):
        return "en-IN"

    text = transcript or ""
    if re.search(r"[\u0C80-\u0CFF]", text):
        return "kn-IN"
    return "en-IN"


def _bump_dict(d: dict, key: str, value: float = 1.0):
    d[key] = d.get(key, 0) + value


_HTTP_SESSION = None
_HTTP_SESSION_LOCK = asyncio.Lock()
_STT_SEMAPHORE = asyncio.Semaphore(int(os.getenv("STT_MAX_CONCURRENT", "3")))
_QUEUE_WAIT_TIMEOUT_SEC = float(os.getenv("STT_QUEUE_WAIT_TIMEOUT_SEC", "1.5"))
_REQUEST_TIMEOUT_SEC = float(os.getenv("STT_REQUEST_TIMEOUT_SEC", "8.0"))
_PRIMARY_RETRIES = int(os.getenv("STT_PRIMARY_RETRIES", "1"))
_MIN_TRANSCRIPT_CHARS = int(os.getenv("STT_MIN_TRANSCRIPT_CHARS", "3"))
_DUP_WINDOW_SEC = float(os.getenv("STT_DUPLICATE_WINDOW_SEC", "3.5"))
_RETRY_BACKOFF_BASE_SEC = float(os.getenv("STT_RETRY_BACKOFF_BASE_SEC", "0.25"))
_RETRY_BACKOFF_MAX_SEC = float(os.getenv("STT_RETRY_BACKOFF_MAX_SEC", "1.5"))
_RETRY_JITTER_SEC = float(os.getenv("STT_RETRY_JITTER_SEC", "0.08"))
_SESSION_TTL_SEC = float(os.getenv("STT_SESSION_TTL_SEC", "1800"))
_SESSION_CLEANUP_INTERVAL_SEC = float(os.getenv("STT_SESSION_CLEANUP_INTERVAL_SEC", "60"))
_MAX_TRACKED_SESSIONS = int(os.getenv("STT_MAX_TRACKED_SESSIONS", "5000"))
_MAX_COST_INR_PER_SESSION = float(os.getenv("STT_MAX_COST_INR_PER_SESSION", "0"))
_DEFAULT_CLIENT_ID = (os.getenv("DEFAULT_CLIENT_ID", "default").strip() or "default")
_DEFAULT_CLIENT_MAX_CONCURRENT = int(os.getenv("STT_DEFAULT_CLIENT_MAX_CONCURRENT", "2"))
_DEFAULT_CLIENT_MAX_RPS = float(os.getenv("STT_DEFAULT_CLIENT_MAX_RPS", "6"))
_DEFAULT_CLIENT_MAX_COST_INR = float(os.getenv("STT_DEFAULT_CLIENT_MAX_COST_INR", "0"))
_STT_CLIENT_CONFIG_MAP = {}

_SESSION_LAST_TEXT = {}
_KEY_ROTATION_LOCK = Lock()
_SARVAM_KEY_INDEX = 0
_LAST_SESSION_CLEANUP_TS = 0.0
_CLIENT_LIMITERS_LOCK = Lock()
_CLIENT_SEMAPHORES = {}
_CLIENT_RPS_WINDOW = {}

# Global Sarvam RPM gate — prevents aggregate 429s under multi-client load
_SARVAM_GLOBAL_RPM_LIMIT = int(os.getenv("SARVAM_API_RPM_LIMIT", "55") or "55")
_SARVAM_GLOBAL_RPM_WINDOW: deque = deque()


def set_stt_client_config_map(config_map: dict):
    global _STT_CLIENT_CONFIG_MAP
    _STT_CLIENT_CONFIG_MAP = dict(config_map or {}) if isinstance(config_map, dict) else {}


def _provider_cost_per_min(provider: str) -> float:
    env_map = {
        "sarvam": "SARVAM_COST_INR_PER_MIN",
        "ink_whisper": "INK_COST_INR_PER_MIN",
        "deepgram": "DEEPGRAM_COST_INR_PER_MIN",
        "openai_whisper": "OPENAI_WHISPER_COST_INR_PER_MIN",
    }
    return float(os.getenv(env_map.get(provider, ""), "0") or "0")


def _resolve_audio_duration_sec(audio_bytes: bytes, chunk_duration_sec: float) -> float:
    if chunk_duration_sec and chunk_duration_sec > 0:
        return float(chunk_duration_sec)
    if not audio_bytes:
        return 0.0
    try:
        with wave.open(io.BytesIO(audio_bytes), "rb") as wf:
            frames = wf.getnframes()
            sr = wf.getframerate()
            if sr > 0:
                return float(frames) / float(sr)
    except Exception:
        pass
    payload_bytes = max(0, len(audio_bytes) - 44)
    return float(payload_bytes) / float(16000 * 2)


def _is_retryable_error(error_type: str) -> bool:
    et = (error_type or "").strip().lower()
    if not et:
        return True
    if et in {"timeout", "network_error", "http_408", "http_425", "http_429"}:
        return True
    if re.match(r"http_5\d\d$", et):
        return True
    if et in {"api_key_missing", "provider_unconfigured", "http_400", "http_401", "http_403", "http_404", "http_409", "http_410", "http_413", "http_415", "http_422"}:
        return False
    return False


def _retry_backoff_sec(attempt_index: int) -> float:
    base = _RETRY_BACKOFF_BASE_SEC * (2 ** max(0, attempt_index))
    jitter = random.uniform(0.0, max(0.0, _RETRY_JITTER_SEC))
    return min(_RETRY_BACKOFF_MAX_SEC, base + jitter)


def _estimate_attempt_cost_inr(provider: str, audio_duration_sec: float, success: bool) -> float:
    if audio_duration_sec <= 0 or not success:
        return 0.0
    return (audio_duration_sec / 60.0) * _provider_cost_per_min(provider)


def _normalize_client_id(client_id: str) -> str:
    cid = (client_id or "").strip()
    return cid if cid else _DEFAULT_CLIENT_ID


def _get_client_limits(client_id: str) -> dict:
    cfg_map = _STT_CLIENT_CONFIG_MAP
    raw_cfg = cfg_map.get(client_id, {}) if isinstance(cfg_map, dict) else {}
    if not isinstance(raw_cfg, dict):
        raw_cfg = {}

    cost_limits = raw_cfg.get("cost_limits", {})
    if not isinstance(cost_limits, dict):
        cost_limits = {}

    max_concurrent = int(raw_cfg.get("max_concurrent", _DEFAULT_CLIENT_MAX_CONCURRENT) or _DEFAULT_CLIENT_MAX_CONCURRENT)
    max_rps = float(raw_cfg.get("max_rps", _DEFAULT_CLIENT_MAX_RPS) or _DEFAULT_CLIENT_MAX_RPS)
    max_cost = float(
        cost_limits.get(
            "max_cost_per_client",
            raw_cfg.get("max_cost_inr", _DEFAULT_CLIENT_MAX_COST_INR),
        )
        or _DEFAULT_CLIENT_MAX_COST_INR
    )

    return {
        "max_concurrent": max(1, max_concurrent),
        "max_rps": max(0.0, max_rps),
        "max_cost_inr": max(0.0, max_cost),
    }


def _get_client_semaphore(client_id: str, max_concurrent: int):
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


def _cleanup_session_state(now_ts: float = 0.0):
    global _LAST_SESSION_CLEANUP_TS
    now = float(now_ts or time.time())
    if (now - _LAST_SESSION_CLEANUP_TS) < _SESSION_CLEANUP_INTERVAL_SEC:
        return
    _LAST_SESSION_CLEANUP_TS = now

    stale_cutoff = now - _SESSION_TTL_SEC

    stale_text_sids = [sid for sid, (_, ts) in _SESSION_LAST_TEXT.items() if ts < stale_cutoff]
    for sid in stale_text_sids:
        _SESSION_LAST_TEXT.pop(sid, None)
    cleanup_stale_sessions(stale_cutoff, _MAX_TRACKED_SESSIONS)


def _global_sarvam_rpm_allow() -> bool:
    """Sliding 60-second global gate across ALL clients. Thread-safe."""
    if _SARVAM_GLOBAL_RPM_LIMIT <= 0:
        return True
    now = time.time()
    with _KEY_ROTATION_LOCK:
        cutoff = now - 60.0
        while _SARVAM_GLOBAL_RPM_WINDOW and _SARVAM_GLOBAL_RPM_WINDOW[0] < cutoff:
            _SARVAM_GLOBAL_RPM_WINDOW.popleft()
        if len(_SARVAM_GLOBAL_RPM_WINDOW) >= _SARVAM_GLOBAL_RPM_LIMIT:
            return False
        _SARVAM_GLOBAL_RPM_WINDOW.append(now)
        return True


def _next_sarvam_key(primary_key: str) -> str:
    keys_raw = os.getenv("SARVAM_API_KEYS", "")
    keys = [k.strip() for k in keys_raw.split(",") if k.strip()]
    if primary_key and primary_key.strip():
        keys.insert(0, primary_key.strip())
    uniq = []
    seen = set()
    for k in keys:
        if k not in seen:
            seen.add(k)
            uniq.append(k)
    if not uniq:
        return ""
    global _SARVAM_KEY_INDEX
    with _KEY_ROTATION_LOCK:
        key = uniq[_SARVAM_KEY_INDEX % len(uniq)]
        _SARVAM_KEY_INDEX += 1
    return key


async def _get_http_session() -> aiohttp.ClientSession:
    global _HTTP_SESSION
    if _HTTP_SESSION and not _HTTP_SESSION.closed:
        return _HTTP_SESSION
    async with _HTTP_SESSION_LOCK:
        if _HTTP_SESSION and not _HTTP_SESSION.closed:
            return _HTTP_SESSION
        connector = aiohttp.TCPConnector(
            limit=int(os.getenv("STT_HTTP_POOL_LIMIT", "50")),
            limit_per_host=int(os.getenv("STT_HTTP_POOL_PER_HOST", "20")),
            ttl_dns_cache=300,
        )
        timeout = aiohttp.ClientTimeout(total=_REQUEST_TIMEOUT_SEC)
        _HTTP_SESSION = aiohttp.ClientSession(connector=connector, timeout=timeout)
        return _HTTP_SESSION


async def close_stt_http_clients():
    global _HTTP_SESSION
    async with _HTTP_SESSION_LOCK:
        if _HTTP_SESSION and not _HTTP_SESSION.closed:
            await _HTTP_SESSION.close()
        _HTTP_SESSION = None


def _normalize_text_for_dup_check(text: str) -> str:
    t = (text or "").strip().lower()
    if not t:
        return ""
    t = re.sub(r"[^\w\u0C80-\u0CFF\s]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _drop_as_noise_or_duplicate(
    text: str,
    session_id: str,
    *,
    min_transcript_chars: Optional[int] = None,
    drop_duplicates: bool = True,
) -> bool:
    t = (text or "").strip()
    normalized = _normalize_text_for_dup_check(t)
    effective_min_chars = int(min_transcript_chars) if min_transcript_chars is not None else _MIN_TRANSCRIPT_CHARS
    if effective_min_chars < 1:
        effective_min_chars = 1

    normalized_compact = normalized.replace(" ", "")
    if not normalized_compact:
        return True
    # Allow short numeric transcripts (e.g. age "27", time "11")
    if len(normalized_compact) < effective_min_chars and not normalized_compact.isdigit():
        return True
    if bool(re.search(r'([.?!,;:\-\'\"])(\1){3,}', t)):
        return True
    sid = (session_id or "unknown").strip()
    prev = _SESSION_LAST_TEXT.get(sid)
    now = time.time()
    if drop_duplicates and prev and prev[0] == normalized and (now - prev[1]) <= _DUP_WINDOW_SEC:
        return True
    _SESSION_LAST_TEXT[sid] = (normalized, now)
    return False


async def _sarvam_attempt(audio_bytes: bytes, filename: str, api_key: str, language_code: str):
    if not api_key:
        return False, "", "", "api_key_missing", "", False
    session = await _get_http_session()
    url = "https://api.sarvam.ai/speech-to-text"
    headers = {"api-subscription-key": api_key}
    data = aiohttp.FormData()
    data.add_field("model", "saaras:v3")
    data.add_field("language_code", "unknown")  # Always auto-detect; session lock is for routing only
    data.add_field("mode", "transcribe")
    data.add_field("file", audio_bytes, filename=filename, content_type="audio/wav")
    try:
        async with session.post(url, headers=headers, data=data) as resp:
            body = await resp.text()
            if resp.status == 200:
                try:
                    import json as _json
                    payload = _json.loads(body)
                except Exception:
                    payload = {}
                text = (payload.get("transcript") or "").strip()
                lang = _normalize_lang_to_en_or_kn(
                    detected_lang=payload.get("language_code", ""),
                    transcript=text,
                )
                return True, text, lang, "", "", False
            timed_out = resp.status == 408
            print(f"[STT][Sarvam] \u274c HTTP {resp.status}: {body[:200]}")
            return False, "", "", f"http_{resp.status}", body[:400], timed_out
    except asyncio.TimeoutError:
        print("[STT][Sarvam] \u274c Timeout")
        return False, "", "", "timeout", "", True
    except Exception as e:
        print(f"[STT][Sarvam] \u274c Exception: {e}")
        return False, "", "", "network_error", str(e), False


async def _ink_attempt(audio_bytes: bytes, filename: str, language_code: str):
    url = os.getenv("INK_WHISPER_URL", "").strip()
    key = os.getenv("INK_WHISPER_API_KEY", "").strip()
    if not url:
        return False, "", "", "provider_unconfigured", "", False
    session = await _get_http_session()
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    data = aiohttp.FormData()
    data.add_field("file", audio_bytes, filename=filename, content_type="audio/wav")
    data.add_field("language", language_code or "unknown")
    try:
        async with session.post(url, headers=headers, data=data) as resp:
            txt = await resp.text()
            if resp.status in (200, 201):
                payload = await resp.json(content_type=None)
                text = (payload.get("transcript") or payload.get("text") or "").strip()
                lang = _normalize_lang_to_en_or_kn(
                    detected_lang=payload.get("language", payload.get("language_code", "")),
                    transcript=text,
                )
                return True, text, lang, "", "", False
            return False, "", "", f"http_{resp.status}", txt[:400], resp.status == 408
    except asyncio.TimeoutError:
        return False, "", "", "timeout", "", True
    except Exception as e:
        return False, "", "", "network_error", str(e), False


async def _deepgram_attempt(audio_bytes: bytes, language_code: str = "unknown"):
    key = os.getenv("DEEPGRAM_API_KEY", "").strip()
    if not key:
        return False, "", "", "provider_unconfigured", "", False
    session = await _get_http_session()
    lc = (language_code or "unknown").lower()
    lang_param = ""
    if lc.startswith("kn"):
        lang_param = "&language=kn"
    elif lc.startswith("en"):
        lang_param = "&language=en"
    url = f"https://api.deepgram.com/v1/listen?model=nova-3&smart_format=true{lang_param}"
    headers = {"Authorization": f"Token {key}", "Content-Type": "audio/wav"}
    try:
        async with session.post(url, headers=headers, data=audio_bytes) as resp:
            txt = await resp.text()
            if resp.status == 200:
                payload = await resp.json(content_type=None)
                text = (
                    payload.get("results", {})
                    .get("channels", [{}])[0]
                    .get("alternatives", [{}])[0]
                    .get("transcript", "")
                    .strip()
                )
                detected = (
                    payload.get("results", {})
                    .get("channels", [{}])[0]
                    .get("detected_language", "")
                )
                lang = _normalize_lang_to_en_or_kn(detected_lang=detected, transcript=text)
                return True, text, lang, "", "", False
            return False, "", "", f"http_{resp.status}", txt[:400], resp.status == 408
    except asyncio.TimeoutError:
        return False, "", "", "timeout", "", True
    except Exception as e:
        return False, "", "", "network_error", str(e), False


async def _groq_whisper_attempt(audio_bytes: bytes, filename: str, language_code: str):
    key = os.getenv("GROQ_API_KEY", "").strip()
    if not key:
        return False, "", "", "provider_unconfigured", "", False
    session = await _get_http_session()
    url = "https://api.groq.com/openai/v1/audio/transcriptions"
    headers = {"Authorization": f"Bearer {key}"}
    data = aiohttp.FormData()
    data.add_field("model", os.getenv("GROQ_WHISPER_MODEL", "whisper-large-v3-turbo"))
    data.add_field("file", audio_bytes, filename=filename, content_type="audio/wav")
    data.add_field("response_format", "json")
    if language_code and language_code != "unknown":
        data.add_field("language", "kn" if language_code.lower().startswith("kn") else "en")
    try:
        async with session.post(url, headers=headers, data=data) as resp:
            txt = await resp.text()
            if resp.status == 200:
                payload = await resp.json(content_type=None)
                text = (payload.get("text") or "").strip()
                lang = _normalize_lang_to_en_or_kn(detected_lang="", transcript=text)
                return True, text, lang, "", "", False
            return False, "", "", f"http_{resp.status}", txt[:400], resp.status == 408
    except asyncio.TimeoutError:
        return False, "", "", "timeout", "", True
    except Exception as e:
        return False, "", "", "network_error", str(e), False


# -----------------------------------------------------------------------
# Audio Codec Helpers (for Vobiz telephony)
# -----------------------------------------------------------------------
def l16_8k_to_pcm16_16k(l16_bytes: bytes) -> bytes:
    """Linear PCM s16le 8kHz → PCM s16le 16kHz."""
    pcm_16k, _ = audioop.ratecv(l16_bytes, 2, 1, 8000, 16000, None)
    return pcm_16k


def mulaw_8k_to_pcm16_16k(mulaw_bytes: bytes) -> bytes:
    """G.711 µ-law 8kHz → PCM s16le 16kHz."""
    pcm_8k = audioop.ulaw2lin(mulaw_bytes, 2)
    pcm_16k, _ = audioop.ratecv(pcm_8k, 2, 1, 8000, 16000, None)
    return pcm_16k


def pcm16_16k_to_mulaw_8k(pcm_16k_bytes: bytes) -> bytes:
    """PCM s16le 16kHz → G.711 µ-law 8kHz."""
    pcm_8k, _ = audioop.ratecv(pcm_16k_bytes, 2, 1, 16000, 8000, None)
    return audioop.lin2ulaw(pcm_8k, 2)


def pcm16_to_wav_bytes(pcm_bytes: bytes, sample_rate: int = 16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_bytes)
    return buf.getvalue()


# -----------------------------------------------------------------------
# Sarvam STT — HTTP batch (used by Vobiz pipeline)
# -----------------------------------------------------------------------
async def run_stt_http(
    audio_bytes: bytes,
    sarvam_api_key: str,
    filename: str = "audio.wav",
    *,
    client_id: str = "default",
    session_id: str = "",
    language_code: str = "unknown",
    chunk_duration_sec: float = 0.0,
    preprocessing_time_sec: float = 0.0,
    priority: str = "normal",
    drop_duplicates: bool = True,
    min_transcript_chars: Optional[int] = None,
):
    _cleanup_session_state()

    resolved_client_id = _normalize_client_id(client_id)
    client_limits = _get_client_limits(resolved_client_id)
    client_max_concurrent = int(client_limits["max_concurrent"])
    client_max_rps = float(client_limits["max_rps"])
    client_max_cost_inr = float(client_limits["max_cost_inr"])

    started = time.time()
    resolved_audio_duration_sec = _resolve_audio_duration_sec(audio_bytes, chunk_duration_sec)
    queue_timeout_sec = _QUEUE_WAIT_TIMEOUT_SEC

    request_event = {
        "ts": round(started, 3),
        "client_id": resolved_client_id,
        "session_id": (session_id or "unknown"),
        "audio_duration_sec": resolved_audio_duration_sec,
        "language_locked": language_code if language_code and language_code != "unknown" else "unknown",
        "priority": (priority or "normal"),
        "preprocessing_ms": round(float(preprocessing_time_sec) * 1000.0, 2),
        "provider": "none",
        "retry_count": 0,
        "fallback_used": False,
        "success": False,
        "dropped": False,
        "queue_rejected": False,
        "timed_out": False,
        "error_type": "",
        "error": "",
    }

    _request_started()

    client_acquired = False
    acquired = False

    if not _allow_client_rps(resolved_client_id, client_max_rps):
        request_event.update({
            "queue_rejected": True,
            "error_type": "client_rps_limited",
            "error": "client max_rps exceeded",
            "total_latency_ms": round((time.time() - started) * 1000.0, 2),
            "queue_wait_ms": 0.0,
            "queue_timeout_budget_ms": round(queue_timeout_sec * 1000.0, 2),
            "estimated_cost_inr": 0.0,
            "transcript_length": 0,
            "detected_lang": "",
        })
        _request_finished(request_event)
        return "", "kn-IN"

    acquire_t0 = time.time()
    try:
        client_sem = _get_client_semaphore(resolved_client_id, client_max_concurrent)
        await asyncio.wait_for(client_sem.acquire(), timeout=queue_timeout_sec)
        client_acquired = True

        elapsed = time.time() - acquire_t0
        remaining_timeout = max(0.05, queue_timeout_sec - elapsed)
        await asyncio.wait_for(_STT_SEMAPHORE.acquire(), timeout=remaining_timeout)
        acquired = True
    except asyncio.TimeoutError:
        request_event.update({
            "queue_rejected": True,
            "error_type": "queue_timeout" if client_acquired else "client_queue_timeout",
            "error": "stt semaphore acquire timeout" if client_acquired else "client semaphore acquire timeout",
            "total_latency_ms": round((time.time() - started) * 1000.0, 2),
            "queue_wait_ms": round((time.time() - acquire_t0) * 1000.0, 2),
            "queue_timeout_budget_ms": round(queue_timeout_sec * 1000.0, 2),
            "estimated_cost_inr": 0.0,
            "transcript_length": 0,
            "detected_lang": "",
        })
        _request_finished(request_event)
        print("[STT][DROP] queue timeout - chunk skipped")
        return "", "kn-IN"

    queue_wait_ms = round((time.time() - acquire_t0) * 1000.0, 2)
    provider_chain = ["sarvam", "deepgram", "groq_whisper"]
    chosen_provider = "none"
    retries = 0
    fallback_used = False
    timed_out = False
    user_text = ""
    detected_lang = "kn-IN"
    error_type = ""
    error_text = ""
    backoff_ms_total = 0.0
    provider_latency_ms_total = 0.0
    provider_attempt_cost_inr = {}

    session_spend_so_far = get_session_spend_inr((session_id or "unknown").strip())
    client_spend_so_far = get_client_spend_inr(resolved_client_id)

    if _MAX_COST_INR_PER_SESSION > 0 and session_spend_so_far >= _MAX_COST_INR_PER_SESSION:
        request_event.update({
            "provider": "none",
            "retry_count": 0,
            "fallback_used": False,
            "success": False,
            "timed_out": False,
            "error_type": "cost_guardrail_exceeded",
            "error": "session max STT cost reached",
            "queue_wait_ms": queue_wait_ms,
            "api_latency_ms": 0.0,
            "pipeline_latency_ms": 0.0,
            "provider_latency_ms": 0.0,
            "retry_backoff_ms": 0.0,
            "total_latency_ms": round((time.time() - started) * 1000.0, 2),
            "transcript_length": 0,
            "detected_lang": "",
            "estimated_cost_inr": 0.0,
        })
        _request_finished(request_event)
        return "", "kn-IN"

    if client_max_cost_inr > 0 and client_spend_so_far >= client_max_cost_inr:
        request_event.update({
            "provider": "none",
            "retry_count": 0,
            "fallback_used": False,
            "success": False,
            "timed_out": False,
            "error_type": "client_cost_guardrail_exceeded",
            "error": "client max STT cost reached",
            "queue_wait_ms": queue_wait_ms,
            "api_latency_ms": 0.0,
            "pipeline_latency_ms": 0.0,
            "provider_latency_ms": 0.0,
            "retry_backoff_ms": 0.0,
            "total_latency_ms": round((time.time() - started) * 1000.0, 2),
            "transcript_length": 0,
            "detected_lang": "",
            "estimated_cost_inr": 0.0,
        })
        _request_finished(request_event)
        return "", "kn-IN"

    try:
        hard_stop_due_to_cost = False
        for p in provider_chain:
            provider_ok = False

            if p == "sarvam" and not _global_sarvam_rpm_allow():
                print(f"[STT][RPM] Global Sarvam RPM limit reached — skipping to fallback")
                error_type = "global_rpm_limit"
                error_text = "global Sarvam RPM window full"
                continue

            attempts = 1
            if p == "sarvam":
                attempts = 1 + max(0, _PRIMARY_RETRIES)

            for idx in range(attempts):
                projected_attempt_cost = _estimate_attempt_cost_inr(p, resolved_audio_duration_sec, True)
                if _MAX_COST_INR_PER_SESSION > 0 and (session_spend_so_far + sum(provider_attempt_cost_inr.values()) + projected_attempt_cost) > _MAX_COST_INR_PER_SESSION:
                    error_type = "cost_guardrail_exceeded"
                    error_text = "retry/fallback blocked by session cost guardrail"
                    hard_stop_due_to_cost = True
                    break
                if client_max_cost_inr > 0 and (client_spend_so_far + sum(provider_attempt_cost_inr.values()) + projected_attempt_cost) > client_max_cost_inr:
                    error_type = "client_cost_guardrail_exceeded"
                    error_text = "retry/fallback blocked by client cost guardrail"
                    hard_stop_due_to_cost = True
                    break

                attempt_t0 = time.time()
                if p == "sarvam":
                    key = _next_sarvam_key(sarvam_api_key)
                    ok, text, lang, et, err, to = await _sarvam_attempt(audio_bytes, filename, key, language_code)
                elif p == "deepgram":
                    ok, text, lang, et, err, to = await _deepgram_attempt(audio_bytes, language_code)
                else:
                    ok, text, lang, et, err, to = await _groq_whisper_attempt(audio_bytes, filename, language_code)
                provider_latency_ms_total += max(0.0, (time.time() - attempt_t0) * 1000.0)

                attempt_cost = _estimate_attempt_cost_inr(p, resolved_audio_duration_sec, ok)
                if attempt_cost > 0:
                    _bump_dict(provider_attempt_cost_inr, p, attempt_cost)

                if idx > 0:
                    retries += 1

                if ok:
                    chosen_provider = p
                    provider_ok = True
                    user_text = text
                    detected_lang = lang or "kn-IN"
                    if p != "sarvam":
                        fallback_used = True
                    break

                if et == "provider_unconfigured":
                    break

                error_type = et or "provider_error"
                error_text = err or ""
                timed_out = timed_out or bool(to)

                if not _is_retryable_error(error_type):
                    break

                if idx < (attempts - 1):
                    delay_sec = _retry_backoff_sec(idx)
                    backoff_ms_total += delay_sec * 1000.0
                    await asyncio.sleep(delay_sec)

            if provider_ok:
                break
            if hard_stop_due_to_cost:
                break

        if not user_text:
            user_text = ""

        if user_text and _drop_as_noise_or_duplicate(
            user_text,
            session_id,
            min_transcript_chars=min_transcript_chars,
            drop_duplicates=drop_duplicates,
        ):
            print(f"[STT][FILTER] Dropped transcript: '{user_text}'")
            user_text = ""
            request_event["dropped"] = True
            if not error_type:
                error_type = "low_signal"

        if user_text:
            print(f"[STT][HTTP] provider={chosen_provider} text='{user_text}' lang={detected_lang}")

        cost_inr = float(sum(provider_attempt_cost_inr.values()))
        total_latency_ms = round((time.time() - started) * 1000.0, 2)
        queue_wait = float(queue_wait_ms)
        preprocess_ms = float(request_event["preprocessing_ms"])
        pipeline_latency_ms = max(0.0, total_latency_ms - preprocess_ms)
        orchestration_ms = max(0.0, pipeline_latency_ms - queue_wait - provider_latency_ms_total - backoff_ms_total)

        request_event.update({
            "provider": chosen_provider,
            "retry_count": retries,
            "fallback_used": fallback_used,
            "success": bool(user_text),
            "timed_out": timed_out,
            "error_type": error_type,
            "error": (error_text or "")[:500],
            "queue_wait_ms": queue_wait_ms,
            "queue_timeout_budget_ms": round(queue_timeout_sec * 1000.0, 2),
            "api_latency_ms": round(provider_latency_ms_total, 2),
            "provider_latency_ms": round(provider_latency_ms_total, 2),
            "retry_backoff_ms": round(backoff_ms_total, 2),
            "pipeline_latency_ms": round(pipeline_latency_ms, 2),
            "orchestration_overhead_ms": round(orchestration_ms, 2),
            "total_latency_ms": total_latency_ms,
            "transcript_length": len(user_text),
            "detected_lang": detected_lang,
            "estimated_cost_inr": round(cost_inr, 8),
            "provider_attempt_cost_inr": {k: round(v, 8) for k, v in provider_attempt_cost_inr.items()},
        })
        _request_finished(request_event)
        return user_text, detected_lang
    except Exception as e:
        total_latency_ms = round((time.time() - started) * 1000.0, 2)
        pipeline_latency_ms = max(0.0, total_latency_ms - float(request_event["preprocessing_ms"]))
        request_event.update({
            "provider": chosen_provider,
            "retry_count": retries,
            "fallback_used": fallback_used,
            "success": False,
            "timed_out": timed_out,
            "error_type": "exception",
            "error": str(e)[:500],
            "queue_wait_ms": queue_wait_ms,
            "queue_timeout_budget_ms": round(queue_timeout_sec * 1000.0, 2),
            "api_latency_ms": round(provider_latency_ms_total, 2),
            "provider_latency_ms": round(provider_latency_ms_total, 2),
            "retry_backoff_ms": round(backoff_ms_total, 2),
            "pipeline_latency_ms": round(pipeline_latency_ms, 2),
            "orchestration_overhead_ms": round(max(0.0, pipeline_latency_ms - float(queue_wait_ms) - provider_latency_ms_total - backoff_ms_total), 2),
            "total_latency_ms": total_latency_ms,
            "transcript_length": 0,
            "detected_lang": "",
            "estimated_cost_inr": round(float(sum(provider_attempt_cost_inr.values())), 8),
            "provider_attempt_cost_inr": {k: round(v, 8) for k, v in provider_attempt_cost_inr.items()},
        })
        _request_finished(request_event)
        print(f"[STT][HTTP] Exception: {e}")
        return "", "kn-IN"
    finally:
        if acquired:
            _STT_SEMAPHORE.release()
        if client_acquired:
            client_sem.release()
