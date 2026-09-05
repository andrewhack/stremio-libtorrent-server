"""The pure half of the Stremio addon: ids, manifest, and the payloads built from library state."""
from stremiosrv.library import addon_model as am

IH = "a1b2c3d4e5" * 4  # 40 hex chars


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
    e = _entry(wantedFile=4, files=[{"index": 1, "name": "a.mkv", "size": 10},
                                    {"index": 4, "name": "b.mkv", "size": 5}])
    assert am.playable_index(e) == 4


def test_without_a_wanted_file_the_biggest_addressable_file_wins():
    """Covers the engine-derived shape: real integer indices, never None. A pack with no recorded
    selection: the feature is the video, and the video is the big file."""
    e = _entry(files=[{"index": 1, "name": "sample.mkv", "size": 10},
                      {"index": 7, "name": "feature.mkv", "size": 9000}])
    assert am.playable_index(e) == 7


def test_a_file_the_engine_can_address_beats_one_it_cannot():
    e = _entry(files=[{"index": None, "name": "unaddressable.mkv", "size": 9000},
                      {"index": 2, "name": "addressable.mkv", "size": 10}])
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
    e = _entry(files=[{"index": None, "name": "only.mkv", "size": 900}])
    assert am.playable_index(e) == 0
    assert am.stream_for(e, ORIGIN)["url"] == f"{ORIGIN}/{IH}/0"


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
