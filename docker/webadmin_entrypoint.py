#!/usr/bin/env python3
"""Apply WebAdmin's persistent JSON settings without patching upstream Python code.

The fork keeps WebAdmin state in /config/admin-settings.json.  Before the stock
server entrypoint starts, this wrapper converts those values to STREMIOSRV_*
environment variables, which is the configuration interface supported by the
core server itself.  This keeps the WebAdmin integration outside upstream
source files and makes upstream synchronisation much less conflict-prone.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

_CONFIG_KEY = re.compile(r"^[a-z][a-z0-9_]*$")


def _env_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, separators=(",", ":"))
    if value is None:
        return ""
    return str(value)


def apply_admin_config() -> None:
    config_path = Path(os.getenv("STREMIOSRV_EXTERNAL_CONFIG", "/config/admin-settings.json"))
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return

    if not isinstance(data, dict):
        return

    for key, value in data.items():
        if not isinstance(key, str) or not _CONFIG_KEY.fullmatch(key):
            continue
        os.environ[f"STREMIOSRV_{key.upper()}"] = _env_value(value)


if __name__ == "__main__":
    apply_admin_config()
    os.execvpe(
        "/srv/app/docker/entrypoint.sh",
        ["/srv/app/docker/entrypoint.sh"],
        os.environ,
    )
