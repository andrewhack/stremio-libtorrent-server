from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from stremiosrv import health
from stremiosrv.api import casting, handshake, hls, netcheck, pins, playback, subs
from stremiosrv.api import cache as cache_api
from stremiosrv.config import Settings


def create_app(settings: Settings | None = None, engine=None, converter=None) -> FastAPI:
    """Application factory. Wires the Stremio streaming-server routers.

    `engine` is the libtorrent engine and `converter` the HLS transcoder (injected by the server
    entrypoint / integration tests). When None, torrent stats return null, the file route returns
    503, and hlsv2 returns 503 — keeping the app importable without libtorrent/ffmpeg.
    """
    settings = settings or Settings()
    app = FastAPI(title="stremio-libtorrent-server")
    # Stremio runs the stock server with NO_CORS=1; mirror that so web/cast clients can call it.
    app.add_middleware(
        CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.converter = converter
    app.include_router(health.router)
    app.include_router(handshake.router)
    app.include_router(pins.router)
    app.include_router(netcheck.router)
    app.include_router(playback.router)
    app.include_router(cache_api.router)
    app.include_router(hls.router)
    app.include_router(subs.router)
    app.include_router(casting.router)
    return app


def build_app() -> FastAPI:
    """Server entrypoint factory: creates the real libtorrent engine from settings.

    Run with:  uvicorn stremiosrv.app:build_app --factory --host 0.0.0.0 --port <p>
    """
    import threading

    from stremiosrv.cache import run_evictor
    from stremiosrv.torrent.engine import Engine
    from stremiosrv.transcode.converter import Converter
    from stremiosrv.transcode.profiler import detect_profile

    settings = Settings()
    settings.transcode_profile = settings.transcode_profile or detect_profile()
    engine = Engine(
        listen_port=settings.bt_listen_port,
        cache_root=settings.cache_root,
        max_connections=settings.bt_max_connections,
        download_rate_limit=settings.download_rate_limit,
        upload_rate_limit=settings.upload_rate_limit,
        cache_size=settings.cache_size,
        resume_save_interval=settings.resume_save_interval,
        idle_download_rate_limit=settings.idle_download_rate_limit,
        seed_on_complete=settings.seed_on_complete,
        max_seed_minutes=settings.max_seed_minutes,
        seed_policy_interval=settings.seed_policy_interval,
    )
    engine.load_pins_into_session()
    converter = Converter(settings.cache_root, settings.transcode_profile)
    # Background cache eviction so the download cache stays under budget during long real-world use.
    threading.Thread(
        target=run_evictor,
        args=(settings.cache_root, settings.cache_size, engine),
        kwargs={"interval": settings.cache_evict_interval, "grace": settings.cache_evict_grace},
        daemon=True,
    ).start()
    return create_app(settings=settings, engine=engine, converter=converter)
