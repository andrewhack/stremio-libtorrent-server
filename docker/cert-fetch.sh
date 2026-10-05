#!/bin/sh
# Fetches the trusted wildcard certificate for the magic-DNS zone $2 and installs it at $1. Answers
# 0 only when a certificate was installed; anything else leaves $1 exactly as it was.
#
# The fetch is judged by the file it writes, never by how it exits: Stremio's certificate.js exits
# 0 even after every attempt against its certificate service failed. The entrypoint used to trust
# that, and copying the file that was never written ended the container under its `set -e` -- a
# crash loop for as long as the service failed quickly.
#
# A failed fetch prints one line, REASON<TAB>DETAIL, for cert-status.sh: certificate.js only says
# "failed", so the service is asked once more, directly, to learn why.
#
# Its own script for the same reason as cert-reuse.sh: entrypoint.sh cannot be exercised by a test.
CERT="$1"
ZONE="$2"
DIR="${CERT_FETCH_DIR:-/srv/stremio-server}"
CMD="${CERT_FETCH_CMD:-node certificate.js --action fetch}"
NEW="$DIR/certificates.pem"
HERE=$(dirname "$0")
PROBE_URL="${CERT_PROBE_URL:-http://api.strem.io/api/certificateGet}"
PROBE_TIMEOUT="${CERT_PROBE_TIMEOUT:-15}"

# A file from an earlier fetch must not pass for this one's.
rm -f "$NEW"
# Time-boxed: on an offline or isolated LAN the fetch would otherwise hang on DNS/HTTP timeouts and
# keep uvicorn from ever starting. Its exit status is deliberately ignored, see above, and so is its
# output: a stack trace per attempt explains nothing; the reason below does.
(cd "$DIR" && timeout "${CERT_FETCH_TIMEOUT:-30}" $CMD) >/dev/null 2>&1 || true
# Usable means what cert-reuse.sh means by it, at a zero window: the wildcard for this zone, valid
# now. Installed through a temporary file so a failed copy cannot leave half a certificate behind.
if sh "$HERE/cert-reuse.sh" "$NEW" "$ZONE" 0; then
    cp "$NEW" "$CERT.new" && mv "$CERT.new" "$CERT" && exit 0
    printf 'other\tcould not install the certificate\n'
    exit 1
fi

# Failed: ask the service once more, the same way certificate.js does, and sort its answer into a
# reason a person can act on. 546 is the service's own refusal for an address it will not issue for.
BODY=$(mktemp)
CODE=$(curl -s -m "$PROBE_TIMEOUT" -o "$BODY" -w '%{http_code}' -X POST \
    -H 'Content-Type: application/json' \
    --data "{\"authKey\":null,\"ipAddress\":\"${IPADDRESS}\"}" "$PROBE_URL")
RC=$?
MSG=$(sed -n 's/.*"message" *: *"\([^"]*\)".*/\1/p' "$BODY" | head -n 1 | tr -d '\t\r\n')
if [ "$(printf '%s' "$MSG" | wc -c)" -gt 160 ]; then
    # cut counts bytes, and half a multi-byte character would leave the status file no longer
    # UTF-8 (/health would then ignore it): cut, then drop a character the cut may have split.
    MSG=$(printf '%s' "$MSG" | LC_ALL=C cut -b1-160 | LC_ALL=C sed 's/[\xC0-\xFF][\x80-\xBF]*$//')
fi
rm -f "$BODY"
case "$RC" in
    0)
        case "$CODE" in
            546|4??) REASON=refused ;;
            5??)     REASON=unavailable; [ -n "$MSG" ] || MSG="HTTP $CODE" ;;
            *)       REASON=other; [ -n "$MSG" ] || MSG="HTTP $CODE" ;;
        esac ;;
    6|7) REASON=no-internet; MSG="" ;;
    28)  REASON=unavailable; MSG="no answer within ${PROBE_TIMEOUT}s" ;;
    *)   REASON=other; MSG="" ;;
esac
printf '%s\t%s\n' "$REASON" "$MSG"
exit 1
