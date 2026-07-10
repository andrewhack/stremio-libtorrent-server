import threading
import types

import pytest

pytest.importorskip("libtorrent")  # Engine imports libtorrent at module load

from stremiosrv.torrent.engine import Engine  # noqa: E402


def test_alerts_loop_polls_pop_never_wait():
    """Regression guard for the 2026-07-09 SIGSEGV (core dump in libstdc++ __dynamic_cast).

    The alert pump must poll ``session.pop_alerts()`` (owned wrappers, stable lifetime) and must
    NEVER call ``session.wait_for_alert()``: even when its return value is discarded in Python, the
    boost.python binding still materialises it — dynamic_cast'ing the *borrowed* front-of-queue alert
    pointer, which can dangle under load and segfault. Runs the real loop against a fake session
    (no libtorrent session, no network)."""
    stop = threading.Event()
    seen = {"wait": 0, "pop": 0}

    class FakeSes:
        def wait_for_alert(self, _ms):
            seen["wait"] += 1
            raise AssertionError("wait_for_alert() is the segfault idiom — must not be called")

        def pop_alerts(self):
            seen["pop"] += 1
            stop.set()  # end the loop after the first poll
            return []

    fake = types.SimpleNamespace(_ses=FakeSes(), _stop=stop)
    Engine._alerts_loop(fake)  # unbound method with a minimal fake self

    assert seen["wait"] == 0, "wait_for_alert must never be called"
    assert seen["pop"] >= 1, "the loop must poll pop_alerts"
