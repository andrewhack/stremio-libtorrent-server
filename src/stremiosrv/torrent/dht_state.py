"""Persist the DHT routing table so a restart does not depend on anyone else's servers.

Joining the DHT requires an entry point. Without a saved routing table libtorrent falls back to its
built-in bootstrap routers — a handful of hostnames operated by two organisations. The DHT itself is
decentralised and cannot be switched off, but *joining* it is a chokepoint: if those names stop
resolving, a freshly booted node never finds its first peer, even though the swarm is alive and it
holds perfectly valid infohashes.

A node that has been online even once already knows hundreds of live peers. Writing that knowledge
to disk turns every reboot into a self-service rejoin. For an appliance — powered off for months,
then plugged in — this is the difference between working and not.

Deliberately free of any libtorrent import: the two calls that need it are injected, so this is
testable on any machine and a libtorrent API change is a one-line edit at the call site.
"""
from __future__ import annotations

import os

STATE_FILENAME = "dht.state"


def state_path(cache_root: str) -> str:
    """Beside the cache, so it shares the volume's lifetime — wiping the cache is a deliberate
    reset and should drop the routing table with it."""
    return os.path.join(cache_root, STATE_FILENAME)


def bootstrap_setting(nodes: str | None) -> str | None:
    """Normalise an operator-supplied bootstrap list, or None to keep libtorrent's defaults.

    Exists so nobody is forced to depend on the built-in routers. Empty means "leave the defaults
    alone", not "no bootstrap" — silently disabling bootstrap would brick a first boot.
    """
    if not nodes or not nodes.strip():
        return None
    return ",".join(part.strip() for part in nodes.split(",") if part.strip()) or None


def load_session_params(path: str, settings: dict, *, read_params):
    """Session params carrying the saved routing table, or None for a cold start.

    Returns None rather than raising on a missing or unreadable file: a corrupt state after a power
    cut must degrade to re-bootstrapping, never to an engine that will not start. An appliance that
    refuses to boot is worse than one that takes a minute to find peers.
    """
    try:
        with open(path, "rb") as fh:
            buf = fh.read()
    except OSError:
        return None
    try:
        params = read_params(buf)
    except Exception:  # noqa: BLE001 — any decode failure is just "no usable state"
        return None
    # Our settings win. The file also carries whatever settings were live when it was written, and
    # restoring those would let a stale listen port or rate limit outlive the config that replaced
    # it. We want the routing table back, not last month's configuration.
    params.settings = {**dict(params.settings), **settings}
    return params


def save_session_params(path: str, params, *, write_buf) -> bool:
    """Write the routing table atomically. Returns success; never raises.

    Called from the background saver thread and from shutdown. Raising in the saver would kill the
    thread that also persists resume data — losing playback progress to protect a routing table
    would be a bad trade. Written via a temp file because the next boot reads this, and a
    half-written file is exactly the corrupt-state case load() has to tolerate.
    """
    tmp = path + ".tmp"
    try:
        with open(tmp, "wb") as fh:
            fh.write(write_buf(params))
        os.replace(tmp, path)
        return True
    except Exception:  # noqa: BLE001 — best-effort by design
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False
