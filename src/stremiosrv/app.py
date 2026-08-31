import contextvars
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from stremiosrv import health
from stremiosrv.api import cache as cache_api
from stremiosrv.api import casting, handshake, hls, netcheck, pins, playback, subs
from stremiosrv.config import Settings
from stremiosrv.library import api as library_api

# Exception leaf types that mean "the client went away mid-stream" (vs a real server bug). Matched by
# name so the check is a pure function (no running event loop needed): asyncio/anyio cancellation is
# 'CancelledError'/'Cancelled'; a broken/closed socket is the rest.
_DISCONNECT_NAMES = frozenset({
    "CancelledError", "Cancelled", "ClientDisconnect", "BrokenResourceError", "ClosedResourceError",
    "EndOfStream", "ConnectionResetError", "BrokenPipeError",
})


# uvicorn's HTTP protocol raises this (protocols/http/*_impl.py) when a StreamingResponse ends
# before the Content-Length it already announced. On the range route that is deliberate, not a
# fault: wait_and_read gives up on a peer-starved piece (or a handle the evictor removed mid-stream)
# and ends the stream, having committed to `Content-Length: end-start+1` in the 206 header.
# Truncating the connection IS the correct HTTP signal for "this body is incomplete" — the player
# re-requests the range and succeeds once more of the file is cached — and wait_and_read has already
# logged the precise reason at WARNING. The ASGI traceback on top is pure noise.
# NB: the sibling "longer than Content-Length" means we computed a range wrong — a genuine bug, so
# it is deliberately NOT matched here.
_TRUNCATED_BODY_MSG = "Response content shorter than Content-Length"


def _leaves(exc: BaseException) -> list[BaseException]:
    """Flatten `exc` (unwrapping nested ExceptionGroups) to its non-group leaf exceptions."""
    out: list[BaseException] = []

    def walk(e: BaseException) -> None:
        if isinstance(e, BaseExceptionGroup):
            for sub in e.exceptions:
                walk(sub)
        else:
            out.append(e)

    walk(exc)
    return out


def _is_truncated_body(exc: BaseException) -> bool:
    """True for uvicorn's deliberate-truncation error (matched on message: the type is a bare
    RuntimeError, so suppressing by type would swallow real bugs)."""
    return type(exc) is RuntimeError and str(exc) == _TRUNCATED_BODY_MSG


def _all_client_disconnect(exc: BaseException) -> bool:
    """True only if `exc` (unwrapping ExceptionGroups) consists *entirely* of client-disconnect /
    cancellation leaves — so a real error mixed in still propagates and gets logged."""
    leaves = _leaves(exc)
    return bool(leaves) and all(type(leaf).__name__ in _DISCONNECT_NAMES for leaf in leaves)


def _all_benign_stream_end(exc: BaseException) -> bool:
    """True only if every leaf is an expected end-of-stream — the client going away, or our own
    deliberate truncation. A real error anywhere in the group still propagates and gets logged."""
    leaves = _leaves(exc)
    return bool(leaves) and all(
        type(leaf).__name__ in _DISCONNECT_NAMES or _is_truncated_body(leaf) for leaf in leaves
    )


# Having swallowed the truncation above, uvicorn's run_asgi still finds the response incomplete and
# logs this at ERROR — misleading, since the callable did not misbehave, we cut the body on purpose.
_INCOMPLETE_MSG = "ASGI callable returned without completing response."

# Set on the request's own context when we suppress a truncation. uvicorn runs each request in a
# task with its own copy of the context, so this cannot bleed into a concurrent stream.
_truncated: contextvars.ContextVar[bool] = contextvars.ContextVar("stremiosrv_truncated")


class _DropTruncationFollowup(logging.Filter):
    """Drops uvicorn's `_INCOMPLETE_MSG` for the one request that deliberately truncated. Scoped by
    contextvar rather than dropping the message outright, so an unrelated incomplete response — a
    real bug — is still logged."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not (record.getMessage().strip() == _INCOMPLETE_MSG and _truncated.get(False))


def _install_log_filter() -> None:
    """Attach the filter to uvicorn's error logger, once. Safe to call before or after uvicorn
    configures logging: dictConfig replaces a logger's handlers but leaves its filters intact."""
    logger = logging.getLogger("uvicorn.error")
    if not any(isinstance(f, _DropTruncationFollowup) for f in logger.filters):
        logger.addFilter(_DropTruncationFollowup())


