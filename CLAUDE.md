# stremio-libtorrent-server

Open **drop-in replacement for Stremio's closed streaming server** (`server.js`), built on
libtorrent. Ships as a single container that serves the Stremio web player *and* a real BitTorrent
engine. Public repo (MIT), published to Docker Hub as `androshack/stremio-libtorrent-server`.

`README.md` is the user-facing pitch and install guide — this file is the engineering companion:
architecture, commands, and the rules that keep the project honest.

## The contract: protocol fidelity

An **unmodified** Stremio client (browser, Android TV, Tizen, webOS, desktop) must be able to point
at this server and just work. That constraint drives most design decisions:

- **`docs/protocol-map.md`** is the authoritative route surface, extracted directly from the
  reference bundle `server.js` v4.20.16 (`docs/server.reference.js`, source pinned in
  `docs/server-url.txt`). Treat it as the spec; if a route's behaviour is unclear, that map is the
  reference, not guesswork.
- **The conformance harness lives in the sibling repo `stremio-loop`**, not here — it replays the
  real client request sequence against a built image, through nginx. This repo is referenced by
  image name only, so the loop can gate it from the outside.

Changing anything on the client-facing route surface means re-running conformance, not just unit
tests. Two failure modes are specific to this project and neither one 404s:

- **A route is not reachable until nginx proxies it.** `docker/nginx-locations.inc` is an explicit
  allowlist; anything missing from it falls through to `location /` and returns **200 with the web
  player's `index.html`**, so the client gets HTML where it expected JSON. `/subtitleSignature`
  shipped that way in 1.3.0 and only the hermetic gate caught it. `tests/test_nginx_allowlist.py`
  now fails on any route that is neither proxied nor declared origin-only.
- **The player origin and the API origin differ.** `:11470` is the direct API; `:8080`/`:12470` are
  nginx in front of the player. A route can work on one and not the other — test both.

## Stack

Python ≥3.12 · **FastAPI** + **uvicorn** · **libtorrent 2.0.11** · `pydantic` /
`pydantic-settings` · dev env via **uv** · **ruff** (line-length 100) · **pytest**
(`asyncio_mode = "auto"`, 48 test files).

## Structure

### `src/stremiosrv/`

- **Top level** — `app.py` (FastAPI app), `config.py` (pydantic-settings), `health.py`,
  `metrics.py`, `cache.py`, `certcheck.py`, `pins.py`
- **`api/`** — the client-facing surface: `handshake.py`, `playback.py`, `hls.py`, `casting.py`,
  `subs.py`, `pins.py`, `cache.py`, `netcheck.py`
- **`torrent/`** — libtorrent layer: `engine.py`, `picker.py` (playhead-first piece selection),
  `prefetch.py` (next-episode head), `dht_state.py`, `trackers.py`, `tracker_source.py`
- **`stream/`** — `fileserver.py`, `ranges.py` (byte-range serving)
- **`transcode/`** — `ffmpeg_cmd.py`, `converter.py`, `probe.py`, `profiler.py`, `fingerprint.py`
  (VAAPI / NVENC with CPU fallback)
- **`subs/`** — `convert.py`, `opensub.py`

### Deployment & tooling

- **`Dockerfile`**, **`docker/`** — `entrypoint.sh`, `launch.sh`, `nginx-allinone.conf`,
  `nginx-locations.inc`, `publish.sh` (the whole release, see Commands)
- **Compose variants** — `compose.yaml` (production base, CPU/VAAPI-safe), `compose.gpu.yaml`
  (NVIDIA overlay, applied *on top of* the base), `compose.hub.yaml` (published image, no build or
  source needed — the regular-user path), `compose.vpn.yaml` (tunnel only the server's traffic
  through gluetun)
- **`scripts/`** — `check_stremio_releases.py`, `capture-fixtures.sh`, `synthetic_playback.py`
- **`tests/`** — 48 files plus `tests/fixtures/`
- **`docs/`** — `protocol-map.md`, `DEVOPS.md`, `cert-guide.md`, `monitoring.md`, `releases/`
  (one file per version, the release-notes source), `plans/`

The gate, the live monitor and the upstream drift detector are **not in this repo** — they are the
four layers of `stremio-loop`, which references this server by image name and never imports it.

## Commands

```bash
uv sync
uv run pytest -q                       # 48 test files
uv run ruff check .                    # NOT `ruff format` — see Rules

docker compose up -d                                             # build + run (base)
docker compose -f compose.yaml -f compose.gpu.yaml up -d         # + NVIDIA overlay
docker compose -f compose.hub.yaml up -d                         # published image, no build

DRY_RUN=1 ./docker/publish.sh          # build + smoke only, publishes nothing
./docker/publish.sh                    # the whole release
```

`publish.sh` is the entire release: build → smoke → push `:$VERSION` and `:latest` → sync the Docker
Hub overview → tag → cut the GitHub release. **Nothing version-shaped is typed by hand** — it reads
the version out of the image it is about to push and refuses if that disagrees with
`pyproject.toml`. Bump `version` in `pyproject.toml` and write `docs/releases/v<x.y.z>.md` first;
the notes file's first heading becomes the release title and is stripped from the body. Full detail
in `docs/DEVOPS.md`.

## Rules

- **Protocol fidelity beats elegance.** If a cleaner design would break an unmodified client, the
  client wins. `docs/protocol-map.md` is the reference.
- **A new route needs three things**, not one: the FastAPI handler, an entry in
  `docker/nginx-locations.inc` (or `ORIGIN_ONLY` in `tests/test_nginx_allowlist.py`, with a reason),
  and a conformance check in `stremio-loop`. Unit tests passing means none of the other two.
- **Releases are gated, not trusted.** Run the hermetic conformance gate from `stremio-loop` against
  the built image before publishing; `DRY_RUN=1` freely.
- **Do not run `ruff format`.** The repo is `ruff check`-clean but was never format-normalised —
  `ruff format .` rewrites 61 files and buries the real diff. Lint only.
- **GPU is opt-in.** The base image must start and stream on a box with no GPU — hardware transcode
  is an overlay, and a missing GPU must never block startup.
- **Content-neutral infrastructure.** The server streams whatever a Stremio addon hands it; it
  bundles no content and is not a source. Keep that framing in code, docs, and commit messages.
- **Public docs get illustrative values, never copied output.** Release notes, the README and the
  Docker Hub overview are the most widely read things this project ships. A pasted log line carries
  file sizes, cache totals and timestamps that together describe a real library and when someone was
  watching it — redacting the titles does not fix that, because the numbers and the clock still do
  the describing. Write the shape, not the capture: `evicted <name> [<infohash>] (<size> MiB, last
  served 41m ago)`, never the line a real server printed. The same goes for `/stats.json` bodies,
  `docker logs` excerpts and screenshots. v1.3.1 shipped with a real eviction log and had to be
  rewritten after publication.
- **This repo is public.** No internal hostnames, customer references, credentials, or planning
  scratch. MIT (`LICENSE`), declared in `pyproject.toml` and shipped in the image.
- Dockerised-service conventions apply: `compose.yaml` carries the `monitor.*` labels and the
  service exposes `/health` (see `docs/DEVOPS.md`).
