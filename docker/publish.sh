#!/bin/sh
# Build, gate, and publish the all-in-one image, then sync the Hub overview.
#
#   docker login -u androshack
#   ./docker/publish.sh                 # build -> smoke -> push :$VERSION and :latest -> sync README
#   DRY_RUN=1 ./docker/publish.sh       # everything except the pushes and the overview sync
#   SKIP_BUILD=1 ./docker/publish.sh    # publish an image that is already built
#
# This replaces the per-release ~/build-NNN.sh scripts. Same steps in the same order, minus the two
# things that were copy-pasted into each one and went stale: the hand-typed version, and a `git reset
# --hard origin/main` that silently discarded whatever was in the tree.
#
# Env overrides: REPO, LOCAL, VERSION, DRY_RUN, SKIP_BUILD, SMOKE_PORT, ALLOW_DIRTY,
#                ALLOW_VERSION_MISMATCH.
set -e

REPO="${REPO:-androshack/stremio-libtorrent-server}"
LOCAL="${LOCAL:-$REPO:latest}"
SMOKE_PORT="${SMOKE_PORT:-18099}"
SMOKE_NAME="${SMOKE_NAME:-stremio-publish-smoke}"
HERE=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)

command -v jq >/dev/null 2>&1 || { echo "jq is required" >&2; exit 2; }

# The old scripts reset --hard to origin/main so the image matched a known commit. Refusing is the
# same guarantee without the part that throws away work you forgot you had. Untracked files are
# ignored: they do not reach the image (.dockerignore aside, they are not in git) and the release
# host accumulates them -- stray logs, a core dump -- which would make this unusable.
if [ -z "${ALLOW_DIRTY:-}" ] && git -C "$HERE" rev-parse --git-dir >/dev/null 2>&1; then
    dirty=$(git -C "$HERE" status --porcelain --untracked-files=no)
    if [ -n "$dirty" ]; then
        echo "ERROR: tracked files are modified -- the image would not match any commit:" >&2
        echo "$dirty" | sed 's/^/  /' >&2
        echo "  commit them, stash them, or set ALLOW_DIRTY=1" >&2
        exit 2
    fi
fi
echo "releasing from commit $(git -C "$HERE" rev-parse --short HEAD 2>/dev/null || echo '(not a git checkout)')"

if [ -z "${SKIP_BUILD:-}" ]; then
    echo "building $LOCAL"
    docker build -t "$LOCAL" "$HERE"
fi

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

# Smoke gate: start the thing and make it answer. Reading the version out of the image proves what was
# packaged; this proves it boots and serves, which is the failure that actually reaches users. /health
# returns 503 until every component is up, so curl -fsS staying empty is itself the signal -- poll
# rather than sleep a fixed nine seconds and hope.
cleanup_smoke() { docker rm -f "$SMOKE_NAME" >/dev/null 2>&1 || true; }
trap cleanup_smoke EXIT INT TERM
cleanup_smoke
echo "smoke-testing $LOCAL on :$SMOKE_PORT"
docker run -d --name "$SMOKE_NAME" -p "$SMOKE_PORT:11470" "$LOCAL" >/dev/null
got=""
i=0
while [ "$i" -lt 60 ]; do
    got=$(curl -fsS "http://127.0.0.1:$SMOKE_PORT/health" 2>/dev/null | jq -r '.version // empty' 2>/dev/null || true)
    [ -n "$got" ] && break
    i=$((i + 1))
    sleep 1
done
if [ -z "$got" ]; then
    echo "SMOKE FAIL: /health never came up healthy in ${i}s" >&2
    echo "  last body: $(curl -sS -o - -w ' [HTTP %{http_code}]' "http://127.0.0.1:$SMOKE_PORT/health" 2>&1 | head -c 300)" >&2
    echo "  container logs:" >&2
    docker logs --tail 20 "$SMOKE_NAME" 2>&1 | sed 's/^/    /' >&2
    exit 3
fi
if [ "$got" != "$VERSION" ]; then
    echo "SMOKE FAIL: /health reports $got but this publishes as $VERSION" >&2
    exit 3
fi
echo "SMOKE OK: healthy in ${i}s, reports $got"
cleanup_smoke
trap - EXIT INT TERM

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
