# TASKS — stremio-libtorrent-server

Known open work. `README.md` describes what the server does today; this file is what it does not do
yet, and why each item is still open.

Convention: `- [ ]` open · `- [x]` done · `- [~]` in progress · `- [!]` blocked. Every entry states
what "done" looks like, so it can be picked up without context.

**Last updated:** 2026-10-04

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

- [ ] **Three rarer ways of naming a torrent's file are still ignored.** 1.6.4 added
  `/:infoHash/create` and the `-1` index, the stock client's two ways of saying "choose for me".
  Not honoured yet: `f=` on the stream URL (an addon's `fileMustInclude`, which stremio-core appends
  as regexes) is ignored, so the index or the guess is used instead; a file *name* in place of the
  index (`/:infoHash/<name>`) 404s; and `POST /create` with a `.torrent` blob (a torrent file opened
  in the app) 404s.
  *Done =* each answers as `server.reference.js` does (18204-18245 and 18383), with the `f=`
  regexes run under a time bound as the stock server's `safeStatelessRegex` does, since an addon
  writes them.

- [x] **`/proxy` is served (1.6.7, rules tightened in 1.6.9).** stremio-video routes a stream
  through it whenever the addon sets `behaviorHints.proxyHeaders`; without the route nginx
  answered with the web player's page and those streams never played.
  A client on the home network may proxy anywhere but to link-local and cloud-metadata addresses;
  a client from the internet, or a web page on another site, only to public addresses.

- [ ] **Other stock routes clients reach are still missing.** A client census (stremio-core,
  stremio-video, stremio-web, the desktop shell) found, besides the file selectors above:
  `POST /settings` (saving Settings → Streaming answers 405); the download link
  `/:infoHash/:idx?external=1&download=1` (stock redirects to `/:infoHash/<file name>` and serves
  it as an attachment); archive and usenet sources (`/{rar,zip,7zip,tar,tgz,nzb}/create`, `/ftp/`);
  `/yt/:id` for trailers played through the server; and casting (`/casting` lists nothing).
  *Done =* each answers as `server.reference.js` does, or is recorded here as deliberately out of
  scope. `unmatchedRoutes` in `/stats.json` shows which of them real clients actually ask for.

- [~] **A `-1` stream URL is not recognised outside the byte route.** stremio-core writes
  `/<infoHash>/-1` for a stream with no file index, but the parser the subtitle routes share
  (`api/subs.py` `_STREAM_RE`) accepts digits only: `/opensubHash` answers `result: null` (hash
  matching silently lost), the embedded-subtitle discovery cannot resolve the file, and
  `/<infoHash>/-1/subtitles.json|.vtt` do not exist.
  *Done =* `-1` resolves to the same file the byte route plays, on every route that takes a stream URL
  or a file index.

- [~] **The embedded-subtitle list and its extractor disagree on what `track` means.**
  `subtitles.json` reports ffprobe's index across all streams, while `subtitles.vtt?track=` maps it as
  an index among subtitle streams, so a file with video and audio first extracts the wrong track or
  none. A track that takes longer than 60 s to extract escapes as a 500.
  *Done =* the number the list gives is the number the extractor takes, as stock does, and a slow
  extraction answers like the other ffmpeg timeouts.

- [ ] **Every audio track in a transcoded stream.** Our HLS output carries the first audio track only;
  stock lists each as an `#EXT-X-MEDIA:TYPE=AUDIO` rendition. A file with two or more audio tracks is
  always transcoded by the player (it refuses direct play), and then loses every language but the
  first. *Done =* the master playlist offers every audio track, as stock does, proven against the
  real-browser gate.

- [ ] **Embedded subtitles in a transcoded stream (the browser half).** A transcoded stream carries
  no subtitle track; stock serves WebVTT renditions (`subtitle<id>.m3u8`). Needs the two items above
  first. *Done =* text subtitle tracks appear as WebVTT renditions in the master playlist; bitmap
  formats are left out and said so.

## Streaming & transcoding

- [ ] **The head check before ffprobe waits for a piece nobody asked for.** The manifest refusal
  (1.6.20) waits for the file's first piece without boosting it, so a cold torrent's first
  `/hlsv2/probe` can wait out the whole first-piece timeout and answer 504. *Done =* the head piece
  is prioritised before the wait, the wait stays fail-closed, and a trace on a cold torrent shows
  the probe answering as soon as the piece lands.

- [ ] **Transcoding a torrent still downloading ends at the first long stall.** The byte route ends
  a response when a piece misses its timeout, and ffmpeg treats a body shorter than its
  Content-Length as the end of the input, so the encode stops and the playlist freezes. *Done =* a
  stall longer than the piece timeout no longer ends the transcode (e.g. ffmpeg reconnecting to the
  same range), shown with a stalled-range test.

- [ ] **HDR sources that are transcoded come out washed out.** When a transcode is already chosen,
  an HDR source is converted to 8-bit without tone mapping. *Done =* HDR→SDR tone mapping on the
  hardware paths that support it, leaving direct play of HDR untouched.

- [ ] **VAAPI assumes one device and one capability.** Only `renderD128` is recognised, and a GPU
  that cannot decode the source profile (HEVC 10-bit on older iGPUs) fails the job instead of
  falling back. *Done =* render nodes are discovered, and an early hardware failure retries with
  software decode + hardware encode, then software.

- [ ] **Two players on one file start two encodes.** Each playback attempt gets its own transcode
  job, so a reload or a second device runs a second full encode until the idle reaper ends the
  first. *Done =* identical workloads share one ffmpeg, with the reaper counting its readers.

