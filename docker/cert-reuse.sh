#!/bin/sh
# Answers 0 when the certificate at $1 is worth keeping instead of fetching a new one: it still has
# more than a week to run and it carries the magic-DNS zone $2. Every other answer -- no file, an
# unreadable one, an openssl that cannot parse it, a certificate for some other name -- is a
# non-zero exit, so the caller fetches exactly as it did before this check existed.
#
# Its own script rather than four lines inside entrypoint.sh, because entrypoint.sh ends by running
# uvicorn and nginx and so cannot be exercised by a test. This can be, and is.
CERT="$1"
ZONE="$2"
RENEW_WITHIN=604800   # 7 days, in seconds: renew a week early rather than on the last day

# An empty zone would make the grep below match every certificate, including the self-signed one.
[ -n "$ZONE" ] || exit 1

# -checkend exits non-zero when the certificate would expire inside the window, and when there is
# nothing readable at that path at all.
openssl x509 -checkend "$RENEW_WITHIN" -noout -in "$CERT" >/dev/null 2>&1 || exit 1

# Only the SAN decides what a TLS client will accept, so only the SAN is read, and each name in it
# is matched whole: a name that merely contains the zone, like evil.<zone>.attacker.example, is
# somebody else's. set -f because a SAN holds a literal "*" that must not become a filename.
set -f
for name in $(openssl x509 -noout -ext subjectAltName -in "$CERT" 2>/dev/null | tr ',' ' '); do
    case "$name" in
        DNS:*."$ZONE") exit 0 ;;
    esac
done
exit 1
