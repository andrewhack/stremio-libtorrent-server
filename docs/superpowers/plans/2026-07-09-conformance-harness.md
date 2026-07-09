# Client-Conformance Harness — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a stdlib Stremio-client conformance harness that replays the real captured client request sequence against a server URL and asserts, exiting non-zero on any regression — the check that would have caught the 1.0.2 OpenSubtitles-on-Android bug.

**Architecture:** A standalone `scripts/conformance.py` (pure `check_*` functions + a CLI) drives HTTP against `--target` (the nginx front, not just uvicorn). Hermetic mode uses a committed fixture torrent (served by infohash with no swarm — the engine's `read_resume_data` path already reconstructs metadata + trusts on-disk pieces offline, engine.py:644-647/:521) plus a local `subs_mock.py` that 403s non-browser User-Agents (reproduces subs5). Real mode points at the live server + a real subtitle URL. This harness is sub-project 1 of 3; the release gate (2) and the L1 monitor (3) both shell out to it and read its exit code / JSON.

**Tech Stack:** Python 3.12, **stdlib only** for `conformance.py`/`subs_mock.py` (urllib, json, ssl, http.server, argparse) so it runs anywhere with no deps; `build_fixture_cache.py` uses `libtorrent` + `ffmpeg` (build-host only, run once, output committed). Tests: pytest. Lint: ruff.

## Global Constraints

- **stdlib-only** for `conformance.py` + `subs_mock.py` — match `scripts/synthetic_playback.py`; they must run on a bare box and against a live URL.
- **Content-neutral fixtures** — the fixture video is an ffmpeg `testsrc` pattern (synthetic); no real media, infohash of real content, or peer IPs committed.
- **Only-legal test content**; **no-Claude-footprint** commits (no AI trailer); imperative lowercase scope-prefixed messages (e.g. `Conformance: add subtitle-CDN mock`).
- The harness asserts **through nginx** — callers pass `--target https://127.0.0.1:12470 --insecure`.
- Ruff line-length 100; snake_case; type hints on signatures.

---

## File Structure

- Create `scripts/subs_mock.py` — localhost subtitle-CDN mock; serves the fixture `.srt`, 403s non-browser UAs.
- Create `scripts/conformance.py` — `_get()` + pure `check_*` functions + `run()` + CLI.
- Create `scripts/build_fixture_cache.py` — one-time: ffmpeg→fixture.mkv, build torrent, place data, save resume w/ info-dict.
- Create `scripts/conformance_fixtures/` — committed: `fixture.mkv`, `fixture.srt`, `fixture.torrent`, `.resume/<ih>.fastresume`, `fixture.json` (`{"infohash": "..."}`).
- Create `tests/test_subs_mock.py` — UA-gate unit test.
- Create `tests/test_conformance.py` — each `check_*` PASSes the right shape, FAILs each known regression (fake `_get`).
- Modify nothing in `src/` — the harness is external; it exercises the running server.

---

### Task 1: Subtitle-CDN mock (`subs_mock.py`)

**Files:**
- Create: `scripts/subs_mock.py`
- Test: `tests/test_subs_mock.py`

**Interfaces:**
- Produces: `build_server(port: int, srt_bytes: bytes) -> http.server.HTTPServer` (binds `127.0.0.1`; `port=0` → ephemeral, read back via `server_address[1]`). CLI `--port --srt`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_subs_mock.py
import threading
import urllib.error
import urllib.request

from scripts import subs_mock

_SRT = b"1\n00:00:01,000 --> 00:00:02,000\nHi\n"


def _serve():
    srv = subs_mock.build_server(0, _SRT)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def test_browser_ua_gets_subtitle():
    srv, port = _serve()
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/x.srt",
                                     headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=5) as r:
            assert r.status == 200
            assert b"-->" in r.read()
    finally:
        srv.shutdown()


def test_non_browser_ua_is_forbidden():
    srv, port = _serve()
    try:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/x.srt", timeout=5)
            raise AssertionError("expected 403")
        except urllib.error.HTTPError as e:
            assert e.code == 403
    finally:
        srv.shutdown()
