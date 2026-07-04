# Adaptive Piece-Picking — Design / Backlog Spec

**Status:** BACKLOG (future release). Captured 2026-07-04. Not scheduled.

**Goal:** close the sustained-throughput gap vs the stock (webtorrent) server **without** sacrificing
uninterruptable playback or fast seeks. A tracker cross-check ruled trackers out; the real deficit is
the piece-picker. Today's picker is strict playhead-first (sequential + deadlines on a fixed readahead
window), which — per the simulation below — harvests only ~⅓ of available line rate on a fast
connection (user measured **30 MB/s reachable but sustained "not good enough"**). Strict in-order fetch
limits how many pieces are in flight → low parallelism → low throughput.

**Core idea:** adapt the picker's **order-discipline** to **buffer health**. Feed the playhead in-order
when the buffer is shallow or just after a seek (protect continuity + seek latency); relax toward
broader, forward-biased parallel fetching when the buffer is deep (harvest throughput + build cache). A
control loop moves the "forward high-priority band" between two watermarks.

---

## Why — simulation (`2026-07-04-adaptive-piece-picking.sim.py`, illustrative model)

Model: order-discipline `d∈[0,1]` trades continuity vs parallelism — `throughput = T·(1−0.7d)`,
`frontier_advance = throughput·d/B`. Pickers: **narrow** `d=1` (≈today), **wide** `d=0.2`, **adaptive**
`d=f(buffer)`. Playback drains 1 s/s; seek empties the buffer.

| Scenario | picker | stalls | throughput | seek latency |
|---|---|---|---|---|
| Fast net, binge | narrow (today) | 0 | 6.3 | — |
| | **adaptive** | **0** | **17.4** | — |
| | wide | 2 ⚠ | 18.0 | — |
| Channel-surf (seek/45s) | narrow | 0 | 6.4 | 2.8s |
| | **adaptive** | 0 | 10.4 | **2.8s** |
| | wide | 0 | 18.5 | 5.0s ⚠ |
| Constrained (≈bitrate) | narrow / adaptive | 26 | 1.5 | — |
| | wide | 20 | 4.3 | (445s stalled) |

**Findings:** (1) reproduces the report — narrow leaves ~3× throughput on the table. (2) **adaptive =
WIDE's throughput + NARROW's zero-stall continuity + fast seeks** (2.8s vs wide's 5.0s, because a seek
snaps it back to full focus). (3) **WIDE alone is a trap** — best raw number but it stalls on a fast
net and doubles seek latency. (4) **Honest limit:** when the line can't sustain the bitrate, no picker
helps — adaptive **degrades to exactly narrow (never worse)**; the right answer is lower quality, and
the [playback-diagnostics](2026-07-04-playback-diagnostics-design.md) feature should say so.

Caveat: the parallelism↔discipline coupling is modeled linearly; real libtorrent parallelism saturates
differently and depends on swarm/peer quality. The sim justifies the **direction + control shape**, not
the constants — those are tuned on real swarms.

---

## Design

**Control variable `d`** is realized in libtorrent as the **width of the forward high-priority /
deadline band** ahead of the playhead — kept forward-biased (NOT rarest-first across the whole file) so
the contiguous frontier keeps advancing:
- narrow band → few pieces prioritized → strict/in-order → low parallelism, low throughput, tight latency.
- wide band → many pieces eligible ahead → high parallelism/throughput, deeper buffer.

**Control loop (every ~1–2 s):**
1. Measure `buffer_seconds` = contiguously-downloaded video ahead of the playhead (have-bitfield +
   playhead piece + bitrate).
2. `buffer ≤ LOW` **or just seeked** → collapse band to `W_min` (full focus, tight deadlines on the
   immediate pieces) — guarantees the next pieces + fast resume.
3. `buffer ≥ HIGH` → widen band to `W_max` — harvest throughput, prefetch/cache ahead.
4. Between → interpolate.
5. Always keep `boost_piece` (prio 7) on the immediate playhead pieces = the continuity floor.

**Watermarks (starting points — MUST be tuned on real swarms):** `LOW≈15 s`, `HIGH≈45 s`, `W_min≈8–16 s`
of pieces (low-latency focus), `W_max≈90–180 s` **bounded by a MiB ceiling** (respect `cache_size` /
the evictor).

**Seek:** buffer≈0 at the new position → immediately collapse to `W_min` there → fast resume (sim:
2.8s vs wide 5.0s). Backward/near seek into a cached region = instant. Reuses the existing `refocus()`.

**Pause:** no drain → buffer builds → picker naturally widens → deep buffer on resume.

**Cross-torrent:** adaptive widening applies to the **active** stream only; idle torrents stay capped by
`idle_download_rate_limit` (existing `note_stream_open/close` active detection).

---

## libtorrent levers (evolution of existing code, not a rewrite)
- **Forward band width:** `piece_priority()` over `[playhead .. playhead+W]` — today's `focus_file` +
  `readahead_bytes`.
- **Deadlines:** `set_piece_deadline()` tightest on the immediate pieces, looser/none beyond the band.
- **Continuity floor:** `boost_piece` (existing) on the immediate pieces.
- **Tick:** widen/narrow `W` each control interval; on seek, `refocus()` at the new playhead (existing).
- **Measurement:** have-bitfield + playhead → contiguous frontier; `status().download_rate` → throughput.

---

## Metrics to instrument (also feed the diagnostics rule engine)
`buffer_seconds`, `contiguous_frontier`, `effective_throughput`, stall count/seconds, seek-resume
latency, current `d`/window width → add to `/stats.json`; shared with
[playback-diagnostics](2026-07-04-playback-diagnostics-design.md).

## Validation plan
- **Unit (TDD, no libtorrent):** the pure control-loop function `(buffer, rate, seeked) → W`.
- **Integration on the stremio box (bug-repro host, real swarms):** measure throughput / stalls / seek
  latency vs current across the sim's scenarios; tune `LOW/HIGH/W_min/W_max` to the hardware/line.
- **Release guardrail:** adaptive must **never be worse than today** on stalls or seek latency (the sim
  shows worst-case = narrow) — gate the release on that.

## Risks / open questions
- Real gains depend on swarm quality — validate before quoting numbers.
- **Bitrate estimate** (bytes↔seconds for watermarks): `ffprobe` on the played file (in-image) vs a live
  piece-rate estimate.
- **Disk/RAM:** a wide window raises outstanding requests + cache pressure — bound `W_max` by a MiB
  ceiling and don't prefetch past what the evictor will keep (`cache_size`).

## Phasing
- **P1:** instrument the buffer/throughput metrics (also needed by diagnostics) + the pure control-loop
  function + tests.
- **P2:** wire into the engine behind an env flag (`STREMIOSRV_ADAPTIVE_PICKING`, default **off**) for
  A/B on real swarms.
- **P3:** tune constants, verify the never-worse guardrail, enable by default.
