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

# A record larger than this is not read, and the walk answers for its torrent: it is decoded in
# the request's thread and kept in memory, and one hostile torrent must not cost more than this.
# A real 5,000-file torrent's record is about 1.5 MB.
_MAX_RECORD_BYTES = 32 * 1024 * 1024


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
    """A path part that stays below the torrent's directory, and that a path can carry at all."""
    return (bool(part) and part not in (".", "..")
            and "/" not in part and "\\" not in part and "\x00" not in part)


def parse_info(info) -> Listing | None:
    """The file list of a v1 info dict, or None for anything else -- a v2-only one included.

    Read the way libtorrent reads it: `files` before `length`, and `path.utf-8` only when it is a
    list. A pad file or a symlink keeps its slot in the numbering, because libtorrent counts it,
    and is not listed: neither has content of its own. A file with a path part that could leave
    the torrent's directory is not listed either.
    """
    if not isinstance(info, dict):
        return None
    files = info.get(b"files")
    if not isinstance(files, list):
        if isinstance(info.get(b"length"), int):
            return Listing(1, (TorrentFile(0, (), info[b"length"]),))
        return None
    out = []
    for index, f in enumerate(files):
        if not isinstance(f, dict) or not isinstance(f.get(b"length"), int):
            return None
        attr = f.get(b"attr")
        if isinstance(attr, bytes) and (b"p" in attr or b"l" in attr):
            continue
        raw = f.get(b"path.utf-8")
        if not isinstance(raw, list):
            raw = f.get(b"path")
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


class _NoListing(Exception):
    """No listing from this record at present. Raised rather than returned, because lru_cache
    keeps what a call returns and nothing that it raises."""


def listing(cache_root: str, info_hash: str) -> Listing | None:
    """The torrent's own file list from its resume record, or None when there is no readable one.

    An info dict never changes -- the infohash is its hash -- so a listing once read is kept by the
    record's path alone. The engine rewrites the record of every torrent in its session (every
    30 s by default), and a cache keyed on the record's version kept one more copy per rewrite. A
    record that is missing, half-written, unreadable, too large or still without an info dict is
    not kept: it is tried again on the next call. One whose info dict cannot be listed is kept,
    as an empty listing.
    """
    try:
        return _read(resume_path(cache_root, info_hash))
    except _NoListing:
        return None


@functools.lru_cache(maxsize=512)
def _read(path: str) -> Listing:
    try:
        with open(path, "rb") as f:
            if os.fstat(f.fileno()).st_size > _MAX_RECORD_BYTES:
                raise _NoListing
            record = bdecode(f.read())
    except (OSError, ValueError):
        raise _NoListing from None
    info = record.get(b"info") if isinstance(record, dict) else None
    if not isinstance(info, dict):
        raise _NoListing  # no info dict yet: a torrent still fetching its metadata
    # An info dict never changes, so one that cannot be listed (a v2-only one, say) is kept too,
    # as an empty listing -- which the library answers with the walk -- rather than decoded again
    # on every build.
    found = parse_info(info)
    return found if found is not None else Listing(0, ())
