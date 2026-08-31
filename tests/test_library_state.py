from stremiosrv import cache as cachemod
from stremiosrv.library import labels, session, state


class FakeEngine:
    def __init__(self, pinned=(), names=None):
        self._pinned = list(pinned)
        self._names = names or {}

    def name_to_hash(self):
        return self._names

    def pinned_status(self):
        return self._pinned


def _seed_cache(tmp_path, *names):
    for n in names:
        d = tmp_path / n
        d.mkdir()
        (d / "f.bin").write_bytes(b"x" * 1024)


def test_empty_cache(tmp_path):
    assert state.build(str(tmp_path), None)["entries"] == []


def test_unlabelled_entry_is_still_reported(tmp_path):
    """The whole point of returning everything: a library-only view lets the disk fill invisibly."""
    _seed_cache(tmp_path, "some-download")
    entries = state.build(str(tmp_path), None)["entries"]
    assert len(entries) == 1
    assert entries[0]["label"] is None
    assert entries[0]["size"] > 0


def test_label_is_attached_by_infohash(tmp_path):
    _seed_cache(tmp_path, "some-download")
    labels.put(str(tmp_path), "aabb", {"name": "Placeholder", "type": "movie"})
    eng = FakeEngine(names={"some-download": "AABB"})
    entries = state.build(str(tmp_path), eng)["entries"]
    assert entries[0]["label"]["name"] == "Placeholder"
    assert entries[0]["infoHash"] == "aabb"


def test_pin_fields_are_merged(tmp_path):
    _seed_cache(tmp_path, "some-download")
    eng = FakeEngine(
        names={"some-download": "aabb"},
        pinned=[{"infoHash": "aabb", "name": "some-download", "progress": 0.5,
                 "state": "downloading", "downloaded": 5, "uploaded": 1, "ratio": 0.2,
                 "uploadSpeed": 10, "peers": 3}],
    )
    e = state.build(str(tmp_path), eng)["entries"][0]
    assert e["pinned"] is True and e["progress"] == 0.5 and e["peers"] == 3


def test_pinned_torrent_with_no_files_yet_still_appears(tmp_path):
    """A download just started has a pin but nothing on disk. Omitting it makes the UI look like the
    click did nothing."""
    eng = FakeEngine(pinned=[{"infoHash": "ccdd", "name": "starting", "progress": 0.0,
                              "state": "downloading", "downloaded": 0, "uploaded": 0,
                              "ratio": 0.0, "uploadSpeed": 0, "peers": 0}])
    entries = state.build(str(tmp_path), eng)["entries"]
    assert [e["infoHash"] for e in entries] == ["ccdd"]
    assert entries[0]["size"] == 0


def test_protected_dirs_are_not_listed(tmp_path):
    _seed_cache(tmp_path, "transcode", "real-download")
    names = [e["name"] for e in state.build(str(tmp_path), None)["entries"]]
    assert "transcode" not in names and "real-download" in names


def test_library_state_files_are_eviction_protected():
    """`labels.json` and `library-ui.json` live in cache_root beside the torrent data, so without
    this the evictor treats them as ordinary cache entries. They are small and rarely rewritten, so
    `select_evictions` — which sorts by mtime — would pick them FIRST when the cache goes over
    budget. Losing library-ui.json is not a cosmetic loss: load_state falls back to a blank
    owner_id, so the pin is gone and the next Stremio account to sign in claims the box.
    `pins.json` is already in PROTECTED for exactly this reason.
    """
    assert labels.LABELS_FILE in cachemod.PROTECTED
    assert session.STATE_FILE in cachemod.PROTECTED


def test_state_files_do_not_show_up_as_cache_entries(tmp_path):
    """The same protection, observed from the other end: they must not appear in the UI as junk
    sitting on the disk."""
    labels.put(str(tmp_path), "aabb", {"name": "Placeholder"})
    session.claim_owner(str(tmp_path), {"_id": "u1"}, "")
    names = [e["name"] for e in state.build(str(tmp_path), None)["entries"]]
    assert labels.LABELS_FILE not in names
    assert session.STATE_FILE not in names


def test_usage_is_reported(tmp_path):
    out = state.build(str(tmp_path), None, budget=1000)
    assert out["budget"]["cacheSize"] == 1000 and "cacheUsed" in out["budget"]


