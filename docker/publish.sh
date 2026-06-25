#!/bin/sh
# Tag + push the all-in-one image to Docker Hub. Run after: docker login -u androshack
# Env overrides: LOCAL (source image), REPO, VERSION
set -e

LOCAL="${LOCAL:-stremio-libtorrent-server:dev}"
REPO="${REPO:-androshack/stremio-libtorrent-server}"
VERSION="${VERSION:-0.2.3}"

docker tag "$LOCAL" "$REPO:$VERSION"
docker tag "$LOCAL" "$REPO:latest"
docker push "$REPO:$VERSION"
docker push "$REPO:latest"
echo "pushed $REPO:$VERSION and $REPO:latest"
