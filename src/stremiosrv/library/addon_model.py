"""Stremio addon payloads, built from the library state dict.

Pure: every function here takes plain data and returns plain data, so the shapes the app depends on
can be tested without a torrent session, a cache root or a network.
"""
from __future__ import annotations

import re
from urllib.parse import unquote

from stremiosrv import pins as pinsmod

ADDON_ID = "org.stremiosrv.library"
CATALOG_ID = "library"
CATALOG_NAME = "My Library"
ID_PREFIX = "stremiosrv:"

_IH_RE = re.compile(r"^[0-9a-f]{40}$")


def format_id(info_hash: str, file_idx: int | None = None) -> str:
    """Our meta id for a whole torrent, or for one file inside it."""
    ih = info_hash.lower()
    return f"{ID_PREFIX}{ih}" if file_idx is None else f"{ID_PREFIX}{ih}:{file_idx}"


def parse_id(value: str) -> tuple[str, int | None] | None:
    """(infohash, fileIdx or None), or None when the id is not ours or is malformed.

    This is the validation boundary. The infohash from here is used to look up cache entries and
    ends up in a URL, so it is checked as 40 hex characters and nothing else -- the same guard the
    remove endpoint applies for the same reason.
    """
    if not value.startswith(ID_PREFIX):
        return None
    ih, _, idx = value[len(ID_PREFIX):].partition(":")
    ih = ih.lower()
    if not _IH_RE.match(ih):
        return None
    if not idx:
        return ih, None
    # isdecimal() matches what int() will accept (0-9 only), unlike isdigit() which is true for
    # Unicode digits like ² and ③ that cause int() to raise ValueError. The contract is to return
    # None for malformed input, not raise, so the boundary rejects anything int() won't decode.
    if not idx.isdecimal():
        return None
    return ih, int(idx)


def manifest(version: str) -> dict:
    """The addon descriptor.

    One catalog, of type `other`: Stremio catalogs are typed, so a single row holding films, series
    and unlabelled folders together has to be `other` -- which is why the stock local addon uses
    `other` for its own local ids too.
    """
    return {
        "id": ADDON_ID,
        "version": version,
        "name": CATALOG_NAME,
        "description": ("What is on this streaming server: cached titles, kept titles, "
                        "and downloads in progress."),
        "types": ["movie", "series", "other"],
        "catalogs": [{"type": "other", "id": CATALOG_ID, "name": CATALOG_NAME}],
        "resources": [
            {"name": "catalog", "types": ["other"]},
            {"name": "meta", "types": ["other"], "idPrefixes": [ID_PREFIX]},
            {"name": "stream", "types": ["other", "movie", "series"],
             "idPrefixes": [ID_PREFIX, "tt"]},
            # Not to serve subtitles -- the answer is always an empty list. The app sends this
            # request, with the video id and the playing file's size and name, whenever it plays
            # anything, and that is the only moment the server can learn what a torrent it never
            # downloaded itself actually is. See learn_labels.
            {"name": "subtitles", "types": ["movie", "series"], "idPrefixes": ["tt"]},
        ],
        "behaviorHints": {"adult": False, "p2p": False,
                          "configurable": False, "configurationRequired": False},
    }


# --- learning a title from playback ----------------------------------------------------------

_VIDEO_ID_RE = re.compile(r"(tt[0-9]+)(?::([0-9]+):([0-9]+))?")


def parse_extra(raw: str) -> dict[str, str]:
    """A subtitles request's `extra` segment, as sent, into its values.

    stremio-core percent-encodes every value as a URI component, so an `&` inside a file name
    arrives as %26. Split on the real separators first and decode each value after: decoding the
    whole segment first -- which is what the path the router hands over already is -- cuts such a
    name in two.
    """
    out: dict[str, str] = {}
    for pair in (raw or "").split("&"):
        key, sep, value = pair.partition("=")
        if sep and key:
            out[unquote(key)] = unquote(value)
    return out


