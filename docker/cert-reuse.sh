#!/bin/sh
# Answers 0 when the certificate at $1 is worth keeping instead of fetching a new one: it still has
# a month to run and it is the wildcard for the magic-DNS zone $2. Every other answer -- no file, an
# unreadable one, an openssl that cannot parse it, a certificate for some other name -- is a
# non-zero exit, so the caller fetches exactly as it did before this check existed.
#
# Its own script rather than four lines inside entrypoint.sh, because entrypoint.sh ends by running
# uvicorn and nginx and so cannot be exercised by a test. This can be, and is.
CERT="$1"
ZONE="$2"
# 30 days. This MUST stay above CERT_WARN_DAYS in src/stremiosrv/health.py, which is 14: from that
# many days out /health reports the certificate degraded and answers 503, the image's own
# HEALTHCHECK fails on it, and the container shows unhealthy. A window below that threshold would
# keep handing back a certificate the same image is already alarming about, and a restart -- the
# one thing an operator would try -- would not renew it. tests/test_cert_reuse.py guards the gap.
RENEW_WITHIN=2592000

# An empty zone leaves nothing to identify a certificate by, so it is refused here rather than left
# to the comparison below, which would then be deciding on an empty string.
[ -n "$ZONE" ] || exit 1

# -checkend exits non-zero when the certificate would expire inside the window, and when there is
# nothing readable at that path at all.
openssl x509 -checkend "$RENEW_WITHIN" -noout -in "$CERT" >/dev/null 2>&1 || exit 1

# Only the SAN decides what a TLS client will accept, so only the SAN is read, and each name in it
# is matched whole: a name that merely contains the zone, like evil.<zone>.attacker.example, is
# somebody else's. The name has to be the wildcard the service issues, not some single host inside
# the zone -- a per-host certificate is exactly the one that stops being valid when IPADDRESS
# changes, which is the case this whole check has to survive. set -f because a SAN holds a literal
# "*" that must not become a filename.
set -f
for name in $(openssl x509 -noout -ext subjectAltName -in "$CERT" 2>/dev/null | tr ',' ' '); do
    case "$name" in
        DNS:\*."$ZONE") exit 0 ;;
    esac
done
exit 1
