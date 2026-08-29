"""Stremio hlsv2 transcode API: probe, master/media playlists, fMP4 segments.

The master playlist references child URIs we serve, so internal naming need not match the stock
server byte-for-byte — the player follows whatever URIs we publish.
"""
from __future__ import annotations

import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse

from stremiosrv.transcode.fingerprint import decide
from stremiosrv.transcode.probe import probe_media

router = APIRouter(prefix="/hlsv2")

_M3U8 = "application/vnd.apple.mpegurl"


def _converter(request: Request):
    return getattr(request.app.state, "converter", None)


def _wait_file(path: Path, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists() and path.stat().st_size > 0:
            return True
        time.sleep(0.2)
    return path.exists()


# HEAD is accepted on the read routes below. FastAPI, unlike bare Starlette, does NOT add HEAD to a
# GET route, so `@router.get` alone answers 405 — which is what the byte-range route already avoids
# by declaring both methods explicitly. Clients that probe a URL before playing it get a hard
# failure otherwise.
#
# /destroy is deliberately NOT in that set. It is the one route here whose GET has a side effect
# (it tears a transcode job down), and HEAD is defined as safe: a crawler, proxy or link-checker
# sending HEAD must not be able to kill someone's playback. It stays GET-only until the reference
# is shown to require otherwise.

@router.api_route("/probe", methods=["GET", "HEAD"])
def probe(mediaURL: str) -> dict:
    return probe_media(mediaURL)


@router.api_route("/{job_id}/master.m3u8", methods=["GET", "HEAD"])
def master(
    job_id: str,
    request: Request,
    mediaURL: str,
    videoCodecs: list[str] = Query(default=[]),
    audioCodecs: list[str] = Query(default=[]),
    maxAudioChannels: int = 2,
    maxWidth: int = 3840,
):
    conv = _converter(request)
    if conv is None:
        raise HTTPException(status_code=503, detail="transcoder unavailable")
    pr = probe_media(mediaURL)
    dec = decide(pr, videoCodecs or ["h264"], audioCodecs or ["aac"], maxAudioChannels, maxWidth)
    try:
        d = conv.ensure_job(job_id, mediaURL, dec)
    except ValueError as e:
        raise HTTPException(status_code=400, detail="invalid job id") from e
    if not _wait_file(d / "master.m3u8", 25):
        raise HTTPException(status_code=504, detail="transcode did not start")
    return FileResponse(d / "master.m3u8", media_type=_M3U8)


@router.get("/{job_id}/destroy")
def destroy(job_id: str, request: Request) -> dict:
    conv = _converter(request)
    if conv is not None:
        try:
            conv.stop(job_id)
        except ValueError as e:
            # This route deletes a directory, so a malformed id is refused rather than ignored.
            raise HTTPException(status_code=400, detail="invalid job id") from e
    return {"ok": True}


@router.api_route("/{job_id}/{filename}", methods=["GET", "HEAD"])
def serve_file(job_id: str, filename: str, request: Request):
    conv = _converter(request)
    if conv is None:
        raise HTTPException(status_code=503, detail="transcoder unavailable")
    try:
        path = conv.job_file(job_id, filename)
    except ValueError as e:
        raise HTTPException(status_code=400, detail="invalid job path") from e
    is_playlist = filename.endswith(".m3u8")
    if not _wait_file(path, 25 if is_playlist else 35):
        raise HTTPException(status_code=404, detail="segment not found")
    if is_playlist:
        return FileResponse(path, media_type=_M3U8)
    media_type = "video/mp4" if filename.endswith(".mp4") else "video/iso.segment"
    return FileResponse(path, media_type=media_type)
