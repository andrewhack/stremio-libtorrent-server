"""POST /<infohash>/create and the -1 file index.

stremio-video calls create before streaming whenever an addon's stream carries `sources` or no
`fileIdx`, reads `guessedFileIdx` off the answer, then plays /<ih>/<guessedFileIdx>?tr=<source>.
stremio-core builds /<ih>/-1 for a stream with no fileIdx (external-player and download links).
Neither route existed: create answered 404 on the API origin and 405 through nginx, which the
player treats as fatal, so every such stream failed to start without a line in any log.
"""
from types import SimpleNamespace

from fastapi.testclient import TestClient

from stremiosrv.api import playback
from stremiosrv.app import create_app

IH = "ab" * 20
MB = 1 << 20
SEASON = [
    ("Show/Show.S01E01.mkv", 1200 * MB),
    ("Show/Show.S01E02.mkv", 400 * MB),
    ("Show/cover.jpg", 1 * MB),
]
# What stremio-video's createTorrent() sends for a stream with `sources`: dht first, then the
# addon's own entries, each still carrying its peer-search prefix.
SOURCES = [f"dht:{IH}", "tracker:udp://tracker.example:1337/announce"]
TRACKER = "udp://tracker.example:1337/announce"

STATUS = SimpleNamespace(info_hashes=SimpleNamespace(v1=IH), num_peers=0, list_peers=0,
                         total_done=0, total_upload=0, download_rate=0, upload_rate=0)


class _Files:
    def __init__(self, files):
        self._f = files

    def num_files(self):
        return len(self._f)

    def file_path(self, i):
        return self._f[i][0]

    def file_name(self, i):
        return self._f[i][0].rsplit("/", 1)[-1]

    def file_size(self, i):
        return self._f[i][1]

    def file_offset(self, i):
        return sum(size for _, size in self._f[:i])


class _Handle:
    """Just enough of engine.Handle for create, and for a HEAD on the stream route."""

    def __init__(self, files, metadata=True):
        self._files = files
        self._metadata = metadata
        self.trackers_added = []
        self.focused = None

    def has_metadata(self):
        return self._metadata

    def torrent_file(self):
        if not self._metadata:
            return None
        return SimpleNamespace(name=lambda: "Show", files=lambda: _Files(self._files))

    def file_paths(self):
        return [path for path, _ in self._files] if self._metadata else []

    def file_size(self, i):
        return self._files[i][1]

    def file_path(self, i):
        return self._files[i][0]

    def status(self):
        return STATUS

    def peer_wires(self):
        return [], 0

    def add_trackers(self, urls):
        self.trackers_added.extend(urls)
        return len(urls)

    def is_active(self):
        return False

    def focus_file(self, idx):
        self.focused = idx

    def refocus(self):
        pass


class _Engine:
    def __init__(self, handle, running=False):
        self.handle = handle
        self.running = running
        self.added = []

    def get(self, ih):
        return self.handle if self.running else None

    def add(self, ih, trackers=None):
        self.added.append((ih, trackers))
        self.running = True
        return self.handle

    def active_torrent_count(self):
        return 0


def _client(handle, running=False):
    eng = _Engine(handle, running)
    return TestClient(create_app(engine=eng)), eng


def _create(client, body, ih=IH):
    return client.post(f"/{ih}/create", json=body)


# --- POST /<ih>/create --------------------------------------------------------------------------


def test_create_answers_with_the_guessed_file():
    c, _ = _client(_Handle(SEASON))
    r = _create(c, {"torrent": {"infoHash": IH},
                    "peerSearch": {"sources": SOURCES, "min": 40, "max": 200},
                    "guessFileIdx": {}})
    assert r.status_code == 200
    assert r.json()["guessedFileIdx"] == 0  # the largest media file


def test_create_guesses_the_requested_episode():
    c, _ = _client(_Handle(SEASON))
    r = _create(c, {"torrent": {"infoHash": IH}, "guessFileIdx": {"season": 1, "episode": 2}})
    assert r.json()["guessedFileIdx"] == 1


def test_create_names_no_file_when_the_client_already_has_one():
    """guessFileIdx is `false` when the stream carried a fileIdx and only its `sources` sent the
    client here; the client keeps its own index, so the answer must not offer another."""
    c, _ = _client(_Handle(SEASON))
    r = _create(c, {"torrent": {"infoHash": IH}, "peerSearch": {"sources": SOURCES},
                    "guessFileIdx": False})
    assert r.status_code == 200
    assert "guessedFileIdx" not in r.json()


def test_create_answers_with_the_torrents_stats():
    c, _ = _client(_Handle(SEASON))
    body = _create(c, {"guessFileIdx": {}}).json()
    assert body["infoHash"] == IH
    names = [f["name"] for f in body["files"]]
    assert names == ["Show.S01E01.mkv", "Show.S01E02.mkv", "cover.jpg"]


def test_create_adds_the_torrent_with_the_addons_trackers_unprefixed():
    c, eng = _client(_Handle(SEASON))
    _create(c, {"peerSearch": {"sources": SOURCES}})
    assert eng.added == [(IH, [TRACKER])]


def test_create_folds_new_trackers_into_a_running_torrent():
    h = _Handle(SEASON)
    c, eng = _client(h, running=True)
    _create(c, {"peerSearch": {"sources": SOURCES}})
    assert eng.added == []
    assert h.trackers_added == [TRACKER]


def test_create_accepts_any_method_and_no_body_like_the_stock_server():
    """server.js registers it with router.all()."""
    c, eng = _client(_Handle(SEASON))
    assert c.get(f"/{IH}/create").status_code == 200
    assert eng.added == [(IH, None)]


def test_create_lowercases_the_infohash():
    c, eng = _client(_Handle(SEASON))
    _create(c, {}, ih=IH.upper())
    assert eng.added[0][0] == IH


def test_create_refuses_something_that_is_not_an_infohash():
    c, eng = _client(_Handle(SEASON))
    assert _create(c, {}, ih="not-an-infohash").status_code == 400
    assert eng.added == []


def test_create_gives_up_when_metadata_never_arrives(monkeypatch):
    monkeypatch.setattr(playback, "METADATA_TIMEOUT", 0.3)
    c, _ = _client(_Handle(SEASON, metadata=False))
    assert _create(c, {"guessFileIdx": {}}).status_code == 504


# --- /<ih>/-1 -----------------------------------------------------------------------------------


def test_minus_one_streams_the_largest_media_file():
    h = _Handle(SEASON)
    c, _ = _client(h)
    r = c.head(f"/{IH}/-1")
    assert r.status_code == 206
    assert r.headers["content-range"] == f"bytes 0-{1200 * MB - 1}/{1200 * MB}"
    assert r.headers["content-type"].startswith("video/")
    assert h.focused == 0


def test_minus_one_is_404_when_nothing_is_playable():
    c, _ = _client(_Handle([("readme.txt", 10)]))
    r = c.get(f"/{IH}/-1")
    assert r.status_code == 404
    assert r.content == b"no media file in this torrent"  # ours, not the router's "Not Found"


# --- tr= on the stream URL ----------------------------------------------------------------------


def test_stream_route_strips_the_peer_search_prefixes_from_tr():
    """After create, the client plays /<ih>/<idx>?tr=<each source>, prefixes and all."""
    c, eng = _client(_Handle(SEASON))
    c.head(f"/{IH}/1", params=[("tr", s) for s in SOURCES])
    assert eng.added == [(IH, [TRACKER])]
