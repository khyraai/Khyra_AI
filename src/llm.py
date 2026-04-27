"""
llm.py — LLM Pool Manager

Drop-in replacement for groq.AsyncGroq with:
  - Round-robin across GROQ_API_KEYS (comma-separated in .env)
  - Per-key asyncio.Semaphore concurrency cap
  - Retry on 429 / rate-limit errors with automatic key rotation
  - Structured metrics: latency, token usage, cost (USD), per-key stats
  - get_llm_metrics_snapshot() for /llm/metrics endpoint

Usage (drop-in for groq_client):
    from llm import llm_pool
    response = await llm_pool.chat.completions.create(model=..., messages=..., ...)

Custom kwargs stripped before Groq call (for metrics only):
    _agent_name  (str) — e.g. "agent1", "agent2_kn"
    _client_id   (str) — tenant / caller identifier
"""

import os
import time
import asyncio
import itertools
import threading
from typing import Any

from groq import AsyncGroq
from dotenv import load_dotenv

load_dotenv()


# -----------------------------------------------------------------------
# Configuration — all overridable via .env
# -----------------------------------------------------------------------
def _load_keys() -> list:
    raw = os.getenv("GROQ_API_KEYS", "").strip()
    if raw:
        keys = [k.strip() for k in raw.split(",") if k.strip()]
        if keys:
            return keys
    single = os.getenv("GROQ_API_KEY", "").strip()
    if single:
        return [single]
    raise ValueError(
        "No Groq API keys found. Set GROQ_API_KEYS (comma-separated) or GROQ_API_KEY in .env"
    )


LLM_MODEL                  = os.getenv("LLM_MODEL",                    "llama-3.3-70b-versatile")
LLM_MAX_CONCURRENT_PER_KEY = int(os.getenv("LLM_MAX_CONCURRENT_PER_KEY", "5"))
LLM_MAX_RETRIES            = int(os.getenv("LLM_MAX_RETRIES",            "2"))
LLM_RETRY_DELAY_SEC        = float(os.getenv("LLM_RETRY_DELAY_SEC",      "0.3"))
_COST_INPUT_PER_1M         = float(os.getenv("LLM_COST_INPUT_PER_1M",    "0.59"))
_COST_OUTPUT_PER_1M        = float(os.getenv("LLM_COST_OUTPUT_PER_1M",   "0.79"))


# -----------------------------------------------------------------------
# Metrics Store (thread-safe — written from async context via threading.Lock)
# -----------------------------------------------------------------------
_mlock = threading.Lock()
_m: dict = {
    "total_requests":        0,
    "success":               0,
    "failure":               0,
    "total_retries":         0,
    "total_rate_limit_hits": 0,
    "total_input_tokens":    0,
    "total_output_tokens":   0,
    "total_cost_usd":        0.0,
    "total_latency_ms":      0.0,
    "per_key":               {},  # "0" | "1" | "2" → {requests, failures, rate_limits, tokens}
    "recent_events":         [],  # capped at 100
}


def _record(ev: dict):
    with _mlock:
        _m["total_requests"]        += 1
        _m["success"]               += int(bool(ev.get("success")))
        _m["failure"]               += int(not ev.get("success"))
        _m["total_retries"]         += ev.get("retry_count",     0)
        _m["total_rate_limit_hits"] += ev.get("rate_limit_hits", 0)
        _m["total_input_tokens"]    += ev.get("input_tokens",    0)
        _m["total_output_tokens"]   += ev.get("output_tokens",   0)
        _m["total_cost_usd"]        += ev.get("cost_usd",        0.0)
        _m["total_latency_ms"]      += ev.get("latency_ms",      0.0)

        ki = str(ev.get("key_index", 0))
        pk = _m["per_key"].setdefault(ki, {
            "requests": 0, "failures": 0,
            "rate_limits": 0, "input_tokens": 0, "output_tokens": 0,
        })
        pk["requests"]      += 1
        pk["failures"]      += int(not ev.get("success"))
        pk["rate_limits"]   += ev.get("rate_limit_hits", 0)
        pk["input_tokens"]  += ev.get("input_tokens",    0)
        pk["output_tokens"] += ev.get("output_tokens",   0)

        _m["recent_events"].append(ev)
        if len(_m["recent_events"]) > 100:
            _m["recent_events"] = _m["recent_events"][-100:]


def get_llm_metrics_snapshot() -> dict:
    with _mlock:
        snap = {k: v for k, v in _m.items() if k != "recent_events"}
        snap["recent_events"]    = list(_m["recent_events"][-20:])
        snap["per_key"]          = {k: dict(v) for k, v in _m["per_key"].items()}
        t = snap["total_requests"]
        snap["success_rate_pct"] = round(snap["success"] / t * 100, 2) if t else 0.0
        snap["avg_latency_ms"]   = round(snap["total_latency_ms"] / t, 2) if t else 0.0
        snap["total_cost_usd"]   = round(snap["total_cost_usd"], 8)
        return snap


