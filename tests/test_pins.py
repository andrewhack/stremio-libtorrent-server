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


# --- applying a pin's file choice --------------------------------------------------------------
# The choice is recorded at pin time, when a magnet has no file list, so it has to be applied
# later. The first attempt keyed that off metadata_received_alert -- which is in the
# status_notification category, and the session's default alert mask is error-only (measured on a
# live box: mask=1, status bit=64, off). The alert never arrived, the choice was never applied,
# and a whole season pack downloaded for one episode.

def test_a_deferred_choice_is_applied_once_metadata_arrives():
    from stremiosrv.torrent.engine import Engine

    class FakeHandle:
        def __init__(self):
            self.meta = False
            self.only = None

        def has_metadata(self):
            return self.meta

        def file_paths(self):
            return ["Show.S01E01.mkv", "Show.S01E02.mkv", "Show.S01E03.mkv"]

        def want_file(self, idx):
            self.only = idx

    class FakeEngine:
        _apply_wanted = Engine._apply_wanted
        _apply_pending_wanted = Engine._apply_pending_wanted

        def __init__(self, h):
            self._wanted = {"aa": [{"fileIdx": 1}]}
            self._wanted_applied = set()
            self._torrents = {"aa": h}

        def _full_priority(self, h):
            raise AssertionError("wanted one file, not all of them")

    h = FakeHandle()
    e = FakeEngine(h)

    e._apply_pending_wanted()
    assert h.only is None and not e._wanted_applied, "applied before the file list existed"

    h.meta = True
    e._apply_pending_wanted()
    assert h.only == 1 and e._wanted_applied == {"aa"}

    h.only = None
    e._apply_pending_wanted()
    assert h.only is None, "re-applied a choice already made; the sweep must settle"


def test_the_choice_is_not_driven_by_an_alert_that_never_arrives():
    """Guards the regression directly: the deferred apply must not depend on an alert category the
    session does not subscribe to. It is driven from the alerts loop's own cadence instead."""
    import pathlib

    from stremiosrv.torrent import engine as eng
    src = pathlib.Path(eng.__file__).read_text(encoding="utf-8")
    # The NAME may appear -- a comment explaining why not to use it is exactly what should stay.
    # What must not come back is the dependency: resolving the alert type and branching on it.
    assert "_META_ALERT" not in src, "back to an alert the default mask filters out"
    assert 'getattr(lt, "metadata_received_alert"' not in src
    assert "len(self._wanted_applied) < len(self._wanted)" in src, "nothing drives the apply"


def test_a_narrowed_pin_reports_complete_when_its_one_file_is_done():
    """A pin narrowed to one file leaves the rest at priority 0, so the torrent is never a full
    seed. Keyed off is_seeding, such a download reported "downloading" for ever — progress 1.0,
    state downloading, stuck in the Downloading shelf and never readable as complete. It is the
    same is_finished-vs-is_seeding trap should_stop_seeding already documents.
    """
    import pathlib

    from stremiosrv.torrent import engine as eng
    src = pathlib.Path(eng.__file__).read_text(encoding="utf-8")
    body = src[src.index("def pinned_status"):src.index("def add(", src.index("def pinned_status"))]
    assert '"state": "seeding" if h.is_finished() else "downloading"' in body
    assert "st.is_seeding" not in body, "back to whole-torrent seeding; a narrowed pin never is one"


def test_playing_another_file_does_not_un_want_the_kept_one():
    """Two files of one torrent are wanted at once: someone downloading an episode while someone
    else streams another. A single wanted index could only resolve that by un-wanting one of them,
    which sets it to priority 0 -- and libtorrent puts a priority-0 file's data in the partfile
    rather than the file, so un-wanting does not merely stop a download, it throws it away.
    """
    from stremiosrv.torrent.engine import IDLE_FILE_PRIO, Handle

    class RawHandle:
        def __init__(self):
            self.prios = None

        def torrent_file(self):
            class FS:
                def num_files(self):
                    return 5
            class TI:
                def files(self):
                    return FS()
            return TI()

        def prioritize_files(self, prios):
            self.prios = list(prios)

        def set_sequential_download(self, on):
            pass

    raw = RawHandle()
    h = Handle.__new__(Handle)
    h._h = raw
    h.pinned = False          # a download is NOT a pin any more
    h.wanted = {3}            # the episode someone is downloading
    h._focused_idx = None
    h._read_pos = h._read_total = 0
    h._prefetched = set()
    h._active = 0

    h.focus_file(1)           # somebody else plays a DIFFERENT episode of the same torrent
    assert raw.prios[3] == IDLE_FILE_PRIO, "playing another file un-wanted the one being fetched"
    assert raw.prios[1] == IDLE_FILE_PRIO
    assert raw.prios[0] == 0 and raw.prios[4] == 0
    assert h.wanted == {1, 3}, "playing a file must make it wanted too, exactly as downloading does"


def test_an_old_download_pin_is_migrated_out_of_the_pin_registry(tmp_path):
    """Downloads used to pin. Those records carry a `want`, and loading one as a pin would mean the
    WHOLE torrent -- re-fetching every file of a pack that had been narrowed to one episode, and
    handing it an eviction exemption the owner never asked for. Move them to the wanted registry.
    """
    import json as _json

    from stremiosrv import pins as pinsmod
    from stremiosrv import wanted as wantedmod
    from stremiosrv.torrent.engine import Engine

    root = str(tmp_path)
    pinsmod.save_pins(root, [
        {"infoHash": "a" * 40, "name": "", "trackers": [], "want": {"fileIdx": 4}},
        {"infoHash": "b" * 40, "name": "kept-on-purpose", "trackers": []},
    ])

    eng = Engine.__new__(Engine)
    eng._cache_root = root
    eng._torrents = {}
    eng._wanted, eng._wanted_applied = {}, set()
    eng.add = lambda ih, trackers=None: type("H", (), {"pinned": False})()
    eng._apply_pending_wanted = lambda: None
    Engine.load_pins_into_session(eng)

    assert eng._pinned == {"b" * 40}, "a download stayed pinned across the upgrade"
    assert wantedmod.load(root) == {"a" * 40: [{"fileIdx": 4}]}
    left = {e["infoHash"] for e in _json.loads((tmp_path / "pins.json").read_text())}
    assert left == {"b" * 40}, "the download was left in the pin registry"


def test_keeping_an_episode_does_not_start_fetching_its_whole_season():
    """Pinning means "do not evict this". Expanding the wanted set turned a click that promised to
    protect one episode into a fetch of the entire pack -- tens of gigabytes, and the card jumped
    from Downloaded back to Downloading. A whole-title pin still means the whole torrent; a pin on
    something already narrowed keeps exactly what was narrowed.
    """
    from stremiosrv.torrent.engine import IDLE_FILE_PRIO, Handle

    class RawHandle:
        def __init__(self):
            self.prios = None

        def torrent_file(self):
            class FS:
                def num_files(self):
                    return 4
            class TI:
                def files(self):
                    return FS()
            return TI()

        def prioritize_files(self, prios):
            self.prios = list(prios)

    raw = RawHandle()
    h = Handle.__new__(Handle)
    h._h = raw
    h.wanted = {2}
    h._focused_idx = None
    h._active = 0

    h.pinned = True
    h.reapply_priorities()
    assert raw.prios == [0, 0, IDLE_FILE_PRIO, 0], "keeping one episode wanted the whole pack"

    # Nothing selected: a whole-title pin still means every file, as the appliance's pin does.
    h.wanted = set()
    h.reapply_priorities()
    assert raw.prios == [1, 1, 1, 1]
