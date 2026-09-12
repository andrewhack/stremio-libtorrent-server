"""The pure half of the Stremio addon: ids, manifest, and the payloads built from library state."""
from stremiosrv.library import addon_model as am
from stremiosrv.library import state as statemod

IH = "a1b2c3d4e5" * 4  # 40 hex chars
GB = 1073741824


def test_an_id_round_trips_with_and_without_a_file_index():
    assert am.parse_id(am.format_id(IH)) == (IH, None)
    assert am.parse_id(am.format_id(IH, 3)) == (IH, 3)


def test_ids_that_are_not_ours_or_are_malformed_are_refused():
    """parse_id is the validation boundary: an infohash from here reaches a path join, so anything
    that is not 40 hex characters must not get through."""
    assert am.parse_id("tt1234567") is None
    assert am.parse_id("stremiosrv:../../etc/passwd") is None
    assert am.parse_id("stremiosrv:" + "z" * 40) is None
    assert am.parse_id(f"stremiosrv:{IH}:notanumber") is None
    # str.isdigit() is true for these and int() then raises -- the boundary must return None,
    # not throw, or the route in front of it answers 500 where it should answer 404.
    assert am.parse_id(f"stremiosrv:{IH}:²") is None
    assert am.parse_id(f"stremiosrv:{IH}:③") is None


def test_the_manifest_declares_one_other_catalog_and_our_id_prefixes():
    m = am.manifest("9.9.9")
    assert m["id"] == "org.stremiosrv.library"
    assert m["version"] == "9.9.9"
    assert m["catalogs"] == [{"type": "other", "id": "library", "name": "My Library"}]
    by_name = {r["name"]: r for r in m["resources"]}
    assert by_name["catalog"]["types"] == ["other"]
    assert by_name["meta"]["idPrefixes"] == ["stremiosrv:"]
    assert set(by_name["stream"]["idPrefixes"]) == {"stremiosrv:", "tt"}


def test_the_manifest_is_not_the_stock_local_addon():
    """org.stremio.local ships pre-installed and flagged official in client profiles. Serving a
    different addon under that id would impersonate it."""
    assert am.manifest("1.0.0")["id"] != "org.stremio.local"


def _entry(**kw):
    """A state.build() entry with the fields the addon reads. Neutral names on purpose: this repo
    is public and carries no media titles."""
    e = {"infoHash": IH, "name": "Sample Title", "size": 4 * 1024 ** 3, "state": "seeding",
         "progress": 1.0, "pinned": False, "seeds": 7, "label": None}
    e.update(kw)
    return e


def test_a_labelled_title_uses_its_real_name_and_poster():
    e = _entry(label={"name": "Real Name", "poster": "https://example.invalid/p.jpg",
                      "type": "movie", "metaId": "tt0000001"})
    item = am.preview(e)
    assert item["id"] == am.format_id(IH)
    assert item["type"] == "other"
    assert item["name"] == "Real Name"
    assert item["poster"] == "https://example.invalid/p.jpg"


def test_an_unlabelled_title_falls_back_to_the_folder_name_and_has_no_poster():
    item = am.preview(_entry())
    assert item["name"] == "Sample Title"
    assert "poster" not in item


def test_a_download_in_progress_carries_its_percentage_in_the_name():
    """The row is a snapshot refreshed on reload, not a live bar -- so the number has to be in the
    text, where the app will redraw it."""
    item = am.preview(_entry(state="downloading", progress=0.4712))
    assert item["name"] == "Sample Title · 47%"
    assert am.preview(_entry(state="seeding", progress=1.0))["name"] == "Sample Title"


def test_the_description_reports_size_state_and_keeping():
    d = am.describe(_entry(pinned=True))
    assert "4.00 GB" in d
    assert "kept" in d
    assert "7 seeders" in d
    assert "seeding" in d


