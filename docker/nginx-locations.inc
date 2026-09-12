# Shared server body: serve the Stremio web build, proxy the streaming API to uvicorn.
# Included by both the HTTP (:8080) and HTTPS (:12470) server blocks in nginx-allinone.conf.
root /srv/stremio-server/build;
index index.html;

proxy_http_version 1.1;
proxy_set_header Host $http_host;
proxy_set_header X-Forwarded-Proto $scheme;
# The addon judges the CLIENT's address, and without this every proxied request arrives as
# nginx's own loopback address -- a LAN check that inspects the proxy passes everything.
proxy_set_header X-Forwarded-For $remote_addr;
proxy_buffering off;
proxy_request_buffering off;
proxy_read_timeout 3600s;
proxy_send_timeout 3600s;

# --- streaming API -> uvicorn (anything not matched here: a web-player file or the 404 at the end) ---
location = /health             { proxy_pass http://127.0.0.1:11470; }
location = /settings           { proxy_pass http://127.0.0.1:11470; }
location = /network-info       { proxy_pass http://127.0.0.1:11470; }
location = /device-info        { proxy_pass http://127.0.0.1:11470; }
location = /stats.json         { proxy_pass http://127.0.0.1:11470; }
location = /hwaccel-profiler   { proxy_pass http://127.0.0.1:11470; }
location = /casting            { proxy_pass http://127.0.0.1:11470; }
location = /opensubHash        { proxy_pass http://127.0.0.1:11470; }
# Exact, and deliberately NOT covered by the /subtitles. prefix below — that prefix is
# case-sensitive, so `/subtitleSignature` misses it at the `s`/`S` and would fall through to
# index.html. The bundled web player is the main caller of this route, so falling through here
# means the route effectively does not exist for the client it was added for.
location = /subtitleSignature  { proxy_pass http://127.0.0.1:11470; }
# Prefix (not exact): the client picks the extension it wants — `.srt` for native players
# (Android/ExoPlayer), `.vtt` for the browser. Route every /subtitles.<ext> to the proxy, else a
# `.srt` request falls through to the web-player index.html (HTML, not a subtitle).
location /subtitles.           { proxy_pass http://127.0.0.1:11470; }
location = /removeAll          { proxy_pass http://127.0.0.1:11470; }
location ^~ /hlsv2/            { proxy_pass http://127.0.0.1:11470; }
# Torrent routes are /<40-hex-infohash>/<idx|-1|stats.json|remove|create>. The second segment MUST
# be anchored: the bundled web build serves its JS/CSS from a 40-hex-named dir
# (e.g. /<hash>/scripts/main.js, /<hash>/styles/main.css), which an unanchored ^/<40hex> would
# wrongly proxy to uvicorn -> the SPA never boots (blank page). Keep these as static.
# `create` is the POST the player sends before streaming a stream with `sources` or no fileIdx,
# and -1 the index stremio-core writes for the latter; both used to land on index.html (405/200).
location ~ "^/[0-9a-fA-F]{40}/(-1|[0-9]+|stats\.json|remove|create)" { proxy_pass http://127.0.0.1:11470; }
# The stock proxy for addon HTTP streams that need request headers (stremio-video routes a stream
# through it when the addon sets behaviorHints.proxyHeaders). A bare proxy_pass forwards the URI as
# sent: the destination rides percent-encoded in the path and must reach the app still encoded.
location ^~ /proxy/            { proxy_pass http://127.0.0.1:11470; }
# Library download UI (opt-in, off unless STREMIOSRV_LIBRARY_UI). Prefix, not exact: the page AND
# its API both live under here, and uvicorn 404s the whole tree when the flag is off — so this
# line is inert rather than wrong on a server that has not enabled it. It must come before the
# `location /` fallback, or /library would be served as the web player's index.html.
location ^~ /library           { proxy_pass http://127.0.0.1:11470; }

# --- the web player's own files; everything else is a counted 404 ---
# The player is hash-routed: its only page is / (served through `index`). Any other path that is
# not one of its files goes to /_unmatched, which answers 404 and counts it by shape in /stats.json.
# It used to get index.html -- 200 for a GET, 405 for a POST -- which is how a missing route stayed
# invisible for months (issue #3). The allowlist above remains the security boundary: every
# unmatched path lands on the one fixed route below, so none can reach an origin-only route.
location / { try_files $uri $uri/ @unmatched; }
location @unmatched {
    # Declaring proxy_set_header here drops the inherited ones; /_unmatched reads only the two below.
    # A client's own X-Forwarded-For would then pass through, and uvicorn believes that header from
    # loopback: the peer would become whatever the client wrote, and /_unmatched would stop trusting
    # the two headers below. Send none, so the peer stays nginx.
    proxy_set_header X-Forwarded-For "";
    proxy_set_header X-Original-Method $request_method;
    proxy_set_header X-Original-URI $request_uri;
    # The trailing `?` drops the original query string; /_unmatched takes the request from the
    # headers above.
    rewrite ^ /_unmatched? break;
    proxy_pass http://127.0.0.1:11470;
}
