"""max_streams: the serve route refuses a new playback once the concurrent-stream cap is reached."""
from fastapi.testclient import TestClient

from stremiosrv.app import create_app
from stremiosrv.config import Settings


class _H:
    def has_metadata(self) -> bool:
        return True

    def is_active(self) -> bool:
        return False  # the requested torrent is NOT already being streamed


class _Eng:
    def get(self, ih):
        return _H()

    def add(self, ih, trackers=None):
        return _H()

    def active_torrent_count(self) -> int:
        return 1  # one other torrent is already streaming


def test_serve_rejects_when_at_max_streams() -> None:
    app = create_app(settings=Settings(max_streams=1), engine=_Eng())
    resp = TestClient(app).get("/aabbccddeeff/0")
    assert resp.status_code == 503
    assert b"max concurrent streams" in resp.content
