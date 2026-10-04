"""Engine.pin's disk guard measures what the pin will fetch, and never admits a pin it could not measure.

A pinned torrent is never evicted, so this guard is all that stands between a pin and a full disk. It
measured `total_wanted - total_done`: 0 for a magnet whose metadata has not arrived, so the guard
always passed; and for a torrent nobody had narrowed, only the part streaming had wanted so far --
while the pin then switches on every file.
"""
from __future__ import annotations

import types

import pytest

from stremiosrv import pins as pinsmod
from stremiosrv.torrent import engine as engmod
from stremiosrv.torrent.engine import Engine, PinSizeUnknownError, PinSpaceError

GiB = 1024 ** 3
IH = "a" * 40


class FakeHandle:
    """The slice of engine.Handle that pin() reads."""

    def __init__(self, *, metadata=True, arrives_after=None, total_size=0, wanted=(),
                 total_wanted=0, total_wanted_done=0, total_done=0):
        self._metadata = metadata
        self._arrives_after = arrives_after  # has_metadata() polls until it arrives; None = never
        self._total_size = total_size
        self.wanted = set(wanted)
        self.pinned = False
        self._st = types.SimpleNamespace(total_wanted=total_wanted,
                                         total_wanted_done=total_wanted_done, total_done=total_done)

    def has_metadata(self):
        if not self._metadata and self._arrives_after is not None:
            self._arrives_after -= 1
            self._metadata = self._arrives_after <= 0
        return self._metadata

    def status(self):
        return self._st

    def torrent_file(self):
        if not self._metadata:
            return None
        return types.SimpleNamespace(total_size=lambda: self._total_size)

    def name(self):
        return "fixture"

    def reapply_priorities(self):
        pass


def _engine(tmp_path, monkeypatch, handle, *, free, cache_size=10 * GiB):
    eng = Engine.__new__(Engine)  # no libtorrent session: pin() needs only these
    eng._cache_root = str(tmp_path)
    eng._cache_size = cache_size
    eng._torrents = {IH: handle}
    eng._pinned = set()
    eng._pin_metadata_wait = 0.5
    eng.get = lambda ih: eng._torrents.get(ih.lower())
    eng._full_priority = lambda h: None
    eng.save_all_resume = lambda: None
    monkeypatch.setattr(engmod.shutil, "disk_usage", lambda p: types.SimpleNamespace(free=free))
    return eng


def test_a_pin_whose_size_cannot_be_known_is_refused_not_admitted(tmp_path, monkeypatch):
    eng = _engine(tmp_path, monkeypatch, FakeHandle(metadata=False), free=100 * GiB)
    with pytest.raises(PinSizeUnknownError):
        eng.pin(IH)
    assert eng._pinned == set()
    assert pinsmod.load_pins(str(tmp_path)) == []


def test_a_pin_waits_for_metadata_that_arrives_and_then_measures_it(tmp_path, monkeypatch):
    """95 GiB on a disk with 100 free leaves less than the 11 GiB headroom a 10 GiB cache needs."""
    h = FakeHandle(metadata=False, arrives_after=3, total_size=95 * GiB)
    eng = _engine(tmp_path, monkeypatch, h, free=100 * GiB)
    with pytest.raises(PinSpaceError):
        eng.pin(IH)
    assert eng._pinned == set()


def test_an_unnarrowed_pin_is_measured_whole(tmp_path, monkeypatch):
    """Streaming wanted 1 GiB of it; the pin is about to want all 95."""
    h = FakeHandle(total_size=95 * GiB, total_wanted=1 * GiB)
    eng = _engine(tmp_path, monkeypatch, h, free=100 * GiB)
    with pytest.raises(PinSpaceError):
        eng.pin(IH)


def test_a_narrowed_pin_is_measured_by_its_selection(tmp_path, monkeypatch):
    """Keeping one episode of a 95 GiB pack keeps that episode, so 3 GiB is what it still needs."""
    h = FakeHandle(total_size=95 * GiB, wanted={2}, total_wanted=4 * GiB,
                   total_wanted_done=1 * GiB, total_done=1 * GiB)
    eng = _engine(tmp_path, monkeypatch, h, free=100 * GiB)
    eng.pin(IH)
    assert eng._pinned == {IH}


def test_a_refusal_reports_everything_the_pin_needs(tmp_path, monkeypatch):
    """`needed` was the headroom alone, so a refusal could read "needs 11 GB, 100 GB free"."""
    h = FakeHandle(total_size=95 * GiB, total_done=5 * GiB)
    eng = _engine(tmp_path, monkeypatch, h, free=100 * GiB)
    with pytest.raises(PinSpaceError) as e:
        eng.pin(IH)
    assert e.value.needed == pinsmod.headroom(10 * GiB) + 90 * GiB
    assert e.value.free == 100 * GiB


# --- the reserve beside a pin: only the room the cache can still grow into (plus 10% slack) ---

def _warm(monkeypatch, used):
    """The cache already holds `used` bytes, measured the way the evictor measures it."""
    monkeypatch.setattr(engmod.cachemod, "scan_cache", lambda root: [{"name": "x", "size": used}])


def test_a_warm_cache_does_not_have_to_stay_free_beside_a_pin(tmp_path, monkeypatch):
    """The reported case: a 30 GB budget, the cache near it, 26 GB free, and a 3 GB film refused
    with "needs 30.7 GB". The cache's bytes are already on the disk and pinned bytes count against
    the same budget, so reserving the whole budget again counted them twice."""
    _warm(monkeypatch, 28 * GiB)
    h = FakeHandle(total_size=3 * GiB)
    eng = _engine(tmp_path, monkeypatch, h, free=26 * GiB, cache_size=30 * GiB)
    eng.pin(IH)
    assert eng._pinned == {IH}


def test_a_cold_cache_keeps_its_room_to_grow(tmp_path, monkeypatch):
    """Nothing cached yet: the cache can still grow by its whole budget, so the reserve is what
    it always was."""
    _warm(monkeypatch, 0)
    h = FakeHandle(total_size=3 * GiB)
    eng = _engine(tmp_path, monkeypatch, h, free=26 * GiB, cache_size=30 * GiB)
    with pytest.raises(PinSpaceError) as e:
        eng.pin(IH)
    assert e.value.needed == pinsmod.headroom(30 * GiB) + 3 * GiB


def test_room_the_cache_already_uses_is_not_reserved_twice(tmp_path, monkeypatch):
    _warm(monkeypatch, 20 * GiB)
    h = FakeHandle(total_size=3 * GiB)
    eng = _engine(tmp_path, monkeypatch, h, free=15 * GiB, cache_size=30 * GiB)
    with pytest.raises(PinSpaceError) as e:
        eng.pin(IH)
    assert e.value.needed == pinsmod.headroom(30 * GiB) - 20 * GiB + 3 * GiB


def test_a_pin_that_would_fill_the_disk_is_still_refused(tmp_path, monkeypatch):
    """However full the cache already is, the 10% slack stays: a pin that exactly fills the disk
    leaves nothing for the evictor's lag, a stream being watched, or transcode segments."""
    _warm(monkeypatch, 40 * GiB)
    h = FakeHandle(total_size=5 * GiB)
    eng = _engine(tmp_path, monkeypatch, h, free=5 * GiB, cache_size=30 * GiB)
    with pytest.raises(PinSpaceError):
        eng.pin(IH)
    assert eng._pinned == set()
