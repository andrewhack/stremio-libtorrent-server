from fastapi import FastAPI

from stremiosrv import health
from stremiosrv.api import handshake, playback
from stremiosrv.config import Settings


def create_app(settings: Settings | None = None) -> FastAPI:
    """Application factory. Wires the Stremio streaming-server routers.

    The libtorrent engine (app.state.engine) is created in Stage 2 Task 6 on a host with
    libtorrent; until then it is None and torrent stats return null (matching the stock
    server's behaviour when no engine is active).
    """
    settings = settings or Settings()
    app = FastAPI(title="stremio-libtorrent-server")
    app.state.settings = settings
    app.state.engine = None
    app.include_router(health.router)
    app.include_router(handshake.router)
    app.include_router(playback.router)
    return app
