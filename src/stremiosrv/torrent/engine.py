"""libtorrent session wrapper.

The key capability vs the stock Stremio server: it **listens for inbound peers**
(`listen_interfaces = 0.0.0.0:<port>`) and downloads **sequentially** (head-first) so the
playhead region arrives before the rest of the file.

Targets libtorrent 2.0.x (python bindings).
"""
from __future__ import annotations

import os
import shutil
import threading
import time

try:
    import libtorrent as lt
except ImportError:  # libtorrent not installed (e.g. test environments without the C extension)
    lt = None  # type: ignore[assignment]

from stremiosrv import cache as cachemod
from stremiosrv import pins as pinsmod
from stremiosrv.torrent.trackers import merge_trackers

# libtorrent's auto_managed flag. A torrent carrying it is driven by the session's auto-manager, which
# OVERRIDES an explicit handle.pause() and resumes the torrent (with unlimited active limits it keeps
# every seed running). That was the "paused:true but still uploading ~1MB/s" bug: pause() flipped our
# bookkeeping but the auto-manager re-activated the seed. So we take torrents OUT of auto-management
# (on add + on pause) — the server manages priorities/deadlines/pause explicitly. None when lt / the
# binding lacks the flag (test envs), in which case the clear is a safe no-op.
_TORRENT_FLAGS = getattr(lt, "torrent_flags", None) if lt is not None else None
_AUTO_MANAGED = getattr(_TORRENT_FLAGS, "auto_managed", None) if _TORRENT_FLAGS is not None else None
# libtorrent's DEFAULT add flags are `auto_managed | paused` — the idiom is "add paused, let the
# auto-manager start it". Once we drop auto_managed we must ALSO drop paused, or the torrent is
# stranded paused forever (no auto-manager to start it → no metadata, no download).
_PAUSED = getattr(_TORRENT_FLAGS, "paused", None) if _TORRENT_FLAGS is not None else None


class PinSpaceError(Exception):
    """Raised when pinning a torrent would leave too little free disk for streaming."""
    def __init__(self, needed: int, free: int) -> None:
        super().__init__("insufficient space to pin")
        self.needed = needed
        self.free = free


# Priority of the *played* file. A file being actively streamed downloads at ACTIVE_FILE_PRIO so it
# beats the background fill of torrents nobody is watching; when no stream is open on it, it drops to
# IDLE_FILE_PRIO — still downloading to completion (the "full torrent client" behaviour), but yielding
# bandwidth to whatever is being watched now. (Non-played files in a pack stay 0 / skipped.)
ACTIVE_FILE_PRIO = 4
IDLE_FILE_PRIO = 1


def idle_download_limit(*, this_active: bool, any_active: bool, idle_limit: int) -> int:
    """Per-torrent download cap (bytes/sec) for CROSS-torrent active prioritization. While any torrent
    has an open stream, the non-active torrents are capped to `idle_limit` so active playback wins the
    pipe; the active torrent(s) and the everything-idle case stay uncapped (0). idle_limit<=0 disables
    the feature. (file_priority only ranks pieces WITHIN a torrent — it can't make one torrent beat
    another, so a busy idle torrent could otherwise starve the one being watched.)"""
    if idle_limit > 0 and any_active and not this_active:
        return idle_limit
    return 0


def should_stop_seeding(*, pinned: bool, finished: bool, completed_at: float | None, now: float,
                        seed_on_complete: bool, max_seed_minutes: int) -> bool:
    """Whether a torrent that has all its WANTED data should stop seeding now. `finished` = all
    priority>0 pieces present (libtorrent is_finished), NOT is_seeding (the WHOLE torrent complete):
    a TV-season pack where only some episodes were watched is finished + progress 1.0 but never a
    full seed (the un-watched episodes are priority 0), yet it still uploads the pieces it kept — so
    keying off is_seeding let SEED_ON_COMPLETE / MAX_SEED_MINUTES silently never fire for packs.
    Pinned torrents always keep seeding (owner asked to keep them). seed_on_complete=False stops as
    soon as it's finished; otherwise stop max_seed_minutes after completion (0 = seed forever)."""
    if pinned or not finished or completed_at is None:
        return False
    if not seed_on_complete:
        return True
    if max_seed_minutes > 0 and (now - completed_at) >= max_seed_minutes * 60:
        return True
    return False