def test_the_description_names_the_state_a_row_is_actually_in():
    """Three words come out of one ternary and only one of them was ever asserted, so a swapped
    branch would have read the same to every test and wrong to every viewer."""
    assert "downloading" in am.describe(_entry(state="downloading", progress=0.5))
    assert "on disk" in am.describe(_entry(state="idle"))
    assert "seeding" in am.describe(_entry(state="seeding"))


def test_orphan_partfiles_and_entries_without_an_infohash_are_not_offered():
    """An orphan is leftover piece data with no torrent -- real disk, nothing to play."""
    state = {"entries": [
        _entry(),
        _entry(kind="orphan", name="incomplete download data (aaaaaaaa)"),
        _entry(infoHash=None, name="a folder we have no hash for"),
    ]}
    items = am.catalog(state)
    assert [i["name"] for i in items] == ["Sample Title"]


def test_parse_skip_reads_the_paging_offset_and_ignores_the_rest():
    """Stremio's paged grid appends the same page again at 100+ entries via `skip=<n>` in the extra
    segment, joined with `&` when other extra properties are present."""
    assert am.parse_skip("skip=100") == 100
    assert am.parse_skip("genre=Action&skip=50") == 50
    assert am.parse_skip("skip=50&genre=Action") == 50


def test_parse_skip_defaults_to_zero_when_absent_or_unparseable():
    """Ignore what is not understood rather than failing the whole row."""
    assert am.parse_skip("") == 0
    assert am.parse_skip("genre=Action") == 0
    assert am.parse_skip("skip=notanumber") == 0
    assert am.parse_skip("skip=-5") == 0


def test_catalog_slices_by_skip():
    state = {"entries": [_entry(infoHash="a" * 40, name="One"),
                         _entry(infoHash="b" * 40, name="Two"),
                         _entry(infoHash="c" * 40, name="Three")]}
    assert [i["name"] for i in am.catalog(state)] == ["One", "Two", "Three"]
    assert [i["name"] for i in am.catalog(state, skip=1)] == ["Two", "Three"]
    assert am.catalog(state, skip=10) == []


ORIGIN = "https://box.invalid:12470"


def test_a_stream_points_at_the_file_route_on_the_origin_it_was_asked_through():
    """Not a configured hostname: the app may reach us by IP, by name or through the appliance's
    own address, and a stream URL built from anything but the request would point somewhere the
    client cannot follow."""
    s = am.stream_for(_entry(), ORIGIN, file_idx=2)
    assert s["url"] == f"{ORIGIN}/{IH}/2"
    assert s["name"] == "My Library"
    assert "4.00 GB" in s["title"]
    assert s["behaviorHints"]["bingeGroup"] == f"stremiosrv:{IH}"


def test_the_played_file_is_the_one_the_download_asked_for():
    """`wantedFile` is the NAME the download was started for (engine.wanted_path/wanted_path()),
    never an index -- a shape that never occurs in production. Matching by name is what makes the
    smaller, wanted file win over the larger one here."""
    e = _entry(wantedFile="b.mkv",
               files=[{"index": 1, "name": "a.mkv", "size": 9000, "downloaded": 9000},
                      {"index": 4, "name": "b.mkv", "size": 5, "downloaded": 5}])
    assert am.playable_index(e) == 4


def test_the_wanted_file_is_not_offered_until_it_has_actually_arrived():
    """It is what the download was started for, whether or not any of it has arrived -- ranking by
    bytes downloaded must never override a recorded selection."""
    e = _entry(wantedFile="b.mkv",
               files=[{"index": 1, "name": "a.mkv", "size": 5000, "downloaded": 5000},
                      {"index": 4, "name": "b.mkv", "size": 900, "downloaded": 0}])
    # The selection still decides WHICH file this entry means -- it simply cannot be played yet,
    # and falling through to the other file would serve something nobody asked for.
    assert am.playable_index(e) is None


