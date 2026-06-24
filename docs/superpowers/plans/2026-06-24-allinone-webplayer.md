# All-in-One Image (Web Player + libtorrent Server) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** One image that serves the bundled Stremio **web player** and our **libtorrent streaming server** on a single origin, so a browser (or native client) gets the full Stremio experience playing through our engine — GPU-optional, LAN-first.

**Architecture:** nginx serves the web build (`/srv/stremio-server/build/`, inherited from the `stremio-docker-dual` base) and reverse-proxies the streaming API to the in-container uvicorn (`127.0.0.1:11470`). Same origin → no mixed content. Two listeners: **HTTP :8080** (LAN, no cert) and **HTTPS :12470** (cert: bring-your-own or auto self-signed). The web player is pointed at the server via the stock `SERVER_URL`/`localStorage.json` mechanism. uvicorn stays internal (also publishable for native clients).

**Tech Stack:** nginx (in base image), jellyfin-ffmpeg (VAAPI/CPU; NVENC if a GPU returns), Python/FastAPI/uvicorn, libtorrent, Docker.

## Global Constraints
- **LAN-first, no security hardening, maximum compatibility** (saved design preference). HTTP for LAN, HTTPS for remote.
- **GPU-optional**: never hard-require a GPU at the orchestration level (keep `launch.sh`'s probe + degrade).
- Web build path: `/srv/stremio-server/build/`. localStorage seed source: `/srv/stremio-server/localStorage.json`.
- nginx regex locations containing `{n}` **must be quoted** (e.g. `location ~ "^/[0-9a-fA-F]{40}"`) or nginx fails to parse.
- API routes to proxy (everything else = web static): exact `/health /settings /network-info /device-info /stats.json /hwaccel-profiler /casting /opensubHash /removeAll`; prefix `^~ /hlsv2/`; regex `~ "^/[0-9a-fA-F]{40}"`.
- Verification is integration-style (build image → run container → `curl`), since this is infra glue.
- Test only with LEGAL torrents. Docs environment-neutral.

---

### Task 1: All-in-one nginx config (single origin: web + API)

**Files:**
- Create: `docker/nginx-allinone.conf`
- Reference (proven shape): the spike config in this session.

**Interfaces:**
- Produces: an nginx conf with HTTP server on `:8080` and HTTPS server on `:12470`, both serving `/srv/stremio-server/build` and proxying the API location set to `http://127.0.0.1:11470`.
- Consumes: uvicorn on `127.0.0.1:11470` (existing, from `build_app`).

- [ ] **Step 1: Write `docker/nginx-allinone.conf`**

```nginx
worker_processes auto;
error_log /dev/stderr warn;
pid /tmp/nginx-allinone.pid;
events { worker_connections 1024; }

http {
    include /etc/nginx/mime.types;
    default_type application/octet-stream;
    access_log off;
    sendfile on;
    client_max_body_size 0;
    proxy_temp_path /tmp/nx-proxy;
    client_body_temp_path /tmp/nx-body;

    # Shared API location set + web SPA fallback. Included into each server block.
    # (kept inline here; nginx has no macro — duplicated across http/https servers below.)

    server {
        listen 8080;                       # HTTP (LAN, no cert)
        include /srv/app/docker/nginx-locations.inc;
    }

    server {
        listen 12470 ssl;
        http2 on;
        ssl_certificate     /root/.stremio-server/certificates.pem;
        ssl_certificate_key /root/.stremio-server/certificates.pem;
        ssl_protocols TLSv1.2 TLSv1.3;
        include /srv/app/docker/nginx-locations.inc;
    }
}
```

- [ ] **Step 2: Write `docker/nginx-locations.inc`** (the shared server body — DRY across http/https)

```nginx
root /srv/stremio-server/build;
index index.html;

proxy_http_version 1.1;
proxy_set_header Host $http_host;
proxy_set_header X-Forwarded-Proto $scheme;
proxy_buffering off;
proxy_request_buffering off;
proxy_read_timeout 3600s;
proxy_send_timeout 3600s;

# --- streaming API -> uvicorn ---
location = /health             { proxy_pass http://127.0.0.1:11470; }
location = /settings           { proxy_pass http://127.0.0.1:11470; }
location = /network-info       { proxy_pass http://127.0.0.1:11470; }
location = /device-info        { proxy_pass http://127.0.0.1:11470; }
location = /stats.json         { proxy_pass http://127.0.0.1:11470; }
location = /hwaccel-profiler   { proxy_pass http://127.0.0.1:11470; }
location = /casting            { proxy_pass http://127.0.0.1:11470; }
location = /opensubHash        { proxy_pass http://127.0.0.1:11470; }
location = /removeAll          { proxy_pass http://127.0.0.1:11470; }
location ^~ /hlsv2/            { proxy_pass http://127.0.0.1:11470; }
location ~ "^/[0-9a-fA-F]{40}" { proxy_pass http://127.0.0.1:11470; }

# --- web player (static SPA) ---
location / { try_files $uri $uri/ /index.html; }
```

- [ ] **Step 3: Add `COPY docker ./docker` already exists in Dockerfile** — verify the include path `/srv/app/docker/nginx-locations.inc` matches `WORKDIR /srv/app` + `COPY docker ./docker`. (It does.)

- [ ] **Step 4: Build + validate config**

Run: `docker build -t stremio-libtorrent-server:dev . && docker run --rm --entrypoint nginx stremio-libtorrent-server:dev -t -c /srv/app/docker/nginx-allinone.conf`
Expected: `configuration file ... test is successful` (cert may be absent in this bare check — if it errors only on the missing cert, that's fine; Task 3 handles cert presence).

- [ ] **Step 5: Commit**
```bash
git add docker/nginx-allinone.conf docker/nginx-locations.inc
git commit -m "feat(allinone): nginx serves web player + proxies API (single origin, http+https)"
```

---

### Task 2: Web-player → server URL wiring (localStorage seed)

**Files:**
- Modify: `docker/entrypoint.sh`

**Interfaces:**
- Consumes env: `SERVER_URL` (explicit streaming-server URL) or `AUTO_SERVER_URL=1` (use bundled default).
- Produces: `/srv/stremio-server/build/localStorage.json` seeded so the web player targets the right origin.

- [ ] **Step 1: Add seeding to `docker/entrypoint.sh`** (before starting nginx)

```sh
# Wire the bundled web player to the streaming server (stock mechanism).
SEED_SRC="/srv/stremio-server/localStorage.json"
SEED_DST="/srv/stremio-server/build/localStorage.json"
if [ -f "$SEED_SRC" ]; then
    cp "$SEED_SRC" "$SEED_DST"
    if [ -n "${SERVER_URL}" ]; then
        case "$SERVER_URL" in */) ;; *) SERVER_URL="$SERVER_URL/" ;; esac
        sed -i "s|http://127.0.0.1:11470/|${SERVER_URL}|g" "$SEED_DST"
        echo "[entrypoint] web player -> $SERVER_URL"
    else
        echo "[entrypoint] web player -> default (127.0.0.1:11470); set SERVER_URL for remote"
    fi
fi
```

- [ ] **Step 2: Build + run with SERVER_URL, verify seed**

Run:
```sh
docker build -t stremio-libtorrent-server:dev .
docker rm -f t2 2>/dev/null
docker run -d --name t2 -e SERVER_URL=https://example.test:12470 -p 18091:8080 stremio-libtorrent-server:dev
sleep 6
curl -s http://127.0.0.1:18091/localStorage.json | grep -o "example.test"
```
Expected: prints `example.test` (seed rewritten). Then `docker rm -f t2`.

- [ ] **Step 3: Commit**
```bash
git add docker/entrypoint.sh
git commit -m "feat(allinone): seed web-player streaming-server URL (SERVER_URL/localStorage)"
```

---

### Task 3: Auto self-signed cert when none provided

**Files:**
- Modify: `docker/entrypoint.sh`

**Interfaces:**
- Consumes env: `DOMAIN` (CN for the self-signed cert; default `localhost`), `CERT_FILE` (default `certificates.pem`).
- Produces: a usable `$CACHE/certificates.pem` so the HTTPS (:12470) server always has a cert.

- [ ] **Step 1: Add self-signed generation to `docker/entrypoint.sh`** (before starting nginx, replacing the current cert-gate logic)

```sh
CACHE="${STREMIOSRV_CACHE_ROOT:-/root/.stremio-server}"
CERT="$CACHE/${CERT_FILE:-certificates.pem}"
if [ ! -f "$CERT" ]; then
    echo "[entrypoint] no cert at $CERT -> generating self-signed (browser one-time warning)"
    mkdir -p "$CACHE"
    openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
        -keyout "$CERT" -out "$CERT" \
        -subj "/CN=${DOMAIN:-localhost}" >/dev/null 2>&1
fi
```
(nginx always starts the :12470 ssl server now; cert is guaranteed present.)

- [ ] **Step 2: Build + run WITHOUT a mounted cert, verify HTTPS comes up self-signed**

Run:
```sh
docker rm -f t3 2>/dev/null
docker run -d --name t3 -p 18092:12470 -p 18093:8080 stremio-libtorrent-server:dev
sleep 6
curl -sk https://127.0.0.1:18092/health; echo
echo | openssl s_client -connect 127.0.0.1:18092 2>/dev/null | openssl x509 -noout -subject
```
Expected: `{"status":"healthy",...}` and a `subject=CN=localhost` (self-signed). Then `docker rm -f t3`.

- [ ] **Step 3: Commit**
```bash
git add docker/entrypoint.sh
git commit -m "feat(allinone): auto self-signed cert so HTTPS always starts (BYO cert still honored)"
```

---

### Task 4: Entrypoint orchestration (uvicorn + all-in-one nginx)

**Files:**
- Modify: `docker/entrypoint.sh`
- Modify: `Dockerfile` (EXPOSE 8080; CMD unchanged — entrypoint)

**Interfaces:**
- Produces: a container that runs uvicorn (`0.0.0.0:11470`) and nginx (`-c /srv/app/docker/nginx-allinone.conf`) serving `:8080` + `:12470`.

- [ ] **Step 1: Update `docker/entrypoint.sh` to start uvicorn + the all-in-one nginx** (replace the old nginx-tls invocation)

```sh
mkdir -p /tmp/nx-proxy /tmp/nx-body
/srv/app/.venv/bin/uvicorn stremiosrv.app:build_app --factory --host 0.0.0.0 --port 11470 &
APP_PID=$!
nginx -c /srv/app/docker/nginx-allinone.conf -g 'daemon off;' &
wait "$APP_PID"
```

- [ ] **Step 2: `Dockerfile` — expose 8080** (modify the EXPOSE line)
```dockerfile
EXPOSE 8080 11470 12470 6881
```

- [ ] **Step 3: Build + run, verify web + API on BOTH origins**

Run:
```sh
docker build -t stremio-libtorrent-server:dev .
docker rm -f t4 2>/dev/null
docker run -d --name t4 -p 18094:8080 -p 18095:12470 stremio-libtorrent-server:dev
sleep 7
curl -s -o /dev/null -w "http /  -> %{http_code}\n" http://127.0.0.1:18094/
curl -s http://127.0.0.1:18094/settings | grep -o stremiosrv
curl -sk -o /dev/null -w "https / -> %{http_code}\n" https://127.0.0.1:18095/
curl -sk https://127.0.0.1:18095/health
```
Expected: `http / -> 200`, `stremiosrv`, `https / -> 200`, healthy. Then `docker rm -f t4`.

- [ ] **Step 4: Commit**
```bash
git add docker/entrypoint.sh Dockerfile
git commit -m "feat(allinone): entrypoint runs uvicorn + all-in-one nginx; expose 8080"
```

---

### Task 5: Launcher + compose publish 8080 and SERVER_URL

**Files:**
- Modify: `docker/launch.sh` (publish 8080; pass `SERVER_URL`)
- Modify: `compose.yaml` (publish 8080; `SERVER_URL` env)

- [ ] **Step 1: `docker/launch.sh`** — add `-p "${WEB_PORT:-8080}":8080` to the `docker run`, and `-e SERVER_URL="${SERVER_URL}"` (only when set).

```sh
# in the docker run invocation, add:
  -p "${WEB_PORT:-8080}":8080 \
  ${SERVER_URL:+-e SERVER_URL="$SERVER_URL"} \
```

- [ ] **Step 2: `compose.yaml`** — add to ports: `- "8080:8080"`, and to environment: `SERVER_URL: ${SERVER_URL:-}` (commented guidance: set to your public https origin).

- [ ] **Step 3: Verify launcher brings up web + API**

Run (on a GPU-less or any host):
```sh
SERVER_URL=https://example.test:12470 WEB_PORT=18096 HTTP_PORT=18097 HTTPS_PORT=18098 BT_PORT=6896 NAME=t5 DATA=/tmp/t5data sh docker/launch.sh
sleep 7
curl -s -o /dev/null -w "web -> %{http_code}\n" http://127.0.0.1:18096/
docker rm -f t5
```
Expected: `web -> 200`. (Launcher probes GPU, degrades, starts.)

- [ ] **Step 4: Commit**
```bash
git add docker/launch.sh compose.yaml
git commit -m "feat(allinone): publish web port 8080 + SERVER_URL in launcher/compose"
```

---

### Task 6: Cert + deployment guide (regular-user focused)

**Files:**
- Create: `docs/cert-guide.md`
- Modify: `docs/DEVOPS.md` (link the guide; document the web player + ports)

- [ ] **Step 1: Write `docs/cert-guide.md`** covering, in order of ease: (a) **self-signed default** (zero config; browser one-time "not secure" → Proceed; native apps may reject → use a real cert), (b) **bring-your-own** (`certificates.pem` full-chain+key in the data dir), (c) **Let's Encrypt** (HTTP-01 needs port 80; if unavailable use **DNS-01**). Include the client-vs-cert matrix and the LAN-HTTP escape hatch (`http://<host>:8080`, no cert).

- [ ] **Step 2: Update `docs/DEVOPS.md`** — add the web-player section: ports `8080` (web+API, LAN) and `12470` (web+API, HTTPS); `SERVER_URL` should equal the origin clients use; link `cert-guide.md`. Note login/addons are the web player's job (Stremio cloud), not ours.

- [ ] **Step 3: Commit**
```bash
git add docs/cert-guide.md docs/DEVOPS.md
git commit -m "docs(allinone): cert guide (self-signed/BYO/LE) + web-player deployment"
```

---

### Task 7: In-image acceptance

- [ ] **Step 1: Build + run the full image** with a real data dir (cert present), publishing 8080 + 12470.
- [ ] **Step 2: Verify** (curl): `http://host:8080/` 200 + Stremio title; `/settings` → `stremiosrv`; `https://host:12470/` 200; `/health` healthy both; `/localStorage.json` seeded to `SERVER_URL`; a `/hlsv2/probe?mediaURL=<sample>` 200; a torrent range `/<infohash>/0` (legal magnet) → 206.
- [ ] **Step 3 (manual/user):** load `https://<domain>:12470` in a browser → web player loads, Settings shows the streaming server connected, play a legal title → streams through our engine (peers/speed in the stats panel).
- [ ] **Step 4: Commit** any fixes; update README roadmap (all-in-one done).

---

## Self-Review
- **Spec coverage:** single-origin web+API (Task 1), player URL wiring (Task 2), self-signed-default cert (Task 3), GPU-optional entrypoint (Task 4, reuses existing launch.sh probe), HTTP+HTTPS LAN-first (Tasks 1/5), regular-user cert guide (Task 6). All from the approved design + memory preferences.
- **Proven foundations:** the nginx serve+proxy model and quoted-regex requirement were validated by the spike in this session.
- **Placeholders:** none — each task has concrete config/code + exact curl checks. The only non-code step is Task 7 Step 3 (browser acceptance), inherently manual.
- **Consistency:** API location set matches the actual routes in `src/stremiosrv/api/*`; `nginx-locations.inc` is shared by both server blocks (DRY).
- **Risk:** native TV/desktop apps may reject the self-signed cert (documented in the cert guide; BYO/LE is the fix). The web player over HTTP/LAN avoids certs entirely.
