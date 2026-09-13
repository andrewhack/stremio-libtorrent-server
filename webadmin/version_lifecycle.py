"""Independent Server/WebAdmin release lifecycle layer.

This module sits on top of the transactional server updater. The streaming
server and WebAdmin deliberately have different release identifiers and update
paths:

* SERVER_VERSION -> transactional in-place Stremio server update.
* WEBADMIN_VERSION -> host-side Compose rebuild of only the WebAdmin service.
* Core version -> informational version reported by the stremiosrv package.

The upstream repository is never used as a runtime update authority.
"""

from __future__ import annotations

import os
import re
import threading
import urllib.request
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import FileResponse, HTMLResponse, Response

import fork_update as transactional

app = transactional.app
legacy = transactional.legacy
SOURCE_REPO = transactional.SOURCE_REPO
SOURCE_BRANCH = transactional.SOURCE_BRANCH
RAW_ROOT = f"{SOURCE_REPO}/raw/{SOURCE_BRANCH}"
SERVER_VERSION_URL = f"{RAW_ROOT}/SERVER_VERSION"
FORK_VERSION_URL = f"{RAW_ROOT}/FORK_VERSION"
WEBADMIN_VERSION_URL = f"{RAW_ROOT}/webadmin/WEBADMIN_VERSION"
WEBADMIN_VERSION_FILE = Path(os.getenv("WEBADMIN_VERSION_FILE", "/app/WEBADMIN_VERSION"))
WEBADMIN_UPDATE_COMMAND = (
    "git pull origin main && docker compose up -d --build --no-deps webadmin"
)
STALE_UPDATE_NOTICE = (
    "Downloads the official source from <code>andrewhack/stremio-libtorrent-server</code>, "
    "validates the Web Admin overlay and activates it only after a successful build. Settings, "
    "pins, cache, certificates and logs are preserved."
)
SAFE_UPDATE_NOTICE = (
    "Server updates are released only from "
    "<code>emmanique/stremio-libtorrent-server-webadmin</code>. "
    "Server and WebAdmin versions are managed independently."
)
STREMIO_ROCKS_RE = re.compile(
    r"([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.stremio\.rocks)", re.IGNORECASE
)


def _request_text(url: str, timeout: int = 5) -> str | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "stremio-webadmin-version"})
        value = urllib.request.urlopen(req, timeout=timeout).read(4096).decode("utf-8").strip()
        return value or None
    except Exception:
        return None


def _remote_server_version() -> str | None:
    return _request_text(SERVER_VERSION_URL) or _request_text(FORK_VERSION_URL)


def _remote_webadmin_version() -> str | None:
    return _request_text(WEBADMIN_VERSION_URL)


def _installed_server_version() -> str | None:
    try:
        container = legacy.client().containers.get(legacy.CONTAINER)
        container.reload()
        for filename in ("SERVER_VERSION", "FORK_VERSION"):
            result = container.exec_run(["cat", f"/srv/app/{filename}"])
            if result.exit_code == 0:
                value = result.output.decode("utf-8", errors="replace").strip()
                if value:
                    return value
    except Exception:
        return None
    return None


def _installed_webadmin_version() -> str | None:
    try:
        value = WEBADMIN_VERSION_FILE.read_text(encoding="utf-8").strip()
        return value or None
    except OSError:
        return None


def _core_version() -> str | None:
    health = legacy.get_json("/health", {})
    if isinstance(health, dict):
        value = health.get("version")
        return str(value) if value else None
    return None


def _container_env(container) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in (container.attrs.get("Config", {}).get("Env") or []):
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        result[key] = value
    return result


def _normalise_url(value: str) -> str:
    value = value.strip()
    if value and not value.endswith("/"):
        value += "/"
    return value


def _connection_urls() -> dict[str, object]:
    """Return the connection URLs actually configured by the running server."""
    ip = os.getenv("IPADDRESS", "localhost") or "localhost"
    explicit_server_url = ""
    trusted_url = ""

    try:
        container = legacy.client().containers.get(legacy.CONTAINER)
        container.reload()
        env = _container_env(container)
        ip = env.get("IPADDRESS") or ip
        explicit_server_url = _normalise_url(env.get("SERVER_URL", ""))

        if explicit_server_url.startswith("https://"):
            trusted_url = explicit_server_url
        elif not explicit_server_url:
            cache_root = (env.get("STREMIOSRV_CACHE_ROOT") or "/root/.stremio-server").rstrip("/")
            cert_info = container.exec_run(["cat", f"{cache_root}/httpsCert.json"])
            if cert_info.exit_code == 0:
                text = cert_info.output.decode("utf-8", errors="replace")
                match = STREMIO_ROCKS_RE.search(text)
                if match:
                    trusted_url = f"https://{match.group(1)}:12470/"
    except Exception:
        pass

    if trusted_url:
        return {
            "ip": ip,
            "webPlayer": trusted_url,
            "streamingServer": trusted_url,
            "desktopFlag": f"--development --webui-url={trusted_url}",
            "trustedHttps": True,
        }

    streaming_server = explicit_server_url or f"http://{ip}:11470/"
    return {
        "ip": ip,
        "webPlayer": f"http://{ip}:8080/",
        "streamingServer": streaming_server,
        "desktopFlag": f"--server={streaming_server.rstrip('/')}",
        "trustedHttps": False,
    }


