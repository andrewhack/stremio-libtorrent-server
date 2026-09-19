"""Untracked torrents list their OWN files: indices from the live handle or the resume record.

A directory walk has no torrent file indices, and nothing at all for a single-file torrent at the
cache root, so the addon offered no episode of a pack and never learned a single-file release.
"""
from __future__ import annotations

import pytest
from bencode_helper import benc

from stremiosrv import cache as cachemod
from stremiosrv.library import addon_model as model
from stremiosrv.library import labels, torrentfiles
from stremiosrv.library import state as statemod

PACK_IH = "11" * 20
FILE_IH = "22" * 20
DIR_IH = "33" * 20
# The torrent's own order, as a real pack had it: the episode numbers are not the indices.
PACK = ["E06", "E02", "E03", "E04", "E05", "E01", "E07", "E08"]


def _size(ep: str) -> int:
    return 4000 + int(ep[1:]) * 10


def _record(root, ih, info):
    d = root / ".resume"
    d.mkdir(exist_ok=True)
    (d / f"{ih}.fastresume").write_bytes(benc({"info": info}))


def _index(root, **names):
    cachemod.save_name_index(str(root), {n: ih for n, ih in names.items()})


@pytest.fixture(autouse=True)
def _whole_files(monkeypatch):
    """Arrival is measured by holes on the box; here every file that exists is whole, unless a
    test says otherwise."""
    monkeypatch.setattr(cachemod, "data_bytes", lambda path, st: st.st_size)
    torrentfiles._read.cache_clear()


class _Eng:
    def __init__(self, names=None, live=None):
        self._names, self._live = names or {}, live or {}

    def name_to_hash(self):
        return self._names

    def tracked_status(self):
        return []

    def live_files(self):
        return self._live


def _pack(root, present=("E01", "E02", "E03", "E04")):
    name = "The Show S02"
    _record(root, PACK_IH, {"name": name, "files": [
        {"length": _size(ep), "path": [f"The Show S02 {ep}.mkv"]} for ep in PACK]})
    d = root / name
    d.mkdir()
    for ep in present:
        (d / f"The Show S02 {ep}.mkv").write_bytes(b"x" * _size(ep))
    _index(root, **{name: PACK_IH})
    return name


def _entry(root, engine=None):
    [e] = [e for e in statemod.build(str(root), engine)["entries"] if e.get("infoHash")]
    return e


def test_a_pack_lists_its_episodes_at_the_torrents_own_indices(tmp_path):
    _pack(tmp_path)
    e = _entry(tmp_path)
    got = {f["name"]: f["index"] for f in e["files"]}
    assert got == {"The Show S02 E01.mkv": 5, "The Show S02 E02.mkv": 1,
                   "The Show S02 E03.mkv": 2, "The Show S02 E04.mkv": 3}
    assert e["numFiles"] == 8
    assert e["filesFrom"] == "resume"
    assert all(f["wanted"] is False for f in e["files"])


def test_every_complete_episode_of_a_pack_is_offered_at_its_own_index(tmp_path):
    _pack(tmp_path)
    labels.put(str(tmp_path), PACK_IH, {"metaId": "tt0000001", "type": "series",
                                        "season": 2, "episode": 1, "videoId": "tt0000001:2:1"})
    state = statemod.build(str(tmp_path), None)
    want = {1: 5, 2: 1, 3: 2, 4: 3}
    for ep in range(1, 9):
        urls = [s["url"] for s in model.streams_for_meta_id(state, f"tt0000001:2:{ep}", "http://o")]
        assert urls == ([f"http://o/{PACK_IH}/{want[ep]}"] if ep in want else []), ep


def _root_file(root, name="The.Show.S04E05.1080p.mkv", size=6000):
    _record(root, FILE_IH, {"name": name, "length": size})
    (root / name).write_bytes(b"x" * size)
    _index(root, **{name: FILE_IH})
    return name, size


def test_a_single_file_at_the_root_is_listed_as_the_torrent_itself(tmp_path):
    name, size = _root_file(tmp_path)
    e = _entry(tmp_path)
    assert [(f["index"], f["name"], f["size"]) for f in e["files"]] == [(0, name, size)]
    assert e["numFiles"] == 1
    assert e["filesFrom"] == "resume"
    assert "children" not in e  # the entry already names its one file


def test_a_single_file_release_is_learned_from_its_size_and_name(tmp_path):
    name, size = _root_file(tmp_path)
    state = statemod.build(str(tmp_path), None)
    hits = model.learn_labels(state, "series", "tt0000002:4:5",
                              {"videoSize": str(size), "filename": name})
    assert [ih for ih, _ in hits] == [FILE_IH]