def test_the_wanted_file_matches_by_basename_even_when_one_side_carries_a_path():
    e = _entry(wantedFile="show.s01e02.mkv",
               files=[{"index": 1, "name": "show.s01e01.mkv", "size": 900, "downloaded": 900},
                      {"index": 2, "name": "subdir/show.s01e02.mkv", "size": 10,
                       "downloaded": 10}])
    assert am.playable_index(e) == 2


def test_without_a_wanted_file_the_biggest_addressable_file_wins():
    """Covers the engine-derived shape: real integer indices, never None. A pack with no recorded
    selection and both files fully on disk: the feature is the video, and the video is the big
    file."""
    e = _entry(files=[{"index": 1, "name": "sample.mkv", "size": 10, "downloaded": 10},
                      {"index": 7, "name": "feature.mkv", "size": 9000, "downloaded": 9000}])
    assert am.playable_index(e) == 7


def test_ranking_with_no_wanted_file_uses_bytes_downloaded_not_declared_size():
    """`meta_for` was already fixed to rank by `downloaded`; this is the same fix for
    `playable_index`. `size` is the torrent's declared size and is identical for a file at 0% and
    one that is finished -- it says nothing about what is actually on disk."""
    e = _entry(files=[{"index": 1, "name": "a.mkv", "size": 9000, "downloaded": 100},
                      {"index": 2, "name": "b.mkv", "size": 10, "downloaded": 5000}])
    assert am.playable_index(e) == 2


def test_a_file_the_engine_can_address_beats_one_it_cannot():
    e = _entry(files=[{"index": None, "name": "unaddressable.mkv", "size": 9000,
                       "downloaded": 9000},
                      {"index": 2, "name": "addressable.mkv", "size": 10, "downloaded": 10}])
    assert am.playable_index(e) == 2


def test_a_pack_recovered_from_disk_has_no_addressable_file():
    """state.py's disk fallback reports index None for every file: it lists what is on disk, not
    what the torrent says. Index 0 is not a safe guess for a pack -- on a real torrent index 0 was
    a text file and the video was index 1 -- so this offers nothing rather than the wrong thing."""
    e = _entry(files=[{"index": None, "name": "one.mkv", "size": 900},
                      {"index": None, "name": "two.mkv", "size": 800}])
    assert am.playable_index(e) is None
    assert am.stream_for(e, ORIGIN) is None


def test_with_no_file_list_at_all_it_falls_back_to_index_zero():
    assert am.playable_index(_entry()) == 0


def test_a_single_file_entry_without_an_index_still_plays_as_index_zero():
    e = _entry(files=[{"index": None, "name": "only.mkv", "size": 900, "downloaded": 900}])
    assert am.playable_index(e) == 0
    assert am.stream_for(e, ORIGIN)["url"] == f"{ORIGIN}/{IH}/0"


class _FakeEngine:
    """The exact three methods state.build calls on an engine -- see state._engine_view, which is
    the only place `build` ever touches its `engine` argument. Nothing else is implemented on
    purpose: a test double growing extra methods is how a fake quietly stops matching the real
    contract."""

    def __init__(self, names: dict, statuses: list[dict], live: dict | None = None) -> None:
        self._names = names
        self._statuses = statuses
        self._live = live or {}

    def name_to_hash(self) -> dict:
        return self._names

    def tracked_status(self) -> list[dict]:
        return self._statuses

    def live_files(self) -> dict:
        return self._live


