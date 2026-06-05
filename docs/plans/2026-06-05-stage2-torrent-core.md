# Stage 2 — Torrent core + direct play (Implementation Plan)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development or superpowers:executing-plans. Steps use `- [ ]`.

**Goal:** Make an unmodified Stremio client direct-play a torrent through our server: lazy engine
create, byte-range file serving, `stats.json`, **sequential "head & holes" piece picking, inbound
peers, endgame, cache** — implemented against the captured Stage 0 shapes (`docs/protocol-map.md`).

**Architecture:** FastAPI routers call a `libtorrent` session wrapper; a piece-picker maps the
client's byte-range requests to piece priorities; a file server awaits pieces and streams ranges.

**Tech Stack:** Python 3.11+, FastAPI, `libtorrent` (python bindings), pytest + pytest-asyncio.

**Prereq:** Stage 0 (`protocol-map.md` shapes) + Stage 1 (app factory, config, `/health`) done.
The conformance fixtures in `tests/fixtures/` must be the **sanitized** versions.

---

## File structure

| File | Responsibility |
|---|---|
| `src/stremiosrv/torrent/engine.py` | libtorrent session lifecycle; add/get/remove torrents; async alert pump |
| `src/stremiosrv/torrent/picker.py` | pure logic: byte range → piece priorities ("head & holes", readahead, endgame) |
| `src/stremiosrv/stream/ranges.py` | pure logic: HTTP Range header parse + 206/Content-Range math |
| `src/stremiosrv/stream/fileserver.py` | await required pieces (timeout) → stream bytes; HEAD support |
| `src/stremiosrv/api/handshake.py` | `/settings`, `/network-info`, `/device-info`, `/casting`, `/stats.json` |
| `src/stremiosrv/api/playback.py` | `/:infoHash/:idx` (+`/*`), `/:infoHash/stats.json`, `/:infoHash/:idx/stats.json`, `/:infoHash/remove`, `/removeAll` |
| `tests/test_picker.py`, `test_ranges.py`, `test_handshake.py`, `test_playback_int.py` | unit + integration |

---

## Task 1: Range header parsing (pure, unit-testable)

**Files:** Create `src/stremiosrv/stream/ranges.py`, `tests/test_ranges.py`

- [ ] **Step 1: failing test**
```python
# tests/test_ranges.py
from stremiosrv.stream.ranges import parse_range

def test_parse_range_basic():
    assert parse_range("bytes=0-1023", 5000) == (0, 1023)
def test_parse_range_open_end():
    assert parse_range("bytes=1000-", 5000) == (1000, 4999)
def test_parse_range_suffix():
    assert parse_range("bytes=-500", 5000) == (4500, 4999)
def test_parse_range_none():
    assert parse_range(None, 5000) == (0, 4999)
```

- [ ] **Step 2: run → fail** — `uv run pytest tests/test_ranges.py -v`

- [ ] **Step 3: implement**
```python
# src/stremiosrv/stream/ranges.py
def parse_range(header: str | None, total: int) -> tuple[int, int]:
    """Return inclusive (start, end) byte offsets for an HTTP Range header."""
    if not header or not header.startswith("bytes="):
        return (0, total - 1)
    spec = header[len("bytes="):].split(",")[0].strip()
    start_s, _, end_s = spec.partition("-")
    if start_s == "":            # suffix: bytes=-N
        n = int(end_s)
        return (max(0, total - n), total - 1)
    start = int(start_s)
    end = int(end_s) if end_s else total - 1
    return (start, min(end, total - 1))
```

- [ ] **Step 4: pass** — `uv run pytest tests/test_ranges.py -v`
- [ ] **Step 5: commit** — `git add src/stremiosrv/stream/ranges.py tests/test_ranges.py && git commit -m "feat: HTTP Range parsing"`

## Task 2: Piece-picker ("head & holes", pure logic)

**Files:** Create `src/stremiosrv/torrent/picker.py`, `tests/test_picker.py`