def should_resume_on_open(*, paused: bool, finished: bool) -> bool:
    """Whether opening a playback stream should resume a torrent the seed policy had paused. Resume
    only when it's paused AND not finished — i.e. the now-focused file still needs downloading (the
    NEXT episode of a pack), so a paused torrent would otherwise stall playback. A finished torrent
    stays paused: it plays straight from disk, so re-seeding it on a re-watch is needless upload."""
    return paused and not finished


def adaptive_sequential(buffer_bytes: int, currently_sequential: bool, low: int, high: int) -> bool:
    """Adaptive piece-picking decision: should the played torrent download strictly in-order?

    Hysteresis on how much is buffered CONTIGUOUSLY ahead of the playhead: once >= `high` we go
    parallel (return False -> rarest-first, saturate the swarm's throughput to fill/cache the rest);
    once <= `low` we go back in-order (return True -> guarantee the next pieces); between the marks we
    hold the current mode (no thrashing). The immediate playhead window stays boosted+deadlined either
    way, so continuity is protected regardless of this choice. (See the adaptive-piece-picking spec.)"""
    if high <= 0 or low < 0 or low >= high:
        return True  # misconfigured -> safe default (today's strict-sequential behaviour)
    if buffer_bytes >= high:
        return False
    if buffer_bytes <= low:
        return True
    return currently_sequential