```

Note: `scripts/` needs to be importable — add an empty `scripts/__init__.py` if not present (check first; `tests/` import `from scripts import ...`). If the repo runs tests with `rootdir` at the project root, `scripts/__init__.py` makes `scripts` a package. Verify the existing test import style first and match it.

- [ ] **Step 2: Run the test — verify it fails**

Run: `uv run pytest tests/test_subs_mock.py -q`
Expected: FAIL (`ModuleNotFoundError: scripts.subs_mock`).

- [ ] **Step 3: Implement `scripts/subs_mock.py`**

```python
#!/usr/bin/env python3
"""Local subtitle-CDN mock for the hermetic conformance gate. Serves a fixture .srt but returns
403 to non-browser User-Agents — reproducing subs5.strem.io, so the browser-UA regression in the
/subtitles proxy is caught offline. stdlib only.

Usage:  python3 scripts/subs_mock.py --port 8099 --srt scripts/conformance_fixtures/fixture.srt
"""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, HTTPServer


def _make_handler(srt_bytes: bytes) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 — http.server API
            ua = self.headers.get("User-Agent", "")
            if not ua.startswith("Mozilla/"):
                self.send_response(403)
                self.end_headers()
                self.wfile.write(b"forbidden")
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/x-subrip; charset=utf-8")
            self.send_header("Content-Length", str(len(srt_bytes)))
            self.end_headers()
            self.wfile.write(srt_bytes)

        def log_message(self, *_a) -> None:  # keep test output quiet
            pass

    return Handler


def build_server(port: int, srt_bytes: bytes) -> HTTPServer:
    return HTTPServer(("127.0.0.1", port), _make_handler(srt_bytes))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8099)
    ap.add_argument("--srt", required=True)
    args = ap.parse_args()
    with open(args.srt, "rb") as f:
        srt = f.read()
    build_server(args.port, srt).serve_forever()


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run the test — verify it passes**

Run: `uv run pytest tests/test_subs_mock.py -q`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add scripts/subs_mock.py tests/test_subs_mock.py scripts/__init__.py
git commit -m "Conformance: add subtitle-CDN mock (403s non-browser UA)"
```

---

### Task 2: Conformance check functions (`conformance.py` core)

**Files:**
- Create: `scripts/conformance.py` (checks + `_get`; CLI added in Task 3)
- Test: `tests/test_conformance.py`

**Interfaces:**
- Produces: `_get(url, headers=None, method="GET", insecure=False, timeout=30) -> (status:int, headers:dict, body:bytes)`; and pure `check_settings(base, insecure=False)`, `check_stream_range(base, ih, insecure=False)`, `check_opensub_hash(base, ih, insecure=False)`, `check_subtitles_srt(base, from_url, insecure=False)`, `check_subtitles_vtt(base, from_url, insecure=False)` — each returns `(ok: bool, detail: str)`. Tests monkeypatch `_get`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_conformance.py
from scripts import conformance as c

IH = "a" * 40


def _patch(monkeypatch, status, body, headers=None):
    monkeypatch.setattr(c, "_get", lambda *a, **k: (status, headers or {}, body))


def test_opensub_hash_accepts_envelope(monkeypatch):
    _patch(monkeypatch, 200, b'{"error":null,"result":{"size":123,"hash":"0000000000020000"}}')
    ok, _ = c.check_opensub_hash("http://x", IH)
    assert ok


def test_opensub_hash_rejects_bare_string(monkeypatch):
    _patch(monkeypatch, 200, b'{"result":"0000000000020000"}')  # the 1.0.1 regression
    ok, _ = c.check_opensub_hash("http://x", IH)
    assert not ok


def test_subtitles_srt_rejects_html(monkeypatch):
    _patch(monkeypatch, 200, b"<!doctype html><html><head>")  # the catch-all regression
    ok, _ = c.check_subtitles_srt("http://x", "http://s/x.srt")
    assert not ok


def test_subtitles_srt_accepts_subrip(monkeypatch):
    _patch(monkeypatch, 200, b"1\n00:00:01,000 --> 00:00:02,000\nHi\n")
    ok, _ = c.check_subtitles_srt("http://x", "http://s/x.srt")
    assert ok


def test_subtitles_srt_flags_403(monkeypatch):
    _patch(monkeypatch, 403, b"forbidden")  # UA regression surfaces as upstream 403 -> our 5xx/4xx
    ok, _ = c.check_subtitles_srt("http://x", "http://s/x.srt")
    assert not ok


def test_subtitles_vtt_requires_webvtt(monkeypatch):
    _patch(monkeypatch, 200, b"WEBVTT\n\n00:01.000 --> 00:02.000\nHi\n")
    assert c.check_subtitles_vtt("http://x", "http://s/x.srt")[0]
    _patch(monkeypatch, 200, b"1\n00:00:01,000 --> 00:00:02,000\nHi\n")  # SRT, not VTT
    assert not c.check_subtitles_vtt("http://x", "http://s/x.srt")[0]


def test_stream_range_requires_206_and_range(monkeypatch):
    _patch(monkeypatch, 206, b"", {"content-range": "bytes 0-1023/9999",
                                   "accept-ranges": "bytes", "content-type": "video/x-matroska"})
    assert c.check_stream_range("http://x", IH)[0]
    _patch(monkeypatch, 200, b"", {"content-type": "video/x-matroska"})  # not partial
    assert not c.check_stream_range("http://x", IH)[0]


def test_settings_requires_three_keys(monkeypatch):
    _patch(monkeypatch, 200, b'{"options":[],"values":{},"baseUrl":"http://h:11470"}')
    assert c.check_settings("http://x")[0]
    _patch(monkeypatch, 200, b'{"values":{}}')  # flat/legacy
    assert not c.check_settings("http://x")[0]


def test_insecure_tls_is_loopback_only():
    # security guard: --insecure skips verification ONLY for loopback (the throwaway gate container),
    # NEVER for the live-server monitor (a lapsed/wrong cert must fail, not be waved through).
    assert c._ctx("https://127.0.0.1:12470/x", insecure=True) is not None
    assert c._ctx("https://stremio.karadimov.info:12470/x", insecure=True) is None
    assert c._ctx("https://127.0.0.1:12470/x", insecure=False) is None
```

