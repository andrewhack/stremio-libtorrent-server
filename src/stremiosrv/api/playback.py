from fastapi import APIRouter, Request

router = APIRouter()


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


@router.get("/{info_hash}/{idx}/stats.json")
def file_stats(info_hash: str, idx: int, request: Request):
    eng = _engine(request)
    h = eng.get(info_hash) if eng else None
    return serialize_stats(h, idx) if h else None


# NOTE: the byte-range file-serving routes (/{info_hash}/{idx} [+ /*]) and lazy engine
# creation are added in Stage 2 Task 6 (integration; needs libtorrent on the server).