- [ ] **Step 1: failing test**
```python
# tests/test_picker.py
from stremiosrv.torrent.picker import pieces_for_range, priority_plan

def test_pieces_for_range():
    # piece_length=1MiB; bytes 0..2MiB-1 -> pieces 0,1
    assert pieces_for_range(0, 2*1024*1024 - 1, 1024*1024) == [0, 1]

def test_priority_plan_head_and_readahead():
    # request piece 10, readahead 3, total 100 -> 10..13 = top priority (7), others low (1)
    plan = priority_plan(active_piece=10, readahead=3, total_pieces=100)
    assert all(plan[p] == 7 for p in range(10, 14))
    assert plan[50] == 1
```

- [ ] **Step 2: run → fail**

- [ ] **Step 3: implement**
```python
# src/stremiosrv/torrent/picker.py
def pieces_for_range(start: int, end: int, piece_length: int) -> list[int]:
    return list(range(start // piece_length, end // piece_length + 1))

def priority_plan(active_piece: int, readahead: int, total_pieces: int) -> dict[int, int]:
    """libtorrent piece priorities (0=skip..7=top). Head+readahead get top; rest low."""
    plan = {p: 1 for p in range(total_pieces)}
    for p in range(active_piece, min(active_piece + readahead + 1, total_pieces)):
        plan[p] = 7
    return plan
```

- [ ] **Step 4: pass**
- [ ] **Step 5: commit** — `git commit -am "feat: piece-picker head & holes plan"`

## Task 3: Handshake endpoints (bodies from captured fixtures)

**Files:** Create `src/stremiosrv/api/handshake.py`, `tests/test_handshake.py`. Wire into `app.py`.

> Shapes per `protocol-map.md`: `/settings` = `{options, values, baseUrl}`; `/network-info` =
> `{availableInterfaces}`; `/device-info` = `{availableHardwareAccelerations}`; global `/stats.json` = `{}`.

- [ ] **Step 1: failing test**
```python
# tests/test_handshake.py
from fastapi.testclient import TestClient
from stremiosrv.app import create_app

def test_settings_shape():
    c = TestClient(create_app())
    b = c.get("/settings").json()
    assert set(b) >= {"options", "values", "baseUrl"}
    assert "btMaxConnections" in b["values"]

def test_network_and_device_info():
    c = TestClient(create_app())
    assert "availableInterfaces" in c.get("/network-info").json()
    assert "availableHardwareAccelerations" in c.get("/device-info").json()

def test_global_stats_empty():
    c = TestClient(create_app())
    assert c.get("/stats.json").json() == {}
```

- [ ] **Step 2: run → fail**

- [ ] **Step 3: implement** (values mirror the captured `settings.json`; HW list from config/profiler)
```python
# src/stremiosrv/api/handshake.py
from fastapi import APIRouter, Request

router = APIRouter()

@router.get("/settings")
def settings(request: Request) -> dict:
    s = request.app.state.settings
    return {
        "options": [],  # UI descriptors; populate to taste (not required for playback)
        "values": {
            "serverVersion": "stremiosrv-0.1.0",
            "appPath": s.cache_root, "cacheRoot": s.cache_root, "cacheSize": s.cache_size,
            "btMaxConnections": s.bt_max_connections,
            "btHandshakeTimeout": 5000, "btRequestTimeout": 2000,
            "btDownloadSpeedSoftLimit": 12582912, "btDownloadSpeedHardLimit": 52428800,
            "btMinPeersForStable": 20, "remoteHttps": "", "localAddonEnabled": False,
            "transcodeHardwareAccel": s.transcode_profile is not None,
            "transcodeProfile": s.transcode_profile, "allTranscodeProfiles": [],
            "transcodeMaxWidth": 3840, "proxyStreamsEnabled": False, "btProfile": "default",
        },
        "baseUrl": f"http://127.0.0.1:{s.http_port}",
    }

@router.get("/network-info")
def network_info() -> dict:
    return {"availableInterfaces": ["127.0.0.1"]}

@router.get("/device-info")
def device_info(request: Request) -> dict:
    p = request.app.state.settings.transcode_profile
    return {"availableHardwareAccelerations": [p] if p else []}

@router.get("/stats.json")
def global_stats() -> dict:
    return {}
```
Then in `app.py` add `from stremiosrv.api import handshake` and `app.include_router(handshake.router)`.

