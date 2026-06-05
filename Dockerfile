# Runtime image: the libtorrent streaming server on the dual-GPU base
# (jellyfin-ffmpeg with NVENC/VAAPI, nginx, GPU runtime). The base provides ffmpeg/ffprobe;
# uv manages its own Python 3.12 venv (the system python on the 22.04 base is 3.10).
FROM stremio-docker-dual:latest

# uv (standalone binary; brings its own Python toolchain)
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /srv/app
# Dependency metadata first (better layer caching), then source.
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY docker ./docker
# Pin Python 3.12: libtorrent 2.0.11 only publishes cp312/cp313 wheels (no 3.14).
RUN uv sync --no-dev --python 3.12 && chmod +x docker/entrypoint.sh

ENV STREMIOSRV_CACHE_ROOT=/root/.stremio-server
ENV PATH="/srv/app/.venv/bin:${PATH}"

# 11470 = HTTP API; 12470 = HTTPS (nginx TLS, if a cert is mounted); 6881 = BitTorrent peer port.
EXPOSE 11470 12470 6881
VOLUME ["/root/.stremio-server"]

# Entrypoint runs uvicorn (http) + nginx TLS (https, when a cert is present).
CMD ["/srv/app/docker/entrypoint.sh"]