- [ ] **Step 2: Run — verify it fails**

Run: `uv run pytest tests/test_conformance.py -q`
Expected: FAIL (`ModuleNotFoundError: scripts.conformance`).

- [ ] **Step 3: Implement `scripts/conformance.py` (core; CLI in Task 3)**

```python
#!/usr/bin/env python3
"""Stremio-client conformance harness: replay the real client request sequence against a server URL
and assert the shapes native clients depend on. Exit 0 iff every check passes. stdlib only, so it
runs on a bare box and against a live URL. See docs/superpowers/specs/2026-07-09-conformance-harness-design.md.
"""
from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.parse
import urllib.request

Result = tuple[bool, str]


def _ctx(url: str, insecure: bool):
    """TLS context. --insecure is honored ONLY for loopback https (the hermetic gate hits a
    throwaway container's self-signed cert on 127.0.0.1 — no MITM surface). Against any non-loopback
    host (the real-mode live-server monitor) verification is ALWAYS on, so a lapsed/wrong cert is
    caught, never skipped. (Per security-guidance: never blanket-disable TLS verification.)"""
    host = urllib.parse.urlsplit(url).hostname or ""
    if insecure and url.startswith("https") and host in ("127.0.0.1", "::1", "localhost"):
        return ssl._create_unverified_context()
    return None


def _get(url: str, headers: dict | None = None, method: str = "GET",
         insecure: bool = False, timeout: int = 30):
    ctx = _ctx(url, insecure)
    req = urllib.request.Request(url, headers=headers or {}, method=method)
    try:
        r = urllib.request.urlopen(req, timeout=timeout, context=ctx)  # noqa: S310 — caller-controlled
        return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read()


def check_settings(base: str, insecure: bool = False) -> Result:
    st, _h, body = _get(base + "/settings", insecure=insecure)
    if st != 200:
        return False, f"/settings -> {st}"
    try:
        j = json.loads(body)
    except ValueError:
        return False, "/settings not JSON"
    missing = [k for k in ("options", "values", "baseUrl") if k not in j]
    return (not missing), ("ok" if not missing else f"/settings missing {missing}")


def check_stream_range(base: str, ih: str, insecure: bool = False) -> Result:
    st, h, _b = _get(base + f"/{ih}/0", headers={"Range": "bytes=0-1023"}, insecure=insecure)
    if st != 206:
        return False, f"stream -> {st} (expected 206)"
    if not h.get("content-range", "").startswith("bytes 0-1023/"):
        return False, f"bad Content-Range: {h.get('content-range')!r}"
    if h.get("accept-ranges") != "bytes":
        return False, "missing Accept-Ranges: bytes"
    if not h.get("content-type", "").strip():
        return False, "missing Content-Type"
    return True, "ok"


def check_opensub_hash(base: str, ih: str, insecure: bool = False) -> Result:
    st, _h, body = _get(base + f"/opensubHash?videoUrl={base}/{ih}/0", insecure=insecure)
    if st != 200:
        return False, f"/opensubHash -> {st}"
    try:
        res = json.loads(body).get("result")
    except ValueError:
        return False, "/opensubHash not JSON"
    if not isinstance(res, dict):
        return False, f"result not an object (bare-string regression?): {res!r}"
    if not isinstance(res.get("size"), int) or not res.get("hash"):
        return False, f"result missing size/hash: {res!r}"
    return True, "ok"


def _sub_url(base: str, ext: str, from_url: str) -> str:
    return f"{base}/subtitles.{ext}?from=" + urllib.parse.quote(from_url, safe="")


def check_subtitles_srt(base: str, from_url: str, insecure: bool = False) -> Result:
    st, _h, body = _get(_sub_url(base, "srt", from_url), insecure=insecure)
    if st != 200:
        return False, f"/subtitles.srt -> {st} (UA-403/502 regression?)"
    if body.lstrip()[:9].lower() == b"<!doctype":
        return False, "/subtitles.srt returned HTML (catch-all route regression)"
    if b"-->" not in body[:200]:
        return False, "/subtitles.srt not SubRip"
    return True, "ok"


def check_subtitles_vtt(base: str, from_url: str, insecure: bool = False) -> Result:
    st, _h, body = _get(_sub_url(base, "vtt", from_url), insecure=insecure)
    if st != 200:
        return False, f"/subtitles.vtt -> {st}"
    if not body.lstrip().startswith(b"WEBVTT"):
        return False, "/subtitles.vtt not WebVTT"
    return True, "ok"
```

