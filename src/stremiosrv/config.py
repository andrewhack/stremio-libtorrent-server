from pydantic import ByteSize
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration. Overridable via STREMIOSRV_* env vars.

    Every size and rate below is a `ByteSize`, so it accepts a plain byte count (what every existing
    compose file and the appliance image already pass — those keep working unchanged) or a suffixed
    string: `64GiB`, `512MiB`, `1.5GiB`, `2MiB`. Two things about the suffixes are worth knowing
    before typing one, because both are easy to get wrong and neither fails loudly:

    * **`GiB` is binary, `GB`/`G` are decimal.** `64GiB` is 68,719,476,736 bytes; `64G` is
      64,000,000,000 — about 4.4 GiB less than most people mean by "64 gig". Prefer the `i` forms.
    * **`b` never means bits.** Parsing is case-insensitive, so `Gb` and `GB` are the same thing.
      That matters most on the rate limits, which are bytes/second while network speeds are habitually
      quoted in bits: `100Mb` here is 100 MB/s, not a 100 Mbit line.

    Anything unparseable is a startup error, not a silent default — a mistyped budget should stop the
    server, not quietly run with 18 GiB.
    """

    model_config = SettingsConfigDict(env_prefix="STREMIOSRV_")

    http_port: int = 11470
    bt_listen_port: int = 6881
    enable_upnp: bool = True
    cache_root: str = "/root/.stremio-server"
    cert_file: str = "certificates.pem"  # active TLS cert (in cache_root); watched by /health
    cache_size: ByteSize = 19_327_352_832  # 18 GiB download-cache budget (must exceed your largest file)
    cache_evict_interval: int = 60  # seconds between eviction sweeps
    # Don't evict torrents served within this many seconds. 30 min, not 5: the grace is refreshed by
    # each byte-range request, and a 4K player that pulls a large chunk then plays it back locally
    # can go minutes between requests — long enough, at 5 min, for the title being watched to become
    # evictable while the cache is over budget. Reclaim is correspondingly later; the evictor sweeps
    # every `cache_evict_interval`, so space comes back within a minute of the grace expiring.
    cache_evict_grace: int = 1800
    bt_max_connections: int = 400
    download_rate_limit: ByteSize = 0  # bytes/sec cap on torrent download (0 = unlimited)
    upload_rate_limit: ByteSize = 0  # bytes/sec cap on torrent upload (0 = unlimited)
    # Cross-torrent active prioritization: while ANY torrent has an open playback stream, cap each
    # OTHER (idle) torrent's download to this many bytes/sec so background fills yield the pipe to what
    # is being watched now. Within-torrent file priority alone can't rank one torrent over another.
    # 0 = disabled. Default 1 MiB/s.
    idle_download_rate_limit: ByteSize = 1_048_576
    max_streams: int = 0  # max concurrent playbacks (distinct torrents being streamed); 0 = unlimited
    seed_on_complete: bool = True  # keep seeding after a torrent finishes; False = stop seeding on complete
    max_seed_minutes: int = 0  # stop seeding this many minutes after completion; 0 = unlimited
    seed_policy_interval: int = 15  # seconds between seeding-policy sweeps
    readahead_bytes: ByteSize = 268_435_456  # 256 MiB rushed playhead window (deeper = fewer rebuffers);
    # the rest of the played file fills via the full sequential background download (engine.focus_file)
    # How long a byte-range request waits for one piece before giving up and ending the stream (the
    # player then re-requests, so this is a stall budget, not a failure budget). The FIRST piece of a
    # request gets the longer budget: it is a cold start — either the very beginning of playback or a
    # seek into a region nothing has downloaded yet — while later pieces are being fed by an already
    # warm sequential window. Raising `stream_piece_timeout` helps a slow or thinly-peered swarm,
    # where the piece does arrive and the wait beats an interruption — but it cuts both ways: when
    # the piece is never coming, it is exactly how long playback freezes before the retry that would
    # have recovered it. Tune it against what the ending-stream warnings say actually happened.
    stream_piece_timeout: float = 30.0
    stream_first_piece_timeout: float = 120.0
    resume_save_interval: int = 30  # seconds between periodic fast-resume saves (survives ungraceful stop)
    # How long a fast-resume record outlives the data it describes. It is kept long past
    # eviction on purpose -- it carries the torrent's info-dict, so re-adding an evicted title
    # skips the metadata fetch -- so this is an upper bound on an unclaimed record rather than
    # a retention target. Cached, kept and wanted torrents are exempt at any age. 0 disables it.
    resume_retention_days: int = 365
    transcode_profile: str | None = None  # set by HW autodetect (later stage)
    # Transcode output GC. HLS segments live in <cache_root>/transcode, which the evictor is
    # forbidden to touch (it is in cache.PROTECTED), so nothing but this reclaims them. At
    # `-hls_time 4` an hour of transcoded playback writes ~900 segment files, and a client that
    # never calls /destroy — a killed TV app, a dropped connection, a restart — leaves every one of
    # them behind. `max_age` is a grace, not a retention target: a directory younger than this is
    # spared even when no process claims it, so a job caught between segment writes is never taken
    # for garbage. Jobs with a live ffmpeg are spared regardless of age.
    transcode_gc_interval: int = 300  # seconds between sweeps
    transcode_gc_max_age: int = 600   # an unclaimed job dir older than this is removed
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
    adaptive_low_bytes: ByteSize = 67_108_864     # 64 MiB buffered-ahead: below -> strict sequential (safe)
    adaptive_high_bytes: ByteSize = 268_435_456   # 256 MiB buffered-ahead: above -> parallel (throughput)
    adaptive_interval: float = 2.0           # seconds between adaptive control ticks
    # Next-episode prefetch (opt-in, OFF by default). Once the read cursor passes
    # prefetch_trigger_fraction of the played file AND that file is fully downloaded, pull the head
    # (and trailing index) of the next video file in the SAME torrent, so pressing Next starts
    # instantly. Only covers packs: a next episode in a separate torrent has an infohash the server
    # was never told. The flag off = today's behaviour, byte for byte.
    prefetch_next: bool = False
    prefetch_next_fraction: float = 0.05        # head size as a fraction of the next file
    prefetch_next_max_bytes: ByteSize = 134_217_728  # 128 MiB ceiling (a 4K episode won't pull 200 MB)
    prefetch_trigger_fraction: float = 0.90     # fire once the read cursor passes this point
    # Library download UI (opt-in). Off by default: this mounts an AUTHENTICATED page on the same
    # origin as the web player, and a stranger pulling a new tag must not silently gain a new public
    # surface. See docs/library-ui.md.
    library_ui: bool = False
    # Pin the Stremio account allowed in, as either the account `_id` or its email. Empty =
    # trust-on-first-use: the first successful sign-in records the id. What is persisted and
    # compared is always `_id` — an email can be changed on the account, the id cannot.
    library_owner: str = ""
    # Permit the UI without TLS. The session cookie then loses `Secure`, so only set this behind a
    # VPN or on a trusted LAN. Default refuses, because the fallback sign-in puts a password on the
    # wire.
    library_allow_http: bool = False
