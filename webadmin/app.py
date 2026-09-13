# ruff: noqa: BLE001, B904
from __future__ import annotations

import json
import os
import re
import shutil
import tarfile
import tempfile
import threading
import time
import urllib.request
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

import httpx
import psutil
import segno
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

import docker

app = FastAPI(title="Stremio Server Web Admin")
STARTED = time.monotonic()
STATIC = Path(__file__).with_name("static")
STATE = Path(os.getenv("WEBADMIN_STATE", "/data"))
CONFIG = Path(os.getenv("STREMIO_CONFIG_FILE", "/config/admin-settings.json"))
STREMIO_URL = os.getenv("STREMIO_URL", "http://stremio-libtorrent-server:11470").rstrip("/")
CONTAINER = os.getenv("STREMIO_CONTAINER", "stremio-libtorrent-server")
SOURCE_REPO = os.getenv(
    "STREMIO_SOURCE_REPO", "https://github.com/emmanique/stremio-libtorrent-server-webadmin"
)
SOURCE_ARCHIVE = SOURCE_REPO.rstrip("/") + "/archive/refs/heads/main.tar.gz"
IMAGE = os.getenv("STREMIO_IMAGE", "stremio-libtorrent-server-webadmin:local")
UPDATE_LOCK = threading.Lock()

DESCRIPTIONS = {
    "http_port": "Porta HTTP interna da API do servidor Stremio.",
    "bt_listen_port": "Porta TCP/UDP usada para receber ligações BitTorrent.",
    "enable_upnp": "Solicita ao router a abertura automática da porta BitTorrent.",
    "cache_root": "Directório persistente da cache, certificados e estado do Stremio.",
    "cert_file": "Ficheiro PEM com o certificado TLS e a chave privada.",
    "cache_size": "Limite máximo reservado para a cache de conteúdos.",
    "cache_evict_interval": "Intervalo entre verificações para remover cache antiga.",
    "cache_evict_grace": "Tempo mínimo sem utilização antes de um conteúdo poder ser removido.",
    "bt_max_connections": "Número máximo de ligações BitTorrent simultâneas.",
    "download_rate_limit": "Limite global de download em bytes/s; zero significa ilimitado.",
    "upload_rate_limit": "Limite global de upload em bytes/s; zero significa ilimitado.",
    "idle_download_rate_limit": "Limite de download para torrents em segundo plano.",
    "max_streams": "Número máximo de reproduções simultâneas; zero significa ilimitado.",
    "seed_on_complete": "Mantém o torrent em partilha depois de concluído.",
    "max_seed_minutes": "Tempo máximo de partilha após conclusão; zero significa ilimitado.",
    "seed_policy_interval": "Intervalo de avaliação da política de partilha.",
    "readahead_bytes": "Quantidade de dados prioritários à frente da reprodução.",
    "stream_piece_timeout": "Espera máxima por uma peça durante uma reprodução.",
    "stream_first_piece_timeout": "Espera máxima pela primeira peça no início ou após seek.",
    "resume_save_interval": "Intervalo de gravação do estado fast-resume.",
    "resume_retention_days": "Retenção máxima dos registos fast-resume em dias.",
    "transcode_profile": "Perfil de transcodificação; vazio activa detecção automática.",
    "transcode_gc_interval": "Intervalo de limpeza de tarefas de transcodificação abandonadas.",
    "transcode_idle_timeout": "Tempo sem leitura antes de terminar um transcodificador.",
    "transcode_gc_max_age": "Idade máxima de directórios de transcodificação abandonados.",
    "extra_trackers": "Trackers adicionais aplicados a todos os torrents.",
    "tracker_list_url": "URL opcional de uma lista de trackers mantida externamente.",
    "tracker_list_refresh_hours": "Intervalo de actualização da lista externa em horas.",
    "dht_bootstrap_nodes": "Nós DHT adicionais em formato host:porta.",
    "adaptive_picking": "Adapta a selecção de peças ao estado do buffer.",
    "adaptive_low_bytes": "Limite inferior do buffer para regressar ao modo sequencial.",
    "adaptive_high_bytes": "Limite superior do buffer para permitir paralelismo.",
    "adaptive_interval": "Intervalo de avaliação do controlador adaptativo.",
    "prefetch_next": "Pré-descarrega o início do episódio seguinte no mesmo torrent.",
    "prefetch_next_fraction": "Fracção do próximo ficheiro que deve ser pré-descarregada.",
    "prefetch_next_max_bytes": "Limite máximo do pré-download do próximo episódio.",
    "prefetch_trigger_fraction": "Posição da reprodução que activa o prefetch.",
    "library_ui": "Activa a interface autenticada de biblioteca do fork.",
    "library_owner": "Conta Stremio autorizada a gerir a biblioteca.",
    "library_allow_http": "Permite autenticação da biblioteca por HTTP numa LAN confiável.",
    "library_addon_allow": "Redes CIDR autorizadas a aceder ao addon da biblioteca.",
}
DEFAULTS = {
    "http_port": 11470,
    "bt_listen_port": 6881,
    "enable_upnp": True,
    "cache_root": "/root/.stremio-server",
    "cert_file": "certificates.pem",
    "cache_size": 19327352832,
    "cache_evict_interval": 60,
    "cache_evict_grace": 1800,
    "bt_max_connections": 400,
    "download_rate_limit": 0,
    "upload_rate_limit": 0,
    "idle_download_rate_limit": 1048576,
    "max_streams": 0,
    "seed_on_complete": True,
    "max_seed_minutes": 0,
    "seed_policy_interval": 15,
    "readahead_bytes": 268435456,
    "stream_piece_timeout": 30.0,
    "stream_first_piece_timeout": 120.0,
    "resume_save_interval": 30,
    "resume_retention_days": 365,
    "transcode_profile": "",
    "transcode_gc_interval": 60,
    "transcode_idle_timeout": 300,
    "transcode_gc_max_age": 600,
    "extra_trackers": "",
    "tracker_list_url": "",
    "tracker_list_refresh_hours": 24.0,
    "dht_bootstrap_nodes": "",
    "adaptive_picking": False,
    "adaptive_low_bytes": 67108864,
    "adaptive_high_bytes": 268435456,
    "adaptive_interval": 2.0,
    "prefetch_next": False,
    "prefetch_next_fraction": 0.05,
    "prefetch_next_max_bytes": 134217728,
    "prefetch_trigger_fraction": 0.90,
    "library_ui": False,
    "library_owner": "",
    "library_allow_http": False,
    "library_addon_allow": "",
}
MINUTES = {
    "cache_evict_interval",
    "cache_evict_grace",
    "seed_policy_interval",
    "stream_piece_timeout",
    "stream_first_piece_timeout",
    "resume_save_interval",
    "transcode_gc_interval",
    "transcode_idle_timeout",
    "transcode_gc_max_age",
    "adaptive_interval",
}
READ_ONLY = {"http_port", "cache_root", "cert_file"}


