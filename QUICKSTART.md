# Quick Start — Stremio WebAdmin Fork

This fork runs the Stremio libtorrent server together with the independent WebAdmin and Pi-hole services.
The fork repository is the authoritative runtime and update source.

> Requires Docker Engine with the Docker Compose plugin (`docker compose`).

## 1. Clone the fork

```sh
git clone https://github.com/emmanique/stremio-libtorrent-server-webadmin.git
cd stremio-libtorrent-server-webadmin
```

## 2. Start everything

No `.env` file is required for the default installation:

```sh
docker compose up -d
```

Docker Compose automatically uses the root `compose.yaml` and builds the Stremio Server and WebAdmin images from this fork.
The default stack contains:

- `stremio-libtorrent-server` — streaming server and web player
- `stremio-webadmin` — independent WebAdmin on port `8090`
- `stremio-pihole` — Pi-hole used internally by the Stremio container

Default ports:

| Service | Port |
|---|---:|
| Stremio Web Player | `8080` |
| Stremio API | `11470` |
| Stremio HTTPS | `12470` |
| WebAdmin | `8090` |
| Pi-hole Web UI | `8053` |
| BitTorrent TCP/UDP | `6881` |

Pi-hole DNS stays on the internal Docker network by default, so host port `53` is not required and the stack can start even when `systemd-resolved` or another DNS service already owns port 53.

## 3. Optional configuration

The stack works without `.env`. To customise the server IP, ports or Pi-hole settings:

```sh
cp .env.example .env
```

Edit `.env`, then apply the changes with:

```sh
docker compose up -d
```

Set `IPADDRESS` to the server's LAN address if you want the automatic trusted `*.stremio.rocks` certificate used by TV clients.

## 4. Optional — expose Pi-hole as LAN DNS

Only use this when the host should provide DNS to other LAN clients and TCP/UDP port 53 is available:

```sh
docker compose -f compose.yaml -f compose.dns.yaml up -d
```

If port 53 is already occupied, either free it or set `PIHOLE_DNS_BIND_IP` to a dedicated host address before enabling the override.

## 5. Open the services

With default settings:

- Web Player: `http://<server-ip>:8080`
- WebAdmin: `http://<server-ip>:8090`
- Pi-hole Web UI: `http://<server-ip>:8053/admin/`
- Stremio API: `http://<server-ip>:11470`

## Updating the fork

For a normal host installation:

```sh
git pull origin main
docker compose up -d --build
```

The WebAdmin software-update function also checks and builds from:

`https://github.com/emmanique/stremio-libtorrent-server-webadmin`

It does not use `andrewhack/stremio-libtorrent-server` as a runtime update source. Upstream changes are imported into this fork only through the controlled GitHub review workflow.
