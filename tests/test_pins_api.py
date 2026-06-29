from fastapi.testclient import TestClient

from stremiosrv.app import create_app


def test_pins_list_empty_without_engine():
    c = TestClient(create_app())  # engine None
    assert c.get("/pins.json").json() == []


def test_pin_ok_and_list(monkeypatch):
    class FakeEngine:
        def __init__(self):
            self.pinned = set()

        def pin(self, ih):
            self.pinned.add(ih)
            return {"infoHash": ih}

        def unpin(self, ih):
            self.pinned.discard(ih)

        def pinned_status(self):
            return [{"infoHash": ih, "name": "x", "progress": 1.0, "state": "seeding",
                     "downloaded": 10, "uploaded": 20, "ratio": 2.0, "uploadSpeed": 0, "peers": 1}
                    for ih in self.pinned]
    eng = FakeEngine()
    c = TestClient(create_app(engine=eng))
    assert c.post("/abc/pin").json() == {"ok": True}
    body = c.get("/pins.json").json()
    assert body and body[0]["ratio"] == 2.0
    assert c.post("/abc/unpin").json() == {"ok": True}
    assert c.get("/pins.json").json() == []


def test_pin_insufficient_space_returns_409():
    from stremiosrv.torrent.engine import PinSpaceError

    class FakeEngine:
        def pin(self, ih): raise PinSpaceError(needed=1100, free=500)
    c = TestClient(create_app(engine=FakeEngine()))
    resp = c.post("/abc/pin")
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"] == "insufficient_space"
    assert body["needed"] == 1100 and body["free"] == 500
