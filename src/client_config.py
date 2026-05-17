"""
client_config.py — Client configuration loader.

Loads src/client_config.json which is keyed by DID phone number.
Each entry identifies a clinic/client and carries all profile data used
to build dynamic agent prompts and to track per-client analytics.

Public API:
    load_client_configs()               -> dict  (full map, keyed by normalised DID)
    get_config_by_did(did)              -> dict | None
    get_config_by_client_id(client_id)  -> dict | None
    get_default_config()                -> dict  (fallback for mic/browser mode)
"""

import json
import os
import re

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "client_config.json")

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

_cached_configs: dict = {}
_did_to_config: dict = {}
_id_to_config: dict = {}


def _normalise_did(raw: str) -> str:
    """Strip non-digit chars, keep leading +."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    keep_plus = raw.startswith("+")
    digits = re.sub(r"\D", "", raw)
    return ("+" + digits) if (keep_plus and digits) else digits


def load_client_configs() -> dict:
    """Load and cache client_config.json. Returns DID-keyed dict."""
    global _cached_configs, _did_to_config, _id_to_config
    if _cached_configs:
        return _cached_configs

    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        print(f"[ClientConfig] {_CONFIG_PATH} not found — using default only")
        raw = {}
    except Exception as e:
        print(f"[ClientConfig] Failed to load config: {e}")
        raw = {}

    normalised: dict = {}
    id_map: dict = {}

    for did, cfg in raw.items():
        if not isinstance(cfg, dict):
            continue
        norm_did = _normalise_did(did)
        if not norm_did:
            continue
        entry = dict(cfg)
        entry["_did"] = norm_did
        normalised[norm_did] = entry
        cid = cfg.get("client_id", "")
        if cid:
            id_map[cid] = entry

    _cached_configs = normalised
    _did_to_config = normalised
    _id_to_config = id_map
    return normalised


def _load_from_db_by_did(norm: str) -> dict | None:
    """DB fallback: query clients table for a DID not found in JSON cache."""
    try:
        from pg import get_conn
        with get_conn() as cur:
            cur.execute(
                "SELECT * FROM clients WHERE did_number = %s OR did_number = %s LIMIT 1",
                (norm, norm.lstrip("+")),
            )
            row = cur.fetchone()
        if row:
            cfg = dict(row)
            # Cache so next lookup is instant
            _did_to_config[norm] = cfg
            cid = cfg.get("client_id", "")
            if cid:
                _id_to_config[cid] = cfg
            return cfg
    except Exception as e:
        print(f"[ClientConfig] DB fallback error for DID {norm}: {e}")
    return None


def _load_from_db_by_client_id(client_id: str) -> dict | None:
    """DB fallback: query clients table for a client_id not in JSON cache."""
    try:
        from pg import get_conn
        with get_conn() as cur:
            cur.execute("SELECT * FROM clients WHERE client_id = %s LIMIT 1", (client_id,))
            row = cur.fetchone()
        if row:
            cfg = dict(row)
            _id_to_config[client_id] = cfg
            norm = _normalise_did(cfg.get("did_number", ""))
            if norm:
                _did_to_config[norm] = cfg
            return cfg
    except Exception as e:
        print(f"[ClientConfig] DB fallback error for client_id {client_id}: {e}")
    return None


def get_config_by_did(did: str) -> dict | None:
    """Return config for the given DID number, or None if not found.
    Checks JSON cache first; falls back to DB for dynamically registered clients."""
    load_client_configs()
    norm = _normalise_did(did)
    if norm in _did_to_config:
        return _did_to_config[norm]
    if not norm.startswith("+") and ("+" + norm) in _did_to_config:
        return _did_to_config["+" + norm]
    if norm.startswith("+") and norm[1:] in _did_to_config:
        return _did_to_config[norm[1:]]
    # DB fallback — client registered in DB but not in client_config.json
    return _load_from_db_by_did(norm)


def get_config_by_client_id(client_id: str) -> dict | None:
    """Return config by client_id string, or None if not found.
    Checks JSON cache first; falls back to DB."""
    load_client_configs()
    cid = (client_id or "").strip()
    result = _id_to_config.get(cid)
    if result:
        return result
    return _load_from_db_by_client_id(cid)


def get_default_config() -> dict:
    """Fallback config used in mic/browser mode (no DID available)."""
    load_client_configs()
    if _id_to_config:
        return next(iter(_id_to_config.values()))
    return dict(_DEFAULT_CONFIG)


def reload_configs() -> dict:
    """Force reload from disk (useful after file edit in dev)."""
    global _cached_configs, _did_to_config, _id_to_config
    _cached_configs = {}
    _did_to_config = {}
    _id_to_config = {}
    return load_client_configs()