def test_a_real_state_build_feeds_playable_index_a_pack_where_the_wanted_file_is_not_the_biggest(
        tmp_path):
    """The root-cause finding: every other test in this file hand-builds the entry dict, and one of
    those hand-built shapes (`wantedFile` as an int) never occurs in production -- which is exactly
    how the dead `isinstance(wantedFile, int)` branch survived. This drives the real
    state.build/_disk_files path against a temporary cache root instead, with a pack whose wanted
    file is the SMALLER of the two, so a ranking-by-size regression fails it just as surely as the
    dead int-branch would."""
    ih = "d" * 40
    name = "Pack"
    d = tmp_path / name
    d.mkdir()
    (d / "small.mkv").write_bytes(b"x" * 10)
    (d / "big.mkv").write_bytes(b"x" * 10)
    files = [
        {"index": 0, "name": "big.mkv", "size": 9_000_000_000, "downloaded": 9_000_000_000,
         "progress": 1.0, "wanted": False},
        {"index": 1, "name": "small.mkv", "size": 400_000_000, "downloaded": 400_000_000,
         "progress": 1.0, "wanted": True},
    ]
    status = {"infoHash": ih, "name": name, "pinned": False, "wantedFile": "small.mkv",
              "files": files, "numFiles": 2, "remaining": 0, "progress": 1.0, "state": "seeding",
              "downloaded": 9_400_000_000, "uploaded": 0, "ratio": 0.0, "uploadSpeed": 0,
              "downloadSpeed": 0, "peers": 0, "seeds": 0}
    engine = _FakeEngine(names={name: ih}, statuses=[status])

    state = statemod.build(str(tmp_path), engine)
    entry = am.find_entry(state, ih)
    assert entry is not None
    assert am.playable_index(entry) == 1


def test_a_series_label_matches_only_its_own_episode():
    state = {"entries": [
        _entry(label={"type": "series", "metaId": "tt0000002", "season": 1, "episode": 5,
                      "name": "Pack Name"}),
    ]}
    assert len(am.streams_for_meta_id(state, "tt0000002:1:5", ORIGIN)) == 1
    assert am.streams_for_meta_id(state, "tt0000002:1:6", ORIGIN) == []
    assert am.streams_for_meta_id(state, "tt0000002", ORIGIN) == []


def test_a_movie_label_matches_its_meta_id():
    state = {"entries": [_entry(label={"type": "movie", "metaId": "tt0000003", "name": "Film"})]}
    assert len(am.streams_for_meta_id(state, "tt0000003", ORIGIN)) == 1


def test_an_unlabelled_entry_can_never_match_a_meta_id():
    """The match key IS the label, so this is true by construction -- asserted so that a future
    'clever' fallback that guesses from the folder name fails here first."""
    assert am.streams_for_meta_id({"entries": [_entry()]}, "tt0000004", ORIGIN) == []


def test_a_pack_with_no_addressable_file_is_not_offered_for_a_meta_id():
    e = _entry(label={"type": "movie", "metaId": "tt0000005", "name": "Film"},
               files=[{"index": None, "name": "one.mkv", "size": 900},
                      {"index": None, "name": "two.mkv", "size": 800}])
    assert am.streams_for_meta_id({"entries": [e]}, "tt0000005", ORIGIN) == []


def test_a_pack_answers_for_every_episode_it_holds_not_just_the_labelled_one():
    """One infohash carries one label, and a season pack carries many episodes -- so matching the
    label's own episode number answered for exactly one of them. On a real box a nine-file pack
    with six episodes on disk offered a stream on one episode page and nothing on the other five.
    The episode is resolved from the pack's own file names instead, the same way the download path
    already picks an episode out of a pack."""
    e = _entry(
        label={"type": "series", "metaId": "tt0000010", "season": 1, "episode": 5, "name": "Pack"},
        files=[{"index": 3, "name": "Show.S01E05.mkv", "size": 4 * GB, "downloaded": 4 * GB,
                "progress": 1.0},
               {"index": 7, "name": "Show.S01E08.mkv", "size": 4 * GB, "downloaded": 4 * GB,
                "progress": 1.0}])
    state = {"entries": [e]}
    five = am.streams_for_meta_id(state, "tt0000010:1:5", ORIGIN)
    eight = am.streams_for_meta_id(state, "tt0000010:1:8", ORIGIN)
    assert len(five) == 1 and five[0]["url"].endswith("/3"), five
    assert len(eight) == 1 and eight[0]["url"].endswith("/7"), eight


