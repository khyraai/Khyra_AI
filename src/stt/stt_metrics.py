"""
stt_metrics.py — STT observability and accounting state.
"""

import time
from collections import deque
from threading import Lock


_STT_METRICS_LOCK = Lock()
_STT_METRICS = {
    "active_requests": 0,
    "total_requests": 0,
    "success_count": 0,
    "failure_count": 0,
    "timeout_count": 0,
    "retry_total": 0,
    "fallback_used_count": 0,
    "dropped_chunks": 0,
    "queue_rejected": 0,
    "provider_usage": {},
    "provider_errors": {},
    "latency_ms_window": deque(maxlen=300),
    "audio_sec_window": deque(maxlen=300),
    "cost_inr_total": 0.0,
    "provider_cost_inr": {},
    "client_cost_inr": {},
    "client_requests": {},
    "client_usage": {},
    "client_errors": {},
    "session_cost_inr": {},
    "session_requests": {},
    "session_last_seen": {},
    "recent_events": deque(maxlen=200),
}


def _bump_dict(d: dict, key: str, value: float = 1.0):
    d[key] = d.get(key, 0) + value


def request_started():
    with _STT_METRICS_LOCK:
        _STT_METRICS["active_requests"] += 1
        _STT_METRICS["total_requests"] += 1


def request_finished(event: dict):
    with _STT_METRICS_LOCK:
        _STT_METRICS["active_requests"] = max(0, _STT_METRICS["active_requests"] - 1)
        if event.get("success"):
            _STT_METRICS["success_count"] += 1
        else:
            _STT_METRICS["failure_count"] += 1
        if event.get("timed_out"):
            _STT_METRICS["timeout_count"] += 1
        _STT_METRICS["retry_total"] += int(event.get("retry_count", 0))
        if event.get("fallback_used"):
            _STT_METRICS["fallback_used_count"] += 1
        if event.get("dropped"):
            _STT_METRICS["dropped_chunks"] += 1
        if event.get("queue_rejected"):
            _STT_METRICS["queue_rejected"] += 1

        provider = event.get("provider") or "none"
        _bump_dict(_STT_METRICS["provider_usage"], provider)
        if not event.get("success"):
            et = event.get("error_type", "unknown")
            _bump_dict(_STT_METRICS["provider_errors"], f"{provider}:{et}")

        client_id = (event.get("client_id") or "default").strip() or "default"
        _bump_dict(_STT_METRICS["client_requests"], client_id)
        _bump_dict(_STT_METRICS["client_usage"], f"{client_id}:{provider}")
        if not event.get("success"):
            et = event.get("error_type", "unknown")
            _bump_dict(_STT_METRICS["client_errors"], f"{client_id}:{et}")

        if event.get("total_latency_ms") is not None:
            _STT_METRICS["latency_ms_window"].append(float(event["total_latency_ms"]))
        if event.get("audio_duration_sec") is not None:
            _STT_METRICS["audio_sec_window"].append(float(event["audio_duration_sec"]))

        cost = float(event.get("estimated_cost_inr", 0.0) or 0.0)
        _STT_METRICS["cost_inr_total"] += cost

        attempt_costs = event.get("provider_attempt_cost_inr") or {}
        if isinstance(attempt_costs, dict) and attempt_costs:
            for p, c in attempt_costs.items():
                _bump_dict(_STT_METRICS["provider_cost_inr"], str(p), float(c or 0.0))
        else:
            _bump_dict(_STT_METRICS["provider_cost_inr"], provider, cost)

        sid = (event.get("session_id") or "unknown").strip()
        _bump_dict(_STT_METRICS["session_cost_inr"], sid, cost)
        _bump_dict(_STT_METRICS["session_requests"], sid, 1)
        _STT_METRICS["session_last_seen"][sid] = time.time()
        _bump_dict(_STT_METRICS["client_cost_inr"], client_id, cost)

        _STT_METRICS["recent_events"].append(event)


