# Quick Start

Self-host Stremio (web player + streaming engine) in one container. No build, no accounts beyond Docker.

## 1. Install Docker
Windows/Mac: Docker Desktop. Linux: `curl -fsSL https://get.docker.com | sh`.

## 2. Run it (one command)
Replace `192.168.1.50` with **your server's IP address**:
```sh
docker run -d --name stremio --restart unless-stopped \
  -e IPADDRESS=192.168.1.50 \
  -p 8080:8080 -p 12470:12470 -p 6881:6881/tcp -p 6881:6881/udp \
  -v stremio-data:/root/.stremio-server \
  androshack/stremio-libtorrent-server:latest
```
`IPADDRESS` gives you a **trusted HTTPS cert that TVs accept** (auto, via `*.stremio.rocks`). Omit it
and you get a self-signed cert (fine in a browser, rejected by most TVs).

*(Prefer compose? `IPADDRESS=192.168.1.50 docker compose -f compose.hub.yaml up -d`.)*

## 3. Open it
- **In a browser (LAN):** `http://<server-ip>:8080`
- **Trusted HTTPS / TVs:** the URL printed by `docker logs stremio` — looks like
  `https://192-168-1-50.519b6502d940.stremio.rocks:12470`. Use this as the **Streaming Server URL** in
  the Stremio app on your TV.

Then log into your Stremio account, add addons, and play. Playback runs through this server.

## Options
| Want | Do |
|---|---|
| Intel GPU transcode (VAAPI) | add `--device /dev/dri:/dev/dri` |
| NVIDIA GPU transcode (NVENC) | use `docker/launch.sh` (auto-detects GPU) |
| Your own domain + cert | drop a `certificates.pem` (full-chain+key) in the data volume, set `-e SERVER_URL=https://yourdomain:12470`, omit `IPADDRESS` |
| Bigger/smaller cache | `-e STREMIOSRV_CACHE_SIZE=<bytes>` (default 18 GiB) |

TLS details: see [`docs/cert-guide.md`](docs/cert-guide.md).
