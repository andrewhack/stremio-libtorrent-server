from stremiosrv.config import Settings


def test_defaults_and_env(monkeypatch):
    monkeypatch.setenv("STREMIOSRV_CACHE_SIZE", "2147483648")
    s = Settings()
    assert s.http_port == 11470
    assert s.bt_listen_port == 6881
    assert s.cache_size == 2147483648
    assert s.cache_root.endswith(".stremio-server")


def test_seed_and_stream_policy_defaults(monkeypatch):
    # Defaults: cross-torrent throttle ON (1 MiB/s), streams unlimited, seed forever.
    s = Settings()
    assert s.idle_download_rate_limit == 1_048_576
    assert s.max_streams == 0
    assert s.seed_on_complete is True
    assert s.max_seed_minutes == 0


def test_seed_and_stream_policy_env(monkeypatch):
    monkeypatch.setenv("STREMIOSRV_MAX_STREAMS", "2")
    monkeypatch.setenv("STREMIOSRV_SEED_ON_COMPLETE", "false")
    monkeypatch.setenv("STREMIOSRV_MAX_SEED_MINUTES", "60")
    monkeypatch.setenv("STREMIOSRV_IDLE_DOWNLOAD_RATE_LIMIT", "0")
    s = Settings()
    assert s.max_streams == 2
    assert s.seed_on_complete is False
    assert s.max_seed_minutes == 60
    assert s.idle_download_rate_limit == 0
