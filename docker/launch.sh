#!/bin/sh
# Durable launcher: always starts the streaming server, using NVIDIA when available and
# degrading to Intel VAAPI / CPU otherwise. Safe to re-run (recreates the container).
#
# The `docker run --gpus all` flag hard-fails when the NVIDIA container runtime or driver is
# missing (e.g. after a driver update/reboot, or on a non-GPU host) — which would take the whole
# service down. This script detects GPU availability at launch and only adds the flags that work.
#
# Env overrides: NAME, IMAGE, DATA, WEB_PORT, HTTP_PORT, HTTPS_PORT, BT_PORT,
#                STREMIOSRV_CACHE_SIZE, SERVER_URL (web-player target; set to your public origin)
set -e

NAME="${NAME:-stremio-libtorrent-server}"
IMAGE="${IMAGE:-stremio-libtorrent-server:dev}"
DATA="${DATA:-/root/stremio-data}"
WEB_PORT="${WEB_PORT:-8080}"          # web player + API (HTTP/LAN)
HTTP_PORT="${HTTP_PORT:-11470}"       # direct API (HTTP)
HTTPS_PORT="${HTTPS_PORT:-12470}"     # web player + API (HTTPS)
BT_PORT="${BT_PORT:-6881}"
CACHE_SIZE="${STREMIOSRV_CACHE_SIZE:-19327352832}"

ENV_ARGS=""
[ -n "${SERVER_URL}" ] && ENV_ARGS="-e SERVER_URL=$SERVER_URL"

GPU_ARGS=""

# NVIDIA: authoritative probe — does `--gpus all` actually work with this image right now?
# (Covers driver-present-but-toolkit-missing, CDI/hook setups, and broken-driver cases.)
if docker run --rm --gpus all --entrypoint true "$IMAGE" >/dev/null 2>&1; then
    GPU_ARGS="--gpus all"
    echo "[launch] NVIDIA usable -> NVENC (--gpus all)"
else
    echo "[launch] NVIDIA not usable -> falling back to VAAPI/CPU"
fi

# Intel VAAPI render node, if present (independent of NVIDIA).
if [ -e /dev/dri ]; then
    GPU_ARGS="$GPU_ARGS --device /dev/dri:/dev/dri"
    echo "[launch] /dev/dri present -> VAAPI enabled"
fi

docker rm -f "$NAME" >/dev/null 2>&1 || true

# shellcheck disable=SC2086 # GPU_ARGS/ENV_ARGS are intentionally word-split
docker run -d --name "$NAME" --restart unless-stopped $GPU_ARGS $ENV_ARGS \
  -e STREMIOSRV_BT_LISTEN_PORT="$BT_PORT" \
  -e STREMIOSRV_CACHE_ROOT=/root/.stremio-server \
  -e CERT_FILE=certificates.pem \
  -e STREMIOSRV_CACHE_SIZE="$CACHE_SIZE" \
  -v "$DATA":/root/.stremio-server \
  -p "$WEB_PORT":8080 -p "$HTTP_PORT":11470 -p "$HTTPS_PORT":12470 -p "$BT_PORT":6881/tcp -p "$BT_PORT":6881/udp \
  --health-cmd "curl -fsS http://127.0.0.1:11470/health || exit 1" \
  --health-interval 30s --health-timeout 5s --health-retries 3 --health-start-period 15s \
  --label monitor.enabled=true --label monitor.name="$NAME" \
  --label monitor.health.path=/health --label monitor.health.port=11470 \
  --log-opt max-size=20m --log-opt max-file=5 \
  "$IMAGE"

echo "[launch] started $NAME (gpu args: ${GPU_ARGS:-none})"
