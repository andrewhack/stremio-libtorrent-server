# Runtime image: the libtorrent streaming server on the dual-GPU base
# (jellyfin-ffmpeg with NVENC/VAAPI, nginx, GPU runtime). The base provides ffmpeg/ffprobe;
# uv manages its own Python 3.12 venv (the system python on the 22.04 base is 3.10).
FROM androshack/stremio-docker-dual:latest

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /srv/app
# SERVER_VERSION is the authoritative release identifier for the server image.
# FORK_VERSION remains as a compatibility alias for older WebAdmin installations.
COPY pyproject.toml uv.lock README.md LICENSE SERVER_VERSION FORK_VERSION ./
COPY src ./src
COPY docker ./docker
RUN set -eux; \
    mkdir -p /usr/local/libexec/stremio; \
    ln -sf "$(readlink -f "$(command -v ffmpeg)")" /usr/local/libexec/stremio/ffmpeg-real; \
    ln -sf "$(readlink -f "$(command -v ffprobe)")" /usr/local/libexec/stremio/ffprobe-real; \
    uv sync --no-dev --python 3.12; \
    chmod +x docker/entrypoint.sh docker/webadmin_entrypoint.py; \
    install -m 0755 docker/ffmpeg_wrapper.py /usr/local/bin/ffmpeg

ENV STREMIOSRV_CACHE_ROOT=/root/.stremio-server
ENV PATH="/srv/app/.venv/bin:${PATH}"
ENV STREMIO_SERVER_VERSION_FILE=/srv/app/SERVER_VERSION
ENV STREMIO_FORK_VERSION_FILE=/srv/app/FORK_VERSION
ENV FFMPEG_REAL=/usr/local/libexec/stremio/ffmpeg-real
ENV FFPROBE_REAL=/usr/local/libexec/stremio/ffprobe-real

EXPOSE 8080 11470 12470 6881
VOLUME ["/root/.stremio-server"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://127.0.0.1:11470/health || exit 1

# Keep WebAdmin integration outside the upstream Python source. The wrapper loads
# /config/admin-settings.json into STREMIOSRV_* variables and then executes the
# stock server entrypoint from this fork.
CMD ["python3", "/srv/app/docker/webadmin_entrypoint.py"]