- [ ] **Step 4: Run — verify it passes**

Run: `uv run pytest tests/test_conformance.py -q`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add scripts/conformance.py tests/test_conformance.py
git commit -m "Conformance: check functions for handshake/stream/opensubHash/subtitles"
```

---

### Task 3: Harness CLI (`run()` + `main()`)

**Files:**
- Modify: `scripts/conformance.py` (append `run()` + `main()`)
- Test: `tests/test_conformance.py` (add a `run()` aggregation test)

**Interfaces:**
- Consumes: the `check_*` functions from Task 2.
- Produces: `run(target, mode, infohash, subs_from, insecure) -> list[dict]` (`{name, ok, detail}` per check, harness-internal exceptions captured as `ok=False`); CLI `--target --mode {hermetic,real} --infohash --subs-from --insecure --json`; exit 0 iff all pass.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_conformance.py
def test_run_aggregates_and_captures_harness_errors(monkeypatch):
    def boom(*a, **k):
        raise ConnectionError("refused")
    monkeypatch.setattr(c, "_get", boom)
    results = c.run("http://x", "real", "a" * 40, "http://s/x.srt", False)
    assert {r["name"] for r in results} == {
        "settings", "stream", "opensubHash", "subtitles.srt", "subtitles.vtt"}
    assert all(r["ok"] is False for r in results)          # unreachable target -> all FAIL, none crash
    assert any("refused" in r["detail"] for r in results)  # fail-loud detail
```

- [ ] **Step 2: Run — verify it fails**

Run: `uv run pytest tests/test_conformance.py::test_run_aggregates_and_captures_harness_errors -q`
Expected: FAIL (`AttributeError: module 'scripts.conformance' has no attribute 'run'`).

- [ ] **Step 3: Implement `run()` + `main()` (append to `scripts/conformance.py`)**

