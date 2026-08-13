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


def test_tracker_defaults_and_env(monkeypatch):
    s = Settings()
    assert s.extra_trackers == ""  # no extra trackers by default
    assert s.tracker_list_url == ""  # live-fetch OFF by default (offline-safe)
    assert s.tracker_list_refresh_hours == 24.0
    monkeypatch.setenv("STREMIOSRV_EXTRA_TRACKERS", "udp://a/announce udp://b/announce")
    monkeypatch.setenv("STREMIOSRV_TRACKER_LIST_URL", "https://x/best.txt")
    monkeypatch.setenv("STREMIOSRV_TRACKER_LIST_REFRESH_HOURS", "6")
    s = Settings()
    assert s.extra_trackers == "udp://a/announce udp://b/announce"
    assert s.tracker_list_url == "https://x/best.txt"
    assert s.tracker_list_refresh_hours == 6.0


# --- byte-valued settings accept units (pydantic ByteSize). Every existing deployment passes plain
# integers, so back-compat is the first thing asserted; the rest pins the convention, because
# GiB-vs-GB and the b/B non-distinction are both silent if they go wrong.

BYTE_FIELDS = [
    "cache_size", "download_rate_limit", "upload_rate_limit", "idle_download_rate_limit",
    "readahead_bytes", "adaptive_low_bytes", "adaptive_high_bytes", "prefetch_next_max_bytes",
]


def _env(monkeypatch, field, value):
    monkeypatch.setenv(f"STREMIOSRV_{field.upper()}", value)
    from stremiosrv.config import Settings

    return getattr(Settings(), field)


def test_plain_integers_still_work_on_every_byte_field(monkeypatch):
    """The back-compat guarantee: compose files and the appliance pass raw byte counts."""
    for f in BYTE_FIELDS:
        assert _env(monkeypatch, f, "12345678") == 12345678, f


def test_units_are_accepted_on_every_byte_field(monkeypatch):
    for f in BYTE_FIELDS:
        assert _env(monkeypatch, f, "2MiB") == 2_097_152, f


def test_gib_is_binary_and_gb_is_decimal(monkeypatch):
    """64G is NOT 64 GiB — it is ~4.4 GiB smaller. Pin it so nobody 'fixes' it into binary."""
    assert _env(monkeypatch, "cache_size", "64GiB") == 68_719_476_736
    assert _env(monkeypatch, "cache_size", "64GB") == 64_000_000_000
    assert _env(monkeypatch, "cache_size", "64G") == 64_000_000_000


def test_lowercase_b_does_not_mean_bits(monkeypatch):
    """Parsing is case-insensitive: `Gb` is gigaBYTES here. Documented, and asserted so it stays
    documented — the rate limits are bytes/sec and network speeds are quoted in bits."""
    assert _env(monkeypatch, "download_rate_limit", "100Mb") == _env(
        monkeypatch, "download_rate_limit", "100MB")


def test_fractional_units(monkeypatch):
    assert _env(monkeypatch, "adaptive_high_bytes", "1.5GiB") == 1_610_612_736


def test_zero_still_means_unlimited(monkeypatch):
    """The rate limits use 0 as a sentinel; a unit-aware type must not turn that into anything else."""
    for f in ("download_rate_limit", "upload_rate_limit", "idle_download_rate_limit"):
        assert _env(monkeypatch, f, "0") == 0, f


def test_unparseable_size_is_a_startup_error(monkeypatch):
    """Fail loud: a mistyped budget must stop the server, not silently fall back to the default."""
    import pytest
    from pydantic import ValidationError

    monkeypatch.setenv("STREMIOSRV_CACHE_SIZE", "plenty")
    from stremiosrv.config import Settings

    with pytest.raises(ValidationError):
        Settings()


def test_defaults_are_unchanged_by_the_type_switch():
    from stremiosrv.config import Settings

    s = Settings()
    assert s.cache_size == 19_327_352_832
    assert s.readahead_bytes == 268_435_456
    assert s.adaptive_low_bytes == 67_108_864
    assert s.adaptive_high_bytes == 268_435_456
    assert s.prefetch_next_max_bytes == 134_217_728
    assert s.idle_download_rate_limit == 1_048_576
    assert s.download_rate_limit == 0 and s.upload_rate_limit == 0
