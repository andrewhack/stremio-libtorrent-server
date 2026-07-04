import os

from stremiosrv.torrent.tracker_source import TrackerSource

BODY = "udp://a.example:1337/announce\nudp://b.example:6969/announce\n"


def _fetcher(text):
    """Build a fetcher(url, timeout) that returns `text` (or raises if text is an Exception)."""
    def fetch(url, timeout):
        if isinstance(text, Exception):
            raise text
        return text
    return fetch


def test_disabled_when_no_url(tmp_path):
    src = TrackerSource("", cache_path=str(tmp_path / "c"))
    assert src.enabled is False
    assert src.current() == []
    src.start()  # must be a no-op, no thread, no raise


def test_refresh_success_updates_and_caches(tmp_path):
    cache = str(tmp_path / "trackers.remote")
    src = TrackerSource("https://x/best.txt", cache_path=cache, fetcher=_fetcher(BODY))
    assert src.refresh_once() is True
    assert src.current() == ["udp://a.example:1337/announce", "udp://b.example:6969/announce"]
    assert os.path.exists(cache)
    with open(cache, encoding="utf-8") as f:
        assert "udp://a.example:1337/announce" in f.read()


def test_offline_keeps_last_good_and_never_raises(tmp_path):
    src = TrackerSource("https://x/best.txt", cache_path=str(tmp_path / "c"), fetcher=_fetcher(BODY))
    src.refresh_once()
    good = src.current()
    src._fetch = _fetcher(OSError("no network"))  # simulate going offline
    assert src.refresh_once() is False
    assert src.current() == good  # unchanged, no exception


def test_malformed_body_does_not_clobber(tmp_path):
    src = TrackerSource("https://x/best.txt", cache_path=str(tmp_path / "c"), fetcher=_fetcher(BODY))
    src.refresh_once()
    good = src.current()
    src._fetch = _fetcher("<html>garbage 404</html>")
    assert src.refresh_once() is False
    assert src.current() == good


def test_loads_cache_on_init(tmp_path):
    cache = tmp_path / "trackers.remote"
    cache.write_text(BODY, encoding="utf-8")
    src = TrackerSource("https://x/best.txt", cache_path=str(cache), fetcher=_fetcher(""))
    # starts from the on-disk cache (offline box uses last good list without any fetch)
    assert src.current() == ["udp://a.example:1337/announce", "udp://b.example:6969/announce"]


def test_refresh_interval_floored(tmp_path):
    src = TrackerSource("https://x/best.txt", cache_path=str(tmp_path / "c"), refresh_hours=0.0)
    assert src._interval >= 60.0  # never hammer, even if misconfigured to 0
