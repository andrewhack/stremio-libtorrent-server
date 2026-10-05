#!/bin/sh
# Writes the certificate status file and, while the trusted certificate is missing or cannot be
# renewed, one plain log line saying why and when the next try is. /health mirrors the file, and the
# appliance's settings page and console read it from there. Its own script so the wording and the
# format live in one place for the entrypoint's first attempt and every retry alike.
#
#   cert-status.sh FILE SOURCE STATE REASON DETAIL HOST NEXT_EPOCH
#
# Empty REASON, DETAIL, HOST and NEXT_EPOCH are written as null. The file is replaced atomically.
FILE="$1"; SOURCE="$2"; STATE="$3"; REASON="$4"; DETAIL="$5"; HOST="$6"; NEXT="$7"

js() {  # a JSON string with control characters dropped, or null when empty
    if [ -n "$1" ]; then
        printf '"%s"' "$(printf '%s' "$1" | tr -d '\000-\037' | sed 's/\\/\\\\/g; s/"/\\"/g')"
    else
        printf null
    fi
}
iso() { date -u -d "@$1" +%Y-%m-%dT%H:%M:%SZ; }

NOW=$(date -u +%s)
LAST=""
case "$STATE" in waiting|renewing) LAST=$(iso "$NOW") ;; esac
NEXT_ISO=""
[ -n "$NEXT" ] && NEXT_ISO=$(iso "$NEXT")
TMP="$FILE.tmp.$$"
printf '{"source":%s,"state":%s,"reason":%s,"detail":%s,"host":%s,"lastTry":%s,"nextTry":%s,"writtenAt":%s}\n' \
    "$(js "$SOURCE")" "$(js "$STATE")" "$(js "$REASON")" "$(js "$DETAIL")" "$(js "$HOST")" \
    "$(js "$LAST")" "$(js "$NEXT_ISO")" "$(js "$(iso "$NOW")")" > "$TMP" && mv "$TMP" "$FILE" || exit 1

[ -n "$NEXT" ] || exit 0
case "$REASON" in
    refused)     WHY="Stremio's certificate service refused ${HOST}${DETAIL:+ ($DETAIL)}" ;;
    unavailable) WHY="Stremio's certificate service is not answering${DETAIL:+ ($DETAIL)}" ;;
    no-internet) WHY="this server cannot reach Stremio's certificate service (no internet?)" ;;
    *)           WHY="Stremio's certificate service did not hand one over${DETAIL:+ ($DETAIL)}" ;;
esac
AT=$(date -u -d "@$NEXT" +%H:%M)
case "$STATE" in
    waiting)  echo "[cert] no trusted certificate yet: $WHY; trying again at $AT UTC. Meanwhile the web player (port 8080) still works." ;;
    renewing) echo "[cert] could not renew the trusted certificate, which is still valid: $WHY; trying again at $AT UTC." ;;
esac
exit 0
