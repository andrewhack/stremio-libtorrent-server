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


def test_orphan_partfiles_and_entries_without_an_infohash_are_not_offered():
    """An orphan is leftover piece data with no torrent -- real disk, nothing to play."""
    state = {"entries": [
        _entry(),
        _entry(kind="orphan", name="incomplete download data (aaaaaaaa)"),
        _entry(infoHash=None, name="a folder we have no hash for"),
    ]}
    items = am.catalog(state)
    assert [i["name"] for i in items] == ["Sample Title"]
