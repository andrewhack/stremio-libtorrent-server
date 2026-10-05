#!/bin/sh
# Keeps asking for the trusted certificate in the background while the server is not serving a
# fresh one: STATE "waiting" (self-signed served) or "renewing" (trusted and still valid, but its
# renewal failed). When the certificate on disk changes it asks the entrypoint to restart in place,
# which serves it with all the wiring of a normal start -- there is no second way to switch.
# Only a CHANGED certificate restarts: if the service hands back the one already served (the shared
# wildcard itself near expiry) the box keeps it and keeps asking, instead of restarting every round.
#
#   cert-retry.sh CERT ZONE STATUS_FILE STATE HOST PARENT_PID
CERT="$1"; ZONE="$2"; STATUS="$3"; STATE="$4"; HOST="$5"; PARENT="$6"
HERE=$(dirname "$0")
INTERVAL="${CERT_RETRY_INTERVAL:-1800}"
MAX="${CERT_RETRY_MAX:-0}"   # tests only: stop after this many attempts; 0 = never
RESTART="${CERT_RESTART_CMD:-kill -USR1 $PARENT}"
SOURCE=self-signed
[ "$STATE" = renewing ] && SOURCE=stremio.rocks
n=0
while :; do
    sleep "$INTERVAL"
    n=$((n + 1))
    BEFORE=$(cksum < "$CERT" 2>/dev/null)
    if OUT=$(sh "$HERE/cert-fetch.sh" "$CERT" "$ZONE"); then
        if [ "$(cksum < "$CERT" 2>/dev/null)" != "$BEFORE" ]; then
            echo "[cert] trusted certificate received -> restarting to serve it"
            $RESTART
            exit 0
        fi
        OUT=$(printf 'other\tthe service returned the certificate already in use')
    fi
    REASON=$(printf '%s' "$OUT" | cut -f1)
    DETAIL=$(printf '%s' "$OUT" | cut -f2)
    sh "$HERE/cert-status.sh" "$STATUS" "$SOURCE" "$STATE" "$REASON" "$DETAIL" "$HOST" \
        "$(( $(date -u +%s) + INTERVAL ))" || true
    if [ "$MAX" -gt 0 ] && [ "$n" -ge "$MAX" ]; then
        exit 0
    fi
done