def learn_labels(state: dict, type_: str, video_id: str, extra: dict) -> list[tuple[str, dict]]:
    """(infohash, label) for each unlabelled cached torrent holding the file a player reports.

    Only a download started from the page writes a label, so a title that arrived through ordinary
    playback had no identity to match on `stream/…/tt…` -- no row on its page, films included. The
    subtitles request is the one place the app says what it is playing: the video id, and the
    file's size and name. That is a join from a real playback to a real file, so the label it gives
    is a fact, not the guess from a folder name that `streams_for_meta_id` refuses to make.

    The size must match to the byte. The name must match too when the app sends one; without it,
    the size alone is accepted only when it points at exactly one torrent. A label that is already
    there is never replaced: the page writes one with a name and a poster, and the owner chose it.
    """
    m = _VIDEO_ID_RE.fullmatch(video_id or "")
    if m is None or type_ not in ("movie", "series"):
        return []
    base, season, episode = m.groups()
    if (type_ == "series") != (season is not None):
        return []
    size = extra.get("videoSize") or ""
    if not size.isdecimal() or int(size) <= 0:
        return []
    size = int(size)
    name = _basename(extra.get("filename") or "")
    label = {"metaId": base, "type": type_}
    if season is not None:
        label.update(season=int(season), episode=int(episode), videoId=video_id)
    hits = [e["infoHash"].lower() for e in state.get("entries", [])
            if is_title(e) and not e.get("label")
            and any((f.get("size") or 0) == size
                    and (not name or _basename(f.get("name") or "") == name)
                    for f in e.get("files") or [])]
    if not name and len(hits) > 1:
        return []
    return [(ih, dict(label)) for ih in hits]


def human_size(n: int) -> str:
    """Sizes for a description line. GB throughout rather than a scaling unit: a catalog row is
    scanned, not read, and one unit keeps two rows comparable at a glance."""
    return f"{(n or 0) / 1073741824:.2f} GB"


def display_name(entry: dict) -> str:
    label = entry.get("label") or {}
    name = label.get("name") or entry.get("name") or ""
    if entry.get("state") == "downloading":
        return f"{name} · {round((entry.get('progress') or 0) * 100)}%"
    return name


def _state_bits(entry: dict) -> list[str]:
    """Everything after the size: facts about the TORRENT, true whichever of its files is offered."""
    state = entry.get("state") or "idle"
    bits = ["downloading" if state == "downloading" else
            ("seeding" if state == "seeding" else "on disk")]
    if entry.get("pinned"):
        bits.append("kept")
    if entry.get("seeds"):
        bits.append(f"{entry['seeds']} seeders")
    return bits


def describe(entry: dict) -> str:
    return " · ".join([human_size(entry.get("size") or 0), *_state_bits(entry)])


def describe_file(entry: dict, f: dict) -> str:
    """The line under a stream offering ONE file of a pack.

    The size is the file's, not the torrent's. A 24 GB season pack printed on the row for a 3.5 GB
    episode is not a rounding error, it is the wrong number -- and it is the number a viewer reads
    to decide what they are about to play.
    """
    size = f.get("size") or f.get("downloaded") or 0
    return " · ".join([human_size(size), *_state_bits(entry)])


def is_complete(f: dict) -> bool:
    """Every byte of this file is here.

    Stricter than `state.is_watchable`, deliberately. That rule answers "is this file worth naming
    on the page", and a file being fetched qualifies. This one answers "can it be played right
    now", which is what a stream row promises: a partial file opens by waiting for its head and its
    index to arrive, so the first attempt stalls and a retry a minute later succeeds -- and the size
    printed on the row reads as what is already on disk when it is not. An episode at 18% behaved
    exactly that way on a real box.
    """
    size = f.get("size") or 0
    if size and (f.get("downloaded") or 0) >= size:
        return True
    return (f.get("progress") or 0) >= 0.999


def is_title(entry: dict) -> bool:
    """Something that can actually be played. Orphan partfiles are real disk usage with no torrent
    and no file, and an entry with no infohash cannot be addressed at all."""
    return entry.get("kind") != "orphan" and bool(entry.get("infoHash"))


def preview(entry: dict) -> dict:
    label = entry.get("label") or {}
    item = {
        "id": format_id(entry["infoHash"]),
        "type": "other",
        "name": display_name(entry),
        "description": describe(entry),
    }
    if label.get("poster"):
        item["poster"] = label["poster"]
    return item