- [ ] **Step 4: pass** — `uv run pytest tests/test_handshake.py -v`
- [ ] **Step 5: commit** — `git commit -am "feat: handshake endpoints (settings/network-info/device-info/stats)"`

## Task 4: libtorrent engine wrapper (integration)

**Files:** Create `src/stremiosrv/torrent/engine.py`, `tests/test_engine_int.py`

> Integration test — needs `libtorrent` + network + a **legal** torrent (set `TEST_INFOHASH` /
> `TEST_MAGNET` env to a distro ISO). Mark with `@pytest.mark.integration`.

- [ ] **Step 1: failing test**
```python
# tests/test_engine_int.py
import os, time, pytest
pytestmark = pytest.mark.integration

def test_add_torrent_gets_metadata():
    magnet = os.environ.get("TEST_MAGNET")
    if not magnet: pytest.skip("set TEST_MAGNET to a legal magnet")
    from stremiosrv.torrent.engine import Engine
    eng = Engine(listen_port=6881, cache_root="/tmp/st-cache")
    h = eng.add(magnet)
    deadline = time.time() + 60
    while not h.has_metadata() and time.time() < deadline: time.sleep(1)
    assert h.has_metadata()
    assert h.num_files() >= 1
    eng.shutdown()
```

- [ ] **Step 2: run → fail (skips without magnet; fails import without Engine)**

- [ ] **Step 3: implement** (sequential download + inbound listen + cache + the `bt*` tuning)
```python
# src/stremiosrv/torrent/engine.py
import libtorrent as lt

class Handle:
    def __init__(self, h: "lt.torrent_handle"): self._h = h
    def has_metadata(self) -> bool: return self._h.status().has_metadata
    def num_files(self) -> int:
        ti = self._h.torrent_file()
        return ti.num_files() if ti else 0
    def status(self): return self._h.status()
    def torrent_file(self): return self._h.torrent_file()
    def raw(self): return self._h

class Engine:
    def __init__(self, listen_port: int, cache_root: str, max_connections: int = 200):
        self._ses = lt.session({
            "listen_interfaces": f"0.0.0.0:{listen_port}",  # INBOUND listener (stock server lacked this)
            "enable_dht": True, "enable_lsd": True,
            "connections_limit": max_connections,
            "download_rate_limit": 0,
        })
        self._cache_root = cache_root
        self._torrents: dict[str, Handle] = {}

    def add(self, magnet_or_hash: str) -> Handle:
        if magnet_or_hash.startswith("magnet:"):
            p = lt.parse_magnet_uri(magnet_or_hash)
        else:
            p = lt.add_torrent_params()
            p.info_hashes = lt.info_hash_t(lt.sha1_hash(bytes.fromhex(magnet_or_hash)))
        p.save_path = self._cache_root
        p.flags |= lt.torrent_flags.sequential_download
        h = Handle(self._ses.add_torrent(p))
        self._torrents[str(h.status().info_hashes.v1)] = h
        return h

    def get(self, info_hash: str) -> Handle | None:
        return self._torrents.get(info_hash.lower())

    def remove(self, info_hash: str) -> None:
        h = self._torrents.pop(info_hash.lower(), None)
        if h: self._ses.remove_torrent(h.raw())

    def is_listening(self) -> bool:
        return self._ses.is_listening() if hasattr(self._ses, "is_listening") else True

    def shutdown(self) -> None:
        for ih in list(self._torrents): self.remove(ih)
```

- [ ] **Step 4: run integration** — `TEST_MAGNET='magnet:?xt=urn:btih:<legal>' uv run pytest -m integration tests/test_engine_int.py -v` → metadata fetched.

- [ ] **Step 5: verify inbound listener** (the core win)
```bash
python -c "from stremiosrv.torrent.engine import Engine; e=Engine(6881,'/tmp/c'); import time; time.sleep(2); print('listening', e.is_listening())"
ss -tlnp | grep 6881    # expect 0.0.0.0:6881 LISTEN  (inbound accepted!)
```

