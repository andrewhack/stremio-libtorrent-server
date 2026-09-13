"""Independent Server/WebAdmin release lifecycle layer.

This module sits on top of the transactional server updater.  The streaming
server and WebAdmin deliberately have different release identifiers and update
paths:

* SERVER_VERSION -> transactional in-place Stremio server update.
* WEBADMIN_VERSION -> host-side Compose rebuild of only the WebAdmin service.
* Core version -> informational version reported by the stremiosrv package.

The upstream repository is never used as a runtime update authority.
"""

from __future__ import annotations

import os
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

from fastapi.responses import FileResponse, HTMLResponse

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


def _request_text(url: str, timeout: int = 5) -> str | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "stremio-webadmin-version"})
        value = urllib.request.urlopen(req, timeout=timeout).read(4096).decode("utf-8").strip()
        return value or None
    except Exception:
        return None


def _remote_server_version() -> str | None:
    # SERVER_VERSION is authoritative. FORK_VERSION is a compatibility fallback
    # for installations created before the lifecycle split.
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
    """Compatibility endpoint used by the existing page JavaScript.

    It now means *server release available in this fork*, never upstream core
    version and never WebAdmin version.
    """
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


_original_update_worker = transactional.update_worker


def guarded_server_update_worker():
    """Avoid rebuilding the server when its independent release is unchanged."""
    try:
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
    except Exception:
        # Network/version detection must never prevent an explicit server update.
        pass
    _original_update_worker()


# The legacy POST /api/update resolves this module global at execution time.
legacy.update_worker = guarded_server_update_worker


SCRIPT_TAG = '<script src="/component-versions.js"></script>'


def lifecycle_home():
    text = (legacy.STATIC / "index.html").read_text(encoding="utf-8")
    # Never render the obsolete upstream-as-update-source message, even if
    # JavaScript is disabled or fails before the lifecycle panel is enhanced.
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


# Replace only compatibility/UI routes. Transactional server update routes remain
# owned by fork_update.py.
_replace_route("/")
app.add_api_route("/", lifecycle_home, methods=["GET"])
_replace_route("/api/github-version")
app.add_api_route("/api/github-version", github_version_compat, methods=["GET"])
app.add_api_route("/api/component-versions", component_versions, methods=["GET"])
app.add_api_route("/component-versions.js", lifecycle_script, methods=["GET"])
