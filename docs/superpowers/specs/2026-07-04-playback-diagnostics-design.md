# Playback Diagnostics — Design / Backlog Spec

**Status:** BACKLOG (future release). Captured 2026-07-04. Not scheduled.

**Goal:** Tell the user, in understandable terms, *why* a torrent isn't playing / is buffering /
is slow — and what to do about it. A ranked list of findings, each with a plain-language
explanation, the evidence it's based on, and a concrete fix/mitigation.

**Tiering (load-bearing):**
- **Free image → deterministic rule engine.** Pure rules over metrics the server already has. On-box,
  offline-safe, content-neutral, **no LLM**. This is the substrate.
- **Supported (paid) image → LLM/chatbot layer** on top of the same structured findings: conversational
  "why won't it play?", follow-ups, guided mitigation. Gated by the **same license/entitlement that
  gates auto-update**, reusing the planned support chatbot (see [[project_stremio_site]] /
  [[project_partner_support]] — LangChain + Ollama + MCP). The diagnostic output becomes an MCP
  tool/resource the chatbot reasons over.

  **Deployment (decided 2026-07-04):** the **chatbot app + MCP servers run in ITCOM infra** (hosted —
  NOT on the appliance box, which only exposes `/diagnose.json`). **LLM inference = Ollama Cloud (Pro
  account)** as primary, with the **own Ollama infra** (local GPU models) also available. The appliance
  stays light; the hosted chatbot pulls the box's diagnostics within the user's authenticated session.

  **Failure isolation (HARD REQUIREMENT):** the LLM must NEVER be able to block the deterministic
  layer. The rule engine (`/diagnose.json` + config-web panel) is the source of truth and has **zero
  dependency** on the LLM/MCP/chatbot/Ollama or any network. If Ollama Cloud is down, the entitlement
  check can't be reached, the MCP is unreachable, or inference times out, the box still returns full
  findings + fixes; even on the paid tier the chatbot **degrades to rendering the raw structured
  findings** (never an error, never a spinner-of-death). The LLM is a presentation/conversation layer
  on top of data that already stands on its own — same pattern as the offline-cert and
  tracker-source fallbacks elsewhere in the project.

This generalizes the existing config-web "Suggestions" advisor, refocused on playback health.

---

## Signal inventory

**Already collected (no new plumbing):**
- `GET /stats.json` (top-level): `cache{cacheUsed, cacheSize, diskFree, diskTotal}`,
  `playback{stalls, stallSeconds, timeouts}` (the engine records a stall when `wait_and_read` waits on
  a not-yet-downloaded piece, a timeout when a piece never arrives).
- `GET /{infohash}/stats.json`: `peers`, `swarmSize`, download/upload speed, `progress`,
  `streamProgress`, `streamName`.
- `GET /netcheck.json`: `listenPort`, `peers`, `inboundPeers`, `portMap` (UPnP/NAT-PMP).
- `GET /active.json`: active vs paused torrents + speeds (cross-torrent contention).
- Running config: rate limits, cache size, `idle_download_rate_limit`, `max_streams`, seed policy.

**New taps into libtorrent (small; the engine already runs an alerts loop + holds handles):**
- `torrent_status`: `num_seeds` vs `num_peers`, `connect_candidates`, `num_complete/num_incomplete`
  (tracker scrape = how many seeds exist), `distributed_copies` (availability), `errc`.
- `piece_availability()` — piece rarity, mapped to the playhead region (engine already computes the
  played file / focus / boost, so the playhead→piece mapping exists).
- Alerts to capture: `performance_alert` (libtorrent literally names "download rate limit too low",
  "disk buffer limit reached", "send buffer watermark too low", etc.), `tracker_error_alert`,
  `listen_failed_alert`, `portmap_error_alert`.
- `peer_info` per peer: `flags` (choked / snubbed / interested), `source` (tracker/dht/pex/lsd).
- **Bitrate** (Phase 2): `ffprobe` (already in the image) on the played file → compare required bitrate
  vs sustained throughput = the single most meaningful buffering answer.