class Handle:
    """Thin wrapper over `lt.torrent_handle` exposing only what the API layer needs."""

    def __init__(self, h: "lt.torrent_handle") -> None:
        self._h = h
        self.pinned = False
        # Playhead pieces rushed to priority 7 (see boost_piece). Mutated from the streaming thread
        # (boost_piece) and read/cleared from request threads (refocus), so guard with a lock —
        # iterating it live crashed refocus with "Set changed size during iteration".
        self._boosted: set[int] = set()
        self._boosted_lock = threading.Lock()
        # Which file is being played, and how many streams are open on this torrent right now.
        # >0 = actively watched (played file at ACTIVE_FILE_PRIO); 0 = idle (drops to IDLE_FILE_PRIO).
        self._focused_idx: int | None = None
        self._active = 0
        self._active_lock = threading.Lock()
        # Monotonic time this torrent was first observed complete (seeding), or None while incomplete.
        # Drives the stop-seeding-on-complete / max-seed-time policy. Paused = we stopped its seeding.
        self.completed_at: float | None = None
        self._paused = False
        # Adaptive piece-picking state: whether we're currently in strict-sequential mode (matches
        # focus_file's set_sequential_download(True) default). Toggled by adaptive_tick under the flag.
        self._adaptive_seq = True

    def status(self):
        return self._h.status()

    def has_metadata(self) -> bool:
        return self._h.status().has_metadata

    def torrent_file(self):
        return self._h.torrent_file()

    def info_hash(self) -> str:
        return str(self._h.status().info_hashes.v1)

    def name(self) -> str:
        ti = self._h.torrent_file()
        return ti.name() if ti else ""

    def add_trackers(self, urls: list[str]) -> int:
        """Add announce URLs not already present (a later stream request may carry new `tr=` for a
        torrent we already added). Returns the count newly added. Best-effort — never raises."""
        if not urls:
            return 0
        # libtorrent 2.0's torrent_handle.trackers() returns a list of dicts ({"url", "tier", ...});
        # be defensive about an announce_entry-object form too.
        try:
            have = set()
            for t in self._h.trackers():
                u = t["url"] if isinstance(t, dict) else getattr(t, "url", None)
                if u:
                    have.add(u)
        except Exception:  # noqa: BLE001
            have = set()
        added = 0
        for u in urls:
            if u and u not in have:
                try:
                    self._h.add_tracker({"url": u})
                    have.add(u)
                    added += 1
                except Exception:  # noqa: BLE001 — one bad URL shouldn't abort the rest
                    pass
        return added

    def peer_wires(self) -> tuple[list[dict], int]:
        """Per-peer connection list (Stremio `wires` shape) + count of peers that have unchoked us."""
        wires: list[dict] = []
        unchoked = 0
        for p in self._h.get_peer_info():
            if not (p.flags & lt.peer_info.remote_choked):
                unchoked += 1
            try:
                addr = f"{p.ip[0]}:{p.ip[1]}"
            except Exception:  # noqa: BLE001
                addr = str(getattr(p, "ip", ""))
            wires.append({
                "requests": p.download_queue_length,
                "address": addr,
                "amInterested": bool(p.flags & lt.peer_info.interesting),
                "isSeeder": bool(p.flags & lt.peer_info.seed),
                "downSpeed": p.payload_down_speed,
                "upSpeed": p.payload_up_speed,
            })
        return wires, unchoked

    # --- file / piece geometry (metadata must be present) ---
    def piece_length(self) -> int:
        return self._h.torrent_file().piece_length()

    def num_pieces(self) -> int:
        return self._h.torrent_file().num_pieces()

    def file_size(self, idx: int) -> int:
        return self._h.torrent_file().files().file_size(idx)

    def file_offset(self, idx: int) -> int:
        return self._h.torrent_file().files().file_offset(idx)

    def file_path(self, idx: int) -> str:
        return self._h.torrent_file().files().file_path(idx)

    def have_piece(self, i: int) -> bool:
        return self._h.have_piece(i)

    def prioritize_pieces(self, pieces: list[int], prio: int = 7) -> None:
        for i in pieces:
            self._h.piece_priority(i, prio)

    def focus_file(self, idx: int) -> None:
        """Download the FULL file being played (sequentially) so seeks/fast-forward land in cached
        data — but NOT the other files in the torrent. A torrent is often a multi-episode pack, so
        we want only the episode/movie being watched, not the whole pack. The played file is wanted;
        other files are priority 0 (skipped). A *pinned* torrent instead wants every file (it's kept
        and seeded). The playhead window is still rushed via per-piece deadlines on top.

        Re-applied only when the focused file changes (cheap + idempotent across a file's many range
        requests)."""
        if self._focused_idx == idx:
            return
        ti = self._h.torrent_file()
        if ti is None:
            return
        nfiles = ti.files().num_files()
        base = 1 if self.pinned else 0  # pinned: seed all files; else only this one
        prios = [base] * nfiles
        if 0 <= idx < nfiles:
            # High priority only while a stream is actually open on this torrent; otherwise idle-low
            # so it keeps downloading but yields to whatever is being watched now.
            prios[idx] = ACTIVE_FILE_PRIO if self._active else IDLE_FILE_PRIO
        try:
            self._h.prioritize_files(prios)
            self._h.set_sequential_download(True)  # fill the wanted file contiguously, front->end
        except Exception:  # noqa: BLE001 — best-effort; deadlines still drive the playhead
            pass
        self._focused_idx = idx

    def _set_focused_priority(self, prio: int) -> None:
        idx = self._focused_idx
        if idx is None:
            return
        try:
            self._h.file_priority(idx, prio)
        except Exception:  # noqa: BLE001 — best-effort
            pass

    def mark_active(self) -> None:
        """A stream opened on this torrent. The first concurrent stream promotes the played file to
        full (active) priority so it out-competes the background fill of unwatched torrents."""
        with self._active_lock:
            self._active += 1
            promote = self._active == 1
        if promote:
            self._set_focused_priority(ACTIVE_FILE_PRIO)

    def mark_idle(self) -> None:
        """A stream closed. When the last one closes, drop the played file to idle-low priority — it
        keeps downloading to completion but yields bandwidth to torrents being watched now."""
        with self._active_lock:
            if self._active > 0:
                self._active -= 1
            demote = self._active == 0
        if demote:
            self._set_focused_priority(IDLE_FILE_PRIO)

    def is_active(self) -> bool:
        return self._active > 0

    def is_seeding(self) -> bool:
        """True once the WHOLE torrent is complete (libtorrent 'is_seeding' — every piece present)."""
        try:
            return bool(self._h.status().is_seeding)
        except Exception:  # noqa: BLE001
            return False

    def is_finished(self) -> bool:
        """True once all WANTED (priority>0) data is downloaded (libtorrent 'is_finished'). Unlike
        is_seeding, this is True for a partially-watched multi-file pack whose un-watched files are
        priority 0 — which is exactly when the seed policy should engage (we're only uploading the
        pieces we kept). Drives stop-seeding-on-complete / max-seed-time."""
        try:
            return bool(self._h.status().is_finished)
        except Exception:  # noqa: BLE001
            return False

    def is_paused(self) -> bool:
        return self._paused

    def pause(self) -> None:
        """Stop the torrent — halts seeding and disconnects peers. Used by the seeding policy on a
        completed torrent (stop-seeding-on-complete / max-seed-time). Playback still serves the
        finished file straight from disk, so pausing a complete torrent doesn't break watching it.

        Clears auto_managed FIRST: an auto_managed torrent gets resumed by the session auto-manager
        right after pause() (the "paused:true but still uploading" bug), so the pause only sticks once
        the torrent is out of auto-management. No-op clear when the binding lacks the flag."""
        try:
            if _AUTO_MANAGED is not None:
                self._h.unset_flags(_AUTO_MANAGED)
            self._h.pause()
        except Exception:  # noqa: BLE001
            pass
        self._paused = True

    def resume(self) -> None:
        try:
            self._h.resume()
        except Exception:  # noqa: BLE001
            pass
        self._paused = False

    def set_download_limit(self, limit: int) -> None:
        """Per-torrent download cap in bytes/sec (0 = unlimited). Used for cross-torrent active
        prioritization — throttle idle torrents while something is being watched."""
        try:
            self._h.set_download_limit(limit)
        except Exception:  # noqa: BLE001
            pass

    def boost_piece(self, piece: int, deadline_ms: int) -> None:
        """Mark a playhead piece as top priority + urgent, and remember it so a later seek can
        drop it (refocus)."""
        self._h.piece_priority(piece, 7)
        self.set_piece_deadline(piece, deadline_ms)
        with self._boosted_lock:
            self._boosted.add(piece)

    def refocus(self) -> None:
        """Drop the previous playhead window from rushed (7) back to normal priority (4) and clear
        its deadline, so a new seek's window gets the bandwidth focus. Pieces are NOT dropped to 0 —
        they keep downloading as part of the full background fill (so a later seek back finds them)."""
        # Snapshot-and-swap under the lock so we never iterate the live set while the streaming
        # thread is adding to it (that raced -> "Set changed size during iteration", which aborted
        # the request and stopped a new episode/seek from starting). Pieces boosted after the swap
        # land in the fresh set and are handled by the next refocus.
        with self._boosted_lock:
            boosted = self._boosted
            self._boosted = set()
        for p in boosted:
            if not self._h.have_piece(p):
                self._h.piece_priority(p, 4)  # normal/wanted (keep downloading), not 0
                try:
                    self._h.reset_piece_deadline(p)
                except Exception:  # noqa: BLE001
                    pass

    def adaptive_tick(self, low: int, high: int):
        """One adaptive-picking step for a playing torrent: measure how much is buffered CONTIGUOUSLY
        ahead of the playhead and toggle sequential download via adaptive_sequential(). Returns the
        new sequential mode if it changed, else None. Best-effort; never raises. The playhead window
        stays boosted+deadlined regardless, so continuity is protected."""
        try:
            ti = self._h.torrent_file()
            if ti is None:
                return None
            with self._boosted_lock:
                if not self._boosted:
                    return None
                playhead = min(self._boosted)  # the piece being waited on == the playhead
            plen = ti.piece_length()
            npieces = ti.num_pieces()
            ahead = 0  # contiguous downloaded bytes from the playhead forward (bounded scan)
            p = playhead
            while p < npieces and ahead <= high and self._h.have_piece(p):
                ahead += plen
                p += 1
            want_seq = adaptive_sequential(ahead, self._adaptive_seq, low, high)
            if want_seq != self._adaptive_seq:
                self._h.set_sequential_download(want_seq)
                self._adaptive_seq = want_seq
                return want_seq
            return None
        except Exception:  # noqa: BLE001
            return None

    def set_piece_deadline(self, piece: int, ms: int) -> None:
        """Ask libtorrent to fetch this piece within `ms` (urgent, order-independent — enables
        responsive seeking and fetching a trailing moov atom without downloading the whole file)."""
        try:
            self._h.set_piece_deadline(piece, ms)
        except Exception:  # noqa: BLE001 — deadline is best-effort
            pass

    def raw(self) -> "lt.torrent_handle":
        return self._h


