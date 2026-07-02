"""Active-streams ('now playing') list endpoint for the appliance card."""
from fastapi.testclient import TestClient

from stremiosrv.api.playback import serialize_active
from stremiosrv.app import create_app


class _IH:
    v1 = "abc123def456"


class _Status:
    info_hashes = _IH()
    download_rate = 1_200_000
    upload_rate = 64_000
    num_peers = 7
    total_done = 5_000_000
    total_upload = 250_000
    progress = 0.4237


class _TI:
    def name(self):
        return "debian-12.iso"


class _Handle:
    def status(self):
        return _Status()

    def torrent_file(self):
        return _TI()

    def has_metadata(self):
        return True

    def is_active(self):
        return True

    def is_paused(self):
        return False


class _Engine:
    def active(self):
        return [_Handle()]


def test_serialize_active_shape():
    assert serialize_active(_Handle()) == {
        "infoHash": "abc123def456",
        "name": "debian-12.iso",
        "downloadSpeed": 1_200_000,
        "uploadSpeed": 64_000,
        "peers": 7,
        "downloaded": 5_000_000,
        "uploaded": 250_000,
        "progress": 0.4237,
        "active": True,
        "paused": False,
    }


def test_active_endpoint_lists_streams():
    c = TestClient(create_app(engine=_Engine()))
    body = c.get("/active.json").json()
    assert isinstance(body, list) and len(body) == 1
    assert body[0]["name"] == "debian-12.iso"
    assert body[0]["downloadSpeed"] == 1_200_000


def test_active_endpoint_empty_without_engine():
    c = TestClient(create_app())  # engine=None
    assert c.get("/active.json").json() == []
