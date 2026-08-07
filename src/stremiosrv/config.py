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
    # Operator-supplied extra trackers appended to every torrent's announce list (in addition to the
    # built-in DEFAULT_TRACKERS). Comma/space/newline separated; udp/http(s)/ws(s) URLs only.
    extra_trackers: str = ""
    # Optional URL of a community tracker list (e.g. the raw ngosang/trackerslist "best" file). When
    # set, it is fetched in a background thread (best-effort, never blocks) to keep the list current;
    # empty = disabled (fully static, offline-safe default).
    tracker_list_url: str = ""
    tracker_list_refresh_hours: float = 24.0  # how often the background source re-fetches
    # Optional DHT bootstrap nodes ("host:port,host:port"). Empty keeps libtorrent's built-in
    # routers. The server saves its DHT routing table to <cache_root>/dht.state and restores it on
    # start, so a node that has been online once rejoins through peers it already knows rather than
    # through anyone's bootstrap server — this setting only matters for a genuinely first boot.
    dht_bootstrap_nodes: str = ""
    # Adaptive piece-picking (experimental, OFF by default — needs on-box A/B tuning per the spec).
    # While a file is playing, relax strict sequential download to parallel/rarest-first once enough
    # is buffered contiguously ahead of the playhead (harvest swarm throughput), and re-tighten to
    # in-order when the buffer drains below the low mark or after a seek. The playhead window stays
    # boosted+deadlined either way, so continuity is protected. 0 lows/highs or the flag off = today.
    adaptive_picking: bool = False
    adaptive_low_bytes: int = 67_108_864     # 64 MiB buffered-ahead: below -> strict sequential (safe)
    adaptive_high_bytes: int = 268_435_456   # 256 MiB buffered-ahead: above -> parallel (throughput)
    adaptive_interval: float = 2.0           # seconds between adaptive control ticks
    # Next-episode prefetch (opt-in, OFF by default). Once the read cursor passes
    # prefetch_trigger_fraction of the played file AND that file is fully downloaded, pull the head
    # (and trailing index) of the next video file in the SAME torrent, so pressing Next starts
    # instantly. Only covers packs: a next episode in a separate torrent has an infohash the server
    # was never told. The flag off = today's behaviour, byte for byte.
    prefetch_next: bool = False
    prefetch_next_fraction: float = 0.05        # head size as a fraction of the next file
    prefetch_next_max_bytes: int = 134_217_728  # 128 MiB ceiling (a 4K episode won't pull 200 MB)
    prefetch_trigger_fraction: float = 0.90     # fire once the read cursor passes this point