## Library UI

- [x] **A title the player streamed can be kept, not only removed.** The library UI used to pin
  whatever it downloaded, so a download survived eviction and anything the client streamed had no
  way to. Both halves of that are gone. Downloading no longer pins — it is ordinary cache the
  evictor manages — and Keep is its own control on every entry that has an infohash, calling
  `POST /library/api/pin`, which is the same act as the appliance's own pin. Unkeep is `unpin`: the
  bytes stay and become evictable again, which is what makes it different from Remove.

- [x] **A pack's card says which episodes it holds.** An entry is one torrent, so a season pack is
  one card -- and its caption named only the episode its label was learned from, while the others
  sat unordered in small print, in whatever order the torrent numbers its files. The card now reads
  `Show · S04 · 3 episodes` and lists each episode on disk in episode order with that file's size
  and progress, whichever surface started it, with spill from neighbouring files summed beneath.
  Chosen over one card per episode: the episodes share one torrent, one directory and one place in
  the eviction order, so Keep and Remove can only act on the whole -- a card per episode would have
  put a whole-pack Remove on every one of them. Removing a single episode is not offered.

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

- [x] **A single-file torrent's "My Library" row names its file.** The addon's stream row showed the
  file name only for a multi-file torrent; a single file got just `<size> · on disk`, so two copies
  of one title — the usual case for a standalone episode or film — differed only by a size. Every
  row that offers one known file now names it, including a lone file listed from disk with no index
  after a restart. The line is left out only on the library's own page for a title that page
  already calls by the file's name; on the title's page in the app it is always there (1.6.26 fixed
  the first cut, which keyed this on the label and dropped the name for every title learned at
  playback).

- [x] **The disk guard can see the size of a magnet.** `Engine.pin` sized the candidate with
  `total_wanted - total_done`: zero before metadata arrives, so a pin on a magnet was admitted
  unmeasured and then could not be evicted; and for a torrent nobody had narrowed, only what
  streaming had wanted so far, though the pin then switches on every file. A pin now waits up to
  15 s for metadata and is otherwise refused (`409 {"error": "size_unknown"}` — try again), measures
  what it will actually fetch (the whole torrent unless narrowed), and a refusal's `needed` is the
  full requirement rather than the headroom alone. Refusing was chosen over admitting first and
  re-checking later: nothing is ever kept unmeasured, and there is no deferred state to surface.

- [ ] **Keep's disk guard counts the cache's own bytes twice.** It asks for the whole cache budget
  plus 10% to stay free beside the pin, but the cache already on disk is not free space, so a warm
  cache needs about 2.1 times the budget: on a small disk Keep is refused for a title that would
  fit. *Done =* the guard reserves only what the cache can still grow into plus the 10% slack —
  never stricter than today, still refusing a pin that would fill the disk.

- [ ] **The free-space reserve lives only in the page.** The download button holds back the larger
  of 2 GiB and 2% of the disk, but `POST /library/api/download` itself has no guard, and `/health`
  says nothing about the disk. *Done =* the reserve is enforced by the server too (a 409 like
  Keep's), the page reads it from the state, and `/health` has a `disk` component.

- [ ] **No way to play a title from the library page.** *Done =* a card (and each file of a pack)
  offers Watch, opening it in the bundled player through its own deep link.

## Deployment & image

- [ ] **A recreated container reads as a rival cache owner.** The cache owner is identified by
  hostname, which changes when `docker compose up` recreates the container, so after an upgrade the
  new server logs "claimed by another server" and skips eviction for up to five minutes.
  *Done =* a stable identity across recreation.

- [ ] **Behind a TLS-terminating reverse proxy the server sees plain HTTP from one address.** nginx
  sets `X-Forwarded-Proto` and `X-Forwarded-For` from its own connection, so the library refuses
  sign-in as not HTTPS, addon URLs come out `http://`, and every visitor looks like the proxy (the
  docs' advice to forward the client address cannot take effect). *Done =* an opt-in trusted-proxy
  setting that believes those headers only from the listed addresses; unchanged when unset.

- [ ] **The GPU overlays assume `/dev/dri`.** `compose.gpu.yaml` maps it even for NVIDIA-only hosts
  that have none, and mapping the whole directory fails in some unprivileged containers. *Done =*
  the NVIDIA overlay needs no DRM node, and VAAPI maps render nodes only.

- [ ] **BitTorrent cannot be bound to one interface.** *Done =* a setting that pins listening and
  outgoing BitTorrent traffic to a named interface (a VPN tunnel), unset by default.

- [ ] **No ARM64 image.** The GPU base image is amd64-only. *Done =* a second, CPU-only image tag
  built for amd64 and arm64, the GPU image unchanged.

- [ ] **Image and repo hygiene.** `.env` is not gitignored and there is no `.env.example`; the image
  relies on `openssl` and `curl` arriving with the base without checking; the trusted-certificate
  fetch trusts its exit code without checking the file. *Done =* `.env` ignored and an example
  committed; a build-time check for the tools the entrypoint needs; the fetch verified by the file.

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
  By 1.6.23 new settings rows had eaten it back to under 600 bytes, so the settings table is now
  split: the everyday settings stay on the Hub page, and the specialist ones (timeouts, transcode
  housekeeping, trackers and DHT, prefetch tuning, the library's network and account rules) moved to
  a GitHub-only "More settings" table that the Hub copy links to. That leaves ~6.5 kB again; a new
  specialist setting belongs in the second table.