```python
def run(target: str, mode: str, infohash: str, subs_from: str, insecure: bool) -> list[dict]:
    target = target.rstrip("/")
    checks = [
        ("settings", lambda: check_settings(target, insecure)),
        ("stream", lambda: check_stream_range(target, infohash, insecure)),
        ("opensubHash", lambda: check_opensub_hash(target, infohash, insecure)),
        ("subtitles.srt", lambda: check_subtitles_srt(target, subs_from, insecure)),
        ("subtitles.vtt", lambda: check_subtitles_vtt(target, subs_from, insecure)),
    ]
    out = []
    for name, fn in checks:
        try:
            ok, detail = fn()
        except Exception as e:  # noqa: BLE001 — a harness error is itself a FAIL (fail-loud), never exit 0
            ok, detail = False, f"harness error: {type(e).__name__}: {e}"
        out.append({"name": name, "ok": ok, "detail": detail})
    return out


def main() -> None:
    import argparse
    import sys

    ap = argparse.ArgumentParser(description="Stremio-client conformance harness")
    ap.add_argument("--target", required=True, help="base URL, e.g. https://127.0.0.1:12470")
    ap.add_argument("--mode", choices=["hermetic", "real"], default="real")
    ap.add_argument("--infohash", required=True)
    ap.add_argument("--subs-from", required=True, help="subtitle source URL to proxy-test")
    ap.add_argument("--insecure", action="store_true", help="accept self-signed TLS (gate :12470)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    results = run(a.target, a.mode, a.infohash, a.subs_from, a.insecure)
    ok = all(r["ok"] for r in results)
    for r in results:
        print(f"  [{'PASS' if r['ok'] else 'FAIL'}] {r['name']}: {r['detail']}", file=sys.stderr)
    if a.json:
        print(json.dumps({"ok": ok, "mode": a.mode, "checks": results}))
    print(("conformance OK" if ok else "CONFORMANCE FAILED"), file=sys.stderr)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run — verify it passes**

Run: `uv run pytest tests/test_conformance.py -q && uv run ruff check scripts tests`
Expected: PASS + ruff clean.

- [ ] **Step 5: Commit**

```bash
git add scripts/conformance.py tests/test_conformance.py
git commit -m "Conformance: CLI runner (modes, JSON, fail-loud exit code)"
```

---

### Task 4: Fixture builder + committed fixtures (build host)

**Files:**
- Create: `scripts/build_fixture_cache.py`
- Create (committed OUTPUT, generated by the script on the build host): `scripts/conformance_fixtures/{fixture.mkv,fixture.srt,fixture.torrent,fixture.json,.resume/<ih>.fastresume}`

**Interfaces:**
- Produces: a cache dir the gate mounts at the container's `cache_root`; `fixture.json` = `{"infohash": "<40hex>"}` the harness reads for `--infohash`.

**Why the offline guarantee holds:** `Engine.add(ih)` reads `.resume/<ih>.fastresume` via `lt.read_resume_data` when present and **trusts on-disk pieces (no recheck, no swarm)** (engine.py:644-647); `save_all_resume` persists **with `save_info_dict`** so the resume blob carries the metadata (engine.py:521). So a mounted fixture cache → first `GET /{ih}/0` serves `206` with no network.

- [ ] **Step 1: Write `scripts/build_fixture_cache.py`**

```python
#!/usr/bin/env python3
"""ONE-TIME (build host): generate the committed hermetic fixture cache for the conformance gate.
Requires libtorrent + ffmpeg. Output under scripts/conformance_fixtures/ is committed; re-run only
to regenerate. Not stdlib — never imported by conformance.py.
"""
from __future__ import annotations

import json
import os
import subprocess
import time

import libtorrent as lt

HERE = os.path.dirname(os.path.abspath(__file__))
FX = os.path.join(HERE, "conformance_fixtures")
MKV = os.path.join(FX, "fixture.mkv")
SRT = os.path.join(FX, "fixture.srt")


def _ensure_media() -> None:
    os.makedirs(os.path.join(FX, ".resume"), exist_ok=True)
    # tiny synthetic (content-neutral) matroska: 1s testsrc, tiny
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=size=128x96:rate=5:duration=1",
                    "-pix_fmt", "yuv420p", MKV], check=True, capture_output=True)
    with open(SRT, "w", encoding="utf-8") as f:
        f.write("1\n00:00:00,500 --> 00:00:01,000\nПътят е затворен.\n")  # non-ASCII locks charset


