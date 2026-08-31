import sys
import threading

from stremiosrv.library.ratelimit import RateLimiter


def test_allows_up_to_the_limit():
    rl = RateLimiter(limit=3, window=900)
    assert [rl.allow("ip", now=0) for _ in range(3)] == [True, True, True]


def test_blocks_past_the_limit():
    rl = RateLimiter(limit=2, window=900)
    rl.allow("ip", now=0)
    rl.allow("ip", now=0)
    assert rl.allow("ip", now=0) is False


def test_window_rolls_over():
    rl = RateLimiter(limit=1, window=900)
    assert rl.allow("ip", now=0) is True
    assert rl.allow("ip", now=100) is False
    assert rl.allow("ip", now=901) is True


def test_keys_are_independent():
    rl = RateLimiter(limit=1, window=900)
    assert rl.allow("a", now=0) is True
    assert rl.allow("b", now=0) is True
    assert rl.allow("a", now=0) is False


def test_old_keys_are_reaped_so_memory_is_bounded():
    """This is reachable from the internet: a flood of distinct source addresses must not grow the
    table forever."""
    rl = RateLimiter(limit=1, window=10)
    for i in range(500):
        rl.allow(f"ip-{i}", now=0)
    rl.allow("late", now=1000)
    assert len(rl._hits) <= 2


def test_default_now_uses_the_real_clock():
    """Every production call omits `now`. If the default path were broken, all five tests above
    would still pass while the live limiter did nothing."""
    rl = RateLimiter(limit=1, window=900)
    assert rl.allow("ip") is True
    assert rl.allow("ip") is False


def test_concurrent_calls_never_exceed_the_limit():
    """uvicorn runs sync handlers in a threadpool, so `allow` IS called concurrently. Without
    locking, two threads can both read len(hits) < limit and both append — the check-then-act race
    lets an attacker exceed the cap by sending requests in parallel, which is how credential
    stuffing is done.

    `setswitchinterval` is what makes this a real regression test rather than decoration. At the
    default 5 ms the GIL runs `allow` to completion often enough that the unlocked version passed
    8 runs out of 8 — a test that cannot fail protects nothing. Forcing preemption at nearly every
    bytecode, the unlocked version granted 14 of 60 against a limit of 5.
    """
    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-9)
    try:
        worst = 0
        for _ in range(20):
            rl = RateLimiter(limit=5, window=900)
            start = threading.Barrier(60)
            granted = []
            lock = threading.Lock()

            def worker(rl=rl, start=start, granted=granted, lock=lock):
                start.wait()
                if rl.allow("same-ip"):
                    with lock:
                        granted.append(1)

            threads = [threading.Thread(target=worker) for _ in range(60)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            worst = max(worst, len(granted))
        assert worst == 5, f"limit of 5 exceeded under concurrency: {worst} granted"
    finally:
        sys.setswitchinterval(old)
