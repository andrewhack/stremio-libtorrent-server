# stremio-libtorrent-server

> ⚠️ **Early development.** The protocol is mapped and the service skeleton runs; the torrent
> engine and transcode pipeline are being built stage by stage (see [Status](#status--roadmap)).

A self-hosted, **Stremio-compatible streaming server** that replaces Stremio's closed `server.js`
with an open Python implementation — a **libtorrent** torrent engine you can actually control
(inbound peers, sequential *"head & holes"* piece picking, endgame, SSD cache) plus a hardware
**transcode** pipeline (NVENC / VAAPI). Unmodified Stremio clients (Android TV, Tizen, webOS, the
web player) just point their **streaming-server URL** at it — no client changes.

## Why

The stock Stremio streaming server is closed-source and, in practice:

- **outbound-only** — it never listens for inbound BitTorrent peers, so you can only reach the
  connectable half of a swarm (verified at runtime: nothing binds the BT port);
- it **hides the torrent levers** — no real control over piece picking, endgame, or connectivity.

This server opens that up: **inbound connectivity + sequential piece picking** for better
reliability on sparse swarms and faster starts, while **preserving hardware transcode** for clients
that can't direct-play (or for bitrate-capped remote viewing).

It is **content-neutral infrastructure** — it streams whatever infohash a Stremio *addon* hands it.
It is **not** a scraper/source addon and does not bundle or surface any content.

## Relationship to `stremio-docker` (companion fork)

This repo is the **server brain**. The container **image** it runs in comes from the companion
dual-GPU fork:

➡️ **[andrewhack/stremio-docker](https://github.com/andrewhack/stremio-docker)** — image `stremio-docker-dual`
(jellyfin-ffmpeg with NVENC + Intel VAAPI, nginx + TLS, the bundled web player, CUDA/VAAPI runtime).

```
┌─────────────────────────── stremio-docker-dual (the fork) ───────────────────────────┐
│  jellyfin-ffmpeg (NVENC/VAAPI) · nginx (TLS) · bundled web player · CUDA/VAAPI runtime  │
└───────────────────────────────────────────────▲───────────────────────────────────────┘
                                                 │ FROM  (base image)
┌────────────────────────── stremio-libtorrent-server (this repo) ──────────────────────┐
│  FastAPI + libtorrent server  →  replaces the closed server.js on :11470               │
│  (nginx proxies client requests here instead of to server.js)                          │
└────────────────────────────────────────────────────────────────────────────────────────┘
```

The fork keeps building/validating the transcode toolchain + packaging; this repo swaps the
**server process**. (See the fork's `NVIDIA-GPU.md` for the GPU/Proxmox/LXC setup.)

## Status / roadmap

| Stage | Scope | State |
|---|---|---|
| 0 | Protocol map + conformance fixtures (`docs/protocol-map.md`) | ✅ done |
| 1 | FastAPI skeleton · Pydantic config · `/health` (ITCOM contract) | ✅ done |
| 2 | Torrent core + direct play (libtorrent engine, Range serving, stats, **inbound peers**, head & holes) | 🔜 in progress (`docs/plans/`) |
| 3 | Transcode / HLS (`hlsv2`, probe, hwaccel-profiler) reusing the dual-GPU ffmpeg | ⏳ planned |
| 4 | Subtitles · `opensubHash` · casting | ⏳ planned |
| 5 | Productionise — Docker/compose, DEVOPS.md, Ansible/Jenkins, AHM | ⏳ planned |

## Architecture (target)

- **`api/`** — FastAPI routers implementing the Stremio streaming-server HTTP API (the exact route
  surface is documented in [`docs/protocol-map.md`](docs/protocol-map.md)).
- **`torrent/`** — `libtorrent` session wrapper + piece-picker (range → piece priorities).
- **`stream/`** — HTTP Range parsing + file server (awaits pieces, streams byte ranges).
- **`transcode/`** *(Stage 3)* — capability fingerprint → jellyfin-ffmpeg (NVENC/VAAPI) → HLS.
- **`config.py`** — Pydantic settings (`STREMIOSRV_*` env). **`health.py`** — `/health`.

## Development

```bash
uv sync
uv run pytest -q          # unit tests
uv run ruff check .       # lint
uv run uvicorn stremiosrv.app:create_app --factory --host 0.0.0.0 --port 11470
curl -s localhost:11470/health   # {"status":"healthy",...}
```

Config via `STREMIOSRV_*` env vars (see `src/stremiosrv/config.py`): `HTTP_PORT`, `BT_LISTEN_PORT`,
`CACHE_ROOT`, `CACHE_SIZE`, `BT_MAX_CONNECTIONS`, `ENABLE_UPNP`, `TRANSCODE_PROFILE`.

## Docs

- [`docs/protocol-map.md`](docs/protocol-map.md) — the Stremio streaming-server protocol (routes + captured shapes).
- [`docs/plans/`](docs/plans/) — staged implementation plans.
- [`scripts/capture-fixtures.sh`](scripts/capture-fixtures.sh) — capture conformance fixtures from a stock server.

## Notes

- Test torrents must be **legal** (Internet Archive, public-domain, distro ISOs); fixtures are
  sanitized (no infohashes / peer IPs / content titles committed).
- Companion image + GPU/Proxmox setup: **[andrewhack/stremio-docker](https://github.com/andrewhack/stremio-docker)**.
