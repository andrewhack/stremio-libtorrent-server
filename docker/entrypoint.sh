#!/bin/sh
# Start the streaming server (uvicorn, http :11470) and, if a TLS cert is present, an nginx TLS
# front on :12470 (stock stremio layout). http-only if no cert.
set -e

CACHE="${STREMIOSRV_CACHE_ROOT:-/root/.stremio-server}"
CERT="$CACHE/${CERT_FILE:-certificates.pem}"

/srv/app/.venv/bin/uvicorn stremiosrv.app:build_app --factory --host 0.0.0.0 --port 11470 &
APP_PID=$!

if [ -f "$CERT" ]; then
    echo "[entrypoint] TLS cert present ($CERT) -> nginx TLS on :12470"
    mkdir -p /tmp/nginx-proxy /tmp/nginx-body
    nginx -c /srv/app/docker/nginx-tls.conf -g 'daemon off;' &
else
    echo "[entrypoint] no cert at $CERT -> http only on :11470"
fi

wait "$APP_PID"
