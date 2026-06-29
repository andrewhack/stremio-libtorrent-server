import json

from stremiosrv import cache, pins


def test_protected_includes_resume_and_pins():
    assert ".resume" in cache.PROTECTED
    assert "pins.json" in cache.PROTECTED


def test_save_and_load_pins_roundtrip(tmp_path):
    entries = [{"infoHash": "abc", "name": "x.iso", "trackers": ["udp://t"], "addedAt": 1}]
    pins.save_pins(str(tmp_path), entries)
    assert json.loads((tmp_path / "pins.json").read_text()) == entries
    assert pins.load_pins(str(tmp_path)) == entries
    assert pins.pinned_hashes(str(tmp_path)) == {"abc"}


def test_load_pins_missing_returns_empty(tmp_path):
    assert pins.load_pins(str(tmp_path)) == []
    assert pins.pinned_hashes(str(tmp_path)) == set()


def test_headroom_is_cache_size_plus_10_percent():
    assert pins.headroom(1000) == 1100


def test_pin_fits_truth_table():
    # cache_size=1000 -> R=1100
    # free 5000, no existing pins, candidate needs 3000 -> 5000-3000=2000 >= 1100 -> fits
    assert pins.pin_fits(5000, 0, 3000, 1000) is True
    # free 5000, existing pins need 3000, candidate needs 1000 -> 5000-4000=1000 < 1100 -> no
    assert pins.pin_fits(5000, 3000, 1000, 1000) is False
    # candidate alone too big
    assert pins.pin_fits(2000, 0, 1500, 1000) is False
