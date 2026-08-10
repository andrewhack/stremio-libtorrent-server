#!/bin/sh
# Tag + push the all-in-one image to Docker Hub, then sync the Hub overview.
#
#   docker build -t androshack/stremio-libtorrent-server:latest .
#   docker login -u androshack
#   ./docker/publish.sh                 # derives the version from the image it is about to push
#   DRY_RUN=1 ./docker/publish.sh       # show what it would do, touch nothing
#
# Env overrides: LOCAL (source image), REPO, VERSION, DRY_RUN, ALLOW_VERSION_MISMATCH.
set -e

REPO="${REPO:-androshack/stremio-libtorrent-server}"
LOCAL="${LOCAL:-$REPO:latest}"
HERE=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)

# Ask the image what it is, rather than keeping a version literal in this script. The literal said
# 0.2.4 long after 1.2.0 had shipped, so a run that forgot VERSION= would have published a build
# under a stale tag *and* moved :latest onto it. A derived version cannot rot, and it is read from
# the artefact actually being pushed, so the tag always describes its contents.
IMAGE_VERSION=$(docker run --rm --entrypoint python "$LOCAL" \
    -c "import importlib.metadata as m; print(m.version('stremiosrv'))" || true)
IMAGE_VERSION=$(echo "$IMAGE_VERSION" | tr -d ' \r')
VERSION="${VERSION:-$IMAGE_VERSION}"
: "${VERSION:?could not read a version out of $LOCAL (see the error above) -- pass VERSION=x.y.z}"

# An image whose version differs from this checkout is a stale build, and pushing it would drag
# :latest backwards onto it -- the one outcome here that is genuinely hard to undo, because every
# `docker pull` in the world follows :latest. Hard error, with a way through for the rare deliberate
# case. `head -1` takes [project].version, which pyproject.toml declares before any other table.
TREE_VERSION=$(sed -n 's/^version = "\(.*\)"/\1/p' "$HERE/pyproject.toml" | head -1)
if [ -n "$IMAGE_VERSION" ] && [ -n "$TREE_VERSION" ] && [ "$IMAGE_VERSION" != "$TREE_VERSION" ]; then
    echo "ERROR: $LOCAL contains $IMAGE_VERSION but this checkout is $TREE_VERSION" >&2
    echo "  rebuild it, or set ALLOW_VERSION_MISMATCH=1 if you really mean to publish that build" >&2
    [ -n "${ALLOW_VERSION_MISMATCH:-}" ] || exit 2
fi
# A deliberate VERSION= that disagrees with the image is a human decision, so this one only warns.
if [ -n "$IMAGE_VERSION" ] && [ "$VERSION" != "$IMAGE_VERSION" ]; then
    echo "WARNING: tagging as $VERSION, but $LOCAL contains $IMAGE_VERSION"
fi

echo "publishing $LOCAL as $REPO:$VERSION and $REPO:latest"
if [ -n "${DRY_RUN:-}" ]; then
    echo "DRY_RUN set -- not tagging, pushing, or syncing the overview"
    exit 0
fi

docker tag "$LOCAL" "$REPO:$VERSION"
docker tag "$LOCAL" "$REPO:latest"
docker push "$REPO:$VERSION"
docker push "$REPO:latest"
echo "pushed $REPO:$VERSION and $REPO:latest"

# The Hub *overview* is separate from the image and a push never updates it -- it silently drifted two
# releases behind once. Sync it here so a release cannot ship with stale docs on the landing page. The
# pushes above already prove a docker login exists, and that is the credential push-readme.sh reuses.
REPO="$REPO" sh "$(dirname "$0")/push-readme.sh"
