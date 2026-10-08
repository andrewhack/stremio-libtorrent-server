"""Guard: serialize_stats output must match stremio-core's Statistics struct (all fields required
on the per-file route, camelCase). If it drifts, the desktop/web 'Statistics' panel silently shows 0/0/0 because core
fails to deserialize the whole response. Schema mirrored from stremio-core
src/types/streaming_server/statistics.rs."""
from stremiosrv.api.playback import serialize_stats


class _IH:
    v1 = "a" * 40


class FakeStatus:
    info_hashes = _IH()
    num_peers = 7
    list_peers = 50
    total_done = 1000
    total_upload = 50
    download_rate = 1234
    upload_rate = 56


class FakeFiles:
    def num_files(self):
        return 1

    def file_path(self, i):
        return "movie.mkv"

    def file_name(self, i):
        return "movie.mkv"

    def file_size(self, i):
        return 8000

    def file_offset(self, i):
        return 0


class FakeTI:
    def name(self):
        return "movie"

    def files(self):
        return FakeFiles()


class FakeHandle:
    def status(self):
        return FakeStatus()

    def torrent_file(self):
        return FakeTI()

    def peer_wires(self):
        return ([], 3)


# (key, accepted python types) — bool is a subclass of int, fine for our checks.
CORE_REQUIRED = {
    "name": str, "infoHash": str, "files": list, "sources": list, "opts": dict,
    "downloadSpeed": (int, float), "uploadSpeed": (int, float),
    "downloaded": int, "uploaded": int, "unchoked": int, "peers": int,
    "queued": int, "unique": int, "connectionTries": int, "peerSearchRunning": bool,
    "streamLen": int, "streamName": str, "streamProgress": (int, float),
    "swarmConnections": int, "swarmPaused": bool, "swarmSize": int,
}
OPTS_REQUIRED = {"dht": bool, "tracker": bool, "virtual": bool, "path": str,
                 "growler": dict, "peerSearch": dict, "swarmCap": dict}


def test_stats_matches_core_schema():
    out = serialize_stats(FakeHandle(), 0)
    for key, types in CORE_REQUIRED.items():
        assert key in out, f"missing required key {key}"
        assert isinstance(out[key], types), f"{key} has wrong type {type(out[key])}"
    o = out["opts"]
    for key, types in OPTS_REQUIRED.items():
        assert key in o and isinstance(o[key], types), f"opts.{key} bad"
    assert isinstance(o["growler"]["flood"], int)
    assert isinstance(o["peerSearch"]["min"], int)
    assert isinstance(o["peerSearch"]["max"], int)
    assert isinstance(o["peerSearch"]["sources"], list)
    # real per-file progress flows through
    assert out["streamProgress"] == 1000 / 8000
    assert out["streamLen"] == 8000


# The torrent-level route (no file index) is asked only by stremio-core 0.64+ (web 5.0.0-beta.41),
# where the stream* fields became optional. The stock server leaves them out there and the web
# player then hides "Completed"; sending zeros showed "Completed 0 %" for the whole stream.
def test_torrent_stats_leave_out_the_per_file_fields():
    out = serialize_stats(FakeHandle())
    assert not {"streamLen", "streamName", "streamProgress"} & out.keys()
    for key, types in CORE_REQUIRED.items():
        if not key.startswith("stream"):
            assert isinstance(out.get(key), types), f"{key} missing or wrong type"


def test_stats_no_negative_source_counts():
    # sources entries (if any) must be non-negative (core u64). We send [] now; assert that holds.
    out = serialize_stats(FakeHandle(), 0)
    assert out["sources"] == []