def _component(installed: str | None, available: str | None, **extra) -> dict:
    if installed and available:
        update_available: bool | None = installed != available
    else:
        update_available = None
    return {
        "installed": installed,
        "available": available,
        "updateAvailable": update_available,
        **extra,
    }


def component_versions():
    server_installed = _installed_server_version()
    server_available = _remote_server_version()
    webadmin_installed = _installed_webadmin_version()
    webadmin_available = _remote_webadmin_version()

    return {
        "repositoryUrl": SOURCE_REPO,
        "branch": SOURCE_BRANCH,
        "checkedAt": datetime.now(UTC).isoformat(),
        "core": {
            "installed": _core_version(),
            "updateManagedBy": "server",
        },
        "server": _component(
            server_installed,
            server_available,
            updateMode="transactional",
            updateEndpoint="/api/update",
            updateInProgress=legacy.UPDATE_LOCK.locked(),
            rollback=True,
        ),
        "webadmin": _component(
            webadmin_installed,
            webadmin_available,
            updateMode="host-compose",
            updateCommand=WEBADMIN_UPDATE_COMMAND,
            selfUpdate=False,
        ),
    }


def github_version_compat():
    available = _remote_server_version()
    installed = _installed_server_version()
    return {
        "available": bool(available),
        "version": available,
        "installed": installed,
        "updateAvailable": bool(available and installed and available != installed),
        "repositoryUrl": SOURCE_REPO,
        "branch": SOURCE_BRANCH,
        "source": "fork-server",
    }


def lifecycle_status():
    data = transactional.fork_status()
    if isinstance(data, dict):
        urls = _connection_urls()
        data["urls"] = urls
        server = data.get("server")
        if isinstance(server, dict):
            server["trustedHttps"] = bool(urls.get("trustedHttps"))
    return data


_original_update_worker = transactional.update_worker


def guarded_server_update_worker():
    installed = _installed_server_version()
    available = _remote_server_version()
    if installed and available and installed == available:
        transactional._write_result(
            status="succeeded",
            phase="no-op",
            finishedAt=datetime.now(UTC).isoformat(),
            version=available,
            repositoryUrl=SOURCE_REPO,
            branch=SOURCE_BRANCH,
            message="Server is already at the latest SERVER_VERSION; no container change was made.",
        )
        return
    _original_update_worker()


legacy.update_worker = guarded_server_update_worker


def guarded_update():
    """Start an update only when a different SERVER_VERSION is verified."""
    if legacy.UPDATE_LOCK.locked():
        raise HTTPException(409, "server update already running")

    installed = _installed_server_version()
    available = _remote_server_version()

    if not installed:
        raise HTTPException(409, "installed SERVER_VERSION could not be determined; update disabled")
    if not available:
        raise HTTPException(503, "available SERVER_VERSION could not be verified; update disabled")
    if installed == available:
        transactional._write_result(
            status="succeeded",
            phase="no-op",
            finishedAt=datetime.now(UTC).isoformat(),
            version=available,
            repositoryUrl=SOURCE_REPO,
            branch=SOURCE_BRANCH,
            message=f"Server {installed} is already up to date; no update was started.",
        )
        return {
            "ok": True,
            "started": False,
            "updateAvailable": False,
            "installed": installed,
            "available": available,
            "message": f"Server {installed} is already up to date. Update disabled.",
        }

    threading.Thread(target=guarded_server_update_worker, daemon=True).start()
    legacy.audit("software.update", f"server {installed} -> {available}")
    return {
        "ok": True,
        "started": True,
        "updateAvailable": True,
        "installed": installed,
        "available": available,
        "message": f"Server update started: {installed} -> {available}",
    }


def lifecycle_qr():
    target = str(_connection_urls()["streamingServer"])
    out = BytesIO()
    legacy.segno.make(target, error="m").save(out, kind="svg", scale=5, border=2)
    return Response(
        out.getvalue(),
        media_type="image/svg+xml",
        headers={"Cache-Control": "no-store"},
    )


SCRIPT_TAG = '<script src="/component-versions.js"></script>'


def lifecycle_home():
    text = (legacy.STATIC / "index.html").read_text(encoding="utf-8")
    text = text.replace(STALE_UPDATE_NOTICE, SAFE_UPDATE_NOTICE)
    if SCRIPT_TAG not in text:
        text = text.replace("</body>", f"  {SCRIPT_TAG}\n</body>")
    return HTMLResponse(text)


def lifecycle_script():
    return FileResponse(
        legacy.STATIC / "component-versions.js",
        media_type="application/javascript",
        headers={"Cache-Control": "no-store"},
    )


def _replace_route(path: str) -> None:
    app.router.routes = [
        route for route in app.router.routes if getattr(route, "path", None) != path
    ]


_replace_route("/")
app.add_api_route("/", lifecycle_home, methods=["GET"])
_replace_route("/api/status")
app.add_api_route("/api/status", lifecycle_status, methods=["GET"])
_replace_route("/api/github-version")
app.add_api_route("/api/github-version", github_version_compat, methods=["GET"])
_replace_route("/api/update")
app.add_api_route("/api/update", guarded_update, methods=["POST"], status_code=202)
_replace_route("/api/qr.svg")
app.add_api_route("/api/qr.svg", lifecycle_qr, methods=["GET"])
app.add_api_route("/api/component-versions", component_versions, methods=["GET"])
app.add_api_route("/component-versions.js", lifecycle_script, methods=["GET"])
