"""Adaptive piece-picking vs current — buffer-dynamics simulation.

ILLUSTRATIVE MODEL (not libtorrent). Captures the core streaming tradeoff:

  * A streaming picker balances *order discipline* d in [0,1]:
      d=1  strict-sequential  -> every byte lands in-order at the playhead frontier,
                                 but few pieces are in flight -> low parallelism -> low throughput.
      d=0  rarest-first       -> max parallelism -> full line rate, but almost nothing lands
                                 contiguously at the playhead -> the frontier barely advances.
  * total throughput  = T * parallelism(d),  parallelism(d) = 1 - 0.7*d   (strict seq ~ 0.3*T)
  * frontier advance  = total_throughput * d / B   (video-seconds/sec added ahead of the playhead)
    -> note this is maximised near d~0.7, NOT at d=1: relaxing discipline a bit *helps* both
       throughput AND frontier fill. That headroom is what adaptive exploits.

Pickers:
  NARROW  (approx. today): d=1 always            -> safe, but throughput-capped (~0.3*T)
  WIDE                     d=0.2 always           -> max throughput, weak continuity/seek
  ADAPTIVE                 d = f(buffer)          -> d->1 when buffer shallow / post-seek (protect
                                                     playback), d->0.2 when buffer deep (harvest speed)

Playback drains 1 video-sec/sec. A stall = buffer hits 0 while playing; it resumes after RESUME
seconds are rebuffered. Seek empties the buffer (worst case: uncached region).
"""
import random

DT = 1.0
B = 3.5          # video bitrate MB/s (~28 Mbit, 4K-ish)
LOW, HIGH = 15.0, 45.0    # adaptive buffer thresholds (s)
RESUME = 5.0     # seconds that must be buffered to (re)start after a stall/seek
WMAX_BUF = 240.0 # cap tracked buffer (readahead ceiling)


def parallelism(d):
    return 1.0 - 0.7 * d          # d=1 -> 0.30, d=0.2 -> 0.86, d=0 -> 1.0


def discipline(strategy, buf):
    if strategy == "narrow":
        return 1.0
    if strategy == "wide":
        return 0.2
    # adaptive: protect the playhead when shallow, harvest throughput when deep
    if buf <= LOW:
        return 1.0
    if buf >= HIGH:
        return 0.2
    return 1.0 - 0.8 * (buf - LOW) / (HIGH - LOW)


def net_series(kind, n, seed):
    rng = random.Random(seed)
    out, x = [], None
    for i in range(n):
        if kind == "fast":
            base = 30.0
            x = base if x is None else 0.85 * x + 0.15 * rng.uniform(18, 34)
            if rng.random() < 0.05:  # occasional deep dip
                x = rng.uniform(6, 12)
        else:  # constrained (near bitrate)
            base = 6.0
            x = base if x is None else 0.85 * x + 0.15 * rng.uniform(4.5, 7.5)
            if rng.random() < 0.05:
                x = rng.uniform(2.0, 3.2)
        out.append(max(1.0, x))
    return out


def simulate(strategy, T, events):
    """events: dict t-> ('seek'|'pause'|'resume'). Returns metrics."""
    n = len(T)
    buf = 0.0            # contiguous video-seconds available ahead of playhead
    playing = False      # becomes True once RESUME buffered (initial buffering)
    paused = False
    stalls, stall_s, thru_sum, buf_sum = 0, 0.0, 0.0, 0.0
    resume_lat, seek_count = [], 0
    since_stall = None

    for t in range(n):
        ev = events.get(t)
        if ev == "seek":
            buf = 0.0
            playing = False
            seek_count += 1
            since_stall = t
        elif ev == "pause":
            paused = True
        elif ev == "resume":
            paused = False

        d = discipline(strategy, buf)
        total_thru = T[t] * parallelism(d)          # MB/s actually pulled
        frontier = (total_thru * d) / B             # video-sec added ahead of playhead per sec
        thru_sum += total_thru

        draining = playing and not paused
        buf += frontier * DT - (DT if draining else 0.0)
        buf = min(buf, WMAX_BUF)

        if buf <= 0.0:
            buf = 0.0
            if draining:                            # ran dry mid-play -> stall
                stalls += 1
                playing = False
                since_stall = t
        if not playing and not paused and buf >= RESUME:
            playing = True                          # (re)start after buffering RESUME
            if since_stall is not None:
                resume_lat.append(t - since_stall)
                since_stall = None
        if not playing and not paused:
            stall_s += DT                           # time spent buffering (initial/stall/seek)
        buf_sum += buf

    return {
        "stalls": stalls,
        "stall_s": round(stall_s, 1),
        "thru": round(thru_sum / n, 1),
        "buf": round(buf_sum / n, 1),
        "seek_lat": round(sum(resume_lat) / len(resume_lat), 1) if resume_lat else 0.0,
    }


SCEN = {
    "S1 binge / fast net":        ("fast", {}),
    "S2 binge / constrained net": ("constrained", {}),
    "S3 channel-surf (seek/45s)": ("fast", {t: "seek" for t in range(45, 600, 45)}),
    "S4 pause+resume + dips":     ("fast", {120: "pause", 200: "resume"}),
}
N = 600

print(f"{'scenario':<30}{'picker':<10}{'stalls':>7}{'stall_s':>9}{'thru MB/s':>11}{'buf s':>8}{'seek lat s':>12}")
print("-" * 87)
for name, (kind, events) in SCEN.items():
    T = net_series(kind, N, seed=hash(name) & 0xFFFF)
    for strat in ("narrow", "adaptive", "wide"):
        m = simulate(strat, T, events)
        print(f"{name:<30}{strat:<10}{m['stalls']:>7}{m['stall_s']:>9}{m['thru']:>11}{m['buf']:>8}{m['seek_lat']:>12}")
    print()