def test_an_episode_the_pack_does_not_hold_is_not_offered():
    """Saying nothing is the honest answer: offering it would play a file with no bytes and start
    fetching it, which is not what "play the local copy" means."""
    e = _entry(
        label={"type": "series", "metaId": "tt0000010", "season": 1, "episode": 5, "name": "Pack"},
        files=[{"index": 3, "name": "Show.S01E05.mkv", "size": 4 * GB, "downloaded": 4 * GB,
                "progress": 1.0}])
    assert am.streams_for_meta_id({"entries": [e]}, "tt0000010:1:9", ORIGIN) == []


def test_an_episode_present_but_with_no_bytes_yet_is_not_offered():
    e = _entry(
        label={"type": "series", "metaId": "tt0000010", "season": 1, "episode": 5, "name": "Pack"},
        files=[{"index": 3, "name": "Show.S01E05.mkv", "size": 4 * GB, "downloaded": 4 * GB,
                "progress": 1.0},
               {"index": 7, "name": "Show.S01E08.mkv", "size": 4 * GB, "downloaded": 0,
                "progress": 0.0}])
    assert am.streams_for_meta_id({"entries": [e]}, "tt0000010:1:8", ORIGIN) == []


def test_boundary_spill_from_a_neighbour_is_not_an_episode():
    """The real shape this was written against: a nine-file pack where two episodes nobody asked
    for hold a few MB each, left behind because a piece straddles the boundary between files. The
    server reported them as present and the addon would have offered a stream that stalls on the
    first seek. `wanted`, 64 MiB, or 2% -- the same rule the library page draws the line with."""
    e = _entry(
        label={"type": "series", "metaId": "tt0000010", "season": 1, "episode": 5, "name": "Pack"},
        files=[{"index": 3, "name": "Show.S01E05.mkv", "size": 4 * GB, "downloaded": 4 * GB,
                "progress": 1.0},
               {"index": 0, "name": "Show.S01E01.mkv", "size": 4 * GB, "downloaded": 11_300_000,
                "progress": 0.003}])
    assert am.streams_for_meta_id({"entries": [e]}, "tt0000010:1:1", ORIGIN) == []
    assert len(am.streams_for_meta_id({"entries": [e]}, "tt0000010:1:5", ORIGIN)) == 1


def test_a_single_file_episode_still_matches_on_its_label_alone():
    """A one-file torrent whose name the episode patterns cannot read is why the label match has to
    stay: the file list can resolve nothing, and the label is all there is."""
    e = _entry(label={"type": "series", "metaId": "tt0000011", "season": 2, "episode": 4,
                      "name": "Episode"},
               files=[{"index": 0, "name": "some.release.name.mkv", "size": 4 * GB,
                       "downloaded": 4 * GB, "progress": 1.0}])
    state = {"entries": [e]}
    assert len(am.streams_for_meta_id(state, "tt0000011:2:4", ORIGIN)) == 1
    assert am.streams_for_meta_id(state, "tt0000011:2:5", ORIGIN) == []


def test_a_pack_never_answers_for_a_different_show():
    e = _entry(label={"type": "series", "metaId": "tt0000010", "season": 1, "episode": 5},
               files=[{"index": 3, "name": "Show.S01E05.mkv", "size": 4 * GB,
                       "downloaded": 4 * GB, "progress": 1.0}])
    assert am.streams_for_meta_id({"entries": [e]}, "tt0009999:1:5", ORIGIN) == []


