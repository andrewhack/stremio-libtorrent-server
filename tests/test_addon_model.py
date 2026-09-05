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
