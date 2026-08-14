# TASKS — stremio-libtorrent-server

Known open work. `README.md` describes what the server does today; this file is what it does not do
yet, and why each item is still open.

Convention: `- [ ]` open · `- [x]` done · `- [~]` in progress · `- [!]` blocked. Every entry states
what "done" looks like, so it can be picked up without context.

**Last updated:** 2026-08-13

---

## Protocol

- [ ] **`HEAD` is not accepted on the `hlsv2` routes.** `GET /hlsv2/probe` answers 200 and `HEAD`
  answers 405, while the byte-range route handles both (`HEAD` → 206). The reference server accepts
  `HEAD` across its surface, so this is a fidelity gap even though no client is currently known to
  rely on it — a client that probes with `HEAD` before playing would see a hard failure.
  *Done =* the `hlsv2` routes answer `HEAD` with the same headers as `GET` and an empty body, plus a
  conformance check so it cannot regress.

- [ ] **`/subtitleSignature` always answers `{"signature": null}`.** `stremio-video` 0.0.93+ calls
  it once per load whose probe does not rule out an embedded subtitle track, but the reference
  implementation has no such route and nothing upstream consumes the value yet, so there is no
  algorithm to match. Returning an invented string would be worse than returning nothing: the client
  accepts any string and would use it the moment a consumer ships.
  *Done =* upstream defines the signature and the server computes the same value. Until then,
  `playback.subtitleSignatureAsks` in `/stats.json` counts how often real clients ask, which is the
  evidence for whether this is worth reverse-engineering.

## Tooling & docs

- [ ] **Ruff 0.16 migration.** The project pins `ruff==0.15.15` and is clean against it; `0.16.x`
  reports 73 findings under its newer default rules. Nothing is wrong with the code — the defaults
  moved. Worth doing deliberately rather than discovering mid-release.
  *Done =* the pin is raised, the findings are fixed or explicitly ignored in `pyproject.toml`, and
  `ruff check` is clean at the new version.

- [ ] **Stale TODO heading in `docs/protocol-map.md`.** "Still TODO in Stage 0" asks for response-body
  captures that already exist in the "Captured shapes" section directly beneath it, and which have
  since become the conformance fixtures. The heading outlived the work.
  *Done =* the heading is removed or rewritten to describe what is genuinely still unmapped.

- [ ] **The Docker Hub overview is close to its length limit.** The page is capped at 25,000 bytes,
  and the README had to have its TLS appendix moved behind `<!--hub:skip-->` markers to fit. About
  2 KB of headroom remains. The publish step checks the size and fails before uploading, so this
  cannot half-publish — but it will stop a release when it trips.
  *Done =* either the README is restructured so the Hub copy has lasting headroom, or the overview
  is reduced to a short summary that links to the full README.
