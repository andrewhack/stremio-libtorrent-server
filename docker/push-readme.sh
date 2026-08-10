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
            # Which kind it is decides what to do when Hub refuses it below: the registry accepts an
            # account password that this API does not, so "stored login exists" is not "API will work".
            case "$DOCKERHUB_TOKEN" in
                dckr_pat_*) kind="access token" ;;
                *)          kind="account password, not a PAT" ;;
            esac
            echo "no DOCKERHUB_TOKEN set -- reusing the stored docker login for $DOCKERHUB_USER ($kind)"
            ;;
    esac
    unset creds
fi

: "${DOCKERHUB_TOKEN:?set DOCKERHUB_TOKEN to a Docker Hub token with write scope, or run docker login}"
# Must be exported, not just set: the request bodies below read it through jq's `env`, which sees only
# the environment. As a plain shell variable it silently serialises to null and Hub rejects the body.
export DOCKERHUB_TOKEN

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT INT TERM
umask 077

# An unmatched open marker makes the sed range below run to end-of-file, silently amputating the
# page. Refuse instead -- that failure would look exactly like a successful publish.
opens=$(grep -c '<!--hub:skip-->' "$README" || true)
closes=$(grep -c '<!--/hub:skip-->' "$README" || true)
if [ "$opens" != "$closes" ]; then
    echo "ERROR: $README has $opens <!--hub:skip--> markers but $closes closing ones" >&2
    exit 2
fi

# Two transforms. Send LF whatever the checkout has: Hub renders it identically, it makes the
# read-back check exact on a CRLF clone, and it saves ~400 bytes against the cap. Then drop regions
# marked <!--hub:skip--> ... <!--/hub:skip-->: the overview has a hard byte cap this README keeps
# growing towards, and part of it is repo-facing anyway (how to run the test suite is no use to
# someone reading a Docker Hub page). GitHub still shows those sections -- HTML comments render as
# nothing there -- so nothing is lost, and the two pages stay one source file.
tr -d '\r' < "$README" | sed '/<!--hub:skip-->/,/<!--\/hub:skip-->/d' > "$tmp/body.md"
bytes=$(wc -c < "$tmp/body.md" | tr -d ' ')
chars=$(LC_ALL=C.UTF-8 wc -m < "$tmp/body.md" 2>/dev/null | tr -d ' ')
echo "syncing README -> $REPO overview ($bytes bytes, $chars chars)"
# Hub's cap is 25000 *bytes*, and it enforces it server-side with "Exceeded max number of bytes".
# Bytes, not characters, is the whole trap: this README is ~880 bytes heavier than its character
# count because every emoji costs four, so a page that looks 700 under the limit is actually over it.
# Checking here turns a cryptic 400 into a number you can act on, before anything is sent.
if [ "$bytes" -gt 25000 ]; then
    echo "ERROR: $bytes bytes exceeds Docker Hub's 25000-byte cap -- trim $((bytes - 25000)) bytes" >&2
    exit 6
fi
if [ "$bytes" -gt 24000 ]; then
    echo "warning: only $((25000 - bytes)) bytes left under the 25000-byte cap"
fi

# Docker Hub has two auth endpoints and they take different credentials. /v2/auth/token
# (identifier + secret -> access_token) is the current one and the only one that accepts a personal
# access token; /v2/users/login (username + password -> token) is the legacy path, which answers a
# PAT with HTTP 409 "Please reset your password." -- a message that sends you off resetting a
# perfectly good password. Try the modern endpoint, fall back for a genuine password login.
# Report what an endpoint actually said. Only `detail` and the field *names* -- never a token value.
# Both attempts get reported on failure: with one shared message you cannot tell which endpoint
# refused you, and they refuse for different reasons.
report_auth() {
    echo "  $1: HTTP $2 -- $(jq -r '.detail // .message // "no detail"' < "$3" 2>/dev/null || echo unparsable)" >&2
    echo "     fields: $(jq -r 'try (keys | join(",")) catch "unparsable"' < "$3")" >&2
}

scheme="Bearer"
code_modern=$(jq -n --arg u "$DOCKERHUB_USER" '{identifier: $u, secret: env.DOCKERHUB_TOKEN}' \
    | curl -sS -X POST -H 'Content-Type: application/json' --data-binary @- \
        -o "$tmp/auth-token.json" -w '%{http_code}' "$API/auth/token")
jwt=$(jq -r '.access_token // empty' < "$tmp/auth-token.json")
if [ -z "$jwt" ]; then
    scheme="JWT"
    code_legacy=$(jq -n --arg u "$DOCKERHUB_USER" '{username: $u, password: env.DOCKERHUB_TOKEN}' \
        | curl -sS -X POST -H 'Content-Type: application/json' --data-binary @- \
            -o "$tmp/users-login.json" -w '%{http_code}' "$API/users/login/")
    jwt=$(jq -r '.token // empty' < "$tmp/users-login.json")
fi
if [ -z "$jwt" ]; then
    echo "Docker Hub refused both auth endpoints for $DOCKERHUB_USER:" >&2
    report_auth "/v2/auth/token   (PAT)     " "$code_modern" "$tmp/auth-token.json"
    report_auth "/v2/users/login  (password)" "$code_legacy" "$tmp/users-login.json"
    echo "set DOCKERHUB_TOKEN to a Docker Hub personal access token with write scope" >&2
    exit 3
fi
printf 'header = "Authorization: %s %s"\n' "$scheme" "$jwt" > "$tmp/auth.conf"

code=$(jq -Rs '{full_description: .}' < "$tmp/body.md" \
    | curl -sS -K "$tmp/auth.conf" -X PATCH -H 'Content-Type: application/json' \
        --data-binary @- -o "$tmp/patch.json" -w '%{http_code}' "$API/repositories/$REPO/")
if [ "$code" != "200" ]; then
    # errinfo carries the field-level reason (which field, which constraint). Without it a validation
    # error is just "400", and the most likely cause -- the length cap -- looks like anything else.
    echo "PATCH returned HTTP $code: $(jq -r '.detail // .message // tostring' < "$tmp/patch.json" 2>/dev/null || cat "$tmp/patch.json")" >&2
    echo "  errinfo: $(jq -c '.errinfo // empty' < "$tmp/patch.json" 2>/dev/null | head -c 600)" >&2
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
