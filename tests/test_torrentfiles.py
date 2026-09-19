"""The resume-record reader: a torrent's own file list, without libtorrent."""
from __future__ import annotations

import os

import pytest
from bencode_helper import benc

from stremiosrv.library import torrentfiles as tf

IH = "ab" * 20


def write_record(root, info, ih=IH) -> str:
    d = root / ".resume"
    d.mkdir(exist_ok=True)
    path = d / f"{ih}.fastresume"
    path.write_bytes(benc({"info": info, "name": info.get("name", "x")}))
    return str(path)


def test_bdecode_round_trips():
    value = {b"a": [1, -2, b"x", {b"k": b""}], b"n": 0}
    assert tf.bdecode(benc(value)) == value


@pytest.mark.parametrize("raw", [
    b"i12", b"i-0e", b"i03e", b"ie", b"5:ab", b"l1:a", b"di1e1:ae", b"x", b"i1ee",
    b"d1:a",
])
def test_bdecode_rejects_malformed(raw):
    with pytest.raises(ValueError):
        tf.bdecode(raw)


def test_bdecode_rejects_deep_nesting():
    with pytest.raises(ValueError):
        tf.bdecode(b"l" * 100 + b"e" * 100)


def test_single_file_is_index_zero_with_no_path():
    got = tf.parse_info({b"name": b"Film.mkv", b"length": 1234})
    assert got == tf.Listing(1, (tf.TorrentFile(0, (), 1234),))


def test_multi_file_keeps_the_torrents_order():
    info = {b"name": b"Pack", b"files": [
        {b"length": 30, b"path": [b"S01E03.mkv"]},
        {b"length": 10, b"path": [b"Sub", b"S01E01.mkv"]},
        {b"length": 20, b"path": [b"S01E02.mkv"]},
    ]}
    got = tf.parse_info(info)
    assert got.count == 3
    assert [(f.index, f.parts, f.size) for f in got.files] == [
        (0, ("S01E03.mkv",), 30), (1, ("Sub", "S01E01.mkv"), 10),
        (2, ("S01E02.mkv",), 20)]


def test_a_pad_file_keeps_its_index_and_is_not_listed():
    info = {b"name": b"P", b"files": [
        {b"length": 5, b"path": [b"a.mkv"]},
        {b"length": 7, b"path": [b".pad", b"7"], b"attr": b"p"},
        {b"length": 9, b"path": [b"b.mkv"]},
    ]}
    got = tf.parse_info(info)
    assert got.count == 3
    assert [f.index for f in got.files] == [0, 2]


def test_a_symlink_keeps_its_index_and_is_not_listed():
    info = {b"name": b"P", b"files": [
        {b"length": 5, b"path": [b"a.mkv"]},
        {b"length": 0, b"path": [b"link.mkv"], b"attr": b"l", b"symlink path": [b"a.mkv"]},
        {b"length": 9, b"path": [b"b.mkv"]},
    ]}
    got = tf.parse_info(info)
    assert got.count == 3
    assert [f.index for f in got.files] == [0, 2]


def test_files_win_over_length_as_in_libtorrent():
    info = {b"name": b"P", b"length": 99, b"files": [{b"length": 5, b"path": [b"a.mkv"]}]}
    assert tf.parse_info(info) == tf.Listing(1, (tf.TorrentFile(0, ("a.mkv",), 5),))


@pytest.mark.parametrize("bad", [[b".."], [b"."], [b""], [b"a/b"], [b"a\\b"], [b"a\x00b.mkv"]])
def test_an_unsafe_path_drops_that_file(bad):
    info = {b"name": b"P", b"files": [
        {b"length": 5, b"path": bad}, {b"length": 9, b"path": [b"ok.mkv"]}]}
    got = tf.parse_info(info)
    assert [f.parts for f in got.files] == [("ok.mkv",)]
    assert got.count == 2


def test_utf8_path_is_preferred():
    info = {b"name": b"P", b"files": [
        {b"length": 5, b"path": [b"legacy.mkv"], b"path.utf-8": [b"proper.mkv"]}]}
    assert tf.parse_info(info).files[0].parts == ("proper.mkv",)