class Values(BaseModel):
    values: dict[str, object]


class SettingsBody(BaseModel):
    seed_on_complete: bool = True
    max_seed_minutes: int = 0
    max_streams: int = 0
    download_rate_limit: int = 0
    upload_rate_limit: int = 0
    idle_download_rate_limit: int = 0


class CacheBody(BaseModel):
    name: str


class LogBody(BaseModel):
    source: str | None = None


def client():
    return docker.from_env(timeout=120)


def read_config():
    try:
        data = json.loads(CONFIG.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_config(values):
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    current = read_config()
    current.update(values)
    tmp = CONFIG.with_suffix(".tmp")
    tmp.write_text(json.dumps(current, indent=2), encoding="utf-8")
    os.replace(tmp, CONFIG)


def audit(action, detail=""):
    STATE.mkdir(parents=True, exist_ok=True)
    with (STATE / "admin.log").open("a", encoding="utf-8") as f:
        f.write(f"{datetime.now(UTC).isoformat()} action={action} {detail}\n")


def get_json(path, fallback):
    try:
        with httpx.Client(timeout=4) as c:
            r = c.get(STREMIO_URL + path)
            r.raise_for_status()
            return r.json()
    except Exception:
        return fallback


def docker_stats():
    try:
        c = client().containers.get(CONTAINER)
        raw = c.stats(stream=False)
        cpu_delta = (
            raw["cpu_stats"]["cpu_usage"]["total_usage"]
            - raw["precpu_stats"]["cpu_usage"]["total_usage"]
        )
        sys_delta = raw["cpu_stats"]["system_cpu_usage"] - raw["precpu_stats"]["system_cpu_usage"]
        cpus = len(raw["cpu_stats"]["cpu_usage"].get("percpu_usage", [])) or 1
        cpu = max(0, cpu_delta / sys_delta * cpus * 100) if sys_delta else 0
        mem = raw["memory_stats"].get("usage", 0) - raw["memory_stats"].get("stats", {}).get(
            "cache", 0
        )
        total = raw["memory_stats"].get("limit", 0)
        nets = raw.get("networks", {}).values()
        return (
            c,
            cpu,
            mem,
            total,
            sum(n.get("rx_bytes", 0) for n in nets),
            sum(n.get("tx_bytes", 0) for n in nets),
        )
    except Exception:
        return None, 0, 0, 0, 0, 0


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/")
def home():
    return FileResponse(STATIC / "index.html")


@app.get("/theme-background.jpg")
def background():
    return FileResponse(STATIC / "theme-background.jpg", media_type="image/jpeg")


@app.get("/favicon.ico")
def favicon():
    return FileResponse(STATIC / "favicon.ico", media_type="image/x-icon")


@app.get("/api/status")
def status():
    health_data = get_json("/health", {})
    active = get_json("/active.json", [])
    cached = get_json("/cache.json", [])
    pins = {str(x.get("infoHash", "")).lower() for x in get_json("/pins.json", [])}
    config = DEFAULTS | read_config()
    streams = active if isinstance(active, list) else active.get("items", [])
    by_name = {x.get("name"): x for x in streams}
    for item in cached if isinstance(cached, list) else []:
        name = item.get("name")
        if name not in by_name:
            row = {
                "name": name,
                "infoHash": item.get("infoHash"),
                "active": False,
                "progress": 1,
                "downloadSpeed": 0,
                "uploadSpeed": 0,
                "peers": 0,
                "downloaded": item.get("size", 0),
                "uploaded": 0,
            }
            streams.append(row)
            by_name[name] = row
        by_name[name].update(cached=True, cacheBytes=item.get("size", 0), mtime=item.get("mtime"))
    for row in streams:
        row.setdefault("cached", False)
        row.setdefault("cacheBytes", 0)
        row["pinned"] = row.get("pinned", str(row.get("infoHash", "")).lower() in pins)
    stats = get_json("/stats.json", {})
    cache = stats.get("cache", {}) if isinstance(stats, dict) else {}
    cont, cpu, mem, total, rx, tx = docker_stats()
    perf = {
        "cpuPercent": round(cpu, 1),
        "load1": psutil.getloadavg()[0],
        "memoryUsed": mem,
        "memoryTotal": total,
        "memoryPercent": round(mem / total * 100, 1) if total else 0,
        "networkReceiveBps": rx,
        "networkTransmitBps": tx,
        "torrentDownloadBps": sum(int(x.get("downloadSpeed", 0) or 0) for x in streams),
        "torrentUploadBps": sum(int(x.get("uploadSpeed", 0) or 0) for x in streams),
        "peers": sum(int(x.get("peers", 0) or 0) for x in streams),
        "activeStreams": sum(bool(x.get("active")) for x in streams),
    }
    return {
        "server": {
            "running": cont is not None,
            "healthy": bool(health_data),
            "version": health_data.get("version", "unknown"),
            "uptimeSeconds": int(time.monotonic() - STARTED),
            "cpuPercent": round(cpu, 1),
            "memoryUsed": mem,
            "memoryTotal": total,
            "certDaysLeft": health_data.get("certDaysLeft"),
        },
        "urls": {
            "ip": os.getenv("IPADDRESS", "localhost"),
            "webPlayer": os.getenv("WEB_PLAYER_URL", "http://localhost:8080"),
            "streamingServer": os.getenv("SERVER_URL", "http://localhost:11470"),
            "desktopFlag": "--server=" + os.getenv("SERVER_URL", "http://localhost:11470"),
        },
        "settings": {
            k: config[k]
            for k in (
                "seed_on_complete",
                "max_seed_minutes",
                "max_streams",
                "download_rate_limit",
                "upload_rate_limit",
                "idle_download_rate_limit",
            )
        },
        "streams": streams,
        "cache": {
            "cacheUsed": cache.get("cacheUsed", 0),
            "cacheSize": cache.get("cacheSize", config["cache_size"]),
            "diskFree": cache.get("diskFree", 0),
            "transcodeUsed": cache.get("transcodeUsed", 0),
        },
        "performance": perf,
        "updates": {
            "enabled": True,
            "running": UPDATE_LOCK.locked(),
            "lastResult": read_update_result(),
        },
    }


def read_update_result():
    try:
        return json.loads((STATE / "update-result.json").read_text())
    except Exception:
        return None


@app.get("/api/github-version")
def github_version():
    try:
        req = urllib.request.Request(
            SOURCE_REPO + "/raw/main/pyproject.toml", headers={"User-Agent": "stremio-webadmin"}
        )
        text = urllib.request.urlopen(req, timeout=4).read(131072).decode()
        m = re.search(r'^version\s*=\s*["\']([^"\']+)', text, re.M)
        return {
            "available": bool(m),
            "version": m.group(1) if m else None,
            "repositoryUrl": SOURCE_REPO,
        }
    except Exception:
        return {"available": False, "version": None, "repositoryUrl": SOURCE_REPO}


@app.get("/api/config")
def config():
    values = DEFAULTS | read_config()
    items = []
    for name, value in values.items():
        kind = (
            "boolean"
            if isinstance(value, bool)
            else "number"
            if isinstance(value, (int, float))
            else "text"
        )
        items.append(
            {
                "name": name,
                "value": value,
                "displayValue": value / 60 if name in MINUTES else value,
                "type": kind,
                "description": DESCRIPTIONS.get(name, ""),
                "unit": "minutes" if name in MINUTES else None,
                "inputScale": 60 if name in MINUTES else 1,
                "editable": name not in READ_ONLY,
                "restartRequired": True,
            }
        )
    return {"items": items}


@app.put("/api/config")
def save_config(body: Values):
    unknown = set(body.values) - set(DEFAULTS)
    locked = set(body.values) & READ_ONLY
    if unknown:
        raise HTTPException(400, f"unknown settings: {', '.join(sorted(unknown))}")
    if locked:
        raise HTTPException(400, f"read-only settings: {', '.join(sorted(locked))}")
    write_config(body.values)
    audit("config.update", ",".join(body.values))
    return {"ok": True, "restartRequired": True}


@app.put("/api/settings")
def save_settings(body: SettingsBody):
    write_config(body.model_dump())
    audit("settings.update")
    return {"ok": True, "settings": body.model_dump()}


def relay(method, path):
    try:
        with httpx.Client(timeout=10) as c:
            r = c.request(method, STREMIO_URL + path)
        if r.status_code >= 400:
            raise HTTPException(r.status_code, r.text)
        return r.json() if r.content else {"ok": True}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(503, f"Stremio unavailable: {exc}")


@app.post("/api/streams/{info_hash}/pin")
def pin(info_hash: str):
    audit("stream.pin", info_hash)
    return relay("POST", f"/{info_hash}/pin")


@app.delete("/api/streams/{info_hash}/pin")
def unpin(info_hash: str):
    audit("stream.unpin", info_hash)
    return relay("POST", f"/{info_hash}/unpin")


@app.delete("/api/streams/{info_hash}")
def remove(info_hash: str):
    audit("stream.remove", info_hash)
    return relay("GET", f"/{info_hash}/remove")


@app.post("/api/cache/remove")
def cache_remove(body: CacheBody):
    try:
        with httpx.Client(timeout=10) as c:
            r = c.post(STREMIO_URL + "/cache/remove", json={"name": body.name})
        if r.status_code >= 400:
            raise HTTPException(r.status_code, r.text)
        audit("cache.remove", body.name)
        return r.json()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(503, str(exc))


@app.post("/api/restart", status_code=202)
def restart():
    try:
        client().containers.get(CONTAINER).restart(timeout=20)
        audit("server.restart")
        return {"ok": True, "message": "restart requested"}
    except Exception as exc:
        raise HTTPException(500, str(exc))


def update_worker():
    result = {"status": "failed", "finishedAt": datetime.now(UTC).isoformat()}
    try:
        with UPDATE_LOCK:
            STATE.mkdir(parents=True, exist_ok=True)
            req = urllib.request.Request(SOURCE_ARCHIVE, headers={"User-Agent": "stremio-webadmin"})
            payload = urllib.request.urlopen(req, timeout=60).read()
            with tempfile.TemporaryDirectory(dir=STATE) as td:
                with tarfile.open(fileobj=BytesIO(payload), mode="r:gz") as tar:
                    root = Path(td).resolve()
                    for member in tar.getmembers():
                        target = (root / member.name).resolve()
                        if root not in target.parents and target != root:
                            raise RuntimeError("unsafe source archive")
                    tar.extractall(td, filter="data")
                dirs = [p for p in Path(td).iterdir() if p.is_dir()]
                if len(dirs) != 1:
                    raise RuntimeError("invalid source archive")
                source = dirs[0]
                app_file = source / "src/stremiosrv/app.py"
                text = app_file.read_text()
                needle = "    settings = Settings()\n"
                inject = "    from stremiosrv.external_config import apply_external_overrides\n\n    settings = Settings()\n    apply_external_overrides(settings)\n"
                if "apply_external_overrides(settings)" not in text:
                    if needle not in text:
                        raise RuntimeError("upstream conflict in app.py; update aborted")
                    app_file.write_text(text.replace(needle, inject, 1))
                shutil.copy2(
                    "/overlay/external_config.py", source / "src/stremiosrv/external_config.py"
                )
                client().images.build(path=str(source), tag=IMAGE, rm=True, pull=True)
            result = {
                "status": "succeeded",
                "finishedAt": datetime.now(UTC).isoformat(),
                "message": "Image built. Run docker compose up -d --force-recreate stremio-libtorrent-server to activate.",
            }
    except Exception as exc:
        result["message"] = str(exc)
    (STATE / "update-result.json").write_text(json.dumps(result, indent=2))


@app.post("/api/update", status_code=202)
def update():
    if UPDATE_LOCK.locked():
        raise HTTPException(409, "update already running")
    threading.Thread(target=update_worker, daemon=True).start()
    audit("software.update")
    return {"ok": True, "message": "Source update started; Web Admin and settings remain unchanged"}


@app.get("/api/logs")
def logs(source: str = "application", lines: int = 300):
    available = [
        {"id": "application", "label": "Stremio container"},
        {"id": "container", "label": "Docker container"},
        {"id": "updater", "label": "Software updates"},
        {"id": "admin", "label": "Web Admin actions"},
    ]
    try:
        if source in {"application", "container"}:
            content = (
                client()
                .containers.get(CONTAINER)
                .logs(tail=min(lines, 1000))
                .decode(errors="replace")
                .splitlines()
            )
        elif source == "admin":
            content = (STATE / "admin.log").read_text(errors="replace").splitlines()[-lines:]
        elif source == "updater":
            content = json.dumps(read_update_result() or {}, indent=2).splitlines()
        else:
            raise HTTPException(400, "unknown log source")
    except HTTPException:
        raise
    except Exception:
        content = []
    labels = {x["id"]: x["label"] for x in available}
    return {
        "source": source,
        "label": labels[source],
        "lines": content,
        "lineCount": len(content),
        "debug": False,
        "updatedAt": datetime.now(UTC).isoformat(),
        "availableSources": available,
    }


@app.post("/api/logs/clear")
def clear_logs(body: LogBody):
    # Docker's logging driver cannot be safely truncated from inside a container.
    targets = [body.source] if body.source else ["admin", "updater"]
    for target in targets:
        path = STATE / ("admin.log" if target == "admin" else "update-result.json")
        if target in {"admin", "updater"}:
            path.write_text("")
    return {"ok": True, "cleared": targets}


@app.get("/api/qr.svg")
def qr():
    out = BytesIO()
    segno.make(os.getenv("SERVER_URL", "http://localhost:11470"), error="m").save(
        out, kind="svg", scale=5, border=2
    )
    return Response(out.getvalue(), media_type="image/svg+xml")
