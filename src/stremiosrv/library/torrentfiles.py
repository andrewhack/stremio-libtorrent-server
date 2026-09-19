"""A torrent's own file list, read from the resume record the engine keeps for it.

The engine saves each torrent's resume data with its info dict (`save_info_dict`) as
`<cache_root>/.resume/<infohash>.fastresume`, and the record outlives the session: after a
restart only kept torrents are loaded again, yet every cached torrent still has one. The info
dict is the torrent's own file list -- each file's index, path and length, in the order
libtorrent numbers them. A directory listing cannot give that: the disk knows names and sizes,
not which index an addon's `fileIdx` or a player's `/<infohash>/<idx>` means, and a pack's
files are seldom in episode order.

Decoded here rather than with libtorrent, so the library needs no engine to answer and the
unit suite runs without the binding.
"""
from __future__ import annotations

import functools
import os
from typing import NamedTuple

from stremiosrv import cache as cachemod

_MAX_DEPTH = 64


class TorrentFile(NamedTuple):
    index: int              # libtorrent's index for the file: its position in the info dict
    parts: tuple[str, ...]  # the path below the torrent's name; () for a single-file torrent
    size: int


class Listing(NamedTuple):
    count: int              # the torrent's own file count, pad files included, as libtorrent counts
    files: tuple[TorrentFile, ...]


def bdecode(buf: bytes):
    """One bencoded value spanning the whole buffer. ValueError on anything else."""
    value, pos = _decode(buf, 0, 0)
    if pos != len(buf):
        raise ValueError("trailing data after the bencoded value")
    return value


def _decode(buf: bytes, pos: int, depth: int):
    if depth > _MAX_DEPTH:
        raise ValueError("bencode nested too deep")
    head = buf[pos:pos + 1]
    if head == b"i":
        end = buf.index(b"e", pos)
        digits = buf[pos + 1:end]
        if (not digits.lstrip(b"-").isdigit() or digits.startswith(b"-0")
                or (digits.startswith(b"0") and len(digits) > 1)):
            raise ValueError("bad bencode integer")
        return int(digits), end + 1
    if head == b"l":
        items, pos = [], pos + 1
        while buf[pos:pos + 1] != b"e":
            item, pos = _decode(buf, pos, depth + 1)
            items.append(item)
        return items, pos + 1
    if head == b"d":
        mapping, pos = {}, pos + 1
        while buf[pos:pos + 1] != b"e":
            key, pos = _decode(buf, pos, depth + 1)
            if not isinstance(key, bytes):
                raise ValueError("bencode dictionary key is not a string")
            mapping[key], pos = _decode(buf, pos, depth + 1)
        return mapping, pos + 1
    if head.isdigit():
        colon = buf.index(b":", pos)
        length = int(buf[pos:colon])
        start = colon + 1
        if start + length > len(buf):
            raise ValueError("bencode string runs past the end")
        return buf[start:start + length], start + length
    raise ValueError("truncated or unknown bencode value")


def _safe(part: str) -> bool:
    """A path part that stays below the torrent's directory."""
    return (bool(part) and part not in (".", "..")
            and "/" not in part and "\\" not in part)


def parse_info(info) -> Listing | None:
    """The file list of a v1 info dict, or None for anything else -- a v2-only one included.

    A pad file keeps its slot in the numbering, because libtorrent counts it, and is not
    listed. A file with a path part that could leave the torrent's directory is not listed
    either.
    """
    if not isinstance(info, dict):
        return None
    if isinstance(info.get(b"length"), int):
        return Listing(1, (TorrentFile(0, (), info[b"length"]),))
    files = info.get(b"files")
    if not isinstance(files, list):
        return None
    out = []
    for index, f in enumerate(files):
        if not isinstance(f, dict) or not isinstance(f.get(b"length"), int):
            return None
        attr = f.get(b"attr")
        if isinstance(attr, bytes) and b"p" in attr:
            continue
        raw = f.get(b"path.utf-8", f.get(b"path"))
        if (not isinstance(raw, list) or not raw
                or not all(isinstance(p, bytes) for p in raw)):
            return None
        parts = tuple(p.decode("utf-8", "replace") for p in raw)
        if all(_safe(p) for p in parts):
            out.append(TorrentFile(index, parts, f[b"length"]))
    return Listing(len(files), tuple(out))


def resume_path(cache_root: str, info_hash: str) -> str:
    return os.path.join(cache_root, cachemod.RESUME_DIR,
                        info_hash.lower() + ".fastresume")


def listing(cache_root: str, info_hash: str) -> Listing | None:
    """The torrent's own file list from its resume record, or None when there is no readable one.

    A record is decoded once per version of it: its mtime and size are part of the cache key, so
    a record the engine rewrites is read again.
    """
    path = resume_path(cache_root, info_hash)
    try:
        st = os.stat(path)
    except OSError:
        return None
    return _read(path, st.st_mtime_ns, st.st_size)


@functools.lru_cache(maxsize=512)
def _read(path: str, _mtime_ns: int, _size: int) -> Listing | None:
    try:
        with open(path, "rb") as f:
            record = bdecode(f.read())
    except (OSError, ValueError):
        return None
    return parse_info(record.get(b"info")) if isinstance(record, dict) else None