def test_a_stream_for_one_episode_describes_that_episode_not_the_whole_pack():
    """The row read "24.74 GB" behind a 3.57 GB episode, because the title described the TORRENT.
    A season pack's size on a single episode's row is not a rounding error -- it is the wrong
    number entirely, and the one the viewer uses to judge what they are about to play."""
    e = _entry(
        size=24 * GB,
        label={"type": "series", "metaId": "tt0000010", "season": 1, "episode": 5, "name": "Pack"},
        files=[{"index": 3, "name": "Show.S01E05.mkv", "size": 4 * GB, "downloaded": 4 * GB,
                "progress": 1.0},
               {"index": 7, "name": "Show.S01E08.mkv", "size": 3 * GB, "downloaded": 3 * GB,
                "progress": 1.0}])
    s = am.streams_for_meta_id({"entries": [e]}, "tt0000010:1:8", ORIGIN)[0]
    assert "3.00 GB" in s["title"], s["title"]
    assert "24.00 GB" not in s["title"], s["title"]
    # and it names the file, so the row is recognisable next to every other source
    assert "Show.S01E08.mkv" in s["title"], s["title"]


def test_a_whole_torrent_stream_still_describes_the_torrent():
    """A film, or a title offered as a whole: there is no single file to describe, so the entry's
    own size is the right one."""
    s = am.stream_for(_entry(size=4 * GB), ORIGIN)
    assert "4.00 GB" in s["title"]


def test_an_episode_still_downloading_is_not_offered():
    """A partial file can be streamed -- the server fetches as it goes -- but a row saying "play the
    local copy" promises something it cannot keep: the first open stalls while the head and the
    index arrive, and the size on the row reads as what is already here. On a real box an episode at
    18% was offered, did not play, and played on a retry a minute later."""
    e = _entry(
        label={"type": "series", "metaId": "tt0000010", "season": 1, "episode": 5, "name": "Pack"},
        files=[{"index": 3, "name": "Show.S01E05.mkv", "size": 4 * GB, "downloaded": 4 * GB,
                "progress": 1.0},
               {"index": 8, "name": "Show.S01E09.mkv", "size": 4 * GB, "downloaded": 842_000_000,
                "progress": 0.18, "wanted": True}])
    state = {"entries": [e]}
    assert am.streams_for_meta_id(state, "tt0000010:1:9", ORIGIN) == []
    assert len(am.streams_for_meta_id(state, "tt0000010:1:5", ORIGIN)) == 1


def test_a_partly_downloaded_single_file_title_is_not_offered_either():
    e = _entry(files=[{"index": 0, "name": "film.mkv", "size": 4 * GB, "downloaded": 2 * GB,
                       "progress": 0.5, "wanted": True}])
    assert am.stream_for(e, ORIGIN) is None


def test_meta_lists_only_episodes_that_are_finished():
    """A video row whose stream request returns nothing is a dead end -- the two have to agree."""
    e = _entry(files=[{"index": 1, "name": "one.mkv", "size": 4 * GB, "downloaded": 4 * GB,
                       "progress": 1.0},
                      {"index": 2, "name": "two.mkv", "size": 4 * GB, "downloaded": 4 * GB,
                       "progress": 1.0},
                      {"index": 3, "name": "three.mkv", "size": 4 * GB, "downloaded": GB,
                       "progress": 0.25, "wanted": True}])
    ids = [v["id"] for v in am.meta_for(e)["videos"]]
    assert ids == [am.format_id(IH, 1), am.format_id(IH, 2)]


def test_meta_carries_the_name_poster_and_description():
    e = _entry(label={"name": "Real Name", "poster": "https://example.invalid/p.jpg"})
    m = am.meta_for(e)
    assert m["id"] == am.format_id(IH)
    assert m["type"] == "other"
    assert m["name"] == "Real Name"
    assert m["poster"] == "https://example.invalid/p.jpg"
    assert "4.00 GB" in m["description"]


