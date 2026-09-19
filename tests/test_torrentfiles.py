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


@pytest.mark.parametrize("bad", [[b".."], [b"."], [b""], [b"a/b"], [b"a\\b"]])
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


def test_listing_rereads_a_rewritten_record(tmp_path):
    path = write_record(tmp_path, {"name": "A.mkv", "length": 1})
    assert tf.listing(str(tmp_path), IH).files[0].size == 1
    write_record(tmp_path, {"name": "B.mkv", "length": 22})
    st = os.stat(path)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    assert tf.listing(str(tmp_path), IH).files[0].size == 22
