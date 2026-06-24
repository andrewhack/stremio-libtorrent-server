#!/bin/sh
# All-in-one: the bundled Stremio web player + our libtorrent streaming server on one origin —
# HTTP :8080 (LAN) and HTTPS :12470 (cert). uvicorn (API) stays internal on :11470.
set -e

CACHE="${STREMIOSRV_CACHE_ROOT:-/root/.stremio-server}"
CERT="$CACHE/${CERT_FILE:-certificates.pem}"

# 1) TLS cert: use a mounted one, else auto-generate a self-signed cert so HTTPS always starts.
mkdir -p "$CACHE"
if [ ! -f "$CERT" ]; then
    echo "[entrypoint] no cert at $CERT -> generating self-signed (CN=${DOMAIN:-localhost})"
    openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
        -keyout "$CERT" -out "$CERT" -subj "/CN=${DOMAIN:-localhost}" >/dev/null 2>&1
else
    echo "[entrypoint] using cert $CERT"
fi

# 2) Point the bundled web player at the streaming server (stock localStorage mechanism).
SEED_SRC="/srv/stremio-server/localStorage.json"
SEED_DST="/srv/stremio-server/build/localStorage.json"
if [ -f "$SEED_SRC" ]; then
    cp "$SEED_SRC" "$SEED_DST"
    if [ -n "${SERVER_URL}" ]; then
        case "$SERVER_URL" in */) ;; *) SERVER_URL="$SERVER_URL/" ;; esac
        sed -i "s|http://127.0.0.1:11470/|${SERVER_URL}|g" "$SEED_DST"
        echo "[entrypoint] web player -> $SERVER_URL"
    else
        echo "[entrypoint] web player -> default 127.0.0.1:11470 (set SERVER_URL for remote clients)"
    fi
fi

# 3) Run uvicorn (API, internal :11470) + nginx (web player + API proxy on :8080 and :12470).
mkdir -p /tmp/nx-proxy /tmp/nx-body
/srv/app/.venv/bin/uvicorn stremiosrv.app:build_app --factory --host 0.0.0.0 --port 11470 &
APP_PID=$!
nginx -c /srv/app/docker/nginx-allinone.conf -g 'daemon off;' &
wait "$APP_PID"
