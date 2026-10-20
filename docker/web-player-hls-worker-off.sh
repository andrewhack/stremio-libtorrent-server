#!/bin/sh
# Switch hls.js's Web Worker off in the bundled web player.
# Usage: web-player-hls-worker-off.sh <web-build-dir>
#
# The web build's minified hls.js cannot start its demuxer worker: the code it builds the worker from
# names something outside it ("ReferenceError: e is not defined"), so every HLS playback starts by
# falling back to the inline demuxer. That fallback restarts the stream but not hls.js's count of the
# codec announcements it waits for before creating its buffers. With audio in renditions of its own
# (a transcode of a file with several audio tracks), a repeated video announcement can use the count
# up: the player gets a video buffer and no audio one, and the start fails or stalls. With the worker
# off from the start there is nothing to fall back from, and demuxing runs inline as it already did.
#
# The service worker precaches main.js under a content revision, updated as for the loader. The build
# fails, rather than shipping a player that still starts the worker, unless the bundle, its hls.js
# setting and its precache entry are each found exactly once.
set -eu

BUILD="$1"
SW="$BUILD/service-worker.js"
FROM='debug:!1,enableWorker:!0'
TO='debug:!1,enableWorker:!1'

set -- "$BUILD"/*/scripts/main.js
if [ "$#" -ne 1 ] || [ ! -f "$1" ]; then
    echo "web-player-hls-worker-off: expected one */scripts/main.js under $BUILD, found: $*" >&2
    exit 1
fi
TARGET="$1"
REL="${TARGET#"$BUILD"/}"   # <commit>/scripts/main.js, the path the service worker precaches

FOUND=$(grep -o "$FROM" "$TARGET" | wc -l)
if [ "$FOUND" -ne 1 ]; then
    echo "web-player-hls-worker-off: expected one '$FROM' in $REL, found $FOUND" >&2
    exit 1
fi
ENTRY="{url:\"$REL\",revision:\"[0-9a-f]*\"}"
FOUND=$(grep -o "$ENTRY" "$SW" | wc -l)
if [ "$FOUND" -ne 1 ]; then
    echo "web-player-hls-worker-off: expected one precache entry for $REL in $SW, found $FOUND" >&2
    exit 1
fi

sed -i "s/$FROM/$TO/" "$TARGET"
if [ "$(grep -o "$TO" "$TARGET" | wc -l)" -ne 1 ] || grep -q "$FROM" "$TARGET"; then
    echo "web-player-hls-worker-off: the setting did not change in $REL" >&2
    exit 1
fi
REV=$(md5sum "$TARGET" | cut -d' ' -f1)
sed -i "s|$ENTRY|{url:\"$REL\",revision:\"$REV\"}|" "$SW"
if ! grep -q "{url:\"$REL\",revision:\"$REV\"}" "$SW"; then
    echo "web-player-hls-worker-off: the new revision did not land in $SW" >&2
    exit 1
fi
echo "web-player-hls-worker-off: $REL now runs hls.js without its worker, service worker revision $REV"
