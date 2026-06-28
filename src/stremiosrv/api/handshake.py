from fastapi import APIRouter, Request

from stremiosrv import metrics
from stremiosrv.cache import usage

router = APIRouter()


@router.get("/settings")
def settings(request: Request) -> dict:
    """Streaming-server settings. Shape per docs/protocol-map.md: {options, values, baseUrl}."""
    s = request.app.state.settings
    # Reflect how the client actually reached us (works behind the nginx TLS front, which sets
    # X-Forwarded-Proto/Host) so remote clients build stream URLs against the right origin.
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("host") or f"127.0.0.1:{s.http_port}"
    return {
        "options": [],  # UI option descriptors; not required for playback
        "values": {
            "serverVersion": "stremiosrv-0.1.0",
            "appPath": s.cache_root,
            "cacheRoot": s.cache_root,
            "cacheSize": s.cache_size,
            "btMaxConnections": s.bt_max_connections,
            "btHandshakeTimeout": 5000,
            "btRequestTimeout": 2000,
            "btDownloadSpeedSoftLimit": 12582912,
            "btDownloadSpeedHardLimit": 52428800,
            "btMinPeersForStable": 20,
            "remoteHttps": "",
            "localAddonEnabled": False,
            "transcodeHardwareAccel": s.transcode_profile is not None,
            "transcodeProfile": s.transcode_profile,
            "allTranscodeProfiles": [],
            "transcodeMaxWidth": 3840,
            "proxyStreamsEnabled": False,
            "btProfile": "default",
        },
        "baseUrl": f"{proto}://{host}",
    }


@router.get("/network-info")
def network_info() -> dict:
    return {"availableInterfaces": ["127.0.0.1"]}


@router.get("/device-info")
def device_info(request: Request) -> dict:
    p = request.app.state.settings.transcode_profile
    return {"availableHardwareAccelerations": [p] if p else []}


@router.get("/stats.json")
def global_stats(request: Request) -> dict:
    """Global server metrics for the appliance suggestion advisor: cache footprint vs budget +
    free disk, and playback stalls. (Per-torrent stats are at /{infoHash}/stats.json.)"""
    s = request.app.state.settings
    return {
        "cache": usage(s.cache_root, s.cache_size),
        "playback": metrics.playback_stats(),
    }


@router.get("/hwaccel-profiler")
def hwaccel_profiler(request: Request) -> dict:
    """Report the active hardware-transcode profile (set by autodetect at startup)."""
    p = request.app.state.settings.transcode_profile
    return {"profile": p, "available": [p] if p else []}
