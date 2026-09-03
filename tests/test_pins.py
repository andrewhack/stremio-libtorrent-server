import json

from stremiosrv import cache, pins
from stremiosrv.cache import load_name_index, save_name_index


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


def test_name_index_roundtrip(tmp_path):
    mapping = {"movie.mkv": "deadbeef01", "show.mkv": "cafebabe02"}
    save_name_index(str(tmp_path), mapping)
    assert load_name_index(str(tmp_path)) == mapping


def test_name_index_missing_returns_empty(tmp_path):
    assert load_name_index(str(tmp_path)) == {}


def test_pin_fits_truth_table():
    # cache_size=1000 -> R=1100
    # free 5000, no existing pins, candidate needs 3000 -> 5000-3000=2000 >= 1100 -> fits
    assert pins.pin_fits(5000, 0, 3000, 1000) is True
    # free 5000, existing pins need 3000, candidate needs 1000 -> 5000-4000=1000 < 1100 -> no
    assert pins.pin_fits(5000, 3000, 1000, 1000) is False
    # candidate alone too big
    assert pins.pin_fits(2000, 0, 1500, 1000) is False


def test_pinning_marks_every_file_wanted_not_just_every_piece():
    """libtorrent stores pieces belonging to a file whose FILE priority is 0 in the
    `.<infohash>.parts` holding file rather than the real file. A torrent that has been streamed
    once has every other file at 0 (focus_file's `base`), so pinning it while only raising PIECE
    priorities downloaded gigabytes into the partfile and left the directory empty — 30 GB of one
    on a real box, with nothing playable on disk.
    """
    import inspect

    from stremiosrv.torrent.engine import Engine, Handle
    src = inspect.getsource(Engine._full_priority)
    assert "want_all_files" in src, "_full_priority no longer raises FILE priorities"

    want = inspect.getsource(Handle.want_all_files)
    assert "prioritize_files" in want
    assert "_focused_idx = None" in want, "focus_file would short-circuit and skip re-applying"
    # Files must be set BEFORE pieces: prioritize_files overwrites every piece priority.
    assert want.index("prioritize_files") < inspect.getsource(Engine._full_priority).index(
        "prioritize_pieces") + len(want)


# --- which file a pin actually wants ---------------------------------------------------------
# Picking one episode used to download the whole pack: /api/download never carried a file index,
# and pin() called want_all_files(). A season pack is ~39 GB for a 4.5 GB episode.

from stremiosrv.pins import select_wanted_file  # noqa: E402

SEASON_PACK = [
    "Show.S01E01.Title.1080p.WEB-DL.mkv",
    "Show.S01E02.Title.1080p.WEB-DL.mkv",
    "Show.S01E05.Worldless.1080p.WEB-DL.mkv",
    "Show.S01E09.Title.1080p.WEB-DL.mkv",
    "NEW upcoming releases.txt",
]


def test_no_want_means_every_file():
    assert select_wanted_file(SEASON_PACK, None) is None
    assert select_wanted_file(SEASON_PACK, {}) is None


def test_explicit_file_index_wins():
    assert select_wanted_file(SEASON_PACK, {"fileIdx": 3, "season": 1, "episode": 5}) == 3


def test_out_of_range_index_falls_through_to_matching():
    assert select_wanted_file(SEASON_PACK, {"fileIdx": 99, "season": 1, "episode": 5}) == 2


def test_season_and_episode_pick_the_one_file():
    assert select_wanted_file(SEASON_PACK, {"season": 1, "episode": 5}) == 2
    assert select_wanted_file(SEASON_PACK, {"season": 1, "episode": 1}) == 0


def test_episode_numbers_do_not_bleed_into_each_other():
    """S01E1 must not match S01E10 — off-by-a-digit here silently downloads the wrong episode."""
    pack = ["Show.S01E10.mkv", "Show.S01E01.mkv"]
    assert select_wanted_file(pack, {"season": 1, "episode": 1}) == 1
    assert select_wanted_file(pack, {"season": 1, "episode": 10}) == 0


def test_alternate_numbering_style():
    assert select_wanted_file(["Show 1x05 Title.mkv", "Show 1x06 Title.mkv"],
                              {"season": 1, "episode": 5}) == 0


def test_non_video_matches_are_not_preferred():
    """Packs carry .nfo/.srt/.txt named after the episode; the video is what was asked for."""
    pack = ["Show.S01E05.nfo", "Show.S01E05.srt", "Show.S01E05.mkv"]
    assert select_wanted_file(pack, {"season": 1, "episode": 5}) == 2


def test_no_match_means_every_file():
    """A film in a folder, or a pack that names episodes some other way: better to fetch it all
    than to fetch nothing and leave the owner with an empty directory."""
    assert select_wanted_file(["Some.Film.2019.1080p.mkv"], {"season": 1, "episode": 5}) is None
