#!/bin/sh
# Build, gate, and publish the all-in-one image, then sync the Hub overview.
#
#   docker login -u androshack
#   ./docker/publish.sh                 # build -> smoke -> push image -> sync README -> tag + release
#   DRY_RUN=1 ./docker/publish.sh       # build + smoke only; publishes and releases nothing
#   SKIP_BUILD=1 ./docker/publish.sh    # publish an image that is already built
#   SKIP_GITHUB=1 ./docker/publish.sh   # Docker Hub only, no git tag and no GitHub release
#
# This replaces the per-release ~/build-NNN.sh and ~/release-NNN.sh scripts. Same steps in the same
# order, minus the two things that were copy-pasted into each one and went stale: the hand-typed
# version, and a `git reset --hard origin/main` that silently discarded whatever was in the tree.
#
# Env overrides: REPO, LOCAL, VERSION, DRY_RUN, SKIP_BUILD, SKIP_GITHUB, SMOKE_PORT, NOTES_FILE,
#                RELEASE_NAME, GH_TOKEN, ALLOW_DIRTY, ALLOW_VERSION_MISMATCH.
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

TREE_VERSION=$(sed -n 's/^version = "\(.*\)"/\1/p' "$HERE/pyproject.toml" | head -1)

# Everything knowable without building is checked here, before the build. Discovering that the notes
# are missing *after* the image is on Docker Hub leaves a half-published release: the image is out and
# public, and there is no undo for that. The version is not derived from the image yet, so preflight
# uses the checkout's -- and the two are reconciled below.
PRE_VERSION="${VERSION:-$TREE_VERSION}"
TAG="v$PRE_VERSION"
NOTES_FILE="${NOTES_FILE:-$HERE/docs/releases/$TAG.md}"
if [ -z "${SKIP_GITHUB:-}" ]; then
    if [ ! -f "$NOTES_FILE" ]; then
        echo "ERROR: no release notes at $NOTES_FILE" >&2
        echo "  write them there, or pass NOTES_FILE=, or SKIP_GITHUB=1 for a Docker-Hub-only push" >&2
        exit 2
    fi
    existing=$(git -C "$HERE" rev-list -n 1 "$TAG" 2>/dev/null || true)
    if [ -n "$existing" ] && [ "$existing" != "$(git -C "$HERE" rev-parse HEAD)" ]; then
        # ^{commit}, because rev-parse on an annotated tag yields the tag object's sha, not the
        # commit's -- printing that would send you looking for a sha that is in no log.
        echo "ERROR: tag $TAG already exists and points at $(git -C "$HERE" rev-parse --short "$TAG^{commit}"), not HEAD" >&2
        echo "  bump the version, or delete the tag if it was created by mistake" >&2
        exit 2
    fi
    # GitHub renders the release title above the body, so a notes file that opens with its own title
    # shows it twice -- v1.1.0 and v1.2.0 both went out that way. Consume the first heading into the
    # title instead, and strip it from the body below, so the duplication cannot happen by omission.
    HEADING=$(sed -n '1s/^#\{1,\} *//p' "$NOTES_FILE" | tr -d '\r')
    if [ -z "${RELEASE_NAME:-}" ]; then
        if [ -n "$HEADING" ]; then RELEASE_NAME="$TAG - $HEADING"; else RELEASE_NAME="$TAG"; fi
    fi
    echo "release notes: $NOTES_FILE ($(wc -l < "$NOTES_FILE" | tr -d ' ') lines) -> $TAG"
    echo "release title: $RELEASE_NAME"
fi

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
# case. TREE_VERSION comes from [project].version, which pyproject.toml declares before any other.
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
if [ -z "${SKIP_GITHUB:-}" ]; then
    echo "then tagging $TAG and cutting the GitHub release from $NOTES_FILE"
fi
if [ -n "${DRY_RUN:-}" ]; then
    echo "DRY_RUN set -- nothing pushed, tagged, released, or synced"
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

[ -n "${SKIP_GITHUB:-}" ] && exit 0

# --- git tag + GitHub release -------------------------------------------------------------------
# Last, deliberately. Everything above is the artefact; this is the announcement, and announcing a
# release whose image failed to push would be the wrong way round. If this step fails the image is
# already public, so it says exactly what is left to do rather than implying the whole thing failed.
#
# The token comes from GH_TOKEN if set, otherwise out of the origin URL, which on the release host
# carries it inline. Extracted into a variable and passed to curl through a 0600 config file -- never
# printed, never in argv, and note `git remote -v` would show it, so it is not printed here either.
ORIGIN=$(git -C "$HERE" remote get-url origin)
SLUG=$(echo "$ORIGIN" | sed -e 's#^.*github\.com[:/]##' -e 's#\.git$##')
GH_TOKEN="${GH_TOKEN:-${GITHUB_TOKEN:-$(echo "$ORIGIN" | sed -n 's#^https://\([^@/]*\)@github\.com/.*#\1#p')}}"
if [ -z "$GH_TOKEN" ]; then
    echo "WARNING: no GH_TOKEN and none embedded in origin -- image published, GitHub release skipped" >&2
    echo "  finish with: git push origin $TAG   then cut the release from $NOTES_FILE" >&2
    exit 0
fi

if git -C "$HERE" rev-parse -q --verify "refs/tags/$TAG" >/dev/null; then
    echo "tag $TAG already exists at HEAD"
else
    git -C "$HERE" tag -a "$TAG" -m "$TAG"
    echo "created tag $TAG"
fi
git -C "$HERE" push origin "$TAG"

ghtmp=$(mktemp -d)
trap 'rm -rf "$ghtmp"' EXIT INT TERM
(umask 077; printf 'header = "Authorization: token %s"\n' "$GH_TOKEN" > "$ghtmp/auth.conf")

# Drop the heading that became the title, and the blank line under it, so the page does not open by
# repeating itself. `/./,$!d` deletes leading blanks; a notes file with no heading is sent whole.
if [ -n "$HEADING" ]; then
    sed '1d' "$NOTES_FILE" | sed '/./,$!d' > "$ghtmp/body.md"
else
    cat "$NOTES_FILE" > "$ghtmp/body.md"
fi

gh_code=$(jq -n --arg tag "$TAG" --arg name "$RELEASE_NAME" --rawfile body "$ghtmp/body.md" \
        '{tag_name: $tag, name: $name, body: $body, draft: false, prerelease: false}' \
    | curl -sS -K "$ghtmp/auth.conf" -X POST --data-binary @- \
        -H 'Accept: application/vnd.github+json' -H 'User-Agent: stremio-publish' \
        -o "$ghtmp/release.json" -w '%{http_code}' "https://api.github.com/repos/$SLUG/releases")
case "$gh_code" in
    201) echo "released: $(jq -r '.html_url' < "$ghtmp/release.json")" ;;
    422) # Almost always "already_exists" -- a re-run after a partial failure, which is not an error.
         echo "release $TAG already exists on GitHub: $(jq -r '.errors[0].code // .message' < "$ghtmp/release.json")" ;;
    *)   echo "GitHub release failed (HTTP $gh_code): $(jq -r '.message // tostring' < "$ghtmp/release.json")" >&2
         echo "  the image IS published -- only the GitHub release is missing" >&2
         exit 4 ;;
esac