def test_a_utf8_path_that_is_not_a_list_is_ignored():
    info = {b"name": b"P", b"files": [
        {b"length": 5, b"path": [b"legacy.mkv"], b"path.utf-8": b"not-a-list"}]}
    assert tf.parse_info(info).files[0].parts == ("legacy.mkv",)


@pytest.mark.parametrize("info", [
    {b"name": b"v2", b"file tree": {}},
    {b"name": b"P", b"files": [{b"path": [b"a"]}]},
    {b"name": b"P", b"files": [{b"length": 1, b"path": b"a"}]},
    b"not a dict",
])
def test_unreadable_info_is_none(info):
    assert tf.parse_info(info) is None


def test_listing_reads_the_record(tmp_path):
    write_record(tmp_path, {"name": "Film.mkv", "length": 99})
    assert tf.listing(str(tmp_path), IH) == tf.Listing(1, (tf.TorrentFile(0, (), 99),))


def test_listing_is_none_without_a_record_or_with_a_broken_one(tmp_path):
    assert tf.listing(str(tmp_path), IH) is None
    (tmp_path / ".resume").mkdir()
    (tmp_path / ".resume" / f"{IH}.fastresume").write_bytes(b"d4:info")
    assert tf.listing(str(tmp_path), IH) is None


def test_a_listing_is_kept_while_the_engine_rewrites_the_record(tmp_path):
    """An info dict never changes -- the infohash is its hash -- but the engine rewrites the record
    around it every 30 s. Keyed on the record's version, the cache kept a copy per rewrite."""
    tf._read.cache_clear()
    path = write_record(tmp_path, {"name": "A.mkv", "length": 1})
    first = tf.listing(str(tmp_path), IH)
    for n in range(1, 4):
        with open(path, "wb") as f:
            f.write(benc({"info": {"name": "A.mkv", "length": 1}, "total_uploaded": n}))
        st = os.stat(path)
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + n * 1_000_000_000))
        assert tf.listing(str(tmp_path), IH) is first
    assert tf._read.cache_info().currsize == 1


def test_a_record_not_readable_yet_is_tried_again(tmp_path):
    """Missing or half-written now is not missing for good: the engine saves every 30 s."""
    tf._read.cache_clear()
    assert tf.listing(str(tmp_path), IH) is None
    (tmp_path / ".resume").mkdir()
    (tmp_path / ".resume" / f"{IH}.fastresume").write_bytes(b"d4:info")
    assert tf.listing(str(tmp_path), IH) is None
    write_record(tmp_path, {"name": "A.mkv", "length": 1})
    assert tf.listing(str(tmp_path), IH) == tf.Listing(1, (tf.TorrentFile(0, (), 1),))


def test_a_record_over_the_size_cap_is_not_read(tmp_path, monkeypatch):
    monkeypatch.setattr(tf, "_MAX_RECORD_BYTES", 10)
    monkeypatch.setattr(tf, "bdecode", lambda buf: pytest.fail("a record over the cap was read"))
    write_record(tmp_path, {"name": "A.mkv", "length": 1})
    assert tf.listing(str(tmp_path), IH) is None


def test_an_info_dict_that_cannot_be_listed_is_kept_as_an_empty_listing(tmp_path):
    """It never changes either. Decoded again on every build, a large v2-only record cost 0.7 s a
    time; kept as an empty listing, it is answered with the walk at no cost."""
    tf._read.cache_clear()
    write_record(tmp_path, {"name": "v2", "file tree": {}})
    first = tf.listing(str(tmp_path), IH)
    assert first == tf.Listing(0, ())
    assert tf.listing(str(tmp_path), IH) is first
    assert tf._read.cache_info().currsize == 1


def test_a_record_without_an_info_dict_yet_is_tried_again(tmp_path):
    """A torrent still fetching its metadata saves a record without an info dict."""
    tf._read.cache_clear()
    (tmp_path / ".resume").mkdir()
    (tmp_path / ".resume" / f"{IH}.fastresume").write_bytes(benc({"name": "x"}))
    assert tf.listing(str(tmp_path), IH) is None
    write_record(tmp_path, {"name": "A.mkv", "length": 1})
    assert tf.listing(str(tmp_path), IH) == tf.Listing(1, (tf.TorrentFile(0, (), 1),))
