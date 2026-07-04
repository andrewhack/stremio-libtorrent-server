from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from stremiosrv import health
from stremiosrv.api import casting, handshake, hls, netcheck, pins, playback, subs
from stremiosrv.api import cache as cache_api
from stremiosrv.config import Settings

# Exception leaf types that mean "the client went away mid-stream" (vs a real server bug). Matched by
# name so the check is a pure function (no running event loop needed): asyncio/anyio cancellation is
# 'CancelledError'/'Cancelled'; a broken/closed socket is the rest.
_DISCONNECT_NAMES = frozenset({
    "CancelledError", "Cancelled", "ClientDisconnect", "BrokenResourceError", "ClosedResourceError",
    "EndOfStream", "ConnectionResetError", "BrokenPipeError",
})


def _all_client_disconnect(exc: BaseException) -> bool:
    """True only if `exc` (unwrapping ExceptionGroups) consists *entirely* of client-disconnect /
    cancellation leaves — so a real error mixed in still propagates and gets logged."""
    leaves: list[BaseException] = []

    def walk(e: BaseException) -> None:
        if isinstance(e, BaseExceptionGroup):
            for sub in e.exceptions:
                walk(sub)
        else:
            leaves.append(e)

    walk(exc)
    return bool(leaves) and all(type(leaf).__name__ in _DISCONNECT_NAMES for leaf in leaves)


class SuppressClientDisconnect:
    """Outermost ASGI wrapper. When a player disconnects mid-`StreamingResponse` (seek / buffer-ahead
    / stop), Starlette's anyio task group surfaces the aborted send() as an ExceptionGroup that
    uvicorn logs as a scary 'Exception in ASGI application' (nginx: 'upstream prematurely closed').
    Playback is unaffected — the player just reconnects — so swallow disconnect-ONLY exceptions to
    keep the log readable. Any group containing a genuine error propagates unchanged."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        try:
            await self.app(scope, receive, send)
        except BaseException as exc:  # noqa: BLE001 — re-raised below unless it's a pure disconnect
            if scope.get("type") == "http" and _all_client_disconnect(exc):
                return
            raise


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

    import os

    from stremiosrv.torrent.tracker_source import TrackerSource
    from stremiosrv.torrent.trackers import parse_tracker_string

    settings = Settings()
    settings.transcode_profile = settings.transcode_profile or detect_profile()
    # Optional live tracker list: fetched in a daemon thread (best-effort, never blocks startup or
    # the request path). start() is a no-op when no URL is configured -> fully static/offline-safe.
    tracker_source = TrackerSource(
        settings.tracker_list_url,
        cache_path=os.path.join(settings.cache_root, ".resume", "trackers.remote"),
        refresh_hours=settings.tracker_list_refresh_hours,
    )
    tracker_source.start()
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
        extra_trackers=parse_tracker_string(settings.extra_trackers),
        tracker_source=tracker_source,
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
    # Wrap outermost so a mid-stream client disconnect doesn't spam the ASGI error log (see
    # SuppressClientDisconnect). create_app stays a plain FastAPI app for tests.
    return SuppressClientDisconnect(
        create_app(settings=settings, engine=engine, converter=converter)
    )