class SuppressClientDisconnect:
    """Outermost ASGI wrapper. Two expected end-of-stream conditions otherwise reach uvicorn as a
    scary 'Exception in ASGI application' + ExceptionGroup traceback (nginx: 'upstream prematurely
    closed connection'):

    * the player disconnects mid-`StreamingResponse` (seek / buffer-ahead / stop), which Starlette's
      anyio task group surfaces as an aborted send();
    * `wait_and_read` ends a stream early on a peer-starved piece, leaving the body short of the
      Content-Length the 206 already announced (see `_TRUNCATED_BODY_MSG`).

    Playback is unaffected in both cases — the player simply re-requests — so swallow them to keep
    the log readable. Any group containing a genuine error propagates unchanged."""

    def __init__(self, app) -> None:
        self.app = app
        _install_log_filter()

    async def __call__(self, scope, receive, send) -> None:
        try:
            await self.app(scope, receive, send)
        except BaseException as exc:
            if scope.get("type") == "http" and _all_benign_stream_end(exc):
                if any(_is_truncated_body(leaf) for leaf in _leaves(exc)):
                    _truncated.set(True)  # silences uvicorn's follow-up for THIS request only
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
    # Opt-in. Registering nothing when off means an unset flag cannot be probed for, and the
    # allowlist test's "flag off -> no route" assertion is about absence, not about a 403.
    if settings.library_ui:
        app.include_router(library_api.router)
    return app


def build_app() -> FastAPI:
    """Server entrypoint factory: creates the real libtorrent engine from settings.

    Run with:  uvicorn stremiosrv.app:build_app --factory --host 0.0.0.0 --port <p>
    """
    import os
    import threading

    from stremiosrv.cache import run_evictor
    from stremiosrv.torrent.engine import Engine
    from stremiosrv.torrent.tracker_source import TrackerSource
    from stremiosrv.torrent.trackers import parse_tracker_string
    from stremiosrv.transcode.converter import Converter, run_transcode_gc
    from stremiosrv.transcode.profiler import detect_profile

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
        dht_bootstrap_nodes=settings.dht_bootstrap_nodes,
        adaptive_picking=settings.adaptive_picking,
        adaptive_low_bytes=settings.adaptive_low_bytes,
        adaptive_high_bytes=settings.adaptive_high_bytes,
        adaptive_interval=settings.adaptive_interval,
        prefetch_next=settings.prefetch_next,
        prefetch_next_fraction=settings.prefetch_next_fraction,
        prefetch_next_max_bytes=settings.prefetch_next_max_bytes,
        prefetch_trigger_fraction=settings.prefetch_trigger_fraction,
    )
    engine.load_pins_into_session()
    converter = Converter(settings.cache_root, settings.transcode_profile)
    # Every transcode directory on disk right now belongs to a process that no longer exists, so
    # this sweep takes no grace. Without it a crash or a `docker restart` orphans the whole segment
    # tree permanently: the evictor may not touch it, and only its own job id could have destroyed
    # it. One such directory survived two months of restarts before this existed.
    converter.sweep(max_age=0)
    # Background cache eviction so the download cache stays under budget during long real-world use.
    threading.Thread(
        target=run_evictor,
        args=(settings.cache_root, settings.cache_size, engine),
        kwargs={"interval": settings.cache_evict_interval, "grace": settings.cache_evict_grace},
        daemon=True,
    ).start()
    # ...and the same for transcode output, which the evictor is forbidden to reclaim.
    threading.Thread(
        target=run_transcode_gc,
        args=(converter,),
        kwargs={
            "interval": settings.transcode_gc_interval,
            "max_age": settings.transcode_gc_max_age,
        },
        daemon=True,
    ).start()
    # Wrap outermost so a mid-stream client disconnect doesn't spam the ASGI error log (see
    # SuppressClientDisconnect). create_app stays a plain FastAPI app for tests.
    return SuppressClientDisconnect(
        create_app(settings=settings, engine=engine, converter=converter)
    )