def get_session_spend_inr(session_id: str) -> float:
    sid = (session_id or "unknown").strip()
    with _STT_METRICS_LOCK:
        return float(_STT_METRICS["session_cost_inr"].get(sid, 0.0) or 0.0)


def get_client_spend_inr(client_id: str) -> float:
    cid = (client_id or "default").strip() or "default"
    with _STT_METRICS_LOCK:
        return float(_STT_METRICS["client_cost_inr"].get(cid, 0.0) or 0.0)


def cleanup_stale_sessions(stale_cutoff: float, max_tracked_sessions: int):
    with _STT_METRICS_LOCK:
        session_last_seen = _STT_METRICS["session_last_seen"]
        stale_sids = [sid for sid, ts in session_last_seen.items() if ts < stale_cutoff]
        for sid in stale_sids:
            session_last_seen.pop(sid, None)
            _STT_METRICS["session_cost_inr"].pop(sid, None)
            _STT_METRICS["session_requests"].pop(sid, None)

        if len(session_last_seen) > max_tracked_sessions:
            overflow = len(session_last_seen) - int(max_tracked_sessions)
            oldest = sorted(session_last_seen.items(), key=lambda item: item[1])[:overflow]
            for sid, _ in oldest:
                session_last_seen.pop(sid, None)
                _STT_METRICS["session_cost_inr"].pop(sid, None)
                _STT_METRICS["session_requests"].pop(sid, None)


def get_stt_metrics_snapshot() -> dict:
    with _STT_METRICS_LOCK:
        total = _STT_METRICS["total_requests"]
        success = _STT_METRICS["success_count"]
        fallback_count = _STT_METRICS["fallback_used_count"]
        failure = _STT_METRICS["failure_count"]
        avg_latency_ms = (
            sum(_STT_METRICS["latency_ms_window"]) / len(_STT_METRICS["latency_ms_window"])
            if _STT_METRICS["latency_ms_window"]
            else 0.0
        )
        total_audio_sec = sum(_STT_METRICS["audio_sec_window"])
        total_cost = float(_STT_METRICS["cost_inr_total"])
        cost_per_audio_sec = (total_cost / total_audio_sec) if total_audio_sec > 0 else 0.0
        latency_per_audio_sec = (
            (sum(_STT_METRICS["latency_ms_window"]) / 1000.0) / total_audio_sec
            if total_audio_sec > 0 and _STT_METRICS["latency_ms_window"]
            else 0.0
        )

        return {
            "active_stt_requests": _STT_METRICS["active_requests"],
            "total_requests": total,
            "success_rate": round((success / total) * 100, 2) if total else 0.0,
            "error_rate": round((failure / total) * 100, 2) if total else 0.0,
            "fallback_rate": round((fallback_count / total) * 100, 2) if total else 0.0,
            "timeout_count": _STT_METRICS["timeout_count"],
            "retry_total": _STT_METRICS["retry_total"],
            "queue_rejected": _STT_METRICS["queue_rejected"],
            "dropped_chunks": _STT_METRICS["dropped_chunks"],
            "avg_latency_ms": round(avg_latency_ms, 2),
            "latency_per_audio_sec": round(latency_per_audio_sec, 4),
            "cost_inr_total": round(total_cost, 6),
            "cost_per_audio_sec": round(cost_per_audio_sec, 6),
            "provider_usage": dict(_STT_METRICS["provider_usage"]),
            "provider_errors": dict(_STT_METRICS["provider_errors"]),
            "provider_cost_inr": dict(_STT_METRICS["provider_cost_inr"]),
            "client_cost_inr": dict(_STT_METRICS["client_cost_inr"]),
            "client_requests": dict(_STT_METRICS["client_requests"]),
            "client_usage": dict(_STT_METRICS["client_usage"]),
            "client_errors": dict(_STT_METRICS["client_errors"]),
            "session_cost_inr": dict(_STT_METRICS["session_cost_inr"]),
            "sessions_tracked": len(_STT_METRICS["session_last_seen"]),
            "recent_events": list(_STT_METRICS["recent_events"]),
        }
