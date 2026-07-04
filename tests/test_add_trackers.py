"""Unit tests for Handle.add_trackers using a fake libtorrent handle (no C extension needed)."""
from stremiosrv.torrent.engine import Handle


class _Entry:
    def __init__(self, url):
        self.url = url


class _FakeHandle:
    def __init__(self, existing=()):
        self._trackers = [_Entry(u) for u in existing]
        self.added = []

    def trackers(self):
        return list(self._trackers)

    def add_tracker(self, d):
        self.added.append(d["url"])
        self._trackers.append(_Entry(d["url"]))


def test_add_trackers_only_new_ones():
    fh = _FakeHandle(existing=["udp://have/announce"])
    h = Handle(fh)
    added = h.add_trackers(["udp://have/announce", "udp://new/announce", "udp://new2/announce"])
    assert added == 2
    assert fh.added == ["udp://new/announce", "udp://new2/announce"]


def test_add_trackers_empty_is_noop():
    fh = _FakeHandle()
    assert Handle(fh).add_trackers([]) == 0
    assert fh.added == []


def test_add_trackers_survives_a_bad_url():
    class Bad(_FakeHandle):
        def add_tracker(self, d):
            if "bad" in d["url"]:
                raise RuntimeError("reject")
            super().add_tracker(d)

    fh = Bad()
    added = Handle(fh).add_trackers(["udp://bad/announce", "udp://ok/announce"])
    assert added == 1
    assert fh.added == ["udp://ok/announce"]
