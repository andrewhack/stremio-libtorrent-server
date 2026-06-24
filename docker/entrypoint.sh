#!/bin/sh
# All-in-one: the bundled Stremio web player + our libtorrent streaming server on one origin —
# HTTP :8080 (LAN) and HTTPS :12470 (cert). uvicorn (API) stays internal on :11470.
set -e

CACHE="${STREMIOSRV_CACHE_ROOT:-/root/.stremio-server}"
CERT="$CACHE/${CERT_FILE:-certificates.pem}"

# 1) TLS cert for HTTPS :12470. TVs require a TRUSTED cert; priority:
#    a. IPADDRESS set -> fetch/refresh a trusted Let's Encrypt *.stremio.rocks cert (TV-compatible,
#       zero config; the dashed-IP subdomain resolves to your IP via Stremio's magic DNS).
#    b. else a cert already at $CERT -> bring-your-own.
#    c. else -> self-signed (HTTPS still starts, but browsers warn and TVs reject).
mkdir -p "$CACHE"
if [ -n "${IPADDRESS}" ]; then
    echo "[entrypoint] IPADDRESS=$IPADDRESS -> fetching trusted stremio.rocks cert"
    if (cd /srv/stremio-server && node certificate.js --action fetch); then
        IPD=$(echo "$IPADDRESS" | sed "s/[.]/-/g")
        SROCKS_DOMAIN="${IPD}.519b6502d940.stremio.rocks"
        cp /srv/stremio-server/certificates.pem "$CERT"
        grep -q "$SROCKS_DOMAIN" /etc/hosts 2>/dev/null || echo "${IPADDRESS} ${SROCKS_DOMAIN}" >> /etc/hosts
        (cd /srv/stremio-server && node certificate.js --action load \
            --pem-path "$CERT" --domain "$SROCKS_DOMAIN" --json-path "$CACHE/httpsCert.json") || true
        echo "[entrypoint] trusted cert for $SROCKS_DOMAIN"
        [ -z "${SERVER_URL}" ] && SERVER_URL="https://${SROCKS_DOMAIN}:12470/"
    else
        echo "[entrypoint] stremio.rocks fetch failed -> falling back to existing/self-signed cert"
    fi
fi
if [ -f "$CERT" ]; then
    [ -n "${IPADDRESS}" ] || echo "[entrypoint] using existing cert $CERT (bring-your-own)"
else
    echo "[entrypoint] no trusted cert -> self-signed (CN=${DOMAIN:-localhost}); TVs may reject it"
    openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
        -keyout "${CERT}.key" -out "${CERT}.crt" -subj "/CN=${DOMAIN:-localhost}" >/dev/null 2>&1
    cat "${CERT}.crt" "${CERT}.key" > "$CERT"
    rm -f "${CERT}.key" "${CERT}.crt"
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
# Render the cert path into the nginx config (honors a custom STREMIOSRV_CACHE_ROOT).
sed "s#/root/.stremio-server/certificates.pem#${CERT}#g" \
    /srv/app/docker/nginx-allinone.conf > /tmp/nginx-allinone.conf
/srv/app/.venv/bin/uvicorn stremiosrv.app:build_app --factory --host 0.0.0.0 --port 11470 &
APP_PID=$!
nginx -c /tmp/nginx-allinone.conf -g 'daemon off;' &
wait "$APP_PID"
