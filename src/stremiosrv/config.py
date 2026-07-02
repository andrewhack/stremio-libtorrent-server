from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration. Overridable via STREMIOSRV_* env vars."""

    model_config = SettingsConfigDict(env_prefix="STREMIOSRV_")

    http_port: int = 11470
    bt_listen_port: int = 6881
    enable_upnp: bool = True
    cache_root: str = "/root/.stremio-server"
    cert_file: str = "certificates.pem"  # active TLS cert (in cache_root); watched by /health
    cache_size: int = 19_327_352_832  # 18 GiB download-cache budget (must exceed your largest file)
    cache_evict_interval: int = 60  # seconds between eviction sweeps
    cache_evict_grace: int = 300  # don't evict torrents served within this many seconds
    bt_max_connections: int = 400
    download_rate_limit: int = 0  # bytes/sec cap on torrent download (0 = unlimited)
    upload_rate_limit: int = 0  # bytes/sec cap on torrent upload (0 = unlimited)
    # Cross-torrent active prioritization: while ANY torrent has an open playback stream, cap each
    # OTHER (idle) torrent's download to this many bytes/sec so background fills yield the pipe to what
    # is being watched now. Within-torrent file priority alone can't rank one torrent over another.
    # 0 = disabled. Default 1 MiB/s.
    idle_download_rate_limit: int = 1_048_576
    max_streams: int = 0  # max concurrent playbacks (distinct torrents being streamed); 0 = unlimited
    seed_on_complete: bool = True  # keep seeding after a torrent finishes; False = stop seeding on complete
    max_seed_minutes: int = 0  # stop seeding this many minutes after completion; 0 = unlimited
    seed_policy_interval: int = 15  # seconds between seeding-policy sweeps
    readahead_bytes: int = 268_435_456  # 256 MiB rushed playhead window (deeper = fewer rebuffers);
    # the rest of the played file fills via the full sequential background download (engine.focus_file)
    resume_save_interval: int = 30  # seconds between periodic fast-resume saves (survives ungraceful stop)
    transcode_profile: str | None = None  # set by HW autodetect (later stage)
