#!/usr/bin/env python3
"""Synthetic end-to-end check: does the streaming server still speak the protocol AND serve bytes?

Adds a known Creative-Commons torrent (Big Buck Bunny), HEADs the stream, and validates the core
streaming-server endpoints. Exit 0 = OK, non-zero = failure — wire to cron / AHM / Telegram. Detects
breakage from a Stremio protocol change or a server regression, regardless of cause (stdlib only).

Usage:  SERVER=http://127.0.0.1:11470 python3 synthetic_playback.py
"""
import json
import os
import sys
import urllib.error
import urllib.request

SERVER = os.environ.get("SERVER", "http://127.0.0.1:11470").rstrip("/")
# Big Buck Bunny — Creative Commons (the canonical WebTorrent sample; legal test content).
INFO_HASH = os.environ.get("SYN_INFOHASH", "dd8255ecdc7ca55fb0bbf81323d87062db1f6d1c")
TIMEOUT = int(os.environ.get("SYN_TIMEOUT", "60"))


def _head(path):
    req = urllib.request.Request(SERVER + path, method="HEAD")
    try:
        r = urllib.request.urlopen(req, timeout=TIMEOUT)
        return r.status, {k.lower(): v for k, v in r.headers.items()}
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}


def check():
    fails = []
    # 1) /settings — protocol shape sanity
    try:
        with urllib.request.urlopen(SERVER + "/settings", timeout=TIMEOUT) as r:
            if "values" not in json.load(r):
                fails.append("/settings missing 'values'")
    except Exception as e:  # noqa: BLE001
        fails.append(f"/settings error: {e}")
    # 2) /stats.json reachable
    try:
        with urllib.request.urlopen(SERVER + "/stats.json", timeout=TIMEOUT) as r:
            if r.status != 200:
                fails.append(f"/stats.json -> {r.status}")
    except Exception as e:  # noqa: BLE001
        fails.append(f"/stats.json error: {e}")
    # 3) stream HEAD -> 206 + Content-Range + Content-Type (the playback contract)
    try:
        code, hdrs = _head(f"/{INFO_HASH}/0")
        if code != 206:
            fails.append(f"stream HEAD -> {code} (expected 206)")
        if "content-range" not in hdrs:
            fails.append("stream missing Content-Range")
        if not hdrs.get("content-type", "").strip():
            fails.append("stream missing Content-Type")
    except Exception as e:  # noqa: BLE001
        fails.append(f"stream error: {e}")
    return fails


if __name__ == "__main__":
    problems = check()
    if problems:
        print("SYNTHETIC PLAYBACK FAILED:")
        for p in problems:
            print("  -", p)
        sys.exit(1)
    print("synthetic playback OK")
