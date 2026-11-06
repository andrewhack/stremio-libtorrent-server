#!/bin/sh
# Fail the image build when the base image no longer provides a program the container runs.
#
# Every one of these arrives with the base image, not with this repo, so a base rebuilt without one
# used to produce an image that built and smoke-tested fine and then failed at startup -- or, for
# curl, only once Docker's health check ran. Checked at build time instead, by name:
#   nginx               serves the web player and the API on :8080 and :12470 (entrypoint.sh)
#   node                runs the trusted-certificate fetch (entrypoint.sh, cert-fetch.sh)
#   openssl             the self-signed fallback, and vetting a certificate to reuse
#                       (entrypoint.sh, cert-reuse.sh)
#   curl                the certificate service probe (cert-fetch.sh) and the image's HEALTHCHECK
#   timeout sed date tr what the certificate scripts use
#   ffmpeg ffprobe      transcoding, probing and subtitle extraction (the server itself)
set -u

missing=""
for tool in nginx node openssl curl timeout sed date tr ffmpeg ffprobe; do
    command -v "$tool" >/dev/null 2>&1 || missing="$missing $tool"
done

if [ -n "$missing" ]; then
    echo "check-runtime-tools: the base image does not provide:$missing" >&2
    exit 1
fi
echo "check-runtime-tools: every program the container runs is present"
