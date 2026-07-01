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


class PinSpaceError(Exception):
    """Raised when pinning a torrent would leave too little free disk for streaming."""
    def __init__(self, needed: int, free: int) -> None:
        super().__init__("insufficient space to pin")
        self.needed = needed
        self.free = free


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
        if getattr(self, "_focused_idx", None) == idx:
            return
        ti = self._h.torrent_file()
        if ti is None:
            return
        nfiles = ti.files().num_files()
        base = 1 if getattr(self, "pinned", False) else 0  # pinned: seed all files; else only this one
        prios = [base] * nfiles
        if 0 <= idx < nfiles:
            prios[idx] = 4  # the played file: download it fully
        try:
            self._h.prioritize_files(prios)
            self._h.set_sequential_download(True)  # fill the wanted file contiguously, front->end
        except Exception:  # noqa: BLE001 — best-effort; deadlines still drive the playhead
            pass
        self._focused_idx = idx

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
                 resume_save_interval: int = 30) -> None:
        self._ses = lt.session({
            "listen_interfaces": f"0.0.0.0:{listen_port}",  # INBOUND listener (stock server lacks this)
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
        p.trackers = merge_trackers(existing, trackers)
        p.save_path = self._cache_root
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