def build() -> str:
    _ensure_media()
    fs = lt.file_storage()
    lt.add_files(fs, MKV)                       # single-file torrent named fixture.mkv
    ct = lt.create_torrent(fs, piece_size=16 * 1024)
    lt.set_piece_hashes(ct, FX)                 # hash pieces from the file on disk
    tdict = ct.generate()
    with open(os.path.join(FX, "fixture.torrent"), "wb") as f:
        f.write(lt.bencode(tdict))
    ti = lt.torrent_info(tdict)
    ih = str(ti.info_hash())

    ses = lt.session({"listen_interfaces": "127.0.0.1:6899", "enable_dht": False})
    p = lt.add_torrent_params()
    p.ti = ti
    p.save_path = FX                            # data already at FX/fixture.mkv
    h = ses.add_torrent(p)
    h.force_recheck()
    end = time.time() + 30
    while time.time() < end and not h.status().is_seeding:
        time.sleep(0.2)
    assert h.status().is_seeding, "fixture failed to verify as complete"
    flags = lt.save_resume_flags_t.save_info_dict
    h.save_resume_data(flags)
    deadline = time.time() + 10
    while time.time() < deadline:
        for a in ses.pop_alerts():
            if isinstance(a, lt.save_resume_data_alert):
                buf = lt.write_resume_data_buf(a.params)
                with open(os.path.join(FX, ".resume", ih.lower() + ".fastresume"), "wb") as f:
                    f.write(buf)
                with open(os.path.join(FX, "fixture.json"), "w") as f:
                    json.dump({"infohash": ih.lower()}, f)
                return ih.lower()
        time.sleep(0.2)
    raise SystemExit("no save_resume_data_alert")


if __name__ == "__main__":
    print("fixture infohash:", build())
```

- [ ] **Step 2: Run it on the build host + verify offline serve**

Run (on stremio, real libtorrent + ffmpeg):
```bash
UV_PROJECT_ENVIRONMENT=/tmp/svenv-conf uv run python scripts/build_fixture_cache.py
```
Expected: prints `fixture infohash: <40hex>`; `scripts/conformance_fixtures/` now has `fixture.mkv`, `fixture.srt`, `fixture.torrent`, `fixture.json`, `.resume/<ih>.fastresume`.

Verify the offline-serve guarantee before trusting it: point a throwaway Engine at a COPY of the fixture cache and confirm `GET /{ih}/0` → 206 with no network:
```bash
UV_PROJECT_ENVIRONMENT=/tmp/svenv-conf uv run python - <<'PY'
import json, os, shutil, tempfile
from fastapi.testclient import TestClient
from stremiosrv.torrent.engine import Engine
from stremiosrv.app import create_app
fx = "scripts/conformance_fixtures"
ih = json.load(open(f"{fx}/fixture.json"))["infohash"]
cache = tempfile.mkdtemp()
shutil.copy(f"{fx}/fixture.mkv", cache)
os.makedirs(f"{cache}/.resume", exist_ok=True)
shutil.copy(f"{fx}/.resume/{ih}.fastresume", f"{cache}/.resume/")
eng = Engine(listen_port=6898, cache_root=cache)
try:
    h = eng.add(ih)
    import time; end=time.time()+15
    while not h.has_metadata() and time.time()<end: time.sleep(0.2)
    assert h.has_metadata(), "no metadata offline -> resume lacks info-dict; fix build script"
    r = TestClient(create_app(engine=eng)).get(f"/{ih}/0", headers={"Range":"bytes=0-1023"})
    print("offline serve:", r.status_code, r.headers.get("content-range"))
    assert r.status_code == 206
finally:
    eng.shutdown()
PY
```
Expected: `offline serve: 206 bytes 0-1023/<total>`. If metadata never arrives, the resume blob lacks the info-dict — stop and fix the build script (the plan's core assumption) before continuing.

- [ ] **Step 3: Commit the builder + generated fixtures**

```bash
git add scripts/build_fixture_cache.py scripts/conformance_fixtures/
git commit -m "Conformance: fixture builder + committed hermetic torrent cache"
```

---

### Task 5: Hermetic end-to-end acceptance (build host)

**Files:**
- Create: `scripts/conformance_hermetic.sh` — spins the built image nginx-fronted with the fixture cache mounted + starts `subs_mock` + runs `conformance.py --hermetic`, asserts exit 0. This is the routine sub-project 2 will call; here it is the acceptance proof.

**Interfaces:**
- Consumes: an image tag (arg `$1`, default `androshack/stremio-libtorrent-server:1.0.2`), the committed fixture cache, `conformance.py`, `subs_mock.py`.

- [ ] **Step 1: Write `scripts/conformance_hermetic.sh`**

```bash
#!/usr/bin/env bash
# Hermetic conformance run: fixture cache mounted into the image (served offline via fast-resume),
# a localhost subtitle-CDN mock (403s non-browser UA), harness driven through nginx :12470.
# Exit 0 iff every check passes. No swarm, no external CDN.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
IMAGE="${1:-androshack/stremio-libtorrent-server:1.0.2}"
FX="$HERE/conformance_fixtures"
IH="$(python3 -c 'import json;print(json.load(open("'"$FX"'/fixture.json"))["infohash"])')"
CACHE="$(mktemp -d)"; cp "$FX/fixture.mkv" "$CACHE/"; mkdir -p "$CACHE/.resume"
cp "$FX/.resume/$IH.fastresume" "$CACHE/.resume/"

