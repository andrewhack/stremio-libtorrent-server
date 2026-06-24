# Shared server body: serve the Stremio web build, proxy the streaming API to uvicorn.
# Included by both the HTTP (:8080) and HTTPS (:12470) server blocks in nginx-allinone.conf.
root /srv/stremio-server/build;
index index.html;

proxy_http_version 1.1;
proxy_set_header Host $http_host;
proxy_set_header X-Forwarded-Proto $scheme;
proxy_buffering off;
proxy_request_buffering off;
proxy_read_timeout 3600s;
proxy_send_timeout 3600s;

# --- streaming API -> uvicorn (everything not matched here is served as web static) ---
location = /health             { proxy_pass http://127.0.0.1:11470; }
location = /settings           { proxy_pass http://127.0.0.1:11470; }
location = /network-info       { proxy_pass http://127.0.0.1:11470; }
location = /device-info        { proxy_pass http://127.0.0.1:11470; }
location = /stats.json         { proxy_pass http://127.0.0.1:11470; }
location = /hwaccel-profiler   { proxy_pass http://127.0.0.1:11470; }
location = /casting            { proxy_pass http://127.0.0.1:11470; }
location = /opensubHash        { proxy_pass http://127.0.0.1:11470; }
location = /removeAll          { proxy_pass http://127.0.0.1:11470; }
location ^~ /hlsv2/            { proxy_pass http://127.0.0.1:11470; }
location ~ "^/[0-9a-fA-F]{40}" { proxy_pass http://127.0.0.1:11470; }

# --- web player (static SPA; hash-routed, so / -> index.html) ---
location / { try_files $uri $uri/ /index.html; }
