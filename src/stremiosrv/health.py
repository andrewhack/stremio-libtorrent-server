import json
import os
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

from fastapi import APIRouter, Request, Response

from stremiosrv.certcheck import cert_days_left

router = APIRouter()

CERT_WARN_DAYS = 14  # flag the trusted cert as degraded once it's within this window of expiry
CERT_STATUS_FILE = "cert-status.json"  # written by docker/cert-status.sh at every start and retry
_CERT_STATUS_KEYS = ("source", "state", "reason", "detail", "nextTry")

try:
    _VERSION: str | None = _pkg_version("stremiosrv")  # the running server version, for the admin card
except PackageNotFoundError:  # pragma: no cover - only when not installed as a package
    _VERSION = None


def _cert_status(cache_root: str) -> dict | None:
    """The stremio.rocks certificate's status, when this server uses it (the file names a host).
    Anything unreadable counts as no file: /health must never fail over its own side channel."""
    try:
        with open(os.path.join(cache_root, CERT_STATUS_FILE), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("host"):
        return None
    return {k: data.get(k) for k in _CERT_STATUS_KEYS}


@router.get("/health")
def health(request: Request, response: Response) -> dict:
    """ITCOM health contract: 200 healthy / 503 degraded|unhealthy.

    Components reflect real dependencies. The `cert` component is reported only when a TLS cert is
    actually present (so dev/test stays healthy), and flags a lapsing trusted cert before it expires
    — early warning for the shared `*.stremio.rocks` wildcard (or a bring-your-own cert).
    """
    components = {"http": "ok"}
    extra: dict = {}
    settings = getattr(request.app.state, "settings", None)
    if settings is not None:
        cert_path = os.path.join(
            settings.cache_root, getattr(settings, "cert_file", "certificates.pem")
        )
        if os.path.exists(cert_path):
            days = cert_days_left(cert_path)
            components["cert"] = "ok" if (days is not None and days >= CERT_WARN_DAYS) else "degraded"
            if days is not None:
                extra["certDaysLeft"] = days
        cert_status = _cert_status(settings.cache_root)
        if cert_status is not None:
            extra["certStatus"] = cert_status
            # The owner asked for the trusted certificate and the server is serving a self-signed
            # one: TVs and the desktop app cannot connect. That is degraded, whatever its day count.
            if cert_status["state"] == "waiting":
                components["cert"] = "degraded"
    status = "healthy" if all(v == "ok" for v in components.values()) else "degraded"
    response.status_code = 200 if status == "healthy" else 503
    return {"status": status, "components": components, "version": _VERSION, **extra}