def test_a_pack_lists_only_the_files_that_are_actually_on_disk():
    """The whole point of the library page's file view: a season folder holds the episodes you
    fetched, not the ones the torrent contains. `size` is the torrent's declared size and stays the
    same whether or not anything has arrived -- `downloaded` is the bytes actually on disk, the
    engine and disk shapes both carry it, and it is the one that says whether a file is really
    here."""
    e = _entry(files=[{"index": 1, "name": "one.mkv", "size": 900, "downloaded": 900,
                       "progress": 1.0},
                      {"index": 2, "name": "two.mkv", "size": 700, "downloaded": 0,
                       "progress": 0.0},
                      {"index": 3, "name": "three.mkv", "size": 500, "downloaded": 250,
                       "progress": 0.5},
                      {"index": 4, "name": "four.mkv", "size": 900, "downloaded": 900,
                       "progress": 1.0}])
    videos = am.meta_for(e)["videos"]
    # Not the untouched file, and not the half-finished one either: a row here has to have a
    # stream behind it, and only a finished file does.
    assert [v["id"] for v in videos] == [am.format_id(IH, 1), am.format_id(IH, 4)]
    assert videos[0]["title"] == "one.mkv"


def test_a_wanted_file_with_no_bytes_yet_is_not_treated_as_on_disk():
    """The review finding this covers: `size` is the torrent's declared size, present the moment
    metadata arrives and identical for a file at 0% and one that is finished -- it is not evidence
    that a single byte of it is on disk. Only one of these two files is really there, which is one
    short of the `> 1` a pack needs to be offered as a video list at all -- the honest outcome for a
    torrent that, in reality, still has only one playable file."""
    e = _entry(files=[{"index": 1, "name": "arrived.mkv", "size": 900, "downloaded": 900},
                      {"index": 2, "name": "requested.mkv", "size": 700, "downloaded": 0,
                       "wanted": True}])
    assert "videos" not in am.meta_for(e)


def test_files_recovered_from_disk_produce_no_video_rows():
    """state.py's disk fallback carries index None, and an id built from that is one parse_id
    refuses -- so those files cannot be offered as rows at all."""
    e = _entry(files=[{"index": None, "name": "one.mkv", "size": 900, "progress": 1.0},
                      {"index": None, "name": "two.mkv", "size": 800, "progress": 1.0}])
    assert "videos" not in am.meta_for(e)


def test_a_single_file_title_has_no_video_list():
    assert "videos" not in am.meta_for(_entry())


def test_find_entry_matches_case_insensitively_and_misses_cleanly():
    state = {"entries": [_entry()]}
    assert am.find_entry(state, IH.upper())["infoHash"] == IH
    assert am.find_entry(state, "b" * 40) is None


def test_a_file_still_downloading_gets_no_row_even_when_the_disk_says_it_is_whole(
        tmp_path, monkeypatch):
    """On ZFS a hole lookup on a file libtorrent is writing can answer "no holes" -- the whole
    file present -- while it is still arriving, and a row offered on that answer plays a file
    that is not there yet. The session's handle counts what has actually arrived, and the row
    goes by that: none while it arrives, one as soon as the handle says the file is whole."""
    from stremiosrv import cache as cachemod
    from stremiosrv.library import labels

    ih, name = "e" * 40, "Show.S01E05.1080p"
    d = tmp_path / name
    d.mkdir()
    (d / f"{name}.mkv").write_bytes(b"x" * 4096)
    monkeypatch.setattr(cachemod, "data_bytes", lambda path, st: st.st_size)  # "no holes"
    labels.put(str(tmp_path), ih, {"metaId": "tt0000011", "type": "series", "season": 1,
                                   "episode": 5})
    arriving = {"index": 0, "name": f"{name}.mkv", "size": 4096, "downloaded": 1024,
                "progress": 0.25, "wanted": True}

    state = statemod.build(str(tmp_path), _FakeEngine({name: ih}, [], live={ih: [arriving]}))
    assert am.streams_for_meta_id(state, "tt0000011:1:5", ORIGIN) == []

    whole = dict(arriving, downloaded=4096, progress=1.0)
    state = statemod.build(str(tmp_path), _FakeEngine({name: ih}, [], live={ih: [whole]}))
    assert len(am.streams_for_meta_id(state, "tt0000011:1:5", ORIGIN)) == 1
