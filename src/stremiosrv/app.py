from fastapi import FastAPI

from stremiosrv import health
from stremiosrv.api import casting, handshake, hls, playback, subs
from stremiosrv.config import Settings


def create_app(settings: Settings | None = None, engine=None, converter=None) -> FastAPI:
    """Application factory. Wires the Stremio streaming-server routers.

    `engine` is the libtorrent engine and `converter` the HLS transcoder (injected by the server
    entrypoint / integration tests). When None, torrent stats return null, the file route returns
    503, and hlsv2 returns 503 — keeping the app importable without libtorrent/ffmpeg.
    """
    settings = settings or Settings()
    app = FastAPI(title="stremio-libtorrent-server")
    app.state.settings = settings
    app.state.engine = engine
    app.state.converter = converter
    app.include_router(health.router)
    app.include_router(handshake.router)
    app.include_router(playback.router)
    app.include_router(hls.router)
    app.include_router(subs.router)
    app.include_router(casting.router)
    return app


def build_app() -> FastAPI:
    """Server entrypoint factory: creates the real libtorrent engine from settings.

    Run with:  uvicorn stremiosrv.app:build_app --factory --host 0.0.0.0 --port <p>
    """
    from stremiosrv.torrent.engine import Engine
    from stremiosrv.transcode.converter import Converter
    from stremiosrv.transcode.profiler import detect_profile

    settings = Settings()
    settings.transcode_profile = settings.transcode_profile or detect_profile()
    engine = Engine(
        listen_port=settings.bt_listen_port,
        cache_root=settings.cache_root,
        max_connections=settings.bt_max_connections,
    )
    converter = Converter(settings.cache_root, settings.transcode_profile)
    return create_app(settings=settings, engine=engine, converter=converter)
