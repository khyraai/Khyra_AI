"""
client_config.py — Client configuration loader.

DB (clients table) is the primary source of truth for all client data.
client_config.json is a dev-only fallback and can be absent on production.

Lookup order for every public function:
  1. In-memory cache (TTL = DB_CACHE_TTL_SECONDS, default 5 min)
  2. PostgreSQL clients table  ← always queried on cache miss/expiry
  3. client_config.json        ← dev / offline fallback only
  4. _DEFAULT_CONFIG           ← last resort

New clients added to the DB are auto-discovered on their first call.
Updated client data is picked up within DB_CACHE_TTL_SECONDS seconds.

Public API:
    get_config_by_did(did)              -> dict | None
    get_config_by_client_id(client_id)  -> dict | None
    get_default_config()                -> dict
    reload_configs()                    -> None
    load_client_configs()               -> dict  (legacy shim)
"""

import json
import os
import re
import time

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "client_config.json")

DB_CACHE_TTL_SECONDS = 300

_DEFAULT_CONFIG = {
    "client_id": "CLN001",
    "clinic_name": "Doctor Deepti's Dental and Orthodontic Centre",
    "doctor_name": "Doctor Naga Deepti",
    "doctor_qualifications": "MDS — Orthodontics and Dentofacial Orthopaedics",
    "address": "Number 39, 3rd Cross, Dwarakanagar, Hoskerehalli, Bangalore",
    "timings": "Monday to Saturday — 10:00 AM to 1:00 PM and 4:00 PM to 7:00 PM. Closed on Sunday.",
    "doctor_mobile": "+91 9187471874",
    "consultation_fee_min": 200,
    "consultation_fee_max": 300,
    "default_language": "kn",
    "emergency_transfer_number": "+918660033297",
    "connection_id": "deepti_dental",
}

_did_to_config: dict = {}
_id_to_config:  dict = {}
_cache_ts:      dict = {}

_json_loaded:   bool = False
_json_did_map:  dict = {}
_json_id_map:   dict = {}


def _normalise_did(raw: str) -> str:
    """Strip non-digit chars, keep leading +."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    keep_plus = raw.startswith("+")
    digits = re.sub(r"\D", "", raw)
    return ("+" + digits) if (keep_plus and digits) else digits


def _is_stale(key: str) -> bool:
    """Return True if the cached entry has exceeded the TTL."""
    return (time.monotonic() - _cache_ts.get(key, 0.0)) > DB_CACHE_TTL_SECONDS


def _cache_cfg(cfg: dict, norm_did: str = "") -> None:
    """Store config in both caches with a fresh timestamp."""
    now = time.monotonic()
    if norm_did:
        _did_to_config[norm_did] = cfg
        _cache_ts[norm_did] = now
    cid = cfg.get("client_id", "")
    if cid:
        _id_to_config[cid] = cfg
        _cache_ts[cid] = now


def _load_json_configs() -> None:
    """Load client_config.json into a separate map (dev/offline fallback only)."""
    global _json_loaded, _json_did_map, _json_id_map
    if _json_loaded:
        return
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        raw = {}
    except Exception as e:
        print(f"[ClientConfig] JSON load error: {e}")
        raw = {}

    did_map: dict = {}
    id_map:  dict = {}
    for did, cfg in raw.items():
        if not isinstance(cfg, dict):
            continue
        norm = _normalise_did(did)
        if not norm:
            continue
        entry = dict(cfg)
        entry["_did"] = norm
        did_map[norm] = entry
        cid = cfg.get("client_id", "")
        if cid:
            id_map[cid] = entry

    _json_did_map = did_map
    _json_id_map  = id_map
    _json_loaded  = True


def _query_db_by_did(norm: str) -> dict | None:
    """Query DB for a client by DID (tries norm, digits, +digits variants)."""
    try:
        from pg import get_conn
        with get_conn() as cur:
            digits = norm.lstrip("+")
            cur.execute(
                "SELECT * FROM clients WHERE did_number = %s OR did_number = %s OR did_number = %s LIMIT 1",
                (norm, digits, "+" + digits),
            )
            row = cur.fetchone()
        if row:
            cfg = dict(row)
            _cache_cfg(cfg, norm)
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
            return cfg
    except Exception as e:
        print(f"[ClientConfig] DB client_id lookup error for {cid}: {e}")
    return None


def get_config_by_did(did: str) -> dict | None:
    """Return config for the given DID. DB is primary; JSON is dev fallback."""
    norm = _normalise_did(did)
    if not norm:
        return None

    cached = _did_to_config.get(norm)
    if cached and not _is_stale(norm):
        return cached

    cfg = _query_db_by_did(norm)
    if cfg:
        return cfg

    _load_json_configs()
    if norm in _json_did_map:
        return _json_did_map[norm]
    if not norm.startswith("+") and ("+" + norm) in _json_did_map:
        return _json_did_map["+" + norm]
    if norm.startswith("+") and norm[1:] in _json_did_map:
        return _json_did_map[norm[1:]]
    return None


def get_config_by_client_id(client_id: str) -> dict | None:
    """Return config by client_id. DB is primary; JSON is dev fallback."""
    cid = (client_id or "").strip()
    if not cid:
        return None

    cached = _id_to_config.get(cid)
    if cached and not _is_stale(cid):
        return cached

    cfg = _query_db_by_client_id(cid)
    if cfg:
        return cfg

    _load_json_configs()
    return _json_id_map.get(cid)


def get_default_config() -> dict:
    """Return any active client config. DB is primary; JSON/hardcoded fallback."""
    try:
        from pg import get_conn
        with get_conn() as cur:
            cur.execute("SELECT * FROM clients WHERE client_id != 'CLN000' LIMIT 1")
            row = cur.fetchone()
        if row:
            cfg = dict(row)
            norm = _normalise_did(cfg.get("did_number", ""))
            _cache_cfg(cfg, norm)
            return cfg
    except Exception as e:
        print(f"[ClientConfig] DB default fetch error: {e}")

    _load_json_configs()
    if _json_id_map:
        return next(iter(_json_id_map.values()))
    return dict(_DEFAULT_CONFIG)


def reload_configs() -> None:
    """Flush all caches — next call will re-fetch everything from DB."""
    global _did_to_config, _id_to_config, _cache_ts
    global _json_loaded, _json_did_map, _json_id_map
    _did_to_config = {}
    _id_to_config  = {}
    _cache_ts      = {}
    _json_loaded   = False
    _json_did_map  = {}
    _json_id_map   = {}
    print("[ClientConfig] Caches cleared — will re-fetch from DB on next call")


def load_client_configs() -> dict:
    """Legacy shim kept for import compatibility. Returns JSON DID map."""
    _load_json_configs()
    return dict(_json_did_map)
