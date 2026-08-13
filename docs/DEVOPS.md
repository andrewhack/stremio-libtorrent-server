# DEVOPS — stremio-libtorrent-server

Deployment and operations for the libtorrent-based Stremio streaming server. This server is a drop-in
replacement for the closed Stremio `server.js`: it implements the streaming-server HTTP API
(handshake, torrent playback with **inbound** peering, hlsv2 transcode, subtitles, opensubHash).

## 1. Automated deployment

### Prerequisites (host)
- Docker Engine + Docker Compose v2.
- An NVIDIA GPU with a driver new enough for the host kernel, plus the NVIDIA Container Toolkit
  (`nvidia-ctk runtime configure`). For GPU driver install — including on a Proxmox/LXC host where the
  driver must be installed at the host level — see `NVIDIA-GPU.md` in the companion image repo.
- (Optional) An Intel iGPU exposed at `/dev/dri/renderD128` for the VAAPI fallback.

### Build & run
The image **always starts**, with or without a GPU — it autodetects the transcode profile at startup
(NVIDIA → `nvenc-linux`, else a VAAPI render node → `vaapi-renderD128`, else CPU/libx264). The GPU is
optional at the orchestration level too, so a missing/broken NVIDIA driver never blocks startup.

**Recommended — durable launcher** (auto-detects GPU, degrades to VAAPI/CPU, safe to re-run):
```sh
docker build -t stremio-libtorrent-server:dev .
DATA=/path/with/certificates.pem ./docker/launch.sh
curl -fsS http://<host>:11470/health
curl http://<host>:11470/hwaccel-profiler   # shows the autodetected profile (null = CPU)
```

**Or with compose** — the base is CPU/VAAPI-safe (starts anywhere); add the GPU overlay only on
hosts with the NVIDIA container runtime:
```sh
docker compose up -d                                   # CPU/VAAPI (always works)
docker compose -f compose.yaml -f compose.gpu.yaml up -d   # + NVENC (NVIDIA hosts)
docker compose ps          # STATUS should show (healthy) once the healthcheck passes
```
> Do **not** put `--gpus all` / `runtime: nvidia` in the always-on path: those hard-fail at container
> creation when the NVIDIA runtime/driver is absent, taking the service down. `launch.sh` (or the
> compose overlay split) keeps startup resilient.

### Publishing to Docker Hub
Bump `version` in `pyproject.toml`, write `docs/releases/v<x.y.z>.md`, commit, then:
```sh
docker login -u <hub-user>
DRY_RUN=1 ./docker/publish.sh   # build + smoke only, publishes nothing
./docker/publish.sh             # the whole release
```
One command end to end: build → smoke → push `:$VERSION` and `:latest` → sync the Hub overview →
tag `v$VERSION` → cut the GitHub release. Order is deliberate — the announcement goes last, after the
artefact is actually public.

Everything checkable is checked **before** the build, because discovering a problem after the image
is on Docker Hub leaves a half-published release with no undo: modified tracked files (the image
would match no commit), missing release notes, and a `v$VERSION` tag that already points somewhere
other than `HEAD`. After building it **reads the version out of the image it is about to push** —
nothing version-shaped is typed by hand — and refuses if that disagrees with `pyproject.toml`, which
means the build is stale. Then it starts the image and polls `/health`, failing with the container
logs if it never comes up healthy or serves a different version than the one being tagged.

The notes file's first heading becomes the release title and is **stripped from the body** — GitHub
already renders the title above it, and v1.1.0 and v1.2.0 both shipped with that line printed twice.
Title and notes therefore cannot drift, and cannot repeat. The
GitHub token is taken from `GH_TOKEN`, else from the credentials in the `origin` URL. If only that
last step fails, the message says so explicitly — the image is already published by then.

Overrides: `SKIP_BUILD`, `SKIP_GITHUB`, `VERSION`, `LOCAL`, `NOTES_FILE`, `RELEASE_NAME`,
`SMOKE_PORT`, `ALLOW_DIRTY`, `ALLOW_VERSION_MISMATCH`.

