"""
Session state for Probe. In-memory, per-process — fine for a demo,
would move to Redis/DB for production. Each session tracks:
  - uploaded files (raw extraction results)
  - domain_data: auto-detected or user-confirmed domain -> dataframe records
  - domain_source: provenance metadata per domain
  - canvas_cells: generative notebook cell history
"""
import threading
import time
import uuid

_sessions = {}
_lock = threading.Lock()

CLINICAL_DOMAINS = ["DM", "EX", "AE", "RS", "DS"]


def new_session() -> str:
    sid = uuid.uuid4().hex[:12]
    with _lock:
        _sessions[sid] = {
            "created_at": time.time(),
            "user_info": {},      # name, organization, email, project
            "files": {},          # filename -> extraction result
            "domain_data": {},    # domain code -> dataframe records
            "domain_source": {},  # domain code -> provenance dict
            "derivation_meta": {},  # recipe + variable_origins written by _derive_*
            "canvas_cells": [],
        }
    return sid


def get_session(sid: str) -> dict:
    with _lock:
        return _sessions.get(sid)


def session_exists(sid: str) -> bool:
    with _lock:
        return sid in _sessions


def update_session(sid: str, **kwargs):
    with _lock:
        if sid in _sessions:
            _sessions[sid].update(kwargs)


def missing_domains(sid: str) -> list:
    """Returns missing clinical SDTM domains (only meaningful for clinical_trial context)."""
    sess = get_session(sid)
    if not sess:
        return CLINICAL_DOMAINS[:]
    return [d for d in CLINICAL_DOMAINS if d not in sess["domain_data"]]


def has_any_data(sid: str) -> bool:
    """True if the session has at least one domain or uploaded file."""
    sess = get_session(sid)
    if not sess:
        return False
    return bool(sess["domain_data"] or sess["files"])
