#!/bin/sh
# Fetches the trusted wildcard certificate for the magic-DNS zone $2 and installs it at $1. Answers
# 0 only when a certificate was installed; anything else leaves $1 exactly as it was.
#
# The fetch is judged by the file it writes, never by how it exits: Stremio's certificate.js exits
# 0 even after every attempt against its certificate service failed. The entrypoint used to trust
# that, and copying the file that was never written ended the container under its `set -e` -- a
# crash loop for as long as the service failed quickly.
#
# Its own script for the same reason as cert-reuse.sh: entrypoint.sh cannot be exercised by a test.
CERT="$1"
ZONE="$2"
DIR="${CERT_FETCH_DIR:-/srv/stremio-server}"
CMD="${CERT_FETCH_CMD:-node certificate.js --action fetch}"
NEW="$DIR/certificates.pem"
HERE=$(dirname "$0")

# A file from an earlier fetch must not pass for this one's.
rm -f "$NEW"
# Time-boxed: on an offline or isolated LAN the fetch would otherwise hang on DNS/HTTP timeouts and
# keep uvicorn from ever starting. Its exit status is deliberately ignored, see above.
(cd "$DIR" && timeout "${CERT_FETCH_TIMEOUT:-30}" $CMD) || true
# Usable means what cert-reuse.sh means by it, at a zero window: the wildcard for this zone, valid
# now. Installed through a temporary file so a failed copy cannot leave half a certificate behind.
sh "$HERE/cert-reuse.sh" "$NEW" "$ZONE" 0 || exit 1
cp "$NEW" "$CERT.new" && mv "$CERT.new" "$CERT"
