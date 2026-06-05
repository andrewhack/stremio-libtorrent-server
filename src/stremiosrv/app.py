from fastapi import FastAPI

from stremiosrv import health
from stremiosrv.api import handshake, playback
from stremiosrv.config import Settings


def create_app(settings: Settings | None = None, engine=None) -> FastAPI:
    """Application factory. Wires the Stremio streaming-server routers.

    `engine` is the libtorrent engine (injected by the server entrypoint / integration tests).
    When None, torrent stats return null and the file route returns 503 — matching the stock
    server's behaviour when no engine is active, and keeping the app importable without libtorrent.
    """
    settings = settings or Settings()
    app = FastAPI(title="stremio-libtorrent-server")
    app.state.settings = settings
    app.state.engine = engine
    app.include_router(health.router)
    app.include_router(handshake.router)
    app.include_router(playback.router)
    return app
