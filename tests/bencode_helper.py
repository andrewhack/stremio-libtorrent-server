"""A bencode encoder for building resume records in tests -- the production code only decodes."""
from __future__ import annotations


def benc(v) -> bytes:
    if isinstance(v, int):
        return b"i%de" % v
    if isinstance(v, str):
        v = v.encode()
    if isinstance(v, bytes):
        return b"%d:%s" % (len(v), v)
    if isinstance(v, list):
        return b"l" + b"".join(benc(x) for x in v) + b"e"
    if isinstance(v, dict):
        keys = sorted(k if isinstance(k, bytes) else k.encode() for k in v)
        by = {(k if isinstance(k, bytes) else k.encode()): val for k, val in v.items()}
        return b"d" + b"".join(benc(k) + benc(by[k]) for k in keys) + b"e"
    raise TypeError(type(v))
