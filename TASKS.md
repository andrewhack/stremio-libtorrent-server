# TASKS — stremio-libtorrent-server

Known open work. `README.md` describes what the server does today; this file is what it does not do
yet, and why each item is still open.

Convention: `- [ ]` open · `- [x]` done · `- [~]` in progress · `- [!]` blocked. Every entry states
what "done" looks like, so it can be picked up without context.

**Last updated:** 2026-08-13

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
