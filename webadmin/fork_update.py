"""Fork-owned transactional updater for Stremio Web Admin.

The upstream project is an input to this fork, never a runtime update source.
Updates are downloaded and built only from the configured fork/branch.

The activation step is transactional:
1. tag the currently running image as the rollback checkpoint;
2. build the new fork image while the current container keeps serving;
3. stop and rename the old Stremio container;
4. create a replacement with the same Docker configuration and networks;
5. wait for Docker health to report healthy;
6. remove the old container only after success;
7. automatically restore the old container and image on any activation failure.

WebAdmin and Pi-hole are not recreated by this updater.
"""

from __future__ import annotations

import copy
import json
import os
import tarfile
import tempfile
import time
import urllib.request
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

import docker
from docker.errors import NotFound

import app as legacy

SOURCE_REPO = os.getenv(
    "STREMIO_SOURCE_REPO", "https://github.com/emmanique/stremio-libtorrent-server-webadmin"
).rstrip("/")
SOURCE_BRANCH = os.getenv("STREMIO_SOURCE_BRANCH", "main")
SOURCE_ARCHIVE = f"{SOURCE_REPO}/archive/refs/heads/{SOURCE_BRANCH}.tar.gz"
FORK_VERSION_URL = f"{SOURCE_REPO}/raw/{SOURCE_BRANCH}/FORK_VERSION"
HEALTH_TIMEOUT = int(os.getenv("STREMIO_UPDATE_HEALTH_TIMEOUT", "180"))
ROLLBACK_CONTAINER = f"{legacy.CONTAINER}-rollback"
CHECKPOINT_FILE = legacy.STATE / "update-checkpoint.json"
_INSTALLED_VERSION_CACHE: dict[str, str] = {}


def _request(url: str, timeout: int = 10) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "stremio-webadmin-fork"})
    return urllib.request.urlopen(req, timeout=timeout).read()


def _remote_version() -> str:
    return _request(FORK_VERSION_URL, timeout=5).decode("utf-8").strip()


def github_version():
    try:
        version = _remote_version()
        return {
            "available": bool(version),
            "version": version or None,
            "repositoryUrl": SOURCE_REPO,
            "branch": SOURCE_BRANCH,
            "source": "fork",
        }
    except Exception:
        return {
            "available": False,
            "version": None,
            "repositoryUrl": SOURCE_REPO,
            "branch": SOURCE_BRANCH,
            "source": "fork",
        }


def _installed_fork_version() -> str | None:
    try:
        container = legacy.client().containers.get(legacy.CONTAINER)
        container.reload()
        image_id = container.image.id
        if image_id in _INSTALLED_VERSION_CACHE:
            return _INSTALLED_VERSION_CACHE[image_id]
        result = container.exec_run(["cat", "/srv/app/FORK_VERSION"])
        if result.exit_code != 0:
            return None
        version = result.output.decode("utf-8", errors="replace").strip()
        if version:
            _INSTALLED_VERSION_CACHE.clear()
            _INSTALLED_VERSION_CACHE[image_id] = version
            return version
    except Exception:
        return None
    return None


_legacy_status = legacy.status


def fork_status():
    data = _legacy_status()
    server = data.get("server") if isinstance(data, dict) else None
    if isinstance(server, dict):
        core_version = server.get("version")
        fork_version = _installed_fork_version()
        server["coreVersion"] = core_version
        server["forkVersion"] = fork_version
        if fork_version:
            server["version"] = fork_version
    return data


def _write_result(**values) -> None:
    legacy.STATE.mkdir(parents=True, exist_ok=True)
    current = legacy.read_update_result() or {}
    current.update(values)
    tmp = legacy.STATE / "update-result.tmp"
    tmp.write_text(json.dumps(current, indent=2), encoding="utf-8")
    os.replace(tmp, legacy.STATE / "update-result.json")


def _write_checkpoint(data: dict) -> None:
    legacy.STATE.mkdir(parents=True, exist_ok=True)
    tmp = CHECKPOINT_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, CHECKPOINT_FILE)


def _image_repository(image: str) -> str:
    value = image.split("@", 1)[0]
    slash = value.rfind("/")
    colon = value.rfind(":")
    return value[:colon] if colon > slash else value


