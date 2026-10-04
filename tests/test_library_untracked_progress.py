"""A title the library does not track still says how much of it is here, and whether it is arriving.

state.build gave every untracked entry progress 1.0 and state "idle", on the reasoning that nothing
was downloading it. Since titles are labelled at playback an untracked title can be "ours", so a
film the player was 45% into sat on the Downloaded shelf reading "complete, cached".
"""
from __future__ import annotations

import types

from bencode_helper import benc

from stremiosrv import cache as cachemod
from stremiosrv.library import state as statemod
from stremiosrv.library import torrentfiles

IH = "33" * 20
NAME = "Film.2026.1080p.mkv"
SIZE = 10_000


def _record(root, info):
    (root / ".resume").mkdir(exist_ok=True)
    (root / ".resume" / f"{IH}.fastresume").write_bytes(benc({"info": info}))
    torrentfiles._read.cache_clear()


def _film(root, monkeypatch, have):
    _record(root, {"name": NAME, "length": SIZE})
    (root / NAME).write_bytes(b"x" * SIZE)
    cachemod.save_name_index(str(root), {NAME: IH})
    monkeypatch.setattr(cachemod, "data_bytes", lambda path, st: have)


class _Eng:
    def __init__(self, held=None):
        self._held = held or {}

    def name_to_hash(self):
        return {NAME: IH}

    def tracked_status(self):
        return []

    def live_files(self):
        return {}

    def held_status(self):
        return self._held


def _entry(root, engine=None):
    [e] = [e for e in statemod.build(str(root), engine)["entries"] if e.get("infoHash")]
    return e


def test_an_untracked_title_half_on_disk_is_not_complete(tmp_path, monkeypatch):
    _film(tmp_path, monkeypatch, SIZE // 2)
    e = _entry(tmp_path)
    assert e["progress"] == 0.5
    assert e["state"] == "idle"


def test_an_untracked_title_all_on_disk_is_complete(tmp_path, monkeypatch):
    _film(tmp_path, monkeypatch, SIZE)
    assert _entry(tmp_path)["progress"] == 1.0


def test_a_title_the_session_is_still_fetching_is_streaming(tmp_path, monkeypatch):
    _film(tmp_path, monkeypatch, SIZE // 2)
    held = {IH: {"state": "downloading", "progress": 0.45, "downloadSpeed": 8_000_000,
                 "uploadSpeed": 0, "peers": 12, "seeds": 9, "playing": True}}
    e = _entry(tmp_path, _Eng(held))
    assert e["state"] == "streaming"
    assert e["progress"] == 0.45
    assert (e["downloadSpeed"], e["seeds"], e["peers"], e["playing"]) == (8_000_000, 9, 12, True)


def test_a_title_the_session_has_finished_is_idle_and_complete(tmp_path, monkeypatch):
    _film(tmp_path, monkeypatch, SIZE)
    held = {IH: {"state": "seeding", "progress": 1.0, "downloadSpeed": 0, "uploadSpeed": 5,
                 "peers": 3, "seeds": 1, "playing": False}}
    e = _entry(tmp_path, _Eng(held))
    assert (e["state"], e["progress"]) == ("idle", 1.0)
    assert e["uploadSpeed"] == 5


def test_spill_from_neighbouring_files_does_not_make_a_finished_episode_partial(tmp_path,
                                                                                monkeypatch):
    """Fetching one episode leaves kilobytes of its neighbours behind. Counting those would make a
    complete episode read as a fraction of the season for ever."""
    folder = "The.Show.S04"
    files = ["The.Show.S04E04.mkv", "The.Show.S04E05.mkv", "The.Show.S04E06.mkv"]
    _record(tmp_path, {"name": folder,
                       "files": [{"length": SIZE, "path": [f]} for f in files]})
    d = tmp_path / folder
    d.mkdir()
    for f in files:
        (d / f).write_bytes(b"x" * SIZE)
    cachemod.save_name_index(str(tmp_path), {folder: IH})
    monkeypatch.setattr(cachemod, "data_bytes",
                        lambda path, st: st.st_size if "E05" in path else st.st_size // 100)
    assert _entry(tmp_path)["progress"] == 1.0


def test_the_engine_reports_what_it_holds_but_does_not_track():
    from stremiosrv.torrent.engine import Engine

    class H:
        def __init__(self, finished=False, active=False, meta=True):
            self._finished, self._active, self._meta = finished, active, meta

        def has_metadata(self):
            return self._meta

        def status(self):
            return types.SimpleNamespace(progress=0.4512, download_rate=8_000_000,
                                         upload_rate=0, num_peers=12, num_seeds=9)

        def is_finished(self):
            return self._finished

        def is_active(self):
            return self._active

    eng = Engine.__new__(Engine)
    eng._torrents = {"a" * 40: H(active=True), "b" * 40: H(finished=True),
                     "c" * 40: H(meta=False), "d" * 40: H(active=True), "e" * 40: H()}
    eng._pinned = {"d" * 40}
    eng._wanted = {"e" * 40: [{}]}
    got = eng.held_status()
    assert set(got) == {"a" * 40, "b" * 40}
    assert got["a" * 40] == {"state": "downloading", "progress": 0.4512,
                             "downloadSpeed": 8_000_000, "uploadSpeed": 0, "peers": 12,
                             "seeds": 9, "playing": True}
    assert got["b" * 40]["state"] == "seeding" and got["b" * 40]["playing"] is False


def test_every_title_reads_its_own_session_state_not_only_the_first(tmp_path, monkeypatch):
    """build() walks every title in one loop, and the session map was once named like a list the
    pack branch reassigns further down -- so from the second title on it read that list instead."""
    pack, pack_ih = "The.Show.S01", "44" * 20
    episodes = ["The.Show.S01E01.mkv", "The.Show.S01E02.mkv"]
    (tmp_path / ".resume").mkdir()
    (tmp_path / ".resume" / f"{pack_ih}.fastresume").write_bytes(
        benc({"info": {"name": pack, "files": [{"length": SIZE, "path": [f]} for f in episodes]}}))
    (tmp_path / pack).mkdir()
    for f in episodes:
        (tmp_path / pack / f).write_bytes(b"x" * SIZE)
    _film(tmp_path, monkeypatch, SIZE // 2)
    cachemod.save_name_index(str(tmp_path), {pack: pack_ih, NAME: IH})
    monkeypatch.setattr(cachemod, "data_bytes", lambda path, st: st.st_size // 2)
    torrentfiles._read.cache_clear()

    class Eng(_Eng):
        def name_to_hash(self):
            return {pack: pack_ih, NAME: IH}

    held = {IH: {"state": "downloading", "progress": 0.5, "downloadSpeed": 1, "uploadSpeed": 0,
                 "peers": 1, "seeds": 1, "playing": True}}
    entries = {e["infoHash"]: e for e in statemod.build(str(tmp_path), Eng(held))["entries"]}
    assert entries[IH]["state"] == "streaming"
    assert entries[pack_ih]["state"] == "idle" and entries[pack_ih]["progress"] == 0.5
