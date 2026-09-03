"""Owner pinning and browser sessions for the library UI.

Two separable things live here, and conflating them is the failure this module exists to prevent:

* **The link** — which Stremio account owns this box. `getUser` succeeding proves only that an
  authKey belongs to *a* valid account; without pinning, anybody with any Stremio account gets in.
* **The door** — which browsers currently hold a session.

Only the account `_id` is persisted. An email can be changed on the account while the id cannot, and
storing the address would put the owner's email in a file for no benefit.
"""
from __future__ import annotations

import json
import os
import secrets
import threading
import time

STATE_FILE = "library-ui.json"
SESSION_TTL = 30 * 24 * 3600  # 30 days

# Every mutator here is read-modify-write, and the auth endpoints run in uvicorn's threadpool.
# Unsynchronised, 16 concurrent sign-ins raised PermissionError from os.replace on Windows — a 500
# on the sign-in route — because all of them wrote the same `library-ui.json.tmp`. Where it does not
# raise it corrupts, and a corrupt state file is not merely lost sessions: load_state falls back to
# a blank owner_id, so the pin is gone and the next account to sign in claims the box.
_lock = threading.Lock()


def _empty() -> dict:
    return {"owner_id": "", "sessions": {}}


class OwnerMismatch(Exception):
    """A valid Stremio account that is not this box's owner."""


def _path(cache_root: str) -> str:
    return os.path.join(cache_root, STATE_FILE)


def load_state(cache_root: str) -> dict:
    """Current state, or a blank one if absent/unreadable/corrupt. Never raises: a damaged state
    file must cost the operator a re-pairing, not a server that will not answer."""
    try:
        with open(_path(cache_root), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return _empty()
    if not isinstance(data, dict):
        return _empty()
    sessions = data.get("sessions")
    return {"owner_id": data.get("owner_id", "") or "",
            "sessions": sessions if isinstance(sessions, dict) else {}}


def save_state(cache_root: str, state: dict) -> None:
    """Atomically write the state, owner-readable only. The mode is set on the temp file BEFORE the
    rename, so the file is never briefly world-readable under its real name."""
    tmp = f"{_path(cache_root)}.{os.getpid()}.{threading.get_ident()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, _path(cache_root))


def owner_matches(user: dict, pinned_id: str, configured: str) -> bool:
    """Whether `user` may enter. `configured` (STREMIOSRV_LIBRARY_OWNER) wins outright when set and
    may name either the id or the email; otherwise the trust-on-first-use pin decides."""
    uid = str(user.get("_id") or "")
    if configured:
        return configured in (uid, str(user.get("email") or ""))
    if not pinned_id:
        return True  # first sign-in claims the box
    return uid == pinned_id


def claim_owner(cache_root: str, user: dict, configured: str) -> str:
    """Authorise `user` and persist the pin. Returns the owner id; raises OwnerMismatch otherwise.

    The empty-id check is not defensive noise: pinning "" would make `owner_matches` fall into its
    trust-on-first-use branch on every later sign-in, so a malformed account object would silently
    turn the pin off for everyone.
    """
    with _lock:
        state = load_state(cache_root)
        if not owner_matches(user, state["owner_id"], configured):
            raise OwnerMismatch("not this server's owner")
        uid = str(user.get("_id") or "")
        if not uid:
            raise OwnerMismatch("account has no id")
        if state["owner_id"] != uid:
            state["owner_id"] = uid
            save_state(cache_root, state)
        return uid


def new_session(cache_root: str, ttl: int = SESSION_TTL) -> str:
    sid = secrets.token_urlsafe(32)
    now = int(time.time())
    with _lock:
        state = load_state(cache_root)
        state["sessions"][sid] = {"created": now, "expires": now + ttl}
        save_state(cache_root, state)
    return sid


def verify_session(cache_root: str, sid: str) -> bool:
    """True if `sid` is live. Expired entries are reaped as they are found, so the file does not
    grow without bound on a box that is signed into often."""
    if not sid:
        return False
    with _lock:
        state = load_state(cache_root)
        entry = state["sessions"].get(sid)
        now = int(time.time())
        expired = [k for k, v in state["sessions"].items()
                   if not isinstance(v, dict) or v.get("expires", 0) <= now]
        if expired:
            for k in expired:
                state["sessions"].pop(k, None)
            save_state(cache_root, state)
    return isinstance(entry, dict) and entry.get("expires", 0) > now


def drop_session(cache_root: str, sid: str) -> None:
    with _lock:
        state = load_state(cache_root)
        if state["sessions"].pop(sid, None) is not None:
            save_state(cache_root, state)
