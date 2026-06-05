import time

from fastapi import APIRouter, Request, Response
from fastapi.responses import StreamingResponse

from stremiosrv.stream.fileserver import wait_and_read
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
    files = []
    if ti:
        fs = ti.files()
        for i in range(fs.num_files()):
            files.append({
                "path": fs.file_path(i), "name": fs.file_name(i),
                "length": fs.file_size(i), "offset": fs.file_offset(i),
                "__cacheEvents": True,
            })
    out = {
        "infoHash": str(st.info_hashes.v1), "name": (ti.name() if ti else ""),
        "peers": st.num_peers, "unchoked": 0, "queued": 0, "unique": 0,
        "connectionTries": 0, "swarmPaused": False,
        "swarmConnections": st.num_peers, "swarmSize": st.list_peers,
        "selections": [], "wires": [], "files": files,
        "downloaded": st.total_done, "uploaded": st.total_upload,
        "downloadSpeed": st.download_rate, "uploadSpeed": st.upload_rate,
        "sources": [], "peerSearchRunning": True, "opts": {},
    }
    if idx is not None and ti:
        fs = ti.files()
        out.update({
            "streamProgress": (st.total_done / fs.file_size(idx)) if fs.file_size(idx) else 0,
            "streamName": fs.file_name(idx), "streamLen": fs.file_size(idx),
        })
    return out


def _engine(request: Request):
    return getattr(request.app.state, "engine", None)


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

    h.ensure_low_baseline()  # don't background-fetch the whole torrent
    h.refocus()              # a new request (often a seek) -> drop the previous window's priorities
    total = h.file_size(idx)
    start, end = parse_range(request.headers.get("Range"), total)
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Range": f"bytes {start}-{end}/{total}",
        "Content-Length": str(end - start + 1),
        **DLNA_HEADERS,
    }
    if request.method == "HEAD":
        return Response(status_code=206, headers=headers)

    # The sliding boost window (in wait_and_read) concentrates bandwidth on the playhead.
    return StreamingResponse(
        wait_and_read(eng.save_path(), h, idx, start, end),
        status_code=206, headers=headers,
    )
