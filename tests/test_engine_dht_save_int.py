"""The DHT routing table is really written, in a form the next start reads back.

Until 1.6.45 every save failed without a word: `Engine.save_dht_state` asked the session for
`session_state()`, which the libtorrent 2.0.11 binding does not have, and `save_session_params`
swallows the AttributeError by design. No server ever had a `dht.state`. Measured on the image
(2026-10-18): `bencode(session.save_state(...))` is what `read_session_params` parses, and a session
started from it rejoins the DHT with its bootstrap routers unreachable, where one without it does not.
"""
import pytest

pytestmark = pytest.mark.integration

lt = pytest.importorskip("libtorrent")

from stremiosrv.torrent import dht_state  # noqa: E402
from stremiosrv.torrent.engine import Engine  # noqa: E402


def _reads_back(path) -> bool:
    return dht_state.load_session_params(str(path), {"enable_dht": True},
                                         read_params=lt.read_session_params) is not None


def test_the_routing_table_is_written_in_a_form_the_next_start_reads(tmp_path):
    """A session this young has no table yet, so this proves the write and the format; the round
    trip of real nodes is proven on the image, where it needs the network."""
    eng = Engine(listen_port=0, cache_root=str(tmp_path), enable_upnp=False)
    try:
        assert eng.save_dht_state() is True
        path = tmp_path / dht_state.STATE_FILENAME
        assert set(lt.bdecode(path.read_bytes())) >= {b"settings"}, "a real session's state"
        assert _reads_back(path), "the next start must be able to read what this one wrote"
    finally:
        eng.shutdown()


def test_a_stop_writes_the_routing_table(tmp_path):
    """The stop path (1.6.45's lifespan call): shutdown() leaves a dht.state the next start reads."""
    eng = Engine(listen_port=0, cache_root=str(tmp_path), enable_upnp=False)
    eng.shutdown()
    assert _reads_back(tmp_path / dht_state.STATE_FILENAME)