---

## Diagnosis catalog (symptom → distinguishing signal → explanation + fix)

| Symptom | Signal | Explanation → fix |
|---|---|---|
| Won't start, 0% | `has_metadata=false` | "Fetching the file list from peers." → wait / no peer has metadata |
| Won't start, 0% | `swarmSize=0` / scrape `num_complete=0` | "No seeders online." → pick another source/release |
| Slow despite many peers | `download_rate ≈ STREMIOSRV_DOWNLOAD_RATE_LIMIT` | "Capped at ~X kB/s by your rate limit (likely a stale auto-detect)." → raise/clear it |
| Slow, few sending | high `num_peers`, low unchoked, low `upload` | "Cold start — little to trade yet; should climb." → wait / raise upload limit |
| Slow, no inbound | `inboundPeers=0`, `portMap` failed | "Peers can't reach you (no forward / CGNAT) — running outbound-only." → forward BT port / VPN PF |
| Frequent buffering | `download_rate < file bitrate` (Phase 2) | "This file needs ~Y Mbps; your line sustains ~Z." → lower quality / wired / raise cap |
| Frequent buffering | rising `stalls`, low `piece_availability` at playhead | "Waiting on rare pieces at your position." → few seeds; expected on niche content |
| Buffering after a while | `cacheUsed≈cacheSize` + evicting active | "Cache too small — dropping pieces you still need." → raise cache size |
| One stream starves another | 2+ active + `idle_download_rate_limit` engaged | "Another download is competing." → it's throttled / reduce concurrent streams |
| Plays but garbled / browser-only | container/codec (mkv/HEVC) | "This device can't decode this file." → use TV/desktop app (transcode is a separate feature) |

Findings are **confidence-scored and ranked** — buffering is multi-causal, so the value is ranking the
most likely cause from the evidence, not asserting certainty. Uses a **rolling sampler** (like the
advisor) — a 1-frame dip isn't "buffering".

---

## Shape

- **`GET /{infohash}/diagnose.json`** (+ top-level `GET /diagnose.json` for the current stream):
  ranked `[{severity: info|warn|critical, title, plain, evidence:{…}, fix, confidence}]`. Pure rule
  engine → unit-testable, content-neutral (reports *your* stream's health, like a download manager).
- **config-web "Playback health / Why isn't it playing?" panel** (extends the Suggestions card).
- Optionally exposed to a Stremio addon/overlay so the *reason* shows during buffering instead of a
  blank spinner.
- **Paid:** the chatbot calls `/diagnose.json` as an MCP tool → conversational diagnosis + guided fixes.

---

## Phasing

- **Phase 1 (small, high value):** rule engine over the *existing* endpoints — rate-cap, no-seeds,
  no-inbound, cold-start, cache-thrash, contention, stall-rate. ~90% of real complaints, no libtorrent
  changes. Free tier ships here.
- **Phase 2:** capture `performance_alert` + `tracker_error` in the alerts loop; add
  bitrate-vs-throughput via `ffprobe`; piece-rarity-at-playhead.
- **Phase 3 (paid):** wire the findings into the support chatbot as an MCP tool (entitlement-gated).

---

## Open questions

1. ~~Where the LLM runs~~ **SETTLED (2026-07-04):** chatbot + MCP in ITCOM infra; LLM = Ollama Cloud
   (Pro) primary + own Ollama infra; box only exposes `/diagnose.json`. See Deployment above.
2. **Privacy / content-neutrality of the paid path — sharper now that Ollama Cloud is external.**
   `/diagnose.json` carries stream metadata (infohash, name). Ollama **Cloud** is a third party, so
   content-bearing fields must NOT go to it raw: send **metrics-only** (strip title/infohash) to the
   cloud model, and route any content-bearing reasoning only through the **own Ollama infra** (local),
   or keep it strictly inside the user's authenticated session. Per-customer scoping stays enforced in
   the MCP/tool layer, never by trusting the LLM (the partner-support rule).
3. **Free tier stays fully offline** — no phone-home; the rule engine and panel work with no network.
