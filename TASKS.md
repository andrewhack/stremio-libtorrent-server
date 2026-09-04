# TASKS — stremio-libtorrent-server

Known open work. `README.md` describes what the server does today; this file is what it does not do
yet, and why each item is still open.

Convention: `- [ ]` open · `- [x]` done · `- [~]` in progress · `- [!]` blocked. Every entry states
what "done" looks like, so it can be picked up without context.

**Last updated:** 2026-09-04

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

- [x] **A title the player streamed can be kept, not only removed.** The library UI used to pin
  whatever it downloaded, so a download survived eviction and anything the client streamed had no
  way to. Both halves of that are gone. Downloading no longer pins — it is ordinary cache the
  evictor manages — and Keep is its own control on every entry that has an infohash, calling
  `POST /library/api/pin`, which is the same act as the appliance's own pin. Unkeep is `unpin`: the
  bytes stay and become evictable again, which is what makes it different from Remove.

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

- [x] **The download gate no longer reserves the whole cache budget.** It applied `pins.pin_fits`
  -- free space must exceed the release PLUS `cache_size * 1.10`. That rule is right for a PIN,
  which can never be evicted and therefore has to leave the entire budget free beside it for
  ordinary streaming. Applied to a download, which since the want/pin split is ordinary evictable
  cache, a 48 GiB budget demanded ~56.7 GB free on top of the release: a 10 GB file refused on a
  disk with 61 GB free. The gate now asks only whether the disk can spare it, holding back the
  larger of 2 GiB and 2% of the disk so a download cannot fill it out from under the logs, the
  resume files and the transcode segments. Overrunning the budget stays what it was -- the red
  warning, not a refusal -- and the pin guard keeps its own rule, because a pin really does have to
  reserve the budget.

- [x] **A release inside a torrent already downloading can be asked for.** The release list decided
  its button from `held[infoHash]`, so every release sharing a torrent took that torrent's state:
  with one episode of a pack downloading, every OTHER episode showed "Downloading" and was
  disabled, though nothing had asked for a byte of them. The same mistake as the episode ticks and
  the on-disk badge before it -- a pack is ONE infohash, so anything keyed on torrent identity can
  only ever describe one episode.
  The button now reflects the FILE: matched by the addon's own `fileIdx` where there is one, and by
  the episode number against the torrent's file list otherwise. Complete -> On server / Keep,
  wanted but incomplete -> Downloading, and otherwise Download even while the torrent is busy with
  something else. An entry carries the torrent's per-file state and its total file count, which is
  what tells a film apart from a pack with a single episode selected; a torrent whose files nothing
  in the session knows still falls back to its own state, as it did before.
  Keep went with it. Since the want/pin split a download does not pin, so a release button labelled
  Keep was calling the download route and keeping nothing -- it wanted a file that was already on
  disk. Both surfaces now go through one `keepTitle`, which is also the only copy of the 409
  handling.

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
