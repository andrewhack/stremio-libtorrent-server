"""Whether this box may offer the password sign-in form.

The image can fetch a trusted cert for `<dashed-ip>.519b6502d940.stremio.rocks` with no domain of
its own. That cert is a single shared wildcard and its private key is served **unauthenticated over
cleartext HTTP** — verified by requesting a cert for an address we do not own and receiving a valid
key pair. Every install therefore holds the same key, and an on-path attacker holding it can present
a valid certificate for any box's hostname.

That is survivable for the localStorage fast path, where the browser sends an authKey it already
had. It is not survivable for a form that asks the owner to type their Stremio *password*, which is
reusable and belongs to an account we do not control. So: shared cert -> fast path only.
"""
from __future__ import annotations

SHARED_NAME = "*.519b6502d940.stremio.rocks"


def _names(san: str) -> list[str]:
    """The DNS/IP entries of an openssl subjectAltName line, lowercased.

    e.g. `DNS:a.example.com, DNS:b.example.com` -> `['a.example.com', 'b.example.com']`
    """
    out = []
    for part in san.split(","):
        _, _, value = part.strip().partition(":")
        if value:
            out.append(value.strip().casefold())
    return out


def is_shared_cert(san: str | None) -> bool:
    """True only when the cert really can answer for the known-public name.

    Distinct from `not password_login_allowed(...)`, which is also true when the SAN cannot be read
    at all. Both refuse the password form, but they are different facts about the operator's setup
    and telling them the wrong one sends them to fix the wrong thing.
    """
    return bool(san) and SHARED_NAME.casefold() in _names(san)


def password_login_allowed(san: str | None) -> bool:
    """False when the cert can answer for the shared name, and false when the SAN cannot be read at
    all — if we cannot prove the key is not shared, we must not invite a password.

    Membership, not equality. An exact whole-string compare would fail open the moment the shared
    cert gained a second SAN entry — a change on Stremio's side, not ours, that would silently start
    offering the password form on every box using their key. Ask whether the cert can answer for the
    known-public name, which stays true however the line is formatted.
    """
    if not san:
        return False
    return SHARED_NAME.casefold() not in _names(san)
