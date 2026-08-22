from .tts_core import (
    run_tts_collect,
    run_tts_collect_chunked,
    run_tts_stream,
    set_tts_client_config_map,
    close_tts_http_clients,
)
from .tts_metrics import get_tts_metrics_snapshot


# ---------------------------------------------------------------------------
# Backward-compatible shims — main.py keeps importing these unchanged
# ---------------------------------------------------------------------------
async def cartesia_tts_collect(text: str, language: str = "kn") -> bytes:
    """Backward-compatible wrapper — delegates to run_tts_collect."""
    return await run_tts_collect(text, language=language)


async def cartesia_tts_chunked(text: str, language: str = "kn", min_chunk_ms: int = 150):
    """Async generator: yields PCM s16le 16kHz in ~150ms chunks for Vobiz streaming."""
    async for chunk in run_tts_collect_chunked(text, language=language, min_chunk_ms=min_chunk_ms):
        yield chunk


async def cartesia_tts_stream(
    text: str,
    safe_send_bytes,
    safe_send_text,
    stt_start_time=None,
    language: str = "kn",
) -> None:
    """Backward-compatible wrapper — delegates to run_tts_stream."""
    await run_tts_stream(
        text,
        safe_send_bytes,
        safe_send_text,
        language=language,
        stt_start_time=float(stt_start_time or 0.0),
    )


__all__ = [
    "run_tts_collect",
    "run_tts_collect_chunked",
    "run_tts_stream",
    "cartesia_tts_collect",
    "cartesia_tts_stream",
    "set_tts_client_config_map",
    "close_tts_http_clients",
    "get_tts_metrics_snapshot",
]