# -----------------------------------------------------------------------
# Proxy namespaces — mimic groq_client.chat.completions.create(...)
# -----------------------------------------------------------------------
class _Completions:
    def __init__(self, pool: "LLMPool"):
        self._pool = pool

    async def create(self, **kwargs) -> Any:
        return await self._pool._execute(**kwargs)


class _Chat:
    def __init__(self, pool: "LLMPool"):
        self.completions = _Completions(pool)


# -----------------------------------------------------------------------
# LLMPool
# -----------------------------------------------------------------------
class LLMPool:
    """
    Round-robin AsyncGroq pool with per-key semaphore and retry-on-429.

    Identical interface to groq.AsyncGroq — pass anywhere groq_client is expected:
        await llm_pool.chat.completions.create(model=..., messages=..., ...)
    """

    def __init__(self):
        self._keys    = _load_keys()
        self._clients = [AsyncGroq(api_key=k) for k in self._keys]
        self._sems    = [asyncio.Semaphore(LLM_MAX_CONCURRENT_PER_KEY) for _ in self._keys]
        self._cycle   = itertools.cycle(range(len(self._keys)))
        self._lock    = asyncio.Lock()
        self.chat     = _Chat(self)
        print(
            f"[LLM Pool] ✅ {len(self._keys)} key(s) loaded | "
            f"{LLM_MAX_CONCURRENT_PER_KEY} concurrent/key | "
            f"max retries={LLM_MAX_RETRIES} | default model={LLM_MODEL}"
        )

    @property
    def num_keys(self) -> int:
        return len(self._keys)

    async def _next(self) -> int:
        async with self._lock:
            return next(self._cycle)

    async def _execute(self, **kwargs) -> Any:
        """
        Execute a chat completion with round-robin key selection.
        On 429/rate-limit: rotate to the next key and retry.
        """
        agent_name = kwargs.pop("_agent_name", "unknown")
        client_id  = kwargs.pop("_client_id",  "default")

        start   = time.time()
        retries = 0
        rl_hits = 0
        ki      = await self._next()
        max_att = LLM_MAX_RETRIES * len(self._keys)

        for attempt in range(max_att):
            try:
                async with self._sems[ki]:
                    result = await self._clients[ki].chat.completions.create(**kwargs)

                latency_ms = (time.time() - start) * 1000
                usage = getattr(result, "usage", None)
                inp   = int(getattr(usage, "prompt_tokens",     0) or 0)
                out   = int(getattr(usage, "completion_tokens", 0) or 0)
                cost  = (inp / 1_000_000) * _COST_INPUT_PER_1M + (out / 1_000_000) * _COST_OUTPUT_PER_1M

                _record({
                    "success":         True,
                    "key_index":       ki,
                    "retry_count":     retries,
                    "rate_limit_hits": rl_hits,
                    "latency_ms":      round(latency_ms, 2),
                    "input_tokens":    inp,
                    "output_tokens":   out,
                    "cost_usd":        round(cost, 8),
                    "agent":           agent_name,
                    "client_id":       client_id,
                    "ts":              round(time.time(), 3),
                })
                return result

            except asyncio.CancelledError:
                raise  # let asyncio.wait_for propagate — agents handle TimeoutError

            except Exception as exc:
                err = str(exc).lower()
                is_rl = "429" in err or "rate limit" in err or "rate_limit" in err

                if is_rl:
                    rl_hits += 1
                    print(f"[LLM Pool] ⚡ 429 rate-limit on key[{ki}] — rotating to next key")
                else:
                    print(f"[LLM Pool] ❌ Error key[{ki}] attempt {attempt + 1}/{max_att}: {exc}")

                retries += 1
                ki = await self._next()
                if not is_rl:  # 429 → rotate instantly; other errors → brief backoff
                    await asyncio.sleep(min(LLM_RETRY_DELAY_SEC * (attempt + 1), 2.0))

        _record({
            "success":         False,
            "key_index":       ki,
            "retry_count":     retries,
            "rate_limit_hits": rl_hits,
            "latency_ms":      round((time.time() - start) * 1000, 2),
            "agent":           agent_name,
            "client_id":       client_id,
            "ts":              round(time.time(), 3),
        })
        raise RuntimeError(f"[LLM Pool] All {max_att} attempts exhausted across {len(self._keys)} key(s)")


# -----------------------------------------------------------------------
# Singleton — import and use directly
# -----------------------------------------------------------------------
llm_pool = LLMPool()
