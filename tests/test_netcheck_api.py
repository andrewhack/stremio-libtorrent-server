from fastapi.testclient import TestClient

from stremiosrv.app import create_app


def test_netcheck_without_engine():
    c = TestClient(create_app())  # engine None
    b = c.get("/netcheck.json").json()
    assert b == {
        "listenPort": None, "peers": 0, "inboundPeers": 0,
        "portMap": {"mapped": False, "transport": None, "externalPort": None},
    }


def test_netcheck_reports_engine_signals():
    class FakeEngine:
        def listen_port(self):
            return 6881

        def peer_count(self):
            return 7

        def inbound_peer_count(self):
            return 3

        def portmap_status(self):
            return {"mapped": True, "transport": "natpmp", "externalPort": 6881}

    b = TestClient(create_app(engine=FakeEngine())).get("/netcheck.json").json()
    assert b["listenPort"] == 6881
    assert b["peers"] == 7
    assert b["inboundPeers"] == 3
    assert b["portMap"]["mapped"] is True
    assert b["portMap"]["externalPort"] == 6881
