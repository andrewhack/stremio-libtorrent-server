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
    SROCKS_ZONE="519b6502d940.stremio.rocks"
    IPD=$(echo "$IPADDRESS" | sed "s/[.]/-/g")
    SROCKS_DOMAIN="${IPD}.${SROCKS_ZONE}"
    HAVE_SROCKS=""
    # A trusted cert we already hold and that still has a month to run is kept: the cert service is
    # not called on every restart, and a box that is briefly offline still comes up trusted.
    # Anything the check cannot settle falls through to the fetch below, unchanged.
    CERT_STATE=ok; CERT_REASON=""; CERT_DETAIL=""
    if sh /srv/app/docker/cert-reuse.sh "$CERT" "$SROCKS_ZONE"; then
        echo "[entrypoint] trusted cert on disk is still valid -> keeping it, no fetch"
        HAVE_SROCKS=1
    else
        echo "[entrypoint] IPADDRESS=$IPADDRESS -> fetching trusted stremio.rocks cert"
        # Time-boxed, and judged by the certificate it installs rather than by its exit code (see
        # cert-fetch.sh). When it fails -- an offline LAN, or the certificate service down -- it says
        # why, a trusted certificate already here is kept for as long as it is valid at all, and the
        # retry loop below keeps asking every half hour.
        if FETCH_OUT=$(sh /srv/app/docker/cert-fetch.sh "$CERT" "$SROCKS_ZONE"); then
            HAVE_SROCKS=1
        else
            CERT_REASON=$(printf '%s' "$FETCH_OUT" | cut -f1)
            CERT_DETAIL=$(printf '%s' "$FETCH_OUT" | cut -f2)
            if sh /srv/app/docker/cert-reuse.sh "$CERT" "$SROCKS_ZONE" 0; then
                echo "[entrypoint] stremio.rocks fetch failed -> keeping the trusted cert on disk until it expires"
                HAVE_SROCKS=1
                CERT_STATE=renewing
            else
                echo "[entrypoint] stremio.rocks fetch failed -> falling back to existing/self-signed cert"
                CERT_STATE=waiting
            fi
        fi
    fi
    # Both paths still do this: it depends on IPADDRESS, which can change between starts while the
    # wildcard cert stays valid.
    if [ -n "$HAVE_SROCKS" ]; then
        grep -q "$SROCKS_DOMAIN" /etc/hosts 2>/dev/null || echo "${IPADDRESS} ${SROCKS_DOMAIN}" >> /etc/hosts
        (cd /srv/stremio-server && node certificate.js --action load \
            --pem-path "$CERT" --domain "$SROCKS_DOMAIN" --json-path "$CACHE/httpsCert.json") || true
        echo "[entrypoint] trusted cert for $SROCKS_DOMAIN"
        [ -z "${SERVER_URL}" ] && SERVER_URL="https://${SROCKS_DOMAIN}:12470/"
    fi
fi
CERT_SOURCE=own
if [ -f "$CERT" ]; then
    [ -n "${IPADDRESS}" ] || echo "[entrypoint] using existing cert $CERT (bring-your-own)"
else
    echo "[entrypoint] no trusted cert -> self-signed (CN=${DOMAIN:-localhost}); TVs may reject it"
    openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
        -keyout "${CERT}.key" -out "${CERT}.crt" -subj "/CN=${DOMAIN:-localhost}" >/dev/null 2>&1
    cat "${CERT}.crt" "${CERT}.key" > "$CERT"
    rm -f "${CERT}.key" "${CERT}.crt"
    CERT_SOURCE=self-signed
fi
# What /health (and through it the appliance) reports about the certificate, written at every start.
CERT_STATUS="$CACHE/cert-status.json"
if [ -n "${IPADDRESS}" ]; then
    CERT_SOURCE=self-signed
    [ -n "$HAVE_SROCKS" ] && CERT_SOURCE=stremio.rocks
    CERT_NEXT=""
    case "$CERT_STATE" in
        waiting|renewing) CERT_NEXT=$(( $(date -u +%s) + ${CERT_RETRY_INTERVAL:-1800} )) ;;
    esac
    sh /srv/app/docker/cert-status.sh "$CERT_STATUS" "$CERT_SOURCE" "$CERT_STATE" \
        "$CERT_REASON" "$CERT_DETAIL" "$SROCKS_DOMAIN" "$CERT_NEXT" || true
else
    sh /srv/app/docker/cert-status.sh "$CERT_STATUS" "$CERT_SOURCE" ok "" "" "" "" || true