# subtitle-CDN mock on the host, reachable from the container via host-gateway
python3 "$HERE/subs_mock.py" --port 8099 --srt "$FX/fixture.srt" &
MOCK=$!
trap 'kill $MOCK 2>/dev/null; docker rm -f conf-smoke >/dev/null 2>&1; rm -rf "$CACHE"' EXIT

docker rm -f conf-smoke >/dev/null 2>&1 || true
docker run -d --name conf-smoke --add-host host.docker.internal:host-gateway \
  -e STREMIOSRV_CACHE_ROOT=/root/.stremio-server \
  -v "$CACHE":/root/.stremio-server \
  -p 12470:12470 -p 11470:11470 "$IMAGE" >/dev/null
sleep 11

python3 "$HERE/conformance.py" --mode hermetic --insecure \
  --target https://127.0.0.1:12470 --infohash "$IH" \
  --subs-from "http://host.docker.internal:8099/fixture.srt"
RC=$?
echo "hermetic conformance exit=$RC"
exit $RC
```

Note: `--subs-from` is fetched **server-side** by the container's proxy, so it must resolve from *inside* the container → `host.docker.internal` (mapped via `--add-host … host-gateway`). Confirm the host firewall lets the container reach host port 8099; if not, run `subs_mock` in a second container on the same docker network instead. Resolve this during implementation.

- [ ] **Step 2: Run the acceptance on the build host**

Run: `bash scripts/conformance_hermetic.sh androshack/stremio-libtorrent-server:1.0.2`
Expected: 5 `[PASS]` lines + `hermetic conformance exit=0`.

- [ ] **Step 3: Prove it catches the real regression (negative control)**

Run the same against the **pre-fix** image (`:1.0.1`, which has the `.srt`/UA bug):
```bash
bash scripts/conformance_hermetic.sh androshack/stremio-libtorrent-server:1.0.1
```
Expected: `[FAIL] subtitles.srt` (+ `subtitles.vtt`) and `exit=1` — proving the harness would have blocked 1.0.1. (If `:1.0.1` isn't available locally, note it and skip — the unit tests already lock the regression shapes.)

- [ ] **Step 4: Commit**

```bash
git add scripts/conformance_hermetic.sh
git commit -m "Conformance: hermetic end-to-end acceptance runner (fixture + mock + nginx)"
```

---

## Self-Review

**Spec coverage:** the four assertion groups (handshake, streaming, opensubHash, subtitles srt+vtt) → Task 2/3; hermetic fixtures (torrent + resume-with-info-dict) → Task 4; subtitle-CDN mock w/ 403 → Task 1; through-nginx + offline → Task 5. The flagged spec risk (metadata-from-resume) → Task 4 Step 2 verifies it explicitly and stops if false. Covered.

**Type/name consistency:** `_get` signature and the `(ok, detail)` tuple are used identically across Tasks 2–3; `run()` consumes the same `check_*` names it defines; `--infohash`/`--subs-from` flow from `fixture.json` (Task 4) into Task 5's runner. Consistent.

**Placeholder scan:** no TBD/TODO; every code step is complete. Two explicitly-flagged implementation-verification points (scripts package-import style in Task 1; container→host-mock reachability in Task 5) are called out with the fallback, per fail-loud.

**Scope:** sub-project 1 only (harness + fixtures + mock + acceptance). The `release.sh` gate wiring and the L1 loop are deliberately out (sub-projects 2 and 3), but Task 5's `conformance_hermetic.sh` is the seam sub-project 2 plugs into.
