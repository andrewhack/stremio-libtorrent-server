"""Minimal client for api.strem.io, used only to prove who is signing in.

**A 200 is not a success.** The API answers a rejected authKey with HTTP 200 and a body of
`{"error":{"code":1,"message":"Session does not exist"}}` — measured, not assumed. A client that
branches on the status code therefore authenticates anybody who sends any string as an authKey. The
envelope check in `_call` is the only place that decision is made; do not add a second path.

Failure detail is preserved on the exception for the server's own log, never for the caller: a
wrong email answers `{"code":2,"message":"User not found","wrongEmail":true}`, which is an
account-enumeration oracle. The API layer above collapses every failure to one opaque response.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

API_BASE = "https://api.strem.io/api"
TIMEOUT = 15.0


class StremioApiError(RuntimeError):
    """Any outcome that is not a well-formed `result` — API error, malformed body, or transport
    failure. Callers must treat every one of them as 'not authenticated'."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"stremio api error {code}: {message}")
        self.code = code
        self.message = message


def _default_transport(url: str, body: bytes, timeout: float) -> bytes:
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}
    )
    # The URL is built from the module-level API_BASE, never from caller input.
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _call(endpoint: str, payload: dict, *, transport=None, base: str = API_BASE) -> dict:
    send = transport or _default_transport
    try:
        raw = send(f"{base}/{endpoint}", json.dumps(payload).encode(), TIMEOUT)
        data = json.loads(raw)
    except (OSError, urllib.error.URLError, ValueError) as e:
        raise StremioApiError(-1, f"{type(e).__name__}: {e}") from e
    if not isinstance(data, dict):
        raise StremioApiError(-1, "malformed response: not an object")
    err = data.get("error")
    if err:
        code = err.get("code", -1) if isinstance(err, dict) else -1
        msg = err.get("message", "") if isinstance(err, dict) else str(err)
        raise StremioApiError(code, msg)
    result = data.get("result")
    if not isinstance(result, dict):
        raise StremioApiError(-1, "no result in response")
    return result


def get_user(auth_key: str, *, transport=None) -> dict:
    """The account behind `auth_key`. Returns the user object (`_id`, `email`, ...)."""
    return _call("getUser", {"authKey": auth_key}, transport=transport)


def login(email: str, password: str, *, transport=None) -> dict:
    """Exchange credentials for `{authKey, user}`. The password is used here and discarded — it is
    never stored, never logged, and never written to the state file."""
    return _call("login", {"email": email, "password": password}, transport=transport)
