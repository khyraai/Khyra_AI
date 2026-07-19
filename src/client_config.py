"""
client_config.py -- Client configuration loader (DB-only).

PostgreSQL clients table is the single source of truth for all client data.
client_config.json is no longer used or required.

Cache strategy:
  - In-memory cache with TTL = DB_CACHE_TTL_SECONDS (default 5 min).
  - On cache miss or expiry, the DB is queried and the result is cached.
  - New clients added to the DB are auto-discovered on their first call.
  - Updated client data is picked up within DB_CACHE_TTL_SECONDS seconds.

Public API:
    get_config_by_did(did)              -> dict | None
    get_config_by_client_id(client_id)  -> dict | None
    get_default_config()                -> dict
    reload_configs()                    -> None
    load_client_configs()               -> dict  (legacy no-op shim)
"""

import json
import re
import time

DB_CACHE_TTL_SECONDS = 300  # 5 minutes

_DEFAULT_CONFIG = {
    "client_id":                 "default",
    "clinic_name":               "our clinic",
    "doctor_name":               "the doctor",
    "doctor_qualifications":     "",
    "address":                   "I don't have the exact address handy right now.",
    "timings":                   "Monday to Saturday -- 10:00 AM to 1:00 PM and 4:00 PM to 7:00 PM. Closed on Sunday.",
    "doctor_mobile":             "",
    "consultation_fee_min":      200,
    "consultation_fee_max":      500,
    "default_language":          "en",
    "emergency_transfer_number": "",
    "connection_id":             "default",
}

# ---------------------------------------------------------------------------
# In-memory cache
# ---------------------------------------------------------------------------
_did_to_config: dict = {}
_id_to_config:  dict = {}
_cache_ts:      dict = {}


def _normalise_did(raw: str) -> str:
    """Strip non-digit chars, preserve leading +."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    keep_plus = raw.startswith("+")
    digits = re.sub(r"\D", "", raw)
    return ("+" + digits) if (keep_plus and digits) else digits


def _is_stale(key: str) -> bool:
    return (time.monotonic() - _cache_ts.get(key, 0.0)) > DB_CACHE_TTL_SECONDS


def _cache_cfg(cfg: dict, norm_did: str = "") -> None:
    now = time.monotonic()
    if norm_did:
        _did_to_config[norm_did] = cfg
        _cache_ts[norm_did] = now
    cid = cfg.get("client_id", "")
    if cid:
        _id_to_config[cid] = cfg
        _cache_ts[cid] = now


# ---------------------------------------------------------------------------
# DB queries
# ---------------------------------------------------------------------------
def _query_db_by_did(norm: str) -> dict | None:
    """Query DB for a client by DID number (tries multiple formats)."""
    try:
        from pg import get_conn
        digits = norm.lstrip("+")
        with get_conn() as cur:
            cur.execute(
                "SELECT * FROM clients WHERE did_number = %s OR did_number = %s OR did_number = %s LIMIT 1",
                (norm, digits, "+" + digits),
            )
            row = cur.fetchone()
        if row:
            cfg = dict(row)
            _cache_cfg(cfg, norm)
            print(f"[ClientConfig] DB DID lookup hit: {norm} -> client_id={cfg.get('client_id')}")
            print(f"[ClientConfig] Fetched details:\n{json.dumps(cfg, default=str, indent=2)}")
            return cfg
    except Exception as e:
        print(f"[ClientConfig] DB DID lookup error for {norm}: {e}")
    return None


def _query_db_by_client_id(cid: str) -> dict | None:
    """Query DB for a client by client_id."""
    try:
        from pg import get_conn
        with get_conn() as cur:
            cur.execute("SELECT * FROM clients WHERE client_id = %s LIMIT 1", (cid,))
            row = cur.fetchone()
        if row:
            cfg = dict(row)
            norm = _normalise_did(cfg.get("did_number", ""))
            _cache_cfg(cfg, norm)
            print(f"[ClientConfig] DB client_id lookup hit: {cid}")
            print(f"[ClientConfig] Fetched details:\n{json.dumps(cfg, default=str, indent=2)}")
            return cfg
    except Exception as e:
        print(f"[ClientConfig] DB client_id lookup error for {cid}: {e}")
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def get_config_by_did(did: str) -> dict | None:
    """
    Return config for the given DID number.
    Checks in-memory cache first (5-min TTL), then queries the DB.
    Returns None if no matching client is found.
    """
    norm = _normalise_did(did)
    if not norm:
        return None

    cached = _did_to_config.get(norm)
    if cached and not _is_stale(norm):
        return cached

    return _query_db_by_did(norm)


def get_config_by_client_id(client_id: str) -> dict | None:
    """
    Return config by client_id.
    Checks in-memory cache first (5-min TTL), then queries the DB.
    Returns None if no matching client is found.
    """
    cid = (client_id or "").strip()
    if not cid:
        return None

    cached = _id_to_config.get(cid)
    if cached and not _is_stale(cid):
        return cached

    return _query_db_by_client_id(cid)


def get_default_config() -> dict:
    """
    Return any single active client config from the DB.
    Falls back to the hardcoded _DEFAULT_CONFIG if DB is unreachable.
    """
    try:
        from pg import get_conn
        with get_conn() as cur:
            cur.execute("SELECT * FROM clients LIMIT 1")
            row = cur.fetchone()
        if row:
            cfg = dict(row)
            norm = _normalise_did(cfg.get("did_number", ""))
            _cache_cfg(cfg, norm)
            print(f"[ClientConfig] DB default fetch hit: {cfg.get('client_id')}")
            print(f"[ClientConfig] Fetched details:\n{json.dumps(cfg, default=str, indent=2)}")
            return cfg
    except Exception as e:
        print(f"[ClientConfig] DB default fetch error: {e}")

    return dict(_DEFAULT_CONFIG)


def reload_configs() -> None:
    """Flush all in-memory caches -- next call will re-fetch from DB."""
    global _did_to_config, _id_to_config, _cache_ts
    _did_to_config = {}
    _id_to_config  = {}
    _cache_ts      = {}
    print("[ClientConfig] Caches cleared -- will re-fetch from DB on next call")


def load_client_configs() -> dict:
    """Legacy no-op shim -- kept for import compatibility. Returns empty dict."""
    return {}
