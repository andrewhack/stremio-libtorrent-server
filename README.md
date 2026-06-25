# 🎬 stremio-libtorrent-server

### Your own Stremio streaming server — open, fast, and *yours*. One command to run it. 🚀

Self-host the **complete Stremio experience** — the **web player** *and* a powerful, open
**BitTorrent streaming engine** — in a single container on your own hardware. Point any Stremio
client (browser, Android TV, Tizen, webOS, desktop) at it and press play.

No subscription. No tracking. No black box. **100% free and open — our gift to the community.** 💛

[![Docker Hub](https://img.shields.io/badge/Docker%20Hub-androshack%2Fstremio--libtorrent--server-2496ED?logo=docker&logoColor=white)](https://hub.docker.com/r/androshack/stremio-libtorrent-server)
[![License: MIT](https://img.shields.io/badge/License-MIT-3DA639.svg)](LICENSE)

---

## ✨ Why you'll love it

- **🚀 Install in one command.** `docker run …` — that's the whole setup. No building, no config files.
- **📺 Just works on TVs.** Automatic **trusted HTTPS** (a real Let's Encrypt cert) that smart TVs actually accept — zero certificate headaches.
- **⚡ Faster, more reliable.** Unlike the closed stock server, this one **accepts inbound peers** and fetches **playhead-first**, so streams start quicker and hold up on thin swarms.
- **🎛️ Truly yours to control.** Real dials for cache, buffering, peers, and transcode — tune deeply, or never touch a thing.
- **🖥️ Hardware transcode, optional.** Intel **VAAPI** / NVIDIA **NVENC** when you expose a GPU to the container (opt-in — see [Advanced](#-advanced--tune-it-your-way)), with graceful CPU fallback — and a missing GPU never stops it from starting.
- **🧩 Your addons, your choice.** It's **neutral infrastructure**: it streams whatever a Stremio addon hands it. It bundles no content and is not a source.
- **🔓 Open source.** Read it, change it, trust it.

---

## 🚀 Quick Start — anyone can do this

> **Architecture:** the published image is **`linux/amd64` (x86-64) only**. It runs on any normal
> PC/server/NAS. ARM hosts (Raspberry Pi, most ARM TV boxes, Apple Silicon) aren't supported by the
> prebuilt image — build from source on those.

**1.** Install [Docker](https://docs.docker.com/get-docker/).
**2.** Run one command — swap `YOUR_SERVER_IP` for your machine's LAN IP (e.g. `192.168.1.50`):

```sh
docker run -d --name stremio --restart unless-stopped \
  -e IPADDRESS=YOUR_SERVER_IP \
  -p 8080:8080 -p 12470:12470 -p 6881:6881/tcp -p 6881:6881/udp \
  -v stremio-data:/root/.stremio-server \
  androshack/stremio-libtorrent-server
```

**3.** Open it:
- 🌐 **In a browser (same network):** `http://YOUR_SERVER_IP:8080` — *browser playback covers MP4/H.264; for MKV/HEVC content use the **desktop or TV** apps (browsers can't decode those).*
- 🔒 **Trusted HTTPS (and TVs):** run `docker logs stremio` and use the printed URL — **it looks like**
  `https://192-168-1-50.519b6502d940.stremio.rocks:12470` *(replace `192-168-1-50` with your internal IP, dots written as dashes)*.

Sign into Stremio, add your addons, press play. 🍿
*(Prefer a file? Grab [`compose.hub.yaml`](compose.hub.yaml) → `IPADDRESS=YOUR_SERVER_IP docker compose -f compose.hub.yaml up -d`.)*

---

## 🧰 Minimum hardware

It's light — direct play (most content) barely touches the CPU; the GPU only matters for transcoding.

| Resource | Minimum | Recommended | Notes |
|---|---|---|---|
| **CPU** | 2 cores, x86-64 | 4+ cores | `amd64` only. Transcoding (clients that can't direct-play) is the only heavy load. |
| **RAM** | 1 GB | 2 GB+ | Engine + buffers + nginx; transcoding adds ~0.5–1 GB. |
| **Disk** | ~3 GB + cache | SSD, cache ≥ largest file | Image ~1.5 GB; download cache defaults to **18 GiB** (tune with `STREMIOSRV_CACHE_SIZE`). Keep free space ≥ your biggest single file. |
| **GPU** | none | Intel VAAPI / NVIDIA NVENC | Optional — only speeds up transcoding; a missing/broken GPU never blocks startup. |
| **Network** | any | wired + `6881` forwarded | Wired beats Wi-Fi for 4K; forward port `6881` for the full swarm (see below). |

---

## 📺 On your TV

Smart TVs insist on a **trusted** HTTPS connection — a self-signed cert won't do. Set `IPADDRESS`
and this server fetches a real Let's Encrypt certificate for you automatically (via Stremio's
`*.stremio.rocks` magic DNS, which maps that long URL back to your server's IP — even on your LAN).
In the TV's Stremio app, set the **Streaming Server URL** to the `…stremio.rocks:12470` address shown
by `docker logs stremio`.

---

## 🌍 Want the *full* swarm? Forward one port.

A lot of BitTorrent's speed comes from peers reaching **you** — and home routers block that by
default. For maximum peers and throughput, **forward port `6881` (TCP *and* UDP)** on your router to
your server. It works fine without forwarding — you'll just reach fewer peers on sparse torrents.
(Unlike the stock server, this one actually *listens* for inbound peers, so the forward genuinely pays off.)

---

## 🔧 Advanced — tune it your way

Everything is a plain `-e NAME=value` environment variable:

| Setting | Default | What it does |
|---|---|---|
| `IPADDRESS` | *(unset)* | Your server IP → auto **trusted TV cert** via `*.stremio.rocks`. Unset → self-signed. |
| `SERVER_URL` | auto | URL the web player targets. Set for a custom domain. |
| `STREMIOSRV_CACHE_SIZE` | `19327352832` (18 GiB) | Download-cache budget in bytes (LRU-evicted). Keep it **above your largest file**. |
| `STREMIOSRV_READAHEAD_BYTES` | `134217728` (128 MiB) | Playhead buffer — bigger absorbs more swarm jitter (fewer rebuffers). |
| `STREMIOSRV_BT_LISTEN_PORT` | `6881` | BitTorrent peer port (the one to forward). |
| `STREMIOSRV_BT_MAX_CONNECTIONS` | `400` | Max peer connections. |
| `STREMIOSRV_DOWNLOAD_RATE_LIMIT` | `0` | Cap download throughput in **bytes/sec** (`0` = unlimited). E.g. `12500000` ≈ 100 Mbit/s. |
| `STREMIOSRV_UPLOAD_RATE_LIMIT` | `0` | Cap upload throughput in **bytes/sec** (`0` = unlimited). Handy so seeding doesn't saturate your line. |
| `DOMAIN` | `localhost` | CN for the self-signed cert (when not using `IPADDRESS`). |
| `CERT_FILE` | `certificates.pem` | Bring-your-own cert (full-chain + key) filename in the data volume. |

**GPU transcode** (only for clients that can't direct-play). Docker does **not** expose the host GPU to a
container by default, so the one-command install above is **CPU-only**. The server *auto-detects* a GPU,
but only one you've handed in — so opt in at launch:
- **Intel/AMD VAAPI** → add `--device /dev/dri:/dev/dri`
- **NVIDIA NVENC** → add `--gpus all` (requires the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html) on the host)
- **Or just use [`docker/launch.sh`](docker/launch.sh)** — it probes the host and adds the right flags for you,
  degrading gracefully (a broken or absent driver never blocks startup). Full NVIDIA driver + **Proxmox
  passthrough** setup: [NVIDIA-GPU.md](https://github.com/andrewhack/stremio-docker/blob/main/NVIDIA-GPU.md) (companion fork).

**Your own domain instead of stremio.rocks:** put a full-chain+key PEM as `certificates.pem` in the
data volume, set `-e SERVER_URL=https://yourdomain:12470`, and leave `IPADDRESS` unset.

**Tailscale (zero port-forwarding, trusted HTTPS):** if you reach the server over a [Tailscale](https://tailscale.com)
tailnet, you can skip both port-forwarding *and* the `*.stremio.rocks` dependency. Provision a cert for the
node's MagicDNS name (`tailscale cert <node>.<tailnet>.ts.net`), concatenate the cert + key into one PEM,
drop it in the data volume as `certificates.pem`, and set `-e SERVER_URL=https://<node>.<tailnet>.ts.net:12470`
with `IPADDRESS` unset. Point your devices' Stremio **Streaming Server URL** at that tailnet address — works
across your own devices and anyone you share the tailnet with, with a browser/TV-trusted cert.

**Ports:** `8080` web+API (HTTP/LAN) · `12470` web+API (HTTPS) · `11470` direct API · `6881` BitTorrent.

📖 Full ops guide: [`docs/DEVOPS.md`](docs/DEVOPS.md) · TLS deep-dive: [`docs/cert-guide.md`](docs/cert-guide.md).

---

## 🧠 Why it exists

The stock Stremio streaming server is closed-source and, in practice:
- **outbound-only** — it never listens for inbound peers, so you only reach the connectable half of a swarm;
- it **hides the torrent levers** — no real control over piece picking, connectivity, or cache.

This opens it up: **inbound connectivity + playhead-first piece picking** for faster starts and better
reliability on sparse swarms, **plus** hardware transcode for clients that can't direct-play — all in
an image you run yourself. It is **content-neutral infrastructure**: it streams whatever infohash a
Stremio *addon* hands it, and bundles or surfaces nothing.

## 🏗️ Under the hood

One image, two source repos. The runtime image is built **`FROM`** a GPU/ffmpeg base (the companion
fork) and layers the open server on top:

```
┌─────────────── stremio-docker-dual  (companion fork, MIT) ──────────────┐
│ jellyfin-ffmpeg (NVENC/VAAPI) · nginx · bundled Stremio web player        │
└───────────────────────────────▲──────────────────────────────────────────┘
                                 │ FROM
┌─────────────── stremio-libtorrent-server  (this repo) ──────────────────┐
│ FastAPI + libtorrent engine · nginx serves web player + proxies the API   │
│ → one container: web player + open engine on a single origin              │
└────────────────────────────────────────────────────────────────────────────┘
```

Companion fork: **[andrewhack/stremio-docker](https://github.com/andrewhack/stremio-docker)**
(builds the `stremio-docker-dual` image; see its [`NVIDIA-GPU.md`](https://github.com/andrewhack/stremio-docker/blob/main/NVIDIA-GPU.md) for GPU/Proxmox setup).

Modules: `api/` (Stremio HTTP API) · `torrent/` (libtorrent + piece-picker) · `stream/` (Range file
server) · `transcode/` (ffmpeg NVENC/VAAPI → HLS) · `config.py` · `health.py`. Protocol reference:
[`docs/protocol-map.md`](docs/protocol-map.md).

## ✅ Status

All stages shipped and verified on hardware:

| Stage | Scope | State |
|---|---|---|
| 0–1 | Protocol map · FastAPI skeleton · `/health` | ✅ |
| 2 | Torrent core + direct play (inbound peers, head & holes, Range serving, stats) | ✅ |
| 3 | Transcode / HLS (`hlsv2`, NVENC/VAAPI) | ✅ |
| 4 | Subtitles · `opensubHash` · casting | ✅ |
| 5 | Productionise — compose, DEVOPS, healthcheck, AHM | ✅ |
| 6 | All-in-one (web player + engine) · TV-trusted SSL · GPU-optional · Docker Hub | ✅ |

## 🛠️ Development

```bash
uv sync
uv run pytest -q          # unit tests
uv run ruff check .       # lint
uv run uvicorn stremiosrv.app:create_app --factory --host 0.0.0.0 --port 11470
```

## 🔐 Appendix — how the TV HTTPS URL works (`*.stremio.rocks`)

*Skip this unless you're curious or customizing certs — the Quick Start needs none of it.*

When you set `IPADDRESS`, the container prints a TV-ready HTTPS URL like:

```
https://192-168-1-50.519b6502d940.stremio.rocks:12470
        └────┬─────┘ └────┬─────┘ └─────┬─────┘ └─┬─┘
          your IP,     shared ID     Stremio's    HTTPS
          dashed       (see below)   free DNS     port
```

- **`192-168-1-50`** — your server's IP with dots turned into dashes.
- **`…stremio.rocks`** — a free service Stremio runs that (a) resolves `<dashed-ip>.…stremio.rocks`
  back to that IP (even a LAN IP — no DNS setup by you), and (b) carries a **trusted Let's Encrypt
  wildcard cert**, so TVs/browsers accept the HTTPS connection with no warning.
- **`:12470`** — the container's HTTPS port.

A TV opening it → resolves to your server's IP → connects on `12470` → sees a trusted cert → connects.
The name resolves to your **internal** IP, so the TV must be on the **same network** (normal home
setup). For **remote** access, use your **public** IP in the URL and forward port `12470`.

### About `519b6502d940` — a shared, third-party dependency

This ID is **inherited from the upstream [tsaridas/stremio-docker](https://github.com/tsaridas/stremio-docker)**
and is **not unique to your install**. Everyone running this (or the upstream) image shares the same
`*.519b6502d940.stremio.rocks` wildcard cert, fetched from **Stremio's free certificate service**.

- ✅ Zero-config trusted HTTPS for TVs.
- ⚠️ It depends on Stremio's cert service keeping that wildcard alive; if it's ever rotated or taken
  down, the automatic cert path stops working (your stream still runs — only the trusted-HTTPS URL is affected).

**Independent fallback — bring your own cert** (no reliance on stremio.rocks): put a full-chain + key
PEM as `certificates.pem` in the data volume, set `-e SERVER_URL=https://yourdomain:12470`, and **leave
`IPADDRESS` unset** — the server uses your cert as-is. Or, on a trusted LAN, skip HTTPS entirely and
use `http://<your-server-ip>:8080`.

## 📜 License & spirit

**MIT** — built on the MIT-licensed [stremio-docker](https://github.com/tsaridas/stremio-docker) fork.

This is **not a commercial product, and we don't monetize it.** It's our contribution to the people
who just want their own open, private streaming server. Use it, share it, make it better. 💛

> Keep it legal: this is neutral infrastructure for content **you** have the right to stream.