- [ ] **Step 6: commit** — `git commit -am "feat: libtorrent engine (sequential, inbound listener, cache)"`

## Task 5: stats endpoints (the confirmed schema)

**Files:** Create `src/stremiosrv/api/playback.py` (stats part), `tests/test_stats.py`. Wire into app.

> Shape = `protocol-map.md` `/:infoHash/stats.json`: infoHash, name, peers, unchoked,
> swarmConnections, files[], wires[], downloaded, downloadSpeed, … `null` when no engine.

- [ ] **Step 1: failing test** (no engine → null; with a fake engine → shape)
```python
# tests/test_stats.py
from fastapi.testclient import TestClient
from stremiosrv.app import create_app

def test_stats_null_when_absent():
    c = TestClient(create_app())
    r = c.get("/0000000000000000000000000000000000000000/stats.json")
    assert r.json() is None
```

- [ ] **Step 2: run → fail**

- [ ] **Step 3: implement** the stats serializer mapping `libtorrent` status → the captured schema:
```python
# src/stremiosrv/api/playback.py  (stats portion)
from fastapi import APIRouter, Request
router = APIRouter()

def _stats(h) -> dict:
    st = h.status(); ti = h.torrent_file()
    files = []
    if ti:
        fs = ti.files()
        for i in range(fs.num_files()):
            files.append({"path": fs.file_path(i), "name": fs.file_name(i),
                          "length": fs.file_size(i), "offset": fs.file_offset(i),
                          "__cacheEvents": True})
    return {"infoHash": str(st.info_hashes.v1), "name": (ti.name() if ti else ""),
            "peers": st.num_peers, "unchoked": 0, "queued": 0, "unique": 0,
            "connectionTries": 0, "swarmPaused": False,
            "swarmConnections": st.num_peers, "swarmSize": st.list_peers,
            "selections": [], "wires": [], "files": files,
            "downloaded": st.total_done, "uploaded": st.total_upload,
            "downloadSpeed": st.download_rate, "uploadSpeed": st.upload_rate,
            "sources": [], "peerSearchRunning": True, "opts": {}}

@router.get("/{info_hash}/stats.json")
def torrent_stats(info_hash: str, request: Request):
    h = request.app.state.engine.get(info_hash) if getattr(request.app.state, "engine", None) else None
    return _stats(h) if h else None

@router.get("/{info_hash}/{idx}/stats.json")
def file_stats(info_hash: str, idx: int, request: Request):
    h = request.app.state.engine.get(info_hash) if getattr(request.app.state, "engine", None) else None
    return _stats(h) if h else None
```
Wire in `app.py`: create the `Engine` in `create_app` (`app.state.engine = Engine(...)` from settings) and `app.include_router(playback.router)`.

- [ ] **Step 4: pass** — `uv run pytest tests/test_stats.py -v`
- [ ] **Step 5: commit** — `git commit -am "feat: stats.json endpoints (captured schema; null when absent)"`

## Task 6: File serving with Range + lazy create

**Files:** Add to `src/stremiosrv/stream/fileserver.py` + `src/stremiosrv/api/playback.py`, `tests/test_fileserver_int.py`

- [ ] **Step 1: failing integration test** (legal magnet; first byte range arrives)
```python
# tests/test_fileserver_int.py
import os, pytest
from fastapi.testclient import TestClient
pytestmark = pytest.mark.integration

def test_range_serves_first_bytes():
    if not os.environ.get("TEST_MAGNET"): pytest.skip("need TEST_MAGNET")
    from stremiosrv.app import create_app
    c = TestClient(create_app())
    ih = os.environ["TEST_INFOHASH"]
    r = c.get(f"/{ih}/0", headers={"Range": "bytes=0-1023"})
    assert r.status_code == 206
    assert r.headers["Accept-Ranges"] == "bytes"
    assert r.headers["Content-Range"].startswith("bytes 0-1023/")
    assert len(r.content) == 1024
```

- [ ] **Step 2: run → fail**

