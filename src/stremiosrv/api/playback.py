import time

from fastapi import APIRouter, Request, Response
from fastapi.responses import StreamingResponse

from stremiosrv.stream.fileserver import content_type_for, wait_and_read
from stremiosrv.stream.ranges import parse_range

router = APIRouter()

DLNA_HEADERS = {
    "transferMode.dlna.org": "Streaming",
    "contentFeatures.dlna.org": (
        "DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01700000000000000000000000000000"
    ),
}


def serialize_stats(handle, idx: int | None = None) -> dict:
    """Map a libtorrent handle to the captured /:infoHash/stats.json schema.

    With idx, adds the per-file fields (streamProgress/streamName/streamLen). Only called
    with a real handle (Stage 2 Task 6 wiring); kept here so the shape lives in one place.
    """
    st = handle.status()
    ti = handle.torrent_file()
    wires, unchoked = handle.peer_wires()
    files = []
    if ti:
        fs = ti.files()
        for i in range(fs.num_files()):
            files.append({
                "path": fs.file_path(i), "name": fs.file_name(i),
                "length": fs.file_size(i), "offset": fs.file_offset(i),
            })
    # stream_* are REQUIRED top-level by stremio-core's Statistics struct (defaults; per-file below).
    stream_len, stream_name, stream_progress = 0, "", 0.0
    if idx is not None and ti:
        flen = ti.files().file_size(idx)
        stream_len = flen
        stream_name = ti.files().file_name(idx)
        stream_progress = (st.total_done / flen) if flen else 0.0
    return {
        "infoHash": str(st.info_hashes.v1), "name": (ti.name() if ti else ""),
        "peers": st.num_peers, "unchoked": unchoked, "queued": 0, "unique": st.num_peers,
        "connectionTries": 0, "swarmPaused": False,
        "swarmConnections": st.num_peers, "swarmSize": st.list_peers,
        "selections": [], "wires": wires, "files": files,
        "downloaded": st.total_done, "uploaded": st.total_upload,
        "downloadSpeed": st.download_rate, "uploadSpeed": st.upload_rate,
        "sources": [], "peerSearchRunning": True,
        "streamLen": stream_len, "streamName": stream_name, "streamProgress": stream_progress,
        # opts MUST be a fully-populated Options object or stremio-core fails to parse the whole
        # stats response (-> blank Statistics panel). Values are nominal; the panel doesn't show them.
        "opts": {
            "connections": 400, "dht": True, "tracker": True, "virtual": True,
            "path": "", "handshakeTimeout": 5000, "timeout": 2000,
            "growler": {"flood": 0, "pulse": 52428800},
            "peerSearch": {"min": 40, "max": 150, "sources": []},
            "swarmCap": {"maxSpeed": 12582912, "minPeers": 20},
        },
    }


def serialize_active(handle) -> dict:
    """Compact 'now playing' entry for the active-streams list (lighter than the full stats shape)."""
    st = handle.status()
    ti = handle.torrent_file()
    return {
        "infoHash": str(st.info_hashes.v1),
        "name": ti.name() if ti else "",
        "downloadSpeed": st.download_rate,
        "uploadSpeed": st.upload_rate,
        "peers": st.num_peers,
        "downloaded": st.total_done,
        "uploaded": st.total_upload,
        "progress": round(st.progress, 4),  # overall torrent completion, 0..1
    }


def _engine(request: Request):
    return getattr(request.app.state, "engine", None)


@router.get("/active.json")
def active_streams(request: Request) -> list:
    """Torrents currently loaded — the owner's own activity on their own box, for the appliance
    'Active streams' card. Content-neutral: names only, no media artwork/sources."""
    eng = _engine(request)
    if eng is None:
        return []
    return [serialize_active(h) for h in eng.active()]


@router.get("/{info_hash}/stats.json")
def torrent_stats(info_hash: str, request: Request):
    eng = _engine(request)
    h = eng.get(info_hash) if eng else None
    return serialize_stats(h) if h else None


@router.get("/{info_hash}/{idx:int}/stats.json")
def file_stats(info_hash: str, idx: int, request: Request):
    eng = _engine(request)
    h = eng.get(info_hash) if eng else None
    return serialize_stats(h, idx) if h else None


@router.get("/removeAll")
def remove_all(request: Request) -> dict:
    eng = _engine(request)
    if eng is not None:
        eng.remove_all()
    return {"ok": True}


@router.get("/{info_hash}/remove")
def remove(info_hash: str, request: Request) -> dict:
    eng = _engine(request)
    if eng is not None:
        eng.remove(info_hash)
    return {"ok": True}


@router.api_route("/{info_hash}/{idx:int}", methods=["GET", "HEAD"])
def serve(info_hash: str, idx: int, request: Request):
    """Byte-range file streaming with lazy engine create + deadline-driven playhead focus."""
    eng = _engine(request)
    if eng is None:
        return Response(status_code=503, content=b"engine unavailable")
    trackers = request.query_params.getlist("tr")  # client-supplied (Stremio passes magnet trackers)
    h = eng.get(info_hash) or eng.add(info_hash, trackers=trackers or None)  # lazy create
    deadline = time.time() + 30
    while not h.has_metadata() and time.time() < deadline:
        time.sleep(0.2)
    if not h.has_metadata():
        return Response(status_code=504, content=b"metadata timeout")

    h.focus_file(idx)        # download the full played file (not other files in a pack); seek-friendly
    h.refocus()              # a new request (often a seek) -> drop the previous window's deadlines
    total = h.file_size(idx)
    start, end = parse_range(request.headers.get("Range"), total)
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Range": f"bytes {start}-{end}/{total}",
        "Content-Length": str(end - start + 1),
        # Without a media type the browser <video> refuses the stream ("video not supported");
        # mpv/desktop ignore it. Derive from the file extension.
        "Content-Type": content_type_for(h.file_path(idx)),
        **DLNA_HEADERS,
    }
    if request.method == "HEAD":
        return Response(status_code=206, headers=headers)

    # The sliding boost window (in wait_and_read) concentrates bandwidth on the playhead.
    readahead = request.app.state.settings.readahead_bytes
    return StreamingResponse(
        wait_and_read(eng.save_path(), h, idx, start, end, window_bytes=readahead),
        status_code=206, headers=headers,
    )
