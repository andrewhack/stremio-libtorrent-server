"""Persisting the DHT routing table across restarts.

Joining the DHT needs an entry point. With no saved routing table libtorrent falls back to its
built-in bootstrap routers — three hostnames run by two organisations. Every reboot then depends on
them still resolving. A node that has been online once already knows hundreds of live peers; saving
that knowledge is the difference between rejoining the swarm on its own and needing someone else's
server to still exist.

No libtorrent import here on purpose: these run everywhere, including where lt is unavailable.
"""
import os

import pytest

from stremiosrv.torrent import dht_state


class FakeParams:
    """Stand-in for lt.session_params — the only attribute we touch is .settings."""

    def __init__(self, settings=None, dht="ROUTING-TABLE"):
        self.settings = dict(settings or {})
        self.dht = dht


def test_state_path_lives_beside_the_cache():
    assert dht_state.state_path("/data") == os.path.join("/data", dht_state.STATE_FILENAME)


def test_cold_start_returns_none(tmp_path):
    """No saved state is not an error — it is a first boot."""
    p = dht_state.state_path(str(tmp_path))
    assert dht_state.load_session_params(p, {"enable_dht": True}, read_params=lambda b: FakeParams()) is None


def test_corrupt_state_is_ignored_not_fatal(tmp_path):
    """A truncated file after a power cut must degrade to a cold start, never crash the engine —
    an appliance that will not boot is worse than one that re-bootstraps."""
    p = tmp_path / dht_state.STATE_FILENAME
    p.write_bytes(b"\x00 not a bencoded session \xff")

    def boom(_buf):
        raise RuntimeError("invalid bencoding")

    assert dht_state.load_session_params(str(p), {"enable_dht": True}, read_params=boom) is None


def test_saved_routing_table_is_restored(tmp_path):
    p = tmp_path / dht_state.STATE_FILENAME
    p.write_bytes(b"saved-state-bytes")
    seen = {}

    def read_params(buf):
        seen["buf"] = buf
        return FakeParams(settings={"enable_dht": False}, dht="RESTORED")

    got = dht_state.load_session_params(str(p), {"enable_dht": True}, read_params=read_params)
    assert seen["buf"] == b"saved-state-bytes"
    assert got.dht == "RESTORED"


def test_our_settings_win_over_the_saved_ones(tmp_path):
    """The file carries the settings that were live when it was written. Restoring those would let
    a stale listen port or rate limit outlive the config that replaced it — we want the routing
    table back, not last month's configuration."""
    p = tmp_path / dht_state.STATE_FILENAME
    p.write_bytes(b"x")
    saved = {"listen_interfaces": "0.0.0.0:OLD", "download_rate_limit": 999, "stale_key": 1}
    got = dht_state.load_session_params(
        str(p), {"listen_interfaces": "0.0.0.0:NEW", "download_rate_limit": 0},
        read_params=lambda b: FakeParams(settings=saved))
    assert got.settings["listen_interfaces"] == "0.0.0.0:NEW"
    assert got.settings["download_rate_limit"] == 0
    assert got.settings["stale_key"] == 1        # untouched keys survive; only conflicts are ours


def test_save_writes_atomically(tmp_path):
    """The saver runs every 30s against a file the next boot depends on. A half-written file is
    exactly the corrupt-state case above, so never write in place."""
    p = str(tmp_path / dht_state.STATE_FILENAME)
    assert dht_state.save_session_params(p, FakeParams(), write_buf=lambda _p: b"BYTES") is True
    with open(p, "rb") as fh:
        assert fh.read() == b"BYTES"
    assert [f for f in os.listdir(tmp_path) if f.endswith(".tmp")] == []


def test_save_never_raises_and_reports_failure(tmp_path):
    """It is called from the background saver thread and from shutdown. Raising there would kill
    the thread that also persists resume data — losing playback progress to protect a routing
    table would be a bad trade."""
    def boom(_p):
        raise RuntimeError("serialisation failed")

    assert dht_state.save_session_params(
        str(tmp_path / dht_state.STATE_FILENAME), FakeParams(), write_buf=boom) is False


def test_a_failed_save_leaves_no_partial_file(tmp_path):
    dht_state.save_session_params(str(tmp_path / dht_state.STATE_FILENAME), FakeParams(),
                                  write_buf=lambda _p: (_ for _ in ()).throw(OSError("disk full")))
    assert os.listdir(tmp_path) == []


def test_save_to_an_unwritable_location_is_not_fatal(tmp_path):
    assert dht_state.save_session_params(str(tmp_path / "no" / "such" / "dir" / "dht.state"),
                                         FakeParams(), write_buf=lambda _p: b"x") is False


@pytest.mark.parametrize("nodes,expected", [
    (None, None),          # config may hand us None; mutation testing found this path untested
    ("", None),
    ("  ", None),
    ("dht.example.org:6881", "dht.example.org:6881"),
    (" a:1 , b:2 ", "a:1,b:2"),
])
def test_bootstrap_override_is_normalised(nodes, expected):
    """Operators who do not want to depend on the built-in routers can name their own. Empty means
    'leave libtorrent's defaults alone' rather than 'no bootstrap at all'."""
    assert dht_state.bootstrap_setting(nodes) == expected