fi

# 2) Point the bundled web player at the streaming server (stock localStorage mechanism).
SEED_SRC="/srv/stremio-server/localStorage.json"
SEED_DST="/srv/stremio-server/build/localStorage.json"
if [ -f "$SEED_SRC" ]; then
    cp "$SEED_SRC" "$SEED_DST"
    # The player's loader.js HEADs server_url.env and applies the server URL seeded below only when
    # that answers 2xx; otherwise it ignores SERVER_URL and uses the page's own origin. The file was
    # never shipped -- the HEAD succeeded only because nginx used to answer every unknown path with
    # index.html. Written here so the seed no longer rides on that fallback. Its content is unused.
    : > /srv/stremio-server/build/server_url.env
    if [ -n "${SERVER_URL}" ]; then
        case "$SERVER_URL" in */) ;; *) SERVER_URL="$SERVER_URL/" ;; esac
        sed -i "s|http://127.0.0.1:11470/|${SERVER_URL}|g" "$SEED_DST"
        echo "[entrypoint] web player -> $SERVER_URL"
        # The v6 desktop app re-points itself at its bundled 127.0.0.1 server on every launch, so the
        # Streaming Server URL won't stick. Launching it with a non-default --webui-url skips that
        # injection; --development also stops the bundled server. Trusted (:12470) URL only.
        echo "[entrypoint] desktop app -> add launch flags: --development --webui-url=$SERVER_URL"
    else
        echo "[entrypoint] web player -> default 127.0.0.1:11470 (set SERVER_URL for remote clients)"
    fi
fi

# /proxy counts a web page on SERVER_URL's host as this server's own (1.6.9). The IPADDRESS branch
# sets SERVER_URL inside this script, and uvicorn sees only what is exported.
export SERVER_URL

# 3) Run uvicorn (API, internal :11470) + nginx (web player + API proxy on :8080 and :12470).
mkdir -p /tmp/nx-proxy /tmp/nx-body
# Render the cert path into the nginx config (honors a custom STREMIOSRV_CACHE_ROOT).
sed "s#/root/.stremio-server/certificates.pem#${CERT}#g" \
    /srv/app/docker/nginx-allinone.conf > /tmp/nginx-allinone.conf
# --no-access-log: don't log every request — those lines include infohash/stream paths (a
# content-neutrality + privacy concern, like nginx's access_log off) and would otherwise bury real
# warnings/errors so the admin Logs card surfaces nothing.
# --timeout-graceful-shutdown: a player holding a stream open must not hold up a stop.
/srv/app/.venv/bin/uvicorn stremiosrv.app:build_app --factory --host 0.0.0.0 --port 11470 \
  --no-access-log --timeout-graceful-shutdown 5 &
APP_PID=$!
nginx -c /tmp/nginx-allinone.conf -g 'daemon off;' &
NGINX_PID=$!
# The certificate service failed at start: keep asking in the background (cert-retry.sh). When the
# certificate on disk changes it sends USR1, and the whole startup runs again below, in place.
RETRY_PID=""
case "${CERT_STATE:-ok}" in
    waiting|renewing)
        sh /srv/app/docker/cert-retry.sh "$CERT" "$SROCKS_ZONE" "$CERT_STATUS" "$CERT_STATE" \
            "$SROCKS_DOMAIN" $$ &
        RETRY_PID=$! ;;
esac
# This shell is PID 1, and PID 1 ignores SIGTERM unless it traps it: `docker stop` used to sit out
# its grace period and SIGKILL everything, so the server never shut down (and never gave its
# cache-root claim back). Pass the signal on, then wait for the server to finish shutting down --
# a trapped signal ends the first `wait` early with 143, and the second one collects the server's
# own exit status. USR1 is the same stop, followed by a fresh start of this script.
trap 'stopping=1; kill -TERM "$NGINX_PID" "$APP_PID" $RETRY_PID 2>/dev/null || true' TERM INT
trap 'restarting=1; kill -TERM "$NGINX_PID" "$APP_PID" 2>/dev/null || true' USR1
rc=0
wait "$APP_PID" || rc=$?
if [ -n "${stopping:-}${restarting:-}" ]; then
    rc=0
    wait "$APP_PID" || rc=$?
fi
if [ -n "${restarting:-}" ] && [ -z "${stopping:-}" ]; then
    wait "$NGINX_PID" 2>/dev/null || true  # its ports must be free before the new one binds them
    echo "[cert] restarting in place to serve the trusted certificate"
    exec "$0"
fi
exit "$rc"
