"""Subtitle + opensub-hash API: matching key for subtitle addons, embedded-track listing/extraction."""
from __future__ import annotations

import gzip
import os
import re
import subprocess
import tempfile
import time
import urllib.request
import zlib

from fastapi import APIRouter, HTTPException, Query, Request, Response

from stremiosrv.stream.fileserver import file_disk_path
from stremiosrv.subs.opensub import opensubtitles_hash_and_size
from stremiosrv.transcode.probe import probe_media

try:  # proper charset detection (Cyrillic/legacy subs); degrade gracefully if absent
    from charset_normalizer import from_bytes as _detect_bytes
except ImportError:  # pragma: no cover
    _detect_bytes = None

router = APIRouter()


def _decompress(raw: bytes, content_encoding: str) -> bytes:
    """Undo gzip/deflate if the CDN compressed the body (urllib doesn't auto-decompress). Sniffs the
    gzip magic bytes too, since some hosts gzip without the header."""
    enc = (content_encoding or "").lower()
    if "gzip" in enc or raw[:2] == b"\x1f\x8b":
        try:
            return gzip.decompress(raw)
        except OSError:
            return raw
    if "deflate" in enc:
        for wbits in (zlib.MAX_WBITS, -zlib.MAX_WBITS):
            try:
                return zlib.decompress(raw, wbits)
            except zlib.error:
                continue
    return raw


def decode_subtitle(raw: bytes) -> str:
    """Decode a subtitle body to text, detecting the charset. A Windows-1251 (Cyrillic) or other
    legacy-encoded sub must NOT be forced through UTF-8 — that yields replacement junk that strict
    players (ExoPlayer) drop, while mpv would have coped. Returns clean Unicode."""
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig")
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    try:
        return raw.decode("utf-8")  # the common, clean case
    except UnicodeDecodeError:
        pass
    if _detect_bytes is not None:
        best = _detect_bytes(raw).best()
        if best is not None:
            return str(best)
    return raw.decode("windows-1251", errors="replace")  # Cyrillic-biased last resort

# Stremio passes videoUrl as our own stream URL: .../<40-hex-infohash>/<fileIdx>[?...]
_STREAM_RE = re.compile(r"/([0-9a-fA-F]{40})/(\d+)")


def parse_stream_url(url: str) -> tuple[str, int] | None:
    m = _STREAM_RE.search(url)
    return (m.group(1).lower(), int(m.group(2))) if m else None


def srt_to_vtt(text: str) -> str:
    """Convert a SubRip (.srt) body to WebVTT (browser <track>-compatible). Pass through if already
    WebVTT. Only difference that matters: SRT timestamps use a comma before milliseconds, VTT a dot."""
    text = text.lstrip("﻿")  # strip UTF-8 BOM
    if text.lstrip().startswith("WEBVTT"):
        return text
    text = re.sub(r"(\d{2}:\d{2}:\d{2}),(\d{3})", r"\1.\2", text)
    return "WEBVTT\n\n" + text


def to_webvtt(text: str) -> str:
    """Convert a fetched subtitle (SRT/ASS/SSA/VTT/SUB/...) to clean WebVTT via ffmpeg — the SAME path
    that already makes embedded subs render on strict players (ExoPlayer/VLC). The naive text
    conversion (srt_to_vtt) only handles well-formed SubRip; ASS/SSA (which OpenSubtitles serves a lot
    of) and CRLF/malformed SRT produce WebVTT that strict players reject (mpv tolerates it). Falls back
    to srt_to_vtt if ffmpeg is unavailable or can't parse the input."""
    tmp = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".sub", delete=False) as f:
            f.write(text)
            tmp = f.name
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-y", "-i", tmp, "-f", "webvtt", "pipe:1"],
            capture_output=True, timeout=15, check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError):
        pass
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    return srt_to_vtt(text)  # fallback: naive but charset-correct


@router.get("/subtitles.vtt")
def subtitles_proxy(source: str = Query(alias="from")) -> Response:
    """Fetch an external subtitle (Stremio addons pass `?from=<url>`) and serve it as clean WebVTT on
    our own origin — CORS-safe for the browser, and format-normalized (ffmpeg) so strict native
    players render it too."""
    if not source.lower().startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="only http(s) subtitle sources are allowed")
    try:
        with urllib.request.urlopen(source, timeout=10) as r:  # noqa: S310 — scheme checked above
            raw = r.read()
            content_encoding = r.headers.get("Content-Encoding", "")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail="failed to fetch subtitle") from e
    text = decode_subtitle(_decompress(raw, content_encoding))
    # charset=utf-8 so strict players (ExoPlayer) don't second-guess the encoding.
    return Response(content=to_webvtt(text), media_type="text/vtt; charset=utf-8")


def _ensure_edges(handle, idx: int, edge: int = 65536, timeout: float = 15.0) -> bool:
    """Make sure the first and last `edge` bytes of file `idx` are downloaded (the only bytes the
    OpenSubtitles hash reads), by boosting the covering pieces and waiting briefly."""
    size = handle.file_size(idx)
    if not size:
        return False
    plen = handle.piece_length()
    base = handle.file_offset(idx)
    spans = [(base, base + min(edge, size) - 1), (base + max(0, size - edge), base + size - 1)]
    pieces = sorted({p for lo, hi in spans for p in range(lo // plen, hi // plen + 1)})
    for p in pieces:
        handle.boost_piece(p, 0)
    end = time.time() + timeout
    while time.time() < end and not all(handle.have_piece(p) for p in pieces):
        time.sleep(0.2)
    return all(handle.have_piece(p) for p in pieces)


@router.get("/opensubHash")
def opensub_hash(request: Request, videoUrl: str | None = None, mediaURL: str | None = None) -> dict:
    """OpenSubtitles matching key, in the stock streaming-server envelope:
    `{"error": null, "result": {"size": <bytes>, "hash": "<16hex>"}}`.

    The OpenSubtitles addon queries by moviehash AND moviebytesize, so BOTH must be returned — a bare
    `{"result": "<hash>"}` (no size) silently breaks OpenSubtitles matching while other subtitle
    sources (IMDb/filename) still work. `result` is null when the file can't be resolved in time; the
    client then falls back to filename matching."""
    src = videoUrl or mediaURL
    if not src:
        raise HTTPException(status_code=422, detail="videoUrl or mediaURL required")
    parsed = parse_stream_url(src)
    eng = getattr(request.app.state, "engine", None)
    if parsed is not None and eng is not None:
        info_hash, idx = parsed
        h = eng.get(info_hash) or eng.add(info_hash)
        end = time.time() + 20
        while not h.has_metadata() and time.time() < end:
            time.sleep(0.2)
        if h.has_metadata() and _ensure_edges(h, idx):
            hsh, size = opensubtitles_hash_and_size(file_disk_path(eng.save_path(), h, idx))
            return {"error": None, "result": {"size": size, "hash": hsh}}
        return {"error": None, "result": None}  # couldn't resolve in time -> client falls back to filename
    if os.path.exists(src):
        hsh, size = opensubtitles_hash_and_size(src)
        return {"error": None, "result": {"size": size, "hash": hsh}}
    return {"error": None, "result": None}


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
