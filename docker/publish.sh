#!/bin/sh
# Tag + push the all-in-one image to Docker Hub. Run after: docker login -u androshack
# Env overrides: LOCAL (source image), REPO, VERSION
set -e

LOCAL="${LOCAL:-stremio-libtorrent-server:dev}"
REPO="${REPO:-androshack/stremio-libtorrent-server}"
VERSION="${VERSION:-0.2.4}"

docker tag "$LOCAL" "$REPO:$VERSION"
docker tag "$LOCAL" "$REPO:latest"
docker push "$REPO:$VERSION"
docker push "$REPO:latest"
echo "pushed $REPO:$VERSION and $REPO:latest"

# The Hub *overview* is separate from the image and a push never updates it -- it silently drifted two
# releases behind once. Sync it here so a release cannot ship with stale docs on the landing page. The
# pushes above already prove a docker login exists, and that is the credential push-readme.sh reuses.
REPO="$REPO" sh "$(dirname "$0")/push-readme.sh"