def _rollback_image() -> str:
    return f"{_image_repository(legacy.IMAGE)}:rollback"


def _network_snapshot(container) -> dict[str, dict]:
    container.reload()
    result: dict[str, dict] = {}
    for name, endpoint in (container.attrs.get("NetworkSettings", {}).get("Networks") or {}).items():
        aliases = [
            alias
            for alias in (endpoint.get("Aliases") or [])
            if alias and alias != container.id[:12]
        ]
        result[name] = {
            "network_id": endpoint.get("NetworkID") or name,
            "ipv4_address": endpoint.get("IPAddress") or None,
            "ipv6_address": endpoint.get("GlobalIPv6Address") or None,
            "aliases": aliases,
        }
    return result


def _disconnect_networks(container, networks: dict[str, dict]) -> None:
    client = legacy.client()
    for name, endpoint in networks.items():
        network = client.networks.get(endpoint.get("network_id") or name)
        try:
            network.disconnect(container, force=True)
        except docker.errors.APIError as exc:
            if "not connected" not in str(exc).lower():
                raise


def _connect_networks(container, networks: dict[str, dict]) -> None:
    client = legacy.client()
    for name, endpoint in networks.items():
        network = client.networks.get(endpoint.get("network_id") or name)
        kwargs = {}
        if endpoint.get("aliases"):
            kwargs["aliases"] = endpoint["aliases"]
        if endpoint.get("ipv4_address"):
            kwargs["ipv4_address"] = endpoint["ipv4_address"]
        if endpoint.get("ipv6_address"):
            kwargs["ipv6_address"] = endpoint["ipv6_address"]
        network.connect(container, **kwargs)


def _networking_config(networks: dict[str, dict]):
    if not networks:
        return None
    api = legacy.client().api
    endpoints = {}
    for name, endpoint in networks.items():
        kwargs = {}
        if endpoint.get("aliases"):
            kwargs["aliases"] = endpoint["aliases"]
        if endpoint.get("ipv4_address"):
            kwargs["ipv4_address"] = endpoint["ipv4_address"]
        if endpoint.get("ipv6_address"):
            kwargs["ipv6_address"] = endpoint["ipv6_address"]
        endpoints[name] = api.create_endpoint_config(**kwargs)
    return api.create_networking_config(endpoints)


def _container_create_payload(container, networks: dict[str, dict]) -> dict:
    """Recreate the compose-managed Stremio container without touching its volumes."""
    container.reload()
    config = copy.deepcopy(container.attrs.get("Config") or {})
    host_config = copy.deepcopy(container.attrs.get("HostConfig") or {})

    old_hostname = config.get("Hostname") or ""
    if old_hostname == container.id[:12]:
        old_hostname = None

    exposed = list((config.get("ExposedPorts") or {}).keys())
    volumes = list((config.get("Volumes") or {}).keys())

    return {
        "image": legacy.IMAGE,
        "command": config.get("Cmd"),
        "hostname": old_hostname,
        "domainname": config.get("Domainname") or None,
        "user": config.get("User") or None,
        "detach": True,
        "stdin_open": bool(config.get("OpenStdin")),
        "tty": bool(config.get("Tty")),
        "ports": exposed or None,
        "environment": config.get("Env") or None,
        "volumes": volumes or None,
        "name": legacy.CONTAINER,
        "entrypoint": config.get("Entrypoint"),
        "working_dir": config.get("WorkingDir") or None,
        "host_config": host_config,
        "labels": config.get("Labels") or None,
        "stop_signal": config.get("StopSignal") or None,
        "networking_config": _networking_config(networks),
        "healthcheck": config.get("Healthcheck") or None,
        "stop_timeout": config.get("StopTimeout"),
    }


def _wait_healthy(container, timeout: int = HEALTH_TIMEOUT) -> dict:
    deadline = time.monotonic() + timeout
    last = {}
    while time.monotonic() < deadline:
        container.reload()
        state = container.attrs.get("State") or {}
        last = state
        status = state.get("Status")
        health = (state.get("Health") or {}).get("Status")

        if status in {"dead", "exited"}:
            raise RuntimeError(
                f"replacement container stopped before becoming healthy (status={status})"
            )
        if health == "healthy":
            return state
        if health == "unhealthy":
            raise RuntimeError("replacement container reported unhealthy")
        if health is None and status == "running":
            if legacy.get_json("/health", {}):
                return state
        time.sleep(2)

    health = (last.get("Health") or {}).get("Status")
    raise RuntimeError(
        f"replacement container did not become healthy within {timeout}s"
        + (f" (last health={health})" if health else "")
    )