def parse_skip(extra: str) -> int:
    """The paged grid's offset, out of an `extra` path segment like `skip=100` or
    `genre=Action&skip=100` -- Stremio's own format, `&`-joined `key=value` pairs. 0 when absent or
    unparseable: an addon must not fail a whole row over an extra property it does not recognise.
    """
    for pair in (extra or "").split("&"):
        key, _, value = pair.partition("=")
        if key == "skip" and value.isdecimal():
            return int(value)
    return 0


def catalog(state: dict, skip: int = 0) -> list[dict]:
    return [preview(e) for e in state.get("entries", []) if is_title(e)][skip:]


def _basename(name: str) -> str:
    """The last path segment, on either separator: an engine-side name and a disk-side one are not
    guaranteed to agree on which one they carry, or whether they carry one at all."""
    return (name or "").replace("\\", "/").rsplit("/", 1)[-1]


def playable_index(entry: dict) -> int | None:
    """Which file in the torrent this entry means, or None when nothing here can be addressed.

    A file is addressable only if its index is an int: the stream URL is
    `<origin>/<infohash>/<fileIdx>` and there is nothing else to put there. `wantedFile` is the
    NAME of the file the download was started for (engine.wanted_path), never an index -- it wins
    by matching basenames against the addressable files, whichever of them it matches, whether or
    not it has bytes yet, because it is what the download was started for. Otherwise the
    addressable file with the most bytes DOWNLOADED, not the largest declared size -- size is
    identical for a file at 0% and one that is finished, so ranking by it can point at a file that
    is not actually here yet. With at most one file listed (a single-file torrent, or no file list
    at all) index 0 is the only answer and a safe one. Anything wider with no addressable file --
    state.py's disk fallback reports every file as index None once the engine handle is gone -- is
    refused rather than guessed: on a real torrent index 0 was a text file and the video was index 1.
    """
    files = entry.get("files") or []
    addressable = [f for f in files if isinstance(f.get("index"), int)]
    wanted = entry.get("wantedFile")
    if isinstance(wanted, str) and wanted:
        wanted_base = _basename(wanted)
        for f in addressable:
            if _basename(f.get("name") or "") == wanted_base:
                return f["index"] if is_complete(f) else None
    # Complete only. Ranking by raw bytes would hand back a neighbour's boundary spill, and
    # offering a file still arriving cannot keep the promise the row makes.
    complete = [f for f in addressable if is_complete(f)]
    if complete:
        return max(complete, key=lambda f: f.get("downloaded") or 0)["index"]
    if addressable:
        return None
    # No addressable index -- state.py's disk fallback reports every file as index None once the
    # engine handle is gone, which after a restart is most of the cache. A single file there is
    # still index 0, and its completeness is knowable even when its index is not.
    if len(files) == 1:
        return 0 if is_complete(files[0]) else None
    # Nothing listed at all: no engine record and nothing readable on disk. Index 0 is the only
    # answer available, and for a single-file torrent it is the right one.
    return 0 if not files else None


def stream_for(entry: dict, origin: str, file_idx: int | None = None) -> dict | None:
    """One stream entry pointing at the copy already on disk, or None when there is no file index
    to point it at -- see `playable_index` for when that happens.

    `bingeGroup` ties every episode of one torrent together so the app can play the next one
    without asking again.
    """
    idx = playable_index(entry) if file_idx is None else file_idx
    if idx is None:
        return None
    ih = entry["infoHash"].lower()
    # Describe the file being offered when the entry knows it -- a pack's own size on one episode's
    # row is the wrong number. The file name goes first, the way every other source row names what
    # it is about to play, so ours is recognisable beside them.
    chosen = next((f for f in (entry.get("files") or []) if f.get("index") == idx), None)
    if chosen is not None and len(entry.get("files") or []) > 1:
        title = _basename(chosen.get("name") or "") + "\n" + describe_file(entry, chosen)
    else:
        title = describe(entry)
    return {
        "url": f"{origin}/{ih}/{idx}",
        "name": CATALOG_NAME,
        "title": title,
        "behaviorHints": {"bingeGroup": f"{ID_PREFIX}{ih}"},
    }


