# Client-Conformance Harness — Design

**Part of: Release Regression Loop** (3 sub-projects; **this is #1 of 3**).
Status: design approved 2026-07-09, brainstorming. Next: `writing-plans` for this sub-project.

---

## Why this exists (the gap)

The 1.0.2 OpenSubtitles-on-Android bug shipped **past 181 green tests + the `/health` smoke check**. Root cause of the *miss* (not the bug): nothing exercised the **real Stremio-client request sequence** — and the specific break (`/subtitles.srt` routing) lived in **nginx (:12470)**, while every automated check hit **uvicorn (:11470)** directly or used `TestClient` (no nginx). It took capturing live device traffic to find it. We keep paying for this class of gap (the tracker `t["url"]`, the `auto_managed` seed bug, now subtitles): **unit + in-process integration tests don't see the client-through-nginx path.**

## The umbrella: Release Regression Loop (context for all 3 sub-projects)

One **shared capability** (this spec) consumed by two layers:

| Piece | What | Level | Blocks a release? |
|-------|------|-------|-------------------|
| **1. Conformance harness** *(this spec)* | Replays the real client request set against a server URL; asserts; exits non-zero on any failure | capability | — |
| **2. Release gate** *(sub-project 2)* | `docker/release.sh` runs the harness **hermetic** against the **nginx-fronted** built image; push/tag only if green | L0 deterministic | **yes** |
| **3. Drift monitor** *(sub-project 3)* | Scheduled loop runs the harness **real** against the live server + watches upstream Stremio; Telegram-via-`assistant` on state change | L1 report-only | no |

**Locked decisions** (2026-07-09):
- **Gate + monitor**, not one or the other.
- **Hermetic gate, real monitor** — the hard gate must never false-red on a flaky swarm/CDN; the monitor uses real deps to catch *external* drift.
- **Appliance boot-test stays decoupled** — it already runs on the builder during appliance rebake (`packer/build.sh` + `boot-test.sh`); server releases do **not** wait on qemu. Server ships often via `:latest`; appliances rebake rarely.
- **Build order: harness → gate → loop.** Each is independently shippable.

Global constraints (inherited): stdlib-only scripts (match `scripts/synthetic_playback.py`), content-neutral fixtures (synthetic, no real media/infohash/peer-IP), no-Claude-footprint commits, only-legal test content.

---

## This sub-project: the conformance harness

**Goal:** one parametrized harness that replays the real Stremio-client sequence against a target URL and asserts, usable three ways with the same code: **(a)** hermetic vs the nginx-fronted image (future gate), **(b)** real vs the live server (future monitor), **(c)** by hand for debugging.

**Non-goals:** the `release.sh` wiring (sub-project 2); the loop schedule/escalation (sub-project 3); re-speccing the whole protocol — only surfaces that native clients depend on **and** have regressed or are high-risk.

### Components / files

- **`scripts/conformance.py`** — CLI, **stdlib-only** (urllib/json/ssl/argparse), so it runs on the build host, in CI, and against a live URL with zero deps.
  - Args: `--target <base-url>` (e.g. `https://127.0.0.1:12470`), `--mode hermetic|real`, `--infohash <hex>` (the fixture torrent in hermetic mode; a known live title in real mode), `--subs-from <url>` (the subtitle source to proxy-test), `--insecure` (accept self-signed for the gate's :12470), `--json` (machine-readable summary).
  - Output: a per-check PASS/FAIL table to stderr + a JSON summary (`{checks:[{name,ok,detail}], ok:bool}`) to stdout. **Exit 0 iff every check passed.** Fail-loud: an unreachable target or a harness-internal error is itself a FAIL, never a silent exit 0.
  - Structure: pure check functions (`check_settings(base)`, `check_stream_range(base, ih)`, `check_opensub_hash(base, ih)`, `check_subtitles_srt(base, from_url)`, `check_subtitles_vtt(base, from_url)`) returning `(ok, detail)` — unit-testable without a live server.

- **`scripts/conformance_fixtures/`** — committed hermetic fixtures (content-neutral, synthetic):
  - `fixture.mkv` — a tiny (~hundreds of KB) synthetic video with **at least one file**; matroska so `Content-Type: video/x-matroska` is asserted.
  - `fixture.torrent` + fast-resume so the **server-under-test serves it by infohash with no swarm** — the fixture's data file placed at the right path under `cache_root` + `.resume/<ih>.fastresume`, mounted into the container, so the first `GET /{ih}/0` → `eng.add(ih)` loads a complete torrent from disk. Built once by a committed helper `scripts/build_fixture_cache.py` (uses libtorrent; run on the build host, output committed).
  - **Implementation risk to resolve in the plan (load-bearing):** the hermetic guarantee needs `add(ih)` to obtain **metadata** (not just piece state) without a swarm. Verify against the real engine whether `.resume/<ih>.fastresume` carries the info-dict (libtorrent `save_info_dict`) so a bare-infohash add reconstructs the torrent. If it does not, the fixture setup falls back to a **loopback-seed sidecar** (a second libtorrent seeding the fixture on localhost) or a tiny "adopt fixture" test-only endpoint. Pick the mechanism that keeps the gate fully offline; confirm it serves `206` before wiring sub-project 2.
  - `fixture.srt` — a short SubRip cue (include a non-ASCII line to lock charset handling).

- **`scripts/subs_mock.py`** — **stdlib** `http.server` that serves `fixture.srt` but returns **403 to non-browser User-Agents** (any UA not matching `Mozilla/...`), reproducing subs5.strem.io. Used only in hermetic mode; the gate starts it as a localhost sidecar and passes its URL as `--subs-from`.

### The assertion set (the captured Android traffic is the spec)

Every check runs against `--target` (so the gate points it at **nginx :12470** — the layer the `.srt` bug lived in):

1. **Handshake shapes** — `GET /settings` returns the 3-key envelope (`options`/`values`/`baseUrl`); `/network-info` has `availableInterfaces`; `/device-info` has `availableHardwareAccelerations`; `/casting` → `[]`.
2. **Streaming** — `GET /{infohash}/0` with `Range: bytes=0-1023` → `206`, `Accept-Ranges: bytes`, `Content-Range: bytes 0-1023/<total>`, `Content-Length: 1024`, `Content-Type: video/x-matroska`; `HEAD` → `206`.
3. **opensubHash** (locks 1.0.1) — `GET /opensubHash?videoUrl=<target>/{ih}/0` → JSON `{"error":null,"result":{"size":<int>,"hash":"<16-hex>"}}`. Assert `result` is an **object with both `size` and `hash`**, not a bare string, not null (hermetic fixture is complete so edges are on disk).
4. **Subtitles** (locks 1.0.2) — `GET /subtitles.srt?from=<subs-from>` → HTTP 200, body **is SubRip** (starts with `1` + a `-->` timestamp), `Content-Type` contains `x-subrip`, and it is **NOT** HTML (`<!doctype` ⇒ FAIL — the catch-all regression); `GET /subtitles.vtt?from=<subs-from>` → 200, body starts with `WEBVTT`. In hermetic mode `--subs-from` is the `subs_mock` URL, so a **200 here also proves the browser-UA fetch** (the mock 403s a non-browser agent → a regressed proxy would surface as 403→502).

### Hermetic vs real mode

- **hermetic** (gate): `--target https://127.0.0.1:12470 --insecure --infohash <fixture-ih> --subs-from http://127.0.0.1:<mockport>/fixture.srt`. Deterministic, offline, no swarm/CDN. A regression fails 100% of the time.
- **real** (monitor): `--target https://stremio.karadimov.info:12470 --infohash <a-known-cached-title> --subs-from <a-real-opensub-url>`. Exercises the live nginx + real subs CDN; catches external drift. (Flakiness here is fine — it's a report, not a gate.)

### Error handling / fail-loud

- Any connection error, timeout, non-2xx where 2xx expected, or shape mismatch = that check FAILS with a one-line `detail` (status, first 80 bytes of body). The process exits non-zero if **any** check failed, or if it couldn't run at all (e.g. target unreachable) — mirrors `improvements.md` §A6 silent-drift avoidance.

### Testing the harness itself

- Unit tests (`tests/test_conformance.py`): feed each `check_*` function canned responses (via a tiny fake-HTTP shim) — assert PASS on the correct shape and FAIL on each known regression (bare-string opensubHash, HTML from `/subtitles.srt`, 403 from a UA-blocked fetch, missing `Content-Range`).
- A self-test that runs the HTTP checks against an in-process `create_app()` where feasible (handshake/subtitles with a mocked `urlopen`), so the harness is exercised in the normal `pytest` run even without a container.
- `subs_mock.py`: unit-test the UA gate (browser UA → 200, `python-urllib` → 403).

### What sub-projects 2 & 3 will consume (interface promise)

`conformance.py` is the stable contract: **`conformance.py --target <url> --mode <m> …` → exit 0/nonzero + JSON summary.** The gate (2) calls it hermetic and reads the exit code; the loop (3) calls it real and reads the JSON to decide escalation. Neither re-implements checks.