Pushing a tag ships the **image** and never touches the repository **overview** (the long description
on the Hub page) — left alone, the overview quietly falls behind the README.

`push-readme.sh` authenticates with the credential `docker login` already stored, so no second secret
is needed; set `DOCKERHUB_TOKEN` to override it (required under a credential helper, where the secret
is not in `config.json`). It verifies the result by reading the page back, so a rejected or truncated
update fails loudly. Note the overview has a length cap (~25k characters) the README is not far from.

### Configuration (env vars, prefix `STREMIOSRV_`)
Sizes and rates accept units (`64GiB`, `512MiB`) as well as plain byte counts. `GiB` is binary, `GB`/`G` decimal; `b` never means bits.

| Var | Default | Purpose |
|---|---|---|
| `STREMIOSRV_HTTP_PORT` | `11470` | streaming-server API port |
| `STREMIOSRV_BT_LISTEN_PORT` | `6881` | BitTorrent peer port (TCP+UDP) |
| `STREMIOSRV_CACHE_ROOT` | `/root/.stremio-server` | download/transcode cache |
| `STREMIOSRV_CACHE_SIZE` | `19327352832` (18 GiB) | download-cache budget in bytes; keep it **above your largest file** |
| `STREMIOSRV_CACHE_EVICT_GRACE` | `1800` | seconds a served torrent is safe from eviction |
| `STREMIOSRV_BT_MAX_CONNECTIONS` | `400` | libtorrent connection cap |
| `STREMIOSRV_TRANSCODE_PROFILE` | autodetect | force a HW profile |

### Web player (all-in-one)
The image bundles the Stremio **web player** and serves it on the same origin as the streaming API
(nginx serves the static build and reverse-proxies the API to uvicorn). A browser gets the full
Stremio UI playing through our engine — no separate client needed.
- Open **`http://<host>:8080`** (LAN, no cert) or **`https://<host>:12470`** (cert).
- Set **`SERVER_URL`** to the origin clients use (e.g. `https://<host>:12470`) so the player targets
  the right streaming server. TLS options: see [`cert-guide.md`](cert-guide.md) (self-signed default /
  bring-your-own / Let's Encrypt).
- **Login & addons** are handled by the web player against Stremio's cloud — not by this server.

### Ports / networking
- **8080** — web player + API (HTTP/LAN, no cert).
- **12470** — web player + API (HTTPS; cert from the cache dir, auto self-signed if absent).
- **11470** — direct streaming-server API (for native clients that bypass the bundled player).
- **6881 TCP+UDP** — BitTorrent peer port. Unlike the stock engine (outbound-only), **this server
  binds an inbound listener**, so forwarding 6881 to the host improves peer connectivity and speeds.

### Health & monitoring
- `GET /health` follows the ITCOM contract: `200` healthy / `503` degraded, body
  `{"status": "...", "components": {...}}`.
- The compose service carries `monitor.*` labels for AHM auto-discovery, plus a Docker `healthcheck`.

### CI/CD
- **Jenkins** — build/push the image and run the test suite (`uv run pytest`, `uv run ruff check`) on
  push; deploy via compose on the target host.
- **Ansible** — a role that installs the GPU driver + container toolkit, renders `compose.yaml`, and
  runs `docker compose up -d`. Target OS: current Debian/Ubuntu LTS.

## 2. Human activities (not automated)
- **GPU driver install** on the host/hypervisor (kernel-coupled; see `NVIDIA-GPU.md`). On Proxmox the
  driver goes on the host and the device is passed into the container.
- **Port-forwarding** 6881 (TCP+UDP) on the edge router for inbound peers.
- **TLS termination / reverse proxy** for remote access (the API serves plain HTTP).
- **Client wiring** — point the Stremio client's streaming-server URL at this server's origin.
