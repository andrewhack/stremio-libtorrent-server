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


@router.get("/probe")
def probe(mediaURL: str) -> dict:
    return probe_media(mediaURL)


@router.get("/{job_id}/master.m3u8")
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
    d = conv.ensure_job(job_id, mediaURL, dec)
    if not _wait_file(d / "master.m3u8", 25):
        raise HTTPException(status_code=504, detail="transcode did not start")
    return FileResponse(d / "master.m3u8", media_type=_M3U8)


@router.get("/{job_id}/destroy")
def destroy(job_id: str, request: Request) -> dict:
    conv = _converter(request)
    if conv is not None:
        conv.stop(job_id)
    return {"ok": True}


@router.get("/{job_id}/{filename}")
def serve_file(job_id: str, filename: str, request: Request):
    conv = _converter(request)
    if conv is None:
        raise HTTPException(status_code=503, detail="transcoder unavailable")
    path = conv.job_dir(job_id) / filename
    is_playlist = filename.endswith(".m3u8")
    if not _wait_file(path, 25 if is_playlist else 35):
        raise HTTPException(status_code=404, detail="segment not found")
    if is_playlist:
        return FileResponse(path, media_type=_M3U8)
    media_type = "video/mp4" if filename.endswith(".mp4") else "video/iso.segment"
    return FileResponse(path, media_type=media_type)
