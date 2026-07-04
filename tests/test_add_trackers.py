"""Unit tests for Handle.add_trackers using a fake libtorrent handle (no C extension needed).

The fake mirrors libtorrent 2.0: torrent_handle.trackers() returns a list of dicts, and
add_tracker() takes a dict — verified against real lt 2.0.11 on the build host.
"""
from stremiosrv.torrent.engine import Handle


class _FakeHandle:
    def __init__(self, existing=()):
        self._urls = list(existing)
        self.added = []

    def trackers(self):
        return [{"url": u, "tier": 0} for u in self._urls]

    def add_tracker(self, d):
        self.added.append(d["url"])
        self._urls.append(d["url"])


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
