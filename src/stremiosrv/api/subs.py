"""Subtitle + opensub-hash API: matching key for subtitle addons, embedded-track listing/extraction."""
from __future__ import annotations

import subprocess

from fastapi import APIRouter, HTTPException, Response

from stremiosrv.subs.opensub import opensubtitles_hash
from stremiosrv.transcode.probe import probe_media

router = APIRouter()


@router.get("/opensubHash")
def opensub_hash(videoUrl: str | None = None, mediaURL: str | None = None) -> dict:
    src = videoUrl or mediaURL
    if not src:
        raise HTTPException(status_code=422, detail="videoUrl or mediaURL required")
    return {"result": opensubtitles_hash(src)}


@router.get("/{info_hash}/{idx:int}/subtitles.json")
def subtitles_list(info_hash: str, idx: int, mediaURL: str) -> dict:
    pr = probe_media(mediaURL)
    subs = [
        {"id": s.get("id"), "track": s.get("index"), "codec": s.get("codec"), "lang": s.get("lang")}
        for s in pr["streams"]
        if s.get("track") == "subtitle"
    ]
    return {"subtitles": subs}


@router.get("/{info_hash}/{idx:int}/subtitles.vtt")
def subtitles_vtt(info_hash: str, idx: int, mediaURL: str, track: int = 0) -> Response:
    argv = ["ffmpeg", "-hide_banner", "-y", "-i", mediaURL,
            "-map", f"0:s:{track}", "-f", "webvtt", "pipe:1"]
    proc = subprocess.run(argv, capture_output=True, timeout=60)
    if proc.returncode != 0:
        raise HTTPException(status_code=404, detail="subtitle track not found")
    return Response(content=proc.stdout, media_type="text/vtt")
