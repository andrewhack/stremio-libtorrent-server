"""The time /proxy gives an upstream to answer (1.6.9): every blocking step waits no longer than
what is left of it, and none starts once it is up."""
from __future__ import annotations

import socket
import time

import pytest

from stremiosrv.proxy import upstream


@pytest.fixture()
def pair():
    a, b = socket.socketpair()
    yield a, b
    a.close()
    b.close()


def test_the_deadline_is_thirty_seconds():
    """The owner's choice (2026-09-13)."""
    assert upstream.DEADLINE == 30.0


def test_deadline_passed_is_a_timeout_error():
    """Every handler that treats a slow upstream as a failed one already catches it."""
    assert issubclass(upstream.DeadlinePassed, TimeoutError)


def test_a_step_waits_no_longer_than_the_time_left(pair):
    a, _b = pair
    d = upstream.Deadline(0.3)
    started = time.monotonic()
    with pytest.raises(upstream.DeadlinePassed):
        d.run(a, 60.0, a.recv, 1)  # nothing is ever sent: only the deadline ends this
    assert 0.2 < time.monotonic() - started < 2.0
    assert d.stop() is False


def test_no_step_starts_once_the_time_is_up(pair):
    a, b = pair
    d = upstream.Deadline(0.05)
    time.sleep(0.1)
    b.sendall(b"x")  # data is waiting: a step that started would return it
    with pytest.raises(upstream.DeadlinePassed):
        d.run(a, 60.0, a.recv, 1)
    with pytest.raises(upstream.DeadlinePassed):
        d.check()
    assert d.stop() is False


def test_a_step_that_times_out_on_its_own_is_not_the_deadline(pair):
    a, _b = pair
    d = upstream.Deadline(30.0)
    with pytest.raises(TimeoutError) as caught:
        d.run(a, 0.1, a.recv, 1)
    assert not isinstance(caught.value, upstream.DeadlinePassed)
    assert d.stop() is True


def test_after_stop_a_step_gets_its_own_full_timeout(pair):
    """A streamed body runs after stop(), for as long as the film lasts."""
    a, b = pair
    d = upstream.Deadline(0.05)
    assert d.stop() is True
    time.sleep(0.1)
    b.sendall(b"x")
    assert d.run(a, 5.0, a.recv, 1) == b"x"
    d.check()  # no longer raises


def test_stop_gives_the_same_answer_every_time():
    d = upstream.Deadline(0.05)
    time.sleep(0.1)
    with pytest.raises(upstream.DeadlinePassed):
        d.check()
    assert d.stop() is False
    assert d.stop() is False