def test_a_single_file_release_is_offered_only_when_complete(tmp_path, monkeypatch):
    _root_file(tmp_path)
    labels.put(str(tmp_path), FILE_IH, {"metaId": "tt0000002", "type": "series", "season": 4,
                                        "episode": 5, "videoId": "tt0000002:4:5"})
    streams = model.streams_for_meta_id(statemod.build(str(tmp_path), None),
                                        "tt0000002:4:5", "http://o")
    assert [s["url"] for s in streams] == [f"http://o/{FILE_IH}/0"]
    monkeypatch.setattr(cachemod, "data_bytes", lambda path, st: st.st_size // 2)
    assert model.streams_for_meta_id(statemod.build(str(tmp_path), None),
                                     "tt0000002:4:5", "http://o") == []


def test_a_folder_whose_first_file_is_not_a_video_offers_the_videos_own_index(tmp_path):
    name = "The.Show.S04E04.2160p"
    _record(tmp_path, DIR_IH, {"name": name, "files": [
        {"length": 115, "path": ["Visit us.url"]},
        {"length": 7000, "path": ["The.Show.S04E04.2160p.mkv"]}]})
    d = tmp_path / name
    d.mkdir()
    (d / "Visit us.url").write_bytes(b"x" * 115)
    (d / "The.Show.S04E04.2160p.mkv").write_bytes(b"x" * 7000)
    _index(tmp_path, **{name: DIR_IH})
    assert model.playable_index(_entry(tmp_path)) == 1


def test_a_file_the_session_holds_is_counted_by_its_handle_matched_on_index(tmp_path,
                                                                             monkeypatch):
    """With a resume record the listing is the torrent's own. A file the session holds is counted
    by its handle -- the disk must not be asked about a file being written (see _disk_files) --
    matched on its index, which is exact; every other file is measured on the disk."""
    name = _pack(tmp_path)
    measured = []
    monkeypatch.setattr(cachemod, "data_bytes",
                        lambda path, st: measured.append(path) or st.st_size)
    live = [{"index": 5, "name": "The Show S02 E01.mkv", "size": _size("E01"),
             "downloaded": 10, "progress": 0.0025, "wanted": True}]
    e = _entry(tmp_path, _Eng({name: PACK_IH}, {PACK_IH: live}))
    got = {f["index"]: f["downloaded"] for f in e["files"]}
    assert got == {5: 10, 1: _size("E02"), 2: _size("E03"), 3: _size("E04")}
    assert all(f["wanted"] is False for f in e["files"])
    assert not any(p.endswith("E01.mkv") for p in measured)
    assert e["numFiles"] == 8 and e["filesFrom"] == "resume"


def test_a_brand_new_single_file_is_listed_from_its_handle(tmp_path):
    """No resume record exists until the engine's next save, and a single file at the root has no
    directory to walk: the session's own record is the listing, so the first play learns it."""
    name, size = "The.Show.S04E05.1080p.mkv", 6000
    (tmp_path / name).write_bytes(b"x" * size)
    live = [{"index": 0, "name": name, "size": size, "downloaded": 600, "progress": 0.1,
             "wanted": True}]
    state = statemod.build(str(tmp_path), _Eng({name: FILE_IH}, {FILE_IH: live}))
    [e] = [e for e in state["entries"] if e.get("infoHash")]
    assert e["files"] == [dict(live[0], wanted=False)]
    assert e["numFiles"] == 0 and e["filesFrom"] == "disk"
    hits = model.learn_labels(state, "series", "tt0000002:4:5",
                              {"videoSize": str(size), "filename": name})
    assert [ih for ih, _ in hits] == [FILE_IH]


def test_no_record_and_no_live_list_is_the_walk_as_before(tmp_path):
    name = "Walked.Folder"
    (tmp_path / name).mkdir()
    (tmp_path / name / "a.mkv").write_bytes(b"x" * 10)
    _index(tmp_path, **{name: PACK_IH})
    e = _entry(tmp_path)
    assert [f["index"] for f in e["files"]] == [None]
    assert e["filesFrom"] == "disk"


def test_a_broken_record_falls_back_to_the_walk(tmp_path):
    name = _pack(tmp_path)
    (tmp_path / ".resume" / f"{PACK_IH}.fastresume").write_bytes(b"d4:info")
    e = _entry(tmp_path)
    assert e["filesFrom"] == "disk"
    assert {f["index"] for f in e["files"]} == {None}
    assert name == e["name"]


def test_a_file_of_another_length_is_not_listed(tmp_path):
    name = _pack(tmp_path, present=("E01",))
    (tmp_path / name / "The Show S02 E01.mkv").write_bytes(b"x" * 5)  # not the torrent's length
    assert _entry(tmp_path)["files"] == []
