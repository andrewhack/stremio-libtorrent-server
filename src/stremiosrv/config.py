from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration. Overridable via STREMIOSRV_* env vars."""

    model_config = SettingsConfigDict(env_prefix="STREMIOSRV_")

    http_port: int = 11470
    bt_listen_port: int = 6881
    enable_upnp: bool = True
    cache_root: str = "/root/.stremio-server"
    cache_size: int = 19_327_352_832  # 18 GiB download-cache budget (must exceed your largest file)
    cache_evict_interval: int = 60  # seconds between eviction sweeps
    cache_evict_grace: int = 300  # don't evict torrents served within this many seconds
    bt_max_connections: int = 400
    download_rate_limit: int = 0  # bytes/sec cap on torrent download (0 = unlimited)
    upload_rate_limit: int = 0  # bytes/sec cap on torrent upload (0 = unlimited)
    readahead_bytes: int = 134_217_728  # 128 MiB playhead buffer (deeper = fewer rebuffers)
    transcode_profile: str | None = None  # set by HW autodetect (later stage)
