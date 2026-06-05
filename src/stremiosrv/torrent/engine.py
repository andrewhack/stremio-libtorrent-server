"""libtorrent session wrapper.

The key capability vs the stock Stremio server: it **listens for inbound peers**
(`listen_interfaces = 0.0.0.0:<port>`) and downloads **sequentially** (head-first) so the
playhead region arrives before the rest of the file.

Targets libtorrent 2.0.x (python bindings).
"""
from __future__ import annotations

import libtorrent as lt

from stremiosrv.torrent.trackers import merge_trackers


class Handle:
    """Thin wrapper over `lt.torrent_handle` exposing only what the API layer needs."""

    def __init__(self, h: "lt.torrent_handle") -> None:
        self._h = h

    def status(self):
        return self._h.status()

    def has_metadata(self) -> bool:
        return self._h.status().has_metadata

    def torrent_file(self):
        return self._h.torrent_file()

    def info_hash(self) -> str:
        return str(self._h.status().info_hashes.v1)

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

    def raw(self) -> "lt.torrent_handle":
        return self._h


class Engine:
    def __init__(self, listen_port: int, cache_root: str, max_connections: int = 200) -> None:
        self._ses = lt.session({
            "listen_interfaces": f"0.0.0.0:{listen_port}",  # INBOUND listener (stock server lacks this)
            "enable_dht": True,
            "enable_lsd": True,
            "enable_upnp": True,
            "enable_natpmp": True,
            "connections_limit": max_connections,
            "download_rate_limit": 0,
        })
        self._cache_root = cache_root
        self._torrents: dict[str, Handle] = {}

    def add(self, magnet_or_hash: str, trackers: list[str] | None = None) -> Handle:
        if magnet_or_hash.startswith("magnet:"):
            p = lt.parse_magnet_uri(magnet_or_hash)
        else:
            p = lt.add_torrent_params()
            p.info_hashes = lt.info_hash_t(lt.sha1_hash(bytes.fromhex(magnet_or_hash)))
        existing = list(p.trackers) if p.trackers else []
        p.trackers = merge_trackers(existing, trackers)
        p.save_path = self._cache_root
        p.flags |= lt.torrent_flags.sequential_download
        th = self._ses.add_torrent(p)
        h = Handle(th)
        self._torrents[h.info_hash().lower()] = h
        return h

    def get(self, info_hash: str) -> Handle | None:
        return self._torrents.get(info_hash.lower())

    def remove(self, info_hash: str) -> None:
        h = self._torrents.pop(info_hash.lower(), None)
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

    def shutdown(self) -> None:
        for ih in list(self._torrents):
            self.remove(ih)