def test_engine_failure_does_not_break_the_listing(tmp_path):
    class Broken(FakeEngine):
        def pinned_status(self):
            raise RuntimeError("libtorrent went away")
    _seed_cache(tmp_path, "some-download")
    entries = state.build(str(tmp_path), Broken())["entries"]
    assert len(entries) == 1 and entries[0]["pinned"] is False


def _partfile(tmp_path, infohash, size=1024):
    (tmp_path / f".{infohash}.parts").write_bytes(b"x" * size)


def test_partfile_size_is_folded_into_its_torrent(tmp_path):
    """libtorrent writes `.<infohash>.parts` beside the data for a partially-downloaded torrent.
    It is engine bookkeeping, not a thing the owner downloaded — showing it as its own card invites
    deleting it, which corrupts the torrent it belongs to. Its bytes are real, so they are added to
    the entry they belong to rather than dropped."""
    _seed_cache(tmp_path, "a-download")
    _partfile(tmp_path, "aa" * 20, 4096)
    eng = FakeEngine(names={"a-download": "aa" * 20})
    entries = state.build(str(tmp_path), eng)["entries"]
    assert len(entries) == 1, f"partfile leaked as its own entry: {[e['name'] for e in entries]}"
    assert entries[0]["size"] >= 4096 + 1024


def test_orphan_partfile_is_still_visible_but_not_removable(tmp_path):
    """A partfile whose torrent is gone still occupies disk, so hiding it would recreate exactly the
    invisible-disk problem this view exists to prevent. Show it — but never offer a delete that
    would corrupt live engine state."""
    _partfile(tmp_path, "bb" * 20, 2048)
    entries = state.build(str(tmp_path), None)["entries"]
    assert len(entries) == 1
    assert entries[0]["removable"] is False
    assert entries[0]["size"] >= 2048


def test_entries_without_an_infohash_are_not_removable(tmp_path):
    """/library/api/remove is keyed by infohash. An entry we cannot name one for would render a
    button that silently does nothing."""
    _seed_cache(tmp_path, "orphan-folder")
    e = state.build(str(tmp_path), None)["entries"][0]
    assert e["infoHash"] is None and e["removable"] is False


def test_ordinary_cache_entry_with_a_hash_is_removable(tmp_path):
    _seed_cache(tmp_path, "a-download")
    eng = FakeEngine(names={"a-download": "aa" * 20})
    assert state.build(str(tmp_path), eng)["entries"][0]["removable"] is True


def test_download_speed_and_seeds_are_reported(tmp_path):
    """Progress alone does not say whether a download is moving. `seeds` counts peers that have the
    whole thing — the figure that predicts whether it will finish — while `peers` counts every
    connection, leechers included."""
    eng = FakeEngine(pinned=[{"infoHash": "aabb", "name": "x", "progress": 0.5,
                              "state": "downloading", "downloaded": 5, "uploaded": 1,
                              "ratio": 0.2, "uploadSpeed": 10, "peers": 9,
                              "seeds": 4, "downloadSpeed": 2_500_000}])
    e = state.build(str(tmp_path), eng)["entries"][0]
    assert e["seeds"] == 4 and e["downloadSpeed"] == 2_500_000 and e["peers"] == 9


def test_missing_speed_fields_default_to_zero(tmp_path):
    """An older engine, or a torrent with no status yet, must not make the row render 'undefined'."""
    eng = FakeEngine(pinned=[{"infoHash": "ccdd", "name": "y", "progress": 0.0,
                              "state": "downloading"}])
    e = state.build(str(tmp_path), eng)["entries"][0]
    assert e["seeds"] == 0 and e["downloadSpeed"] == 0


def test_engine_pinned_status_reports_the_fields_the_ui_needs():
    """The engine dict is the contract state.build passes through; if a key is renamed there the
    UI silently shows zeros."""
    import inspect

    from stremiosrv.torrent.engine import Engine
    src = inspect.getsource(Engine.pinned_status)
    for key in ('"downloadSpeed"', '"seeds"', '"peers"', '"uploadSpeed"'):
        assert key in src, f"pinned_status no longer reports {key}"
    assert "st.download_rate" in src and "st.num_seeds" in src