def _restore_old_container(old, networks: dict[str, dict]) -> None:
    client = legacy.client()
    try:
        failed = client.containers.get(legacy.CONTAINER)
    except NotFound:
        failed = None

    if failed is not None and failed.id != old.id:
        failed.remove(force=True)

    old.reload()
    if old.name != legacy.CONTAINER:
        old.rename(legacy.CONTAINER)

    current_networks = old.attrs.get("NetworkSettings", {}).get("Networks") or {}
    if not current_networks and networks:
        _connect_networks(old, networks)

    old.start()
    _wait_healthy(old, timeout=HEALTH_TIMEOUT)


def _activate_image(version: str) -> dict:
    client = legacy.client()
    try:
        old = client.containers.get(legacy.CONTAINER)
    except NotFound as exc:
        raise RuntimeError(
            f"cannot activate update: container {legacy.CONTAINER!r} does not exist"
        ) from exc

    old.reload()

    try:
        stale = client.containers.get(ROLLBACK_CONTAINER)
    except NotFound:
        stale = None
    if stale is not None and stale.id != old.id:
        stale.reload()
        if stale.attrs.get("State", {}).get("Running"):
            raise RuntimeError(
                f"refusing update: unexpected running rollback container {ROLLBACK_CONTAINER!r}"
            )
        stale.remove(force=True)

    networks = _network_snapshot(old)
    old_image_id = old.image.id
    old_tags = list(old.image.tags)
    rollback_image = _rollback_image()

    old.image.tag(_image_repository(legacy.IMAGE), tag="rollback", force=True)

    checkpoint = {
        "createdAt": datetime.now(UTC).isoformat(),
        "stage": "checkpointed",
        "container": legacy.CONTAINER,
        "rollbackContainer": ROLLBACK_CONTAINER,
        "previousImageId": old_image_id,
        "previousImageTags": old_tags,
        "rollbackImage": rollback_image,
        "targetImage": legacy.IMAGE,
        "targetVersion": version,
        "networks": networks,
    }
    _write_checkpoint(checkpoint)

    old.stop(timeout=30)
    old.rename(ROLLBACK_CONTAINER)
    _disconnect_networks(old, networks)

    checkpoint["stage"] = "swapping"
    _write_checkpoint(checkpoint)

    replacement = None
    try:
        payload = _container_create_payload(old, networks)
        created = client.api.create_container(**payload)
        replacement = client.containers.get(created["Id"])
        replacement.start()

        checkpoint["stage"] = "validating"
        checkpoint["replacementContainerId"] = replacement.id
        _write_checkpoint(checkpoint)

        _wait_healthy(replacement, timeout=HEALTH_TIMEOUT)

        replacement.reload()
        new_image_id = replacement.image.id

        old.remove(force=True)

        checkpoint["stage"] = "committed"
        checkpoint["committedAt"] = datetime.now(UTC).isoformat()
        checkpoint["newImageId"] = new_image_id
        _write_checkpoint(checkpoint)

        return {
            "previousImageId": old_image_id,
            "newImageId": new_image_id,
            "rollbackImage": rollback_image,
        }
    except Exception as activation_error:
        rollback_error = None
        try:
            _restore_old_container(old, networks)
            checkpoint["stage"] = "rolled-back"
            checkpoint["rolledBackAt"] = datetime.now(UTC).isoformat()
            checkpoint["activationError"] = str(activation_error)
            _write_checkpoint(checkpoint)
        except Exception as exc:
            rollback_error = exc
            checkpoint["stage"] = "rollback-failed"
            checkpoint["activationError"] = str(activation_error)
            checkpoint["rollbackError"] = str(exc)
            _write_checkpoint(checkpoint)

        if rollback_error is not None:
            raise RuntimeError(
                f"activation failed ({activation_error}); automatic rollback also failed "
                f"({rollback_error})"
            ) from activation_error
        raise RuntimeError(
            f"activation failed and was rolled back automatically: {activation_error}"
        ) from activation_error


