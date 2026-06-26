import os

from fastapi import APIRouter, Request, Response

from stremiosrv.certcheck import cert_days_left

router = APIRouter()

CERT_WARN_DAYS = 14  # flag the trusted cert as degraded once it's within this window of expiry


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
    status = "healthy" if all(v == "ok" for v in components.values()) else "degraded"
    response.status_code = 200 if status == "healthy" else 503
    return {"status": status, "components": components, **extra}
