#!/bin/sh
# Sync README.md into the Docker Hub repository *overview* (the long description on the repo page).
#
# The overview is not part of the image, so pushing a tag never touches it -- it drifted two releases
# behind before this existed. Run it whenever the README changes; docker/publish.sh calls it too.
#
#   sh docker/push-readme.sh                                     # reuses your `docker login`
#   DOCKERHUB_TOKEN=<pat, read+write> sh docker/push-readme.sh   # or an explicit token
#
# The credential is sent only to hub.docker.com and never printed. It is kept out of argv as well (jq
# reads it from the environment; the session JWT goes to curl via a 0600 config file), so it does not
# show up in `ps` on a shared host.
#
# Env overrides: DOCKERHUB_USER, REPO, README, DOCKER_CONFIG_JSON.
set -e

DOCKERHUB_USER="${DOCKERHUB_USER:-androshack}"
REPO="${REPO:-androshack/stremio-libtorrent-server}"
README="${README:-$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)/README.md}"
API="https://hub.docker.com/v2"
DOCKER_CONFIG_JSON="${DOCKER_CONFIG_JSON:-$HOME/.docker/config.json}"

[ -f "$README" ] || { echo "no README at $README" >&2; exit 2; }
command -v jq >/dev/null 2>&1 || { echo "jq is required" >&2; exit 2; }

# With no token in the environment, reuse the credential `docker login` already stored for this user
# -- the same one `docker push` authenticates with, so shipping the image and shipping the docs need
# no second secret. It owns the username too, since a credential only works for the account it
# belongs to. Skipped entirely when DOCKERHUB_TOKEN is set, and a no-op under a credential helper
# (credsStore), where the secret is not in the file at all -- pass a token explicitly there.
if [ -z "${DOCKERHUB_TOKEN:-}" ] && [ -f "$DOCKER_CONFIG_JSON" ]; then
    creds=$(jq -r '.auths["https://index.docker.io/v1/"].auth // empty' "$DOCKER_CONFIG_JSON" \
        | base64 -d 2>/dev/null || true)
    case "$creds" in
        ?*:?*)
            DOCKERHUB_USER="${creds%%:*}"
            DOCKERHUB_TOKEN="${creds#*:}"
            echo "no DOCKERHUB_TOKEN set -- reusing the stored docker login for $DOCKERHUB_USER"
            ;;
    esac
    unset creds
fi

: "${DOCKERHUB_TOKEN:?set DOCKERHUB_TOKEN to a Docker Hub token with write scope, or run docker login}"

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

login_code=$(jq -n --arg u "$DOCKERHUB_USER" '{username: $u, password: env.DOCKERHUB_TOKEN}' \
    | curl -sS -X POST -H 'Content-Type: application/json' --data-binary @- \
        -o "$tmp/login.json" -w '%{http_code}' "$API/users/login/")
jwt=$(jq -r '.token // empty' < "$tmp/login.json")
if [ -z "$jwt" ]; then
    # Say what Hub said. "login failed" alone sends you hunting for the wrong problem: a registry
    # credential that pushes images fine can still be refused here, and 2FA answers with a challenge
    # rather than an error. Only `detail` and the field *names* are printed -- never a token value.
    echo "login failed for $DOCKERHUB_USER (HTTP $login_code): $(jq -r '.detail // .message // "no detail"' < "$tmp/login.json")" >&2
    echo "response fields: $(jq -r 'try (keys | join(",")) catch "unparsable"' < "$tmp/login.json")" >&2
    echo "set DOCKERHUB_TOKEN to a Docker Hub personal access token with write scope" >&2
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