def episode_index(entry: dict, season: int, episode: int) -> int | None:
    """The torrent file index holding this episode, or None if the pack does not hold it.

    There is one label per infohash and a season pack holds many episodes, so matching the label's
    own episode number answered for exactly one of them: on a real box, a pack with six episodes on
    disk offered a stream on one episode page and nothing on the other five. The pack's file names
    know better, and `pins.select_wanted_file` already reads them -- it is what the download path
    uses to pick an episode out of a pack, so the same names resolve the same way in both places.

    Only files with bytes count. Offering an episode that is not here would start fetching it on
    play, which is the opposite of what "play the local copy" promises.
    """
    have = [f for f in (entry.get("files") or [])
            if isinstance(f.get("index"), int) and is_complete(f)]
    if not have:
        return None
    # select_wanted_file returns a position in the list it was handed, not a torrent file index.
    pos = pinsmod.select_wanted_file([f.get("name") or "" for f in have],
                                     {"season": season, "episode": episode})
    return None if pos is None else have[pos]["index"]


def _label_matches(label: dict, base: str, season: int | None, episode: int | None) -> bool:
    if (label.get("metaId") or "") != base:
        return False
    if season is None:
        return label.get("season") is None and label.get("episode") is None
    return label.get("season") == season and label.get("episode") == episode


def streams_for_meta_id(state: dict, meta_id: str, origin: str) -> list[dict]:
    """Streams for a Stremio meta id (`tt…` or `tt…:S:E`).

    Matching is on the label, which is the only place this server records what a torrent IS. An
    entry with no label cannot match and must not: guessing an identity from a folder name would
    put the wrong film behind a right-looking row. A matched entry that `stream_for` refuses (no
    addressable file) is dropped rather than included: the list this returns is what the app can
    actually play, not a row of everything that matched by name.
    """
    parts = meta_id.split(":")
    base = parts[0]
    season = int(parts[1]) if len(parts) > 2 and parts[1].isdecimal() else None
    episode = int(parts[2]) if len(parts) > 2 and parts[2].isdecimal() else None
    out: list[dict] = []
    for e in state.get("entries", []):
        if not is_title(e):
            continue
        label = e.get("label") or {}
        if not label or (label.get("metaId") or "") != base:
            continue
        stream = None
        if season is not None:
            # The pack's own files first: they cover every episode it holds, not only the one the
            # label happens to name. The label match stays below as the fallback for a torrent
            # whose file names carry no readable episode number -- there, the label is all we have.
            idx = episode_index(e, season, episode)
            if idx is not None:
                stream = stream_for(e, origin, idx)
        if stream is None and _label_matches(label, base, season, episode):
            stream = stream_for(e, origin)
        if stream is not None:
            out.append(stream)
    return out


def find_entry(state: dict, info_hash: str) -> dict | None:
    ih = (info_hash or "").lower()
    for e in state.get("entries", []):
        if is_title(e) and (e.get("infoHash") or "").lower() == ih:
            return e
    return None


def meta_for(entry: dict) -> dict:
    """The detail page for one of our ids.

    `videos` is emitted only for a pack, and only for files with bytes on disk: offering an episode
    that is not there produces a row that cannot play, which is worse than not listing it.
    """
    label = entry.get("label") or {}
    ih = entry["infoHash"].lower()
    meta = {
        "id": format_id(ih),
        "type": "other",
        "name": display_name(entry),
        "description": describe(entry),
    }
    if label.get("poster"):
        meta["poster"] = label["poster"]
    # Addressable only: state.py's disk fallback reports index None for every file, and an id
    # built from None is one parse_id rejects -- a video row that cannot be opened. `downloaded`,
    # not `size`: `size` is the file's declared size in the torrent, present the instant metadata
    # arrives and identical for a file at 0% and one that is finished, so it is not evidence that
    # anything of it is actually on disk -- `downloaded` is.
    on_disk = [f for f in (entry.get("files") or [])
               if is_complete(f) and isinstance(f.get("index"), int)]
    if len(on_disk) > 1:
        meta["videos"] = [
            {"id": format_id(ih, f["index"]), "title": f.get("name") or f"file {f['index']}",
             "released": None}
            for f in on_disk
        ]
    return meta
