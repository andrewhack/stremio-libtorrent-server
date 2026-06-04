#!/usr/bin/env bash
# Stage 0 Task 0.3 — capture live response fixtures from the stock Stremio server.js.
# Run on the LINUX SERVER where the stock image (stremio-docker-dual) is running.
# Output: tests/fixtures/*  (the conformance fixtures that unblock Stage 2+).
#
# Usage:
#   ./scripts/capture-fixtures.sh                 # phase 1 (handshake) + phase 3 prep
#   H=<40hex-legal-infohash> ./scripts/capture-fixtures.sh   # also phase 2 (torrent)
# Use only LEGAL torrents (Internet Archive, distro ISOs, public domain).

set -u
OUT="tests/fixtures"
mkdir -p "$OUT"

# --- locate the running stock container ---
C=$(docker ps --filter "ancestor=stremio-docker-dual:latest" -q | head -1)
[ -z "$C" ] && C=$(docker ps --format '{{.ID}} {{.Image}}' | grep -iE 'stremio' | awk '{print $1}' | head -1)
if [ -z "$C" ]; then echo "ERROR: no stremio container found. Set C=<id> and re-run." >&2; exit 1; fi
echo "container: $C"

# helper: GET an internal URL and save the body
grab() { # grab <url-path> <outfile>
  docker exec "$C" curl -s "http://127.0.0.1:11470$1" -o "/tmp/_fx" 2>/dev/null \
    && docker cp "$C:/tmp/_fx" "$OUT/$2" >/dev/null 2>&1 \
    && echo "  saved $2" || echo "  MISS  $2  ($1)"
}

echo "== Phase 1: handshake / info (no torrent needed) =="
grab "/settings"          settings.json
grab "/network-info"      network-info.json
grab "/device-info"       device-info.json
grab "/stats.json"        global-stats.json
grab "/hwaccel-profiler"  hwaccel-profiler.json
grab "/status"            status.json
grab "/casting/"          casting.json

echo "== Phase 2: torrent playback (needs H=<legal infohash>) =="
if [ -n "${H:-}" ]; then
  echo "  using infohash $H"
  # lazily create the engine + capture the Range response headers of the file endpoint
  docker exec "$C" curl -s -r 0-1048575 "http://127.0.0.1:11470/$H/0" -D /tmp/_rh -o /dev/null 2>/dev/null \
    && docker cp "$C:/tmp/_rh" "$OUT/range.headers.txt" >/dev/null 2>&1 && echo "  saved range.headers.txt"
  sleep 6
  grab "/$H/stats.json"     torrent-stats.json
  grab "/$H/0/stats.json"   file-stats.json
else
  echo "  SKIPPED (no H). Re-run as: H=<40hex> ./scripts/capture-fixtures.sh"
fi

echo "== Phase 3: HLS transcode + subtitles (needs manual playback) =="
echo "  1) In a browser/TV pointed at this server, PLAY an HEVC title for ~15s, then re-run this phase."
# Discover the real hlsv2 / opensubHash URLs the client used, from the logs:
docker logs --tail 400 "$C" 2>&1 | grep -oE '/hlsv2/[^ "]+'      | sort -u > "$OUT/hlsv2-urls.txt"
docker logs --tail 400 "$C" 2>&1 | grep -oE '/opensubHash[^ "]+' | sort -u > "$OUT/opensubhash-urls.txt"
echo "  discovered $(wc -l < "$OUT/hlsv2-urls.txt") hlsv2 URL(s) -> $OUT/hlsv2-urls.txt"

# Auto-fetch the probe + the first master/video0/audio0 playlist + opensubHash, if discovered:
PROBE=$(grep -m1 '/hlsv2/probe' "$OUT/hlsv2-urls.txt" || true)
[ -n "$PROBE" ] && grab "$PROBE" probe.json
for trk in master video0 audio0 subtitle0; do
  U=$(grep -m1 "/$trk\.m3u8" "$OUT/hlsv2-urls.txt" || true)
  [ -n "$U" ] && grab "$U" "hls-$trk.m3u8"
done
OSH=$(head -1 "$OUT/opensubhash-urls.txt" 2>/dev/null || true)
[ -n "$OSH" ] && grab "$OSH" opensubHash.json

echo "== done. Review $OUT, then commit:"
echo "   git add $OUT && git commit -m 'test: Stage 0 live fixtures from stock server' && git push"
