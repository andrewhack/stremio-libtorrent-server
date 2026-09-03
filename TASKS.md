# TASKS — stremio-libtorrent-server

Known open work. `README.md` describes what the server does today; this file is what it does not do
yet, and why each item is still open.

Convention: `- [ ]` open · `- [x]` done · `- [~]` in progress · `- [!]` blocked. Every entry states
what "done" looks like, so it can be picked up without context.

**Last updated:** 2026-09-03

---

## Protocol

- [x] **`HEAD` is accepted on the `hlsv2` read routes.** FastAPI does not add HEAD to a `@router.get`
  route the way bare Starlette does, so every hlsv2 route answered 405 while the byte-range route
  answered 206. `/probe`, `/master.m3u8` and the segment route now declare both methods; the loop's
  `head-parity` conformance check compares HEAD against GET on both surfaces so it cannot regress.
  `/destroy` stays GET-only on purpose — HEAD is defined as safe, and that route tears a transcode
  job down, so a crawler or link-checker must not be able to end a playback.

- [ ] **`/subtitleSignature` always answers `{"signature": null}`.** `stremio-video` 0.0.93+ calls
  it once per load whose probe does not rule out an embedded subtitle track, but the reference
  implementation has no such route and nothing upstream consumes the value yet, so there is no
  algorithm to match. Returning an invented string would be worse than returning nothing: the client
  accepts any string and would use it the moment a consumer ships.
  *Done =* upstream defines the signature and the server computes the same value. Until then,
  `playback.subtitleSignatureAsks` in `/stats.json` counts how often real clients ask, which is the
  evidence for whether this is worth reverse-engineering.

## Library UI

- [ ] **A title the player streamed can be removed but not kept.** The library UI pins whatever it
  downloads, so those survive eviction. Anything the client streamed is an ordinary cache entry, and
  the page offers only Remove — there is no way to say "keep this one" short of downloading it again
  through the library, which re-fetches bytes already on disk. The card now states the difference
  ("complete, kept" vs "complete, cached"), so the gap is visible rather than silent, but naming a
  problem is not fixing it: a complete title the owner wants is reclaimed by the next eviction pass
  the moment the cache goes over budget.
  *Done =* a Keep action on any complete entry that has an infohash, pinning through the existing
  `POST /{infoHash}/pin` route, with the disk guard's refusal surfaced the way the download path
  already surfaces it; and Unkeep on a pinned entry, which is `unpin` without deleting anything —
  distinct from Remove, which stops the torrent and reclaims the disk.

- [ ] **The library lists torrents, but a pack holds many episodes.** An entry is one cache
  directory, so every episode inside a season pack is folded into a single card — the one the pin
  was labelled with. Watch a second episode from that pack through the player and there is nothing
  in the library to show for it: no card, no size, no way to remove just that episode. The card's
  size is the whole directory too, which is why a 4.2 GB episode can report 8.6 GB.
  The cache does not care where a request came from, and that is correct: the library UI is
  owner-gated, but the streaming server is not, so any client pointed at this box adds to the same
  cache. One person pinning an episode through the library and another streaming a different
  episode of the same pack through the player land in one torrent, sharing one directory — which
  the owner then sees as a single card whose size climbs with nothing on the page attributing it.
  So this is not a rendering nicety: it is what makes a SHARED cache legible.
  *Done =* a multi-file torrent renders one card per file that has data, driven by libtorrent's
  per-file progress and independent of which surface started it, with the torrent as their shared
  parent for removal; the card's size reports the file, not the directory it happens to share; and
  each says kept or cached, which is the distinction that decides whether it survives eviction and
  is orthogonal to who asked for it.

- [!] **The download gate reserves the whole cache budget, so almost nothing can be downloaded.**
  It applies `pins.pin_fits`'s rule -- free space must exceed the release PLUS `cache_size * 1.10`.
  That rule is right for a PIN, which can never be evicted and therefore has to leave the entire
  budget free beside it for ordinary streaming. It is wrong for a download, which since the
  want/pin split is ordinary evictable cache. With a 48 GiB budget the gate demands ~56.7 GB free
  on top of the release, so on a 72 GB disk a 10 GB file is refused with 61 GB free.
  *Done =* the download gate asks only whether it fits on the disk with a modest reserve (a small
  floor, or a fraction of the disk -- not the cache budget), and "would push the cache over budget"
  stays what it already is: the amber/red warning, not a refusal. The pin guard keeps its own rule
  unchanged, because a pin really does have to reserve the budget.

- [!] **A release inside a torrent already downloading cannot be asked for.** The release list
  decides its button from `held[infoHash]`, so every release sharing a torrent takes that torrent's
  state: with one episode of a pack downloading, every OTHER episode of the same pack shows
  "Downloading" and is disabled. Nothing has asked for those files -- the torrent is busy, the
  episode is not.
  This is the same mistake as the episode ticks and the on-disk badge before it: a pack is ONE
  infohash, so anything keyed on torrent identity can only ever describe one episode. The server
  side is already correct -- `Engine.want` appends a selector to an existing torrent, which is
  exactly the two-files-one-torrent case the wanted SET exists for.
  *Done =* the button reflects the FILE being offered, matched by the release's `fileIdx` or by the
  episode's number against the torrent's child files: complete -> On server / Keep, wanted but
  incomplete -> Downloading, otherwise -> Download, even when the torrent is already busy.

- [ ] **The disk guard cannot see the size of a magnet.** `Engine.pin` sizes the candidate with
  `total_wanted - total_done`, which is zero before metadata arrives — and a library download pins
  immediately after `add`, so the guard always measures nothing and always passes. A torrent far
  larger than the cache budget is admitted without complaint, then cannot be evicted because it is
  pinned.
  *Done =* the guard re-runs from `_apply_pending_wanted` once metadata gives a real size, with a
  loud, actionable outcome when what arrived does not fit — the pin is the owner's instruction, so
  silently dropping it is not the answer either.

## Tooling & docs

- [x] **Ruff 0.16 migration — done by pinning the rule *selection*, not chasing the findings.** The
  code never rotted: 0.16 widened ruff's built-in default set, which turned a clean tree into 66
  findings with no source change (0.16 against the classic `E4,E7,E9,F` set is clean). The lint
  surface is now declared in `pyproject.toml`, so upgrading ruff changes behaviour only when we edit
  that list. 35 findings were auto-fixed, 13 resolved by hand, `SIM105` and the prose-dash rules
  ignored with reasons, and bugbear told that FastAPI's `Query`/`Depends` are immutable calls. Clean
  under both 0.15.15 and 0.16.3.

- [x] **Stale TODO heading in `docs/protocol-map.md` rewritten.** "Still TODO in Stage 0" asked for
  captures that already sat directly beneath it and had long since become the conformance fixtures.
  It now records that work as done and names what is genuinely unmapped instead: `/proxy`, and the
  built-in addon / archive / cast families.

- [x] **The Docker Hub overview has lasting headroom.** The page is capped at 25,000 bytes and had
  ~330 left, which is one edit from blocking a release. The TLS appendix and the next-episode
  prefetch section — reference material for someone already running the server, not getting-started
  material — moved behind `<!--hub:skip-->` with pointers to the full README. The Hub copy is now
  ~18.9 kB, leaving over 6 kB. The publish step still checks the size and fails before uploading.
