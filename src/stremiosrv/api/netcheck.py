"""Inbound-connectivity diagnostic: is the BitTorrent listen port (6881) reachable?

The appliance's whole advantage is *inbound* peering. Behind NAT without a port-forward (or UPnP),
the box reaches only the few publicly-connectable peers -> slow cold-start playback. This endpoint
surfaces the signals the admin page needs to tell the owner whether 6881 is open:

- inboundPeers: peers THEY initiated -> any >0 proves the port is reachable from the internet.
- portMap: whether the router auto-forwarded the port via UPnP/NAT-PMP.
- listenPort / peers: context.
"""
from fastapi import APIRouter, Request

router = APIRouter()

_CLOSED_PORTMAP = {"mapped": False, "transport": None, "externalPort": None}


def _engine(request: Request):
    return getattr(request.app.state, "engine", None)


@router.get("/netcheck.json")
def netcheck(request: Request) -> dict:
    eng = _engine(request)
    if eng is None:
        return {"listenPort": None, "peers": 0, "inboundPeers": 0, "portMap": dict(_CLOSED_PORTMAP)}
    return {
        "listenPort": eng.listen_port(),
        "peers": eng.peer_count(),
        "inboundPeers": eng.inbound_peer_count(),
        "portMap": eng.portmap_status(),
    }
