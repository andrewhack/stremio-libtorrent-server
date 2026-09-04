from stremiosrv import cache as cachemod
from stremiosrv.library import labels, session, state


class FakeEngine:
    def __init__(self, pinned=(), names=None):
        self._pinned = list(pinned)
        self._names = names or {}

    def name_to_hash(self):
        return self._names

    def tracked_status(self):
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
        pinned=[{"infoHash": "aabb", "pinned": True, "name": "some-download", "progress": 0.5,
                 "state": "downloading", "downloaded": 5, "uploaded": 1, "ratio": 0.2,
                 "uploadSpeed": 10, "peers": 3}],
    )
    e = state.build(str(tmp_path), eng)["entries"][0]
    assert e["pinned"] is True and e["progress"] == 0.5 and e["peers"] == 3


def test_pinned_torrent_with_no_files_yet_still_appears(tmp_path):
    """A download just started has a pin but nothing on disk. Omitting it makes the UI look like the
    click did nothing."""
    eng = FakeEngine(pinned=[{"infoHash": "ccdd", "pinned": True, "name": "starting", "progress": 0.0,
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
        def tracked_status(self):
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


def test_orphan_partfile_is_visible_and_reclaimable(tmp_path):
    """A partfile whose torrent is gone still occupies disk — one on a real box held 30 GB — so
    hiding it would recreate the invisible-disk problem this view exists to prevent. It must also
    be removable, or that space cannot be reclaimed from the UI at all: /library/api/remove stops
    the torrent before deleting, and an orphan has no torrent in the session to corrupt."""
    _partfile(tmp_path, "bb" * 20, 2048)
    entries = state.build(str(tmp_path), None)["entries"]
    assert len(entries) == 1
    assert entries[0]["removable"] is True
    assert entries[0]["infoHash"] == "bb" * 20
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
    eng = FakeEngine(pinned=[{"infoHash": "aabb", "pinned": True, "name": "x", "progress": 0.5,
                              "state": "downloading", "downloaded": 5, "uploaded": 1,
                              "ratio": 0.2, "uploadSpeed": 10, "peers": 9,
                              "seeds": 4, "downloadSpeed": 2_500_000}])
    e = state.build(str(tmp_path), eng)["entries"][0]
    assert e["seeds"] == 4 and e["downloadSpeed"] == 2_500_000 and e["peers"] == 9


def test_missing_speed_fields_default_to_zero(tmp_path):
    """An older engine, or a torrent with no status yet, must not make the row render 'undefined'."""
    eng = FakeEngine(pinned=[{"infoHash": "ccdd", "pinned": True, "name": "y", "progress": 0.0,
                              "state": "downloading"}])
    e = state.build(str(tmp_path), eng)["entries"][0]
    assert e["seeds"] == 0 and e["downloadSpeed"] == 0


def test_engine_pinned_status_reports_the_fields_the_ui_needs():
    """The engine dict is the contract state.build passes through; if a key is renamed there the
    UI silently shows zeros."""
    import inspect

    from stremiosrv.torrent.engine import Engine
    src = inspect.getsource(Engine._status_for)
    for key in ('"downloadSpeed"', '"seeds"', '"peers"', '"uploadSpeed"'):
        assert key in src, f"the tracked-torrent status no longer reports {key}"
    assert "st.download_rate" in src and "st.num_seeds" in src


def test_an_idle_pack_lists_the_episodes_on_its_disk(tmp_path):
    """Only PINS are re-added to the session at startup, so after any restart the rest of the cache
    has no handle -- and per-file facts came only from a handle. A season pack then collapsed into
    one card carrying the pack's name and the whole directory's size, saying nothing about which
    episode is actually there. Restarting is ordinary: changing a setting on the appliance does it.
    """
    d = tmp_path / "Show.S01.COMPLETE.1080p"
    d.mkdir()
    (d / "Show.S01E05.1080p.mkv").write_bytes(b"x" * 4096)
    (d / "Show.S01E06.1080p.mkv").write_bytes(b"y" * 2048)
    (d / "readme.nfo").write_bytes(b"not a video")
    cachemod.save_name_index(str(tmp_path), {"Show.S01.COMPLETE.1080p": "d" * 40})
    e = state.build(str(tmp_path), None)["entries"][0]
    assert e["filesFrom"] == "disk"
    assert [f["name"] for f in e["files"]] == ["Show.S01E05.1080p.mkv", "Show.S01E06.1080p.mkv"]
    # a card per episode, so the owner can see WHICH ones are here
    assert [k["name"] for k in e["children"]] == ["Show.S01E05.1080p.mkv", "Show.S01E06.1080p.mkv"]


def test_one_episode_of_a_pack_still_gets_its_own_card(tmp_path):
    """The entry is named after the TORRENT. With a single episode on disk and no children, the
    card showed the season pack's name and nothing saying which episode it held -- which is what
    the owner was looking at when they reported this."""
    d = tmp_path / "Show.S01.COMPLETE.1080p"
    d.mkdir()
    (d / "Show.S01E05.1080p.mkv").write_bytes(b"x" * 4096)
    e = state.build(str(tmp_path), None)["entries"][0]
    assert [k["name"] for k in e["children"]] == ["Show.S01E05.1080p.mkv"]


def test_the_engines_own_list_wins_over_the_disk(tmp_path):
    """A handle knows what is WANTED as well as what is present, so it is always the better
    answer. Reading both and merging them would be two sources for one fact."""
    d = tmp_path / "Show.S01.COMPLETE.1080p"
    d.mkdir()
    (d / "Show.S01E05.1080p.mkv").write_bytes(b"x" * 4096)
    eng = FakeEngine(
        names={"Show.S01.COMPLETE.1080p": "e" * 40},
        pinned=[{"infoHash": "e" * 40, "numFiles": 9, "files": [
            {"index": 5, "name": "Show.S01E06.1080p.mkv", "size": 10, "downloaded": 0,
             "progress": 0.0, "wanted": True}]}],
    )
    e = state.build(str(tmp_path), eng)["entries"][0]
    assert e["filesFrom"] == "engine"
    assert [f["name"] for f in e["files"]] == ["Show.S01E06.1080p.mkv"]


def test_an_entry_carries_the_torrents_files_and_its_file_count(tmp_path):
    """`children` is the subset worth drawing as a card -- it drops boundary spill, and it is not
    built at all for a pack with a single episode in flight. The release list needs the facts
    underneath it: which files this torrent holds or wants, and how many it has in all. Without
    the count, one file in the list is ambiguous -- a film, or a pack with one episode selected.
    """
    _seed_cache(tmp_path, "Some.Pack")
    eng = FakeEngine(
        names={"Some.Pack": "b" * 40},
        pinned=[{"infoHash": "b" * 40, "pinned": False, "progress": 0.3, "numFiles": 9,
                 "files": [{"index": 4, "name": "S01E05.mkv", "size": 10, "downloaded": 3,
                            "progress": 0.3, "wanted": True}]}],
    )
    e = state.build(str(tmp_path), eng)["entries"][0]
    assert e["numFiles"] == 9
    assert [f["index"] for f in e["files"]] == [4]
    # one file, no scraps: no cards to draw, and the facts are still there
    assert "children" not in e


def test_a_download_with_nothing_on_disk_yet_still_carries_its_files(tmp_path):
    """This is the moment the release list matters most: the click has happened, the bytes have
    not arrived, and the button for the NEXT episode must not inherit this one's state."""
    eng = FakeEngine(pinned=[{"infoHash": "c" * 40, "state": "downloading", "numFiles": 9,
                              "files": [{"index": 4, "name": "S01E05.mkv", "size": 10,
                                         "downloaded": 0, "progress": 0.0, "wanted": True}]}])
    e = state.build(str(tmp_path), eng)["entries"][0]
    assert e["numFiles"] == 9 and [f["index"] for f in e["files"]] == [4]


def test_a_pack_holding_several_episodes_lists_them(tmp_path):
    """One entry per torrent could not account for a pack: download one episode through the
    library, let the player stream another, and the second one is invisible while its bytes sit on
    the disk. The parent entry stays -- removal is still per-torrent, because these share one
    handle and one directory -- but it now carries what it actually holds.
    """
    from stremiosrv.library import state as st

    class Eng:
        def name_to_hash(self):
            return {"Some.Pack": "a" * 40}

        def tracked_status(self):
            return [{
                "infoHash": "a" * 40, "pinned": False, "progress": 0.4, "state": "downloading",
                "name": "Some.Pack",
                "files": [
                    {"index": 4, "name": "Show.S01E05.mkv", "size": 4_000_000_000,
                     "downloaded": 2_400_000_000, "progress": 0.6, "wanted": True},
                    {"index": 5, "name": "Show.S01E06.mkv", "size": 4_000_000_000,
                     "downloaded": 747_000_000, "progress": 0.19, "wanted": True},
                    {"index": 7, "name": "Show.S01E08.mkv", "size": 4_000_000_000,
                     "downloaded": 0, "progress": 0.0, "wanted": False},
                ],
            }]

    d = tmp_path / "Some.Pack"
    d.mkdir()
    (d / "payload").write_bytes(b"x" * 32)
    out = st.build(str(tmp_path), Eng(), budget=1)
    entry = next(e for e in out["entries"] if e["name"] == "Some.Pack")
    kids = entry.get("children") or []
    assert [k["name"] for k in kids] == ["Show.S01E05.mkv", "Show.S01E06.mkv"], \
        "a file with bytes on disk was left unaccounted for"
    assert [k["size"] for k in kids] == [2_400_000_000, 747_000_000], "sizes must be per file"
    assert all(k["removable"] is False for k in kids), \
        "removing one file would have to stop the torrent the others are read from"


def test_a_single_file_torrent_is_not_split(tmp_path):
    """Most torrents are one file. Splitting those would double every card for nothing."""
    from stremiosrv.library import state as st

    class Eng:
        def name_to_hash(self):
            return {"One.Film": "b" * 40}

        def tracked_status(self):
            return [{"infoHash": "b" * 40, "pinned": True, "progress": 1.0, "state": "seeding",
                     "name": "One.Film",
                     "files": [{"index": 0, "name": "One.Film.mkv", "size": 5, "downloaded": 5,
                                "progress": 1.0, "wanted": True}]}]

    d = tmp_path / "One.Film"
    d.mkdir()
    (d / "payload").write_bytes(b"x" * 8)
    out = st.build(str(tmp_path), Eng(), budget=1)
    entry = next(e for e in out["entries"] if e["name"] == "One.Film")
    assert "children" not in entry


def test_boundary_spill_is_summarised_not_listed_as_episodes(tmp_path):
    """A piece straddles the boundary between two files, so fetching one leaves kilobytes of its
    neighbours behind. Listing those as episodes tells the owner they have six when they have two
    -- the same lie as hiding the real ones, told the other way round. They are still counted,
    because unattributed disk is the thing this view exists to stop.
    """
    from stremiosrv.library import state as st

    def f(idx, name, got, size=4_000_000_000, wanted=False):
        return {"index": idx, "name": name, "size": size, "downloaded": got,
                "progress": round(got / size, 4), "wanted": wanted}

    class Eng:
        def name_to_hash(self):
            return {"Pack": "a" * 40}

        def tracked_status(self):
            return [{"infoHash": "a" * 40, "pinned": False, "progress": 1.0, "state": "seeding",
                     "name": "Pack", "files": [
                         f(0, "Show.S01E01.mkv", 16_384),          # boundary spill
                         f(3, "Show.S01E04.mkv", 4_100_000),       # boundary spill
                         f(4, "Show.S01E05.mkv", 4_000_000_000, wanted=True),
                         f(5, "Show.S01E06.mkv", 4_000_000_000),
                         f(6, "Show.S01E07.mkv", 256_500_000),     # a real partial: prefetch
                         f(7, "Show.S01E08.mkv", 192_000),         # boundary spill
                     ]}]

    d = tmp_path / "Pack"
    d.mkdir()
    (d / "payload").write_bytes(b"x" * 8)
    out = st.build(str(tmp_path), Eng(), budget=1)
    entry = next(e for e in out["entries"] if e["name"] == "Pack")
    assert [k["name"] for k in entry["children"]] == [
        "Show.S01E05.mkv", "Show.S01E06.mkv", "Show.S01E07.mkv"]
    assert entry["scraps"] == {"count": 3, "size": 16_384 + 4_100_000 + 192_000}


def test_a_download_just_started_is_never_called_a_scrap(tmp_path):
    """It is wanted, so it is listed however little has arrived -- otherwise clicking Download
    makes something appear only minutes later, which is how this looked before any of it existed.
    """
    from stremiosrv.library import state as st

    class Eng:
        def name_to_hash(self):
            return {"Pack": "b" * 40}

        def tracked_status(self):
            return [{"infoHash": "b" * 40, "pinned": False, "progress": 0.0, "state": "downloading",
                     "name": "Pack", "files": [
                         {"index": 0, "name": "a.mkv", "size": 4_000_000_000,
                          "downloaded": 4_000_000_000, "progress": 1.0, "wanted": False},
                         {"index": 1, "name": "b.mkv", "size": 4_000_000_000,
                          "downloaded": 900_000, "progress": 0.0002, "wanted": True},
                     ]}]

    d = tmp_path / "Pack"
    d.mkdir()
    (d / "payload").write_bytes(b"x" * 8)
    out = st.build(str(tmp_path), Eng(), budget=1)
    entry = next(e for e in out["entries"] if e["name"] == "Pack")
    assert [k["name"] for k in entry["children"]] == ["a.mkv", "b.mkv"]
    assert "scraps" not in entry


def test_the_budget_reports_what_downloads_have_already_claimed(tmp_path):
    """Bytes a download still owes the disk are invisible to both `df` and the cache total, so the
    page cannot judge the next download without being told."""
    from stremiosrv.library import state as st

    class Eng:
        def name_to_hash(self):
            return {}

        def tracked_status(self):
            return [{"infoHash": "a" * 40, "pinned": False, "progress": 0.4,
                     "state": "downloading", "name": "One", "remaining": 2_500_000_000},
                    {"infoHash": "b" * 40, "pinned": True, "progress": 1.0,
                     "state": "seeding", "name": "Two", "remaining": 0}]

    out = st.build(str(tmp_path), Eng(), budget=1)
    assert out["budget"]["committed"] == 2_500_000_000