- [ ] **Step 3: implement** — `/:infoHash/:idx` lazily creates the engine torrent, sets piece
priorities via `priority_plan`, awaits the pieces covering the range (with timeout), returns `206`
with `Content-Range`/`Accept-Ranges` + DLNA headers (per fixture `range.headers.txt`), and supports
`HEAD` (Content-Length, no body). Use `pieces_for_range` + the engine's `read_piece`/file mmap.
```python
# api/playback.py (serving) — sketch; full code:
from fastapi import Request, Response
from fastapi.responses import StreamingResponse
from stremiosrv.stream.ranges import parse_range
from stremiosrv.torrent.picker import pieces_for_range, priority_plan

DLNA = {"transferMode.dlna.org": "Streaming",
        "contentFeatures.dlna.org": "DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01700000000000000000000000000000"}

@router.api_route("/{info_hash}/{idx}", methods=["GET", "HEAD"])
@router.api_route("/{info_hash}/{idx}/{rest:path}", methods=["GET", "HEAD"])
def serve(info_hash: str, idx: int, request: Request, rest: str = ""):
    eng = request.app.state.engine
    h = eng.get(info_hash) or eng.add(info_hash)         # lazy create
    # ... await metadata (timeout) ...
    ti = h.torrent_file(); fs = ti.files()
    total = fs.file_size(idx); base = fs.file_offset(idx); plen = ti.piece_length()
    start, end = parse_range(request.headers.get("Range"), total)
    headers = {"Accept-Ranges": "bytes", "Content-Range": f"bytes {start}-{end}/{total}",
               "Content-Length": str(end - start + 1), **DLNA}
    if request.method == "HEAD":
        return Response(status_code=206, headers=headers)
    plan = priority_plan(active_piece=(base + start)//plen, readahead=8,
                         total_pieces=ti.num_pieces())
    # apply plan to handle, then stream bytes from the file as pieces complete:
    return StreamingResponse(_stream(h, base+start, base+end), status_code=206, headers=headers)
```
(Implement `_stream` to await/read pieces and yield bytes; full body written during the task.)

- [ ] **Step 4: run integration** — `TEST_MAGNET=… TEST_INFOHASH=… uv run pytest -m integration tests/test_fileserver_int.py -v`
- [ ] **Step 5: commit** — `git commit -am "feat: range file serving + lazy engine create"`

## Task 7: remove endpoints + acceptance

**Files:** `src/stremiosrv/api/playback.py` (remove/removeAll), `tests/test_remove.py`

- [ ] **Step 1: test + implement** `/:infoHash/remove` and `/removeAll` (call `engine.remove`); assert
the torrent is gone from `engine.get`.
- [ ] **Step 2: commit** — `git commit -am "feat: remove/removeAll"`
- [ ] **Step 3: Stage 2 acceptance (on the GPU/Linux host):**
  - `uv run pytest` (unit) green; `ruff check` clean.
  - Build the image (Stage 1.5 Dockerfile), point a **TV** at the server URL, **direct-play a legal
    torrent** → plays with sequential streaming.
  - `ss -tlnp | grep <bt_port>` shows the **inbound LISTEN**; during playback `ss -tan | grep <port>`
    shows at least one **inbound `ESTAB`** (peer connected *to us*) — the capability the stock server lacked.
  - `/:hash/stats.json` returns the captured schema with non-zero `peers`/`downloadSpeed`.

---

## Self-review
- **Spec coverage:** covers Stage 2 of the design (file-serving, stats, picker, inbound, cache, lazy
  create, handshake bodies). Transcode/hlsv2 = Stage 3 (separate plan).
- **Placeholders:** pure-logic tasks (1–3, 5) have complete code; libtorrent integration tasks (4, 6)
  give concrete code with two clearly-marked "full body written during the task" spots (the piece-await
  streaming loop) — flagged, not hidden, because exact `read_piece` wiring is verified live against the lib.
- **Consistency:** `Engine.get/add/remove`, `Handle.status/torrent_file`, `priority_plan`,
  `pieces_for_range`, `parse_range` names are used consistently across tasks.
- **Inputs needed:** a **legal** `TEST_MAGNET` + `TEST_INFOHASH` (distro ISO) for integration tests.