def update_worker():
    started = time.monotonic()
    phase = "starting"
    result = {
        "status": "failed",
        "phase": phase,
        "finishedAt": datetime.now(UTC).isoformat(),
        "repositoryUrl": SOURCE_REPO,
        "branch": SOURCE_BRANCH,
    }
    try:
        with legacy.UPDATE_LOCK:
            legacy.STATE.mkdir(parents=True, exist_ok=True)
            phase = "downloading"
            _write_result(
                status="running",
                phase=phase,
                startedAt=datetime.now(UTC).isoformat(),
                repositoryUrl=SOURCE_REPO,
                branch=SOURCE_BRANCH,
                message="Downloading fork source.",
            )

            payload = _request(SOURCE_ARCHIVE, timeout=60)
            with tempfile.TemporaryDirectory(dir=legacy.STATE) as td:
                root = Path(td).resolve()
                with tarfile.open(fileobj=BytesIO(payload), mode="r:gz") as tar:
                    for member in tar.getmembers():
                        target = (root / member.name).resolve()
                        if root not in target.parents and target != root:
                            raise RuntimeError("unsafe fork source archive")
                    tar.extractall(td, filter="data")

                dirs = [p for p in root.iterdir() if p.is_dir()]
                if len(dirs) != 1:
                    raise RuntimeError("invalid fork source archive")
                source = dirs[0]

                required = [
                    source / "Dockerfile",
                    source / "compose.yaml",
                    source / "FORK_VERSION",
                    source / "docker/webadmin_entrypoint.py",
                    source / "webadmin",
                ]
                missing = [str(p.relative_to(source)) for p in required if not p.exists()]
                if missing:
                    raise RuntimeError(
                        "fork archive is missing protected WebAdmin files: " + ", ".join(missing)
                    )

                version = (source / "FORK_VERSION").read_text(encoding="utf-8").strip()
                if not version:
                    raise RuntimeError("FORK_VERSION is empty")

                current = legacy.client().containers.get(legacy.CONTAINER)
                current.reload()
                current_image_id = current.image.id
                rollback_image = _rollback_image()
                current.image.tag(_image_repository(legacy.IMAGE), tag="rollback", force=True)

                phase = "building"
                _write_result(
                    status="running",
                    phase=phase,
                    version=version,
                    previousImageId=current_image_id,
                    rollbackImage=rollback_image,
                    message="Building new fork image while the current server remains online.",
                )

                legacy.client().images.build(
                    path=str(source), tag=legacy.IMAGE, rm=True, pull=True
                )

            phase = "activating"
            _write_result(
                status="running",
                phase=phase,
                version=version,
                message="Replacing only the Stremio container and validating health.",
            )
            activation = _activate_image(version)

            result = {
                "status": "succeeded",
                "phase": "committed",
                "finishedAt": datetime.now(UTC).isoformat(),
                "durationSeconds": round(time.monotonic() - started, 1),
                "version": version,
                "repositoryUrl": SOURCE_REPO,
                "branch": SOURCE_BRANCH,
                **activation,
                "message": (
                    "Fork update activated successfully. Stremio passed health validation; "
                    "WebAdmin and Pi-hole were not recreated. Previous image kept as rollback."
                ),
            }
    except Exception as exc:
        result.update(
            {
                "status": "failed",
                "phase": phase,
                "finishedAt": datetime.now(UTC).isoformat(),
                "durationSeconds": round(time.monotonic() - started, 1),
                "message": str(exc),
            }
        )
    _write_result(**result)


legacy.update_worker = update_worker

legacy.app.router.routes = [
    route for route in legacy.app.router.routes if getattr(route, "path", None) != "/api/github-version"
]
legacy.app.add_api_route("/api/github-version", github_version, methods=["GET"])

legacy.app.router.routes = [
    route for route in legacy.app.router.routes if getattr(route, "path", None) != "/api/status"
]
legacy.app.add_api_route("/api/status", fork_status, methods=["GET"])

app = legacy.app