class Engine:
    def __init__(self, listen_port: int, cache_root: str, max_connections: int = 400,
                 download_rate_limit: int = 0, upload_rate_limit: int = 0,
                 cache_size: int = 0,  # 0 = guard disabled; build_app passes settings.cache_size
                 resume_save_interval: int = 30,
                 idle_download_rate_limit: int = 0,  # cross-torrent active prioritization (0 = off)
                 seed_on_complete: bool = True, max_seed_minutes: int = 0,
                 seed_policy_interval: int = 15,
                 extra_trackers: list[str] | None = None,  # operator env trackers, added to every add()
                 tracker_source=None,  # optional TrackerSource (live list); None = static only
                 adaptive_picking: bool = False,  # experimental parallel-fill when buffer is deep
                 adaptive_low_bytes: int = 0, adaptive_high_bytes: int = 0,
                 adaptive_interval: float = 2.0) -> None:
        self._ses = lt.session({
            # INBOUND listener (stock server lacks this) — dual-stack so IPv6 peers can reach us too;
            # a host without IPv6 just fails that bind and keeps IPv4 (libtorrent degrades gracefully).
            "listen_interfaces": f"0.0.0.0:{listen_port},[::]:{listen_port}",
            "enable_dht": True,
            "enable_lsd": True,
            "enable_upnp": True,
            "enable_natpmp": True,
            "download_rate_limit": download_rate_limit,  # bytes/sec, 0 = unlimited
            "upload_rate_limit": upload_rate_limit,      # bytes/sec, 0 = unlimited
            # Streaming-tuned (mirrors the stock server's "ultra_fast" profile): ramp peers fast,
            # keep deep request queues, prefer TCP, suggest from read cache.
            "connections_limit": max_connections,
            "connection_speed": 500,
            "request_queue_time": 1,
            "max_out_request_queue": 1500,
            "max_allowed_in_request_queue": 2000,
            "whole_pieces_threshold": 5,
            "peer_connect_timeout": 2,
            "piece_timeout": 10,
            "aio_threads": 8,
            "send_buffer_watermark": 4194304,
            "suggest_mode": 1,            # suggest_read_cache
            "mixed_mode_algorithm": 0,    # prefer_tcp
            "active_downloads": -1,
            "active_limit": -1,
            "announce_to_all_trackers": True,
            "announce_to_all_tiers": True,
            "allow_multiple_connections_per_ip": True,
        })
        self._cache_root = cache_root
        # Extra trackers injected into every torrent: operator-supplied (env) + an optional live
        # source. Both feed merge_trackers; the source is read (never awaited) on each add().
        self._extra_trackers = list(extra_trackers or [])
        self._tracker_source = tracker_source
        self._torrents: dict[str, Handle] = {}
        self._last_access: dict[str, float] = {}  # infohash -> monotonic time of last serve
        self._resume_dir = os.path.join(cache_root, ".resume")
        os.makedirs(self._resume_dir, exist_ok=True)
        self._pinned: set[str] = set()  # lowercased infohashes; populated by caller/pin()
        self._cache_size = cache_size
        # Latest UPnP/NAT-PMP port-map result (best-effort; populated by the alerts loop if the
        # router auto-forwards). {"mapped": bool, "transport": str|None, "externalPort": int|None}
        self._portmap = {"mapped": False, "transport": None, "externalPort": None}
        self._stop = threading.Event()
        self._alerts = threading.Thread(target=self._alerts_loop, daemon=True)
        self._alerts.start()
        # Periodically persist fast-resume so an ungraceful container stop (SIGKILL, power loss)
        # still leaves recent resume data -> next play re-adds without a full recheck -> no black
        # first-play after restart. shutdown() also saves on a graceful stop.
        self._resume_save_interval = resume_save_interval
        self._saver = threading.Thread(target=self._resume_saver_loop, daemon=True)
        self._saver.start()
        # Seeding policy (stop-seeding-on-complete / max-seed-time) + cross-torrent bandwidth policy.
        self._idle_download_rate_limit = idle_download_rate_limit
        self._seed_on_complete = seed_on_complete
        self._max_seed_minutes = max_seed_minutes
        self._seed_policy_interval = seed_policy_interval
        self._policy = threading.Thread(target=self._seed_policy_loop, daemon=True)
        self._policy.start()
        # Adaptive piece-picking (experimental, opt-in). Thread only runs when enabled, so default
        # behaviour is byte-for-byte unchanged (the "never worse than today" guardrail).
        self._adaptive_picking = adaptive_picking
        self._adaptive_low = adaptive_low_bytes
        self._adaptive_high = adaptive_high_bytes
        self._adaptive_interval = max(0.5, adaptive_interval)
        if self._adaptive_picking and self._adaptive_high > 0:
            threading.Thread(target=self._adaptive_loop, daemon=True).start()

    def _touch(self, info_hash: str) -> None:
        self._last_access[info_hash.lower()] = time.monotonic()

    def _resume_file(self, info_hash: str) -> str:
        return os.path.join(self._resume_dir, info_hash.lower() + ".fastresume")

    def _alerts_loop(self) -> None:
        while not self._stop.is_set():
            self._ses.wait_for_alert(1000)
            for a in self._ses.pop_alerts():
                if isinstance(a, lt.save_resume_data_alert):
                    try:
                        ih = str(a.params.info_hashes.v1)
                        buf = lt.write_resume_data_buf(a.params)
                        path = self._resume_file(ih)
                        tmp = path + ".tmp"
                        with open(tmp, "wb") as f:
                            f.write(buf)
                        os.replace(tmp, path)
                    except Exception:  # noqa: BLE001 — never let the alerts thread die
                        pass
                    try:
                        name = a.params.name
                        if name:
                            index = cachemod.load_name_index(self._cache_root)
                            index[name] = ih
                            cachemod.save_name_index(self._cache_root, index)
                    except Exception:  # noqa: BLE001 — index is best-effort
                        pass
                elif isinstance(a, lt.portmap_alert):
                    # router auto-forwarded our BT port (UPnP / NAT-PMP)
                    self._portmap = {"mapped": True, "transport": str(a.map_transport),
                                     "externalPort": int(a.external_port)}
                elif isinstance(a, lt.portmap_error_alert):
                    self._portmap = {"mapped": False, "transport": str(a.map_transport),
                                     "externalPort": None}

    def save_all_resume(self) -> None:
        """Ask libtorrent to persist resume data for every torrent (alerts loop writes the files)."""
        flags = getattr(lt, "save_resume_flags_t", None)
        for h in self._torrents.values():
            try:
                if flags is not None and hasattr(flags, "save_info_dict"):
                    h.raw().save_resume_data(flags.save_info_dict)
                else:
                    h.raw().save_resume_data()
            except Exception:  # noqa: BLE001
                pass

    def _resume_saver_loop(self) -> None:
        """Background loop: periodically persist resume data so a crash/kill loses < interval of
        progress (avoids the recheck/black-first-play after a non-graceful restart)."""
        while not self._stop.is_set():
            self._stop.wait(self._resume_save_interval)
            if self._stop.is_set():
                break
            try:
                self.save_all_resume()
            except Exception:  # noqa: BLE001 — never let the saver thread die
                pass

    def recent_names(self, grace: int) -> set[str]:
        """Torrent file/dir names served within `grace` seconds — protected from eviction."""
        now = time.monotonic()
        names: set[str] = set()
        for ih, t in self._last_access.items():
            if now - t <= grace:
                h = self._torrents.get(ih)
                if h is not None and h.has_metadata():
                    names.add(h.name())
        return names

    def name_to_hash(self) -> dict[str, str]:
        """Map on-disk torrent name -> infohash for active torrents (so eviction can stop them)."""
        return {h.name(): ih for ih, h in self._torrents.items() if h.has_metadata()}

    def load_pins_into_session(self) -> None:
        """At startup: re-add every pinned torrent from resume data and resume seeding."""
        self._pinned = pinsmod.pinned_hashes(self._cache_root)
        for e in pinsmod.load_pins(self._cache_root):
            ih = (e.get("infoHash") or "").lower()
            if not ih:
                continue
            h = self.add(ih, trackers=e.get("trackers"))
            h.pinned = True
            self._full_priority(h)

    def _full_priority(self, h: "Handle") -> None:
        n = h.num_pieces() if h.has_metadata() else 0
        if n:
            h.raw().prioritize_pieces([1] * n)

    def _remaining_bytes(self, h: "Handle") -> int:
        st = h.status()
        return max(0, st.total_wanted - st.total_done)

    def is_pinned(self, info_hash: str) -> bool:
        return info_hash.lower() in self._pinned

    def pinned_names(self) -> set[str]:
        return {h.name() for ih, h in self._torrents.items()
                if ih in self._pinned and h.has_metadata()}

    def pin(self, info_hash: str) -> dict:
        ih = info_hash.lower()
        h = self.get(info_hash) or self.add(info_hash)
        # disk guard: existing incomplete pins + this candidate must still leave headroom
        free = shutil.disk_usage(self._cache_root).free
        pinned_remaining = sum(self._remaining_bytes(self._torrents[p])
                               for p in self._pinned if p in self._torrents and p != ih)
        candidate_remaining = self._remaining_bytes(h)
        if not pinsmod.pin_fits(free, pinned_remaining, candidate_remaining, self._cache_size):
            raise PinSpaceError(pinsmod.headroom(self._cache_size), free)
        self._pinned.add(ih)
        h.pinned = True
        self._full_priority(h)
        entry = {"infoHash": ih, "name": h.name() if h.has_metadata() else "",
                 "trackers": [], "addedAt": int(time.time())}
        existing = [e for e in pinsmod.load_pins(self._cache_root)
                    if (e.get("infoHash") or "").lower() != ih]
        existing.append(entry)
        pinsmod.save_pins(self._cache_root, existing)
        self.save_all_resume()
        return entry

    def unpin(self, info_hash: str) -> None:
        ih = info_hash.lower()
        self._pinned.discard(ih)
        h = self._torrents.get(ih)
        if h is not None:
            h.pinned = False
        remaining = [e for e in pinsmod.load_pins(self._cache_root)
                     if (e.get("infoHash") or "").lower() != ih]
        pinsmod.save_pins(self._cache_root, remaining)

    def pinned_status(self) -> list[dict]:
        out = []
        for ih in self._pinned:
            h = self._torrents.get(ih)
            if h is None or not h.has_metadata():
                continue
            st = h.status()
            down = st.all_time_download or st.total_done or 0
            up = st.all_time_upload or st.total_upload or 0
            out.append({
                "infoHash": ih,
                "name": h.name(),
                "progress": round(st.progress, 4),
                "state": "seeding" if st.is_seeding else "downloading",
                "downloaded": down,
                "uploaded": up,
                "ratio": round(up / down, 3) if down else 0.0,
                "uploadSpeed": st.upload_rate,
                "peers": st.num_peers,
            })
        return out

    def add(self, magnet_or_hash: str, trackers: list[str] | None = None) -> Handle:
        if magnet_or_hash.startswith("magnet:"):
            p = lt.parse_magnet_uri(magnet_or_hash)
        else:
            p = lt.add_torrent_params()
            p.info_hashes = lt.info_hash_t(lt.sha1_hash(bytes.fromhex(magnet_or_hash)))
        info_hash = str(p.info_hashes.v1)
        resume_path = self._resume_file(info_hash)
        if os.path.exists(resume_path):
            try:
                with open(resume_path, "rb") as f:
                    p = lt.read_resume_data(f.read())  # trusts on-disk pieces -> no recheck
            except Exception:  # noqa: BLE001 — corrupt resume: fall back to a fresh add
                pass
        existing = list(p.trackers) if p.trackers else []
        live = self._tracker_source.current() if self._tracker_source else None
        p.trackers = merge_trackers(existing, trackers, env=self._extra_trackers, live=live)
        p.save_path = self._cache_root
        # Take the torrent OUT of libtorrent's auto-management so our explicit pause() (the seed
        # policy) is actually honored — an auto_managed torrent is resumed by the auto-manager,
        # defeating stop-seeding-on-complete / max-seed-time. We manage priorities/deadlines/pause
        # ourselves; with active_limit=-1 auto-management wasn't queuing anything anyway. Clear
        # `paused` in the same breath: the default flags set BOTH, and without the auto-manager to
        # start it, a paused-and-unmanaged torrent would never run (no metadata, no download).
        if _AUTO_MANAGED is not None:
            try:
                p.flags = p.flags & ~_AUTO_MANAGED
                if _PAUSED is not None:
                    p.flags = p.flags & ~_PAUSED
            except Exception:  # noqa: BLE001 — binding without settable flags: keep the default
                pass
        # No sequential_download flag: playback uses per-piece deadlines (set on the requested
        # range) so seeks and trailing-moov fetches are fast instead of waiting for in-order download.
        th = self._ses.add_torrent(p)
        h = Handle(th)
        h.pinned = info_hash.lower() in self._pinned
        self._torrents[h.info_hash().lower()] = h
        self._touch(h.info_hash())
        return h

    def active(self) -> list[Handle]:
        """Live torrent handles that have metadata — for the 'now playing' / active-streams view."""
        return [h for h in self._torrents.values() if h.has_metadata()]

    def active_torrent_count(self) -> int:
        """Number of distinct torrents currently being streamed (for the max-concurrent-streams cap)."""
        return sum(1 for h in self._torrents.values() if h.is_active())

    def _apply_bandwidth_policy(self) -> None:
        """Cross-torrent active prioritization: while any torrent is being streamed, cap the download
        rate of the OTHERS so active playback isn't crowded by background fills; lift the cap when
        nothing is playing. No-op when idle_download_rate_limit is 0."""
        if self._idle_download_rate_limit <= 0:
            return
        handles = list(self._torrents.values())
        any_active = any(h.is_active() for h in handles)
        for h in handles:
            h.set_download_limit(idle_download_limit(
                this_active=h.is_active(), any_active=any_active,
                idle_limit=self._idle_download_rate_limit,
            ))

    def note_stream_open(self, h: "Handle") -> None:
        """A playback stream opened: promote its played file to active priority and re-apply the
        cross-torrent bandwidth caps so idle torrents yield to it.

        If the seed policy had PAUSED this torrent (stop-seeding-on-complete / max-seed-time) and the
        now-focused file still needs downloading — e.g. the NEXT episode of a TV pack, not yet on disk
        — resume it, else playback would stall on a paused torrent. `focus_file()` already ran
        (playback.py) so `is_finished()` reflects the newly-focused file: a complete torrent being
        re-watched stays paused (served from disk, no needless re-seed); an incomplete focus resumes.
        The seed policy re-pauses once the new file finishes."""
        if should_resume_on_open(paused=h.is_paused(), finished=h.is_finished()):
            h.resume()
        h.mark_active()
        self._apply_bandwidth_policy()

    def note_stream_close(self, h: "Handle") -> None:
        """A playback stream closed: demote to idle-low and re-apply cross-torrent bandwidth caps."""
        h.mark_idle()
        self._apply_bandwidth_policy()

    def _seed_policy_loop(self) -> None:
        """Background loop enforcing stop-seeding-on-complete / max-seed-time (pausing disconnects
        peers too). Pinned torrents always keep seeding."""
        while not self._stop.is_set():
            self._stop.wait(self._seed_policy_interval)
            if self._stop.is_set():
                break
            try:
                self._enforce_seed_policy()
            except Exception:  # noqa: BLE001 — never let the policy thread die
                pass

    def _adaptive_loop(self) -> None:
        """Background loop (only started when adaptive_picking is on): for each ACTIVELY-streamed
        torrent, run one adaptive_tick so a deep buffer relaxes strict-sequential download (throughput)
        and a shallow one / a seek re-tightens it (continuity)."""
        while not self._stop.is_set():
            self._stop.wait(self._adaptive_interval)
            if self._stop.is_set():
                break
            for h in list(self._torrents.values()):
                if h.is_active():
                    try:
                        h.adaptive_tick(self._adaptive_low, self._adaptive_high)
                    except Exception:  # noqa: BLE001 — never let the adaptive thread die
                        pass

    def _enforce_seed_policy(self) -> None:
        if self._seed_on_complete and self._max_seed_minutes <= 0:
            return  # seed forever -> nothing to enforce
        now = time.monotonic()
        for h in list(self._torrents.values()):
            if not h.has_metadata():
                continue
            finished = h.is_finished()  # all WANTED data present (covers TV-packs); NOT is_seeding
            if finished and h.completed_at is None:
                h.completed_at = now
            elif not finished:
                h.completed_at = None
            if not h.is_paused() and should_stop_seeding(
                pinned=h.pinned, finished=finished, completed_at=h.completed_at, now=now,
                seed_on_complete=self._seed_on_complete, max_seed_minutes=self._max_seed_minutes,
            ):
                h.pause()

    def get(self, info_hash: str) -> Handle | None:
        h = self._torrents.get(info_hash.lower())
        if h is not None:
            self._touch(info_hash)
        return h

    def remove(self, info_hash: str) -> None:
        h = self._torrents.pop(info_hash.lower(), None)
        self._last_access.pop(info_hash.lower(), None)
        if h is not None:
            self._ses.remove_torrent(h.raw())

    def remove_all(self) -> None:
        for ih in list(self._torrents):
            self.remove(ih)

    def save_path(self) -> str:
        return self._cache_root

    def listen_port(self) -> int:
        """The actual TCP port the session is listening on (0 if not yet listening)."""
        return self._ses.listen_port()

    def peer_count(self) -> int:
        """Total peers connected across all torrents."""
        return sum(h.status().num_peers for h in self._torrents.values())

    def inbound_peer_count(self) -> int:
        """Connected peers that THEY initiated (remote-initiated). Any inbound peer proves the
        BT listen port is reachable from the internet — the core signal for 'is 6881 forwarded'."""
        if lt is None:
            return 0
        n = 0
        for h in self._torrents.values():
            try:
                for p in h.raw().get_peer_info():
                    if not (p.flags & lt.peer_info.local_connection):
                        n += 1
            except Exception:  # noqa: BLE001
                pass
        return n

    def portmap_status(self) -> dict:
        """Latest UPnP/NAT-PMP auto-forward result for the BT port (best-effort)."""
        return dict(self._portmap)

    def shutdown(self) -> None:
        self.save_all_resume()
        time.sleep(2)  # let the alerts loop flush resume files
        self._stop.set()
        for ih in list(self._torrents):
            self.remove(ih)
