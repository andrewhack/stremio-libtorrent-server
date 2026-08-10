#!/bin/sh
# Sync README.md into the Docker Hub repository *overview* (the long description on the repo page).
#
# The overview is not part of the image, so pushing a tag never touches it -- it drifted two releases
# behind before this existed. Run it whenever the README changes; docker/publish.sh calls it too.
#
#   DOCKERHUB_TOKEN=<personal access token, read+write> sh docker/push-readme.sh
#
# The token is read from the environment, sent only to hub.docker.com, and never printed. It is kept
# out of argv as well (jq reads it from the environment; the session JWT goes to curl via a 0600
# config file), so it does not show up in `ps` on a shared host.
#
# Env overrides: DOCKERHUB_USER, REPO, README.
set -e

DOCKERHUB_USER="${DOCKERHUB_USER:-androshack}"
REPO="${REPO:-androshack/stremio-libtorrent-server}"
README="${README:-$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)/README.md}"
API="https://hub.docker.com/v2"

: "${DOCKERHUB_TOKEN:?set DOCKERHUB_TOKEN to a Docker Hub access token with write scope}"
[ -f "$README" ] || { echo "no README at $README" >&2; exit 2; }
command -v jq >/dev/null 2>&1 || { echo "jq is required" >&2; exit 2; }

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT INT TERM
umask 077

# Send LF, whatever the checkout has. Docker Hub renders it identically, it makes the read-back check
# below exact on a CRLF clone, and it buys ~400 characters of headroom against the cap.
tr -d '\r' < "$README" > "$tmp/body.md"
chars=$(LC_ALL=C.UTF-8 wc -m < "$tmp/body.md" 2>/dev/null || wc -m < "$tmp/body.md")
chars=$(echo "$chars" | tr -d ' ')
echo "syncing README -> $REPO overview ($chars chars)"
# Docker Hub caps the overview around 25k characters. The read-back below is what actually proves the
# text landed whole -- this is just an early nudge, because a truncated page looks fine until someone
# scrolls to the bottom of it.
if [ "$chars" -gt 23000 ]; then
    echo "warning: $chars chars is close to Docker Hub's ~25k overview cap"
fi

jwt=$(jq -n --arg u "$DOCKERHUB_USER" '{username: $u, password: env.DOCKERHUB_TOKEN}' \
    | curl -sS -X POST -H 'Content-Type: application/json' --data-binary @- "$API/users/login/" \
    | jq -r '.token // empty')
if [ -z "$jwt" ]; then
    echo "login failed for $DOCKERHUB_USER -- check DOCKERHUB_TOKEN (needs write scope)" >&2
    exit 3
fi
printf 'header = "Authorization: JWT %s"\n' "$jwt" > "$tmp/auth.conf"

code=$(jq -Rs '{full_description: .}' < "$tmp/body.md" \
    | curl -sS -K "$tmp/auth.conf" -X PATCH -H 'Content-Type: application/json' \
        --data-binary @- -o "$tmp/patch.json" -w '%{http_code}' "$API/repositories/$REPO/")
if [ "$code" != "200" ]; then
    echo "PATCH returned HTTP $code: $(jq -r '.detail // .message // tostring' < "$tmp/patch.json" 2>/dev/null || cat "$tmp/patch.json")" >&2
    exit 4
fi

# Read it back from the public API. A 200 only says the request was accepted; this says the whole
# text is actually on the page -- the failure worth catching is a silent truncation at the cap.
sent=$(cat "$tmp/body.md")
live=$(curl -sS "$API/repositories/$REPO/" | jq -r '.full_description // ""' | tr -d '\r')
if [ "$sent" != "$live" ]; then
    # Bytes, not characters -- ${#var} counts bytes in dash and characters in bash, and a diagnostic
    # that means two different things depending on the shell is worse than one that is merely coarse.
    nsent=$(printf '%s' "$sent" | wc -c | tr -d ' ')
    nlive=$(printf '%s' "$live" | wc -c | tr -d ' ')
    echo "MISMATCH after push: sent $nsent bytes, Docker Hub is serving $nlive" >&2
    echo "the overview is NOT in sync -- check the length cap and re-run" >&2
    exit 5
fi

echo "overview in sync: https://hub.docker.com/r/$REPO"
