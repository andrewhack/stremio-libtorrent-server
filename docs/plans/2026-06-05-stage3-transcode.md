# Stage 3 — Transcode / HLS (Implementation Plan)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development or superpowers:executing-plans. Steps use `- [ ]`.

**Goal:** Serve the Stremio `hlsv2` transcode API so clients that **can't direct-play** get a
compatible HLS stream — probe → per-stream copy-vs-transcode decision → fMP4 HLS, using
jellyfin-ffmpeg with **NVENC/VAAPI** (reusing the dual-GPU image + the Pascal 10-bit CPU-scale path).

**Architecture:** Unlike Stage 2 (verifiable as a plain process), Stage 3 needs jellyfin-ffmpeg +
GPU, so it runs **inside the `stremio-docker-dual` image**. The server shells out to `/usr/bin/ffmpeg`.
Decision logic + ffmpeg-arg construction are **pure functions** (unit-testable anywhere); probe,
playlists, and segments are **image integration** (ffmpeg/GPU).

**Tech Stack:** Python/FastAPI, jellyfin-ffmpeg 4.4.1 (from base image), libtorrent, pytest.

**Prereqs verified:** base image `stremio-docker-dual:latest` present; ffmpeg has
`h264_nvenc/h264_vaapi/hevc_nvenc/hevc_vaapi`; `/hlsv2/probe` + `video0.m3u8` shapes captured
(`docs/protocol-map.md`).

---

## Captured contracts (ground truth)

**`GET /hlsv2/probe?mediaURL=…`** →
```jsonc
{"format":{"name":"matroska,webm","duration":0.2},
 "streams":[{"id":0,"index":0,"track":"video","codec":"hevc","streamBitRate":0,"streamMaxBitRate":0,
             "startTime":0,"startTimeTs":0,"timescale":1000,"width":1920,"height":960,"frameRate":25,
             "numberOfFrames":null,"isHdr":false,"isDoVi":false,"hasBFrames":true,
             "formatBitRate":0,"formatMaxBitRate":0,"bps":0,"numberOfBytes":0,"formatDuration":0.2}],
 "samples":{}}
```
**`GET /hlsv2/:id/video0.m3u8?mediaURL=…&profile=…&maxWidth=…`** → fMP4 v7 VOD playlist:
`#EXTM3U / #EXT-X-VERSION:7 / #EXT-X-TARGETDURATION / #EXT-X-MEDIA-SEQUENCE / #EXT-X-PLAYLIST-TYPE:VOD /
#EXT-X-MAP:URI="video0/init.mp4?mediaURL=…&profile=…&maxWidth=…" / #EXTINF / video0/segmentN.m4s?…` (+ `#EXT-X-ENDLIST` for a full title).
Request params on hlsv2: `mediaURL`, `videoCodecs` (repeatable), `audioCodecs` (repeatable),
`maxAudioChannels`, `maxWidth`, `profile`.

---

## File structure

| File | Responsibility |
|---|---|
| `src/stremiosrv/transcode/probe.py` | run `ffprobe` → the probe JSON shape |
| `src/stremiosrv/transcode/fingerprint.py` | **pure**: (probe + client caps) → per-stream copy/transcode decision |
| `src/stremiosrv/transcode/ffmpeg_cmd.py` | **pure**: decision → ffmpeg argv (copy / nvenc / vaapi; fMP4) |
| `src/stremiosrv/transcode/converter.py` | manage an ffmpeg process per `id`; fMP4 segment output; idle teardown |
| `src/stremiosrv/transcode/profiler.py` | HW autodetect (nvidia-smi → nvenc-linux else vaapi); `/hwaccel-profiler` |
| `src/stremiosrv/api/hls.py` | routers: `/hlsv2/probe`, `/hlsv2/:id/:track.m3u8`, `init.mp4`, `segmentN.m4s`, `/destroy`, `/hwaccel-profiler` |
| `Dockerfile` | `FROM stremio-docker-dual:latest` + python + libtorrent + our server (CMD build_app) |
| `tests/test_fingerprint.py`, `test_ffmpeg_cmd.py` | unit (pure) |
| `tests/test_hls_int.py` | integration (in image; forced HEVC→H264 via NVENC) |

---

## Task 0: Capture remaining hlsv2 fixtures (parity)

We have `probe` + `video0.m3u8`. Capture `master.m3u8`, `audio0.m3u8`, and a binary `init.mp4`/`.m4s`
during a **live HEVC play** against the stock server (full URLs incl. query), sanitize, commit.
- [ ] Play an HEVC title; from logs grab the full `/hlsv2/.../master.m3u8?…`, `audio0.m3u8?…` URLs.
- [ ] `docker exec <prod> curl -s "http://127.0.0.1:11470<url>"` → save `hls-master.m3u8`, `hls-audio0.m3u8`.
- [ ] Commit to `tests/fixtures/` (content-neutral sample only; no titles/infohashes).

## Task 1: Capability fingerprint (PURE — unit-testable anywhere)

**Files:** `src/stremiosrv/transcode/fingerprint.py`, `tests/test_fingerprint.py`

- [ ] **Step 1: failing test**
```python
# tests/test_fingerprint.py
from stremiosrv.transcode.fingerprint import decide

PROBE = {"streams":[
  {"track":"video","codec":"hevc","width":3840,"height":2160},
  {"track":"audio","codec":"eac3","channels":6},
]}

def test_video_copy_when_supported_and_within_width():
    d = decide(PROBE, video_codecs=["h264","hevc"], audio_codecs=["aac"], max_audio_channels=2, max_width=3840)
    assert d["video"]["action"] == "copy"
    assert d["audio"]["action"] == "transcode"  # eac3 not in [aac] -> AAC

def test_video_transcode_when_codec_unsupported():
    d = decide(PROBE, video_codecs=["h264"], audio_codecs=["aac","eac3"], max_audio_channels=6, max_width=3840)
    assert d["video"]["action"] == "transcode"
    assert d["audio"]["action"] == "copy"

def test_video_transcode_when_over_maxwidth():
    d = decide(PROBE, video_codecs=["hevc"], audio_codecs=["aac"], max_audio_channels=2, max_width=1920)
    assert d["video"]["action"] == "transcode"  # 3840 > 1920 -> downscale
    assert d["video"]["scale_width"] == 1920
```

- [ ] **Step 2: run → fail**
- [ ] **Step 3: implement**
```python
# src/stremiosrv/transcode/fingerprint.py
def decide(probe: dict, video_codecs, audio_codecs, max_audio_channels, max_width) -> dict:
    v = next((s for s in probe["streams"] if s["track"] == "video"), None)
    a = next((s for s in probe["streams"] if s["track"] == "audio"), None)
    out: dict = {}
    if v:
        over_width = v.get("width", 0) > max_width
        unsupported = v["codec"] not in video_codecs
        if unsupported or over_width:
            out["video"] = {"action": "transcode",
                            "scale_width": min(v.get("width", max_width), max_width)}
        else:
            out["video"] = {"action": "copy"}
    if a:
        bad_codec = a["codec"] not in audio_codecs
        too_many = a.get("channels", 2) > max_audio_channels
        out["audio"] = {"action": "transcode" if (bad_codec or too_many) else "copy"}
    return out
```
- [ ] **Step 4: pass** — `uv run pytest tests/test_fingerprint.py -v`
- [ ] **Step 5: commit**

## Task 2: ffmpeg argv builder (PURE — unit-testable anywhere)

**Files:** `src/stremiosrv/transcode/ffmpeg_cmd.py`, `tests/test_ffmpeg_cmd.py`

Mirrors the stock commands we captured (audio: `-c:a aac -ac 2 -ab 384000`; video copy:
`-c:v copy -force_key_frames:v source`; transcode video uses the active profile). For Pascal NVENC
10-bit input, use the CPU-scale path (no `-hwaccel_output_format cuda`) per the fork's `NVIDIA-GPU.md`.

- [ ] **Step 1: failing test** — assert argv contains the right `-c:v`/`-c:a` for a decision+profile:
```python
# tests/test_ffmpeg_cmd.py
from stremiosrv.transcode.ffmpeg_cmd import build_video_cmd, build_audio_cmd

def test_video_copy():
    cmd = build_video_cmd(media_url="http://x/0", decision={"action":"copy"}, profile="nvenc-linux")
    assert "-c:v" in cmd and "copy" in cmd

def test_video_nvenc_transcode_downscale():
    cmd = build_video_cmd(media_url="http://x/0",
                          decision={"action":"transcode","scale_width":1920}, profile="nvenc-linux")
    assert "h264_nvenc" in cmd
    assert any("scale" in p for p in cmd)  # CPU scale to 1920

def test_audio_to_aac_stereo():
    cmd = build_audio_cmd(media_url="http://x/0", decision={"action":"transcode"})
    assert "aac" in cmd and "-ac" in cmd
```
- [ ] **Step 2: run → fail**
- [ ] **Step 3: implement** the builders (return `list[str]` argv; `ffmpeg -i <media_url> … -f mp4 pipe:1`).
- [ ] **Step 4: pass** — `uv run pytest tests/test_ffmpeg_cmd.py -v`
- [ ] **Step 5: commit**

## Task 3: Stage 3 Docker image

**Files:** `Dockerfile`

- [ ] **Step 1: write `Dockerfile`** — `FROM stremio-docker-dual:latest`; install `python3 python3-pip`
      (or copy a uv venv); `COPY src`, install `fastapi uvicorn pydantic-settings libtorrent`;
      `CMD ["python3","-m","uvicorn","stremiosrv.app:build_app","--factory","--host","0.0.0.0","--port","11470"]`.
- [ ] **Step 2: build** — `docker build -t stremio-libtorrent-server:dev .`
- [ ] **Step 3: smoke** — `docker run --rm --gpus all --device /dev/dri -p 18470:11470 -d --name s3 …`;
      `curl :18470/health` → healthy; `docker exec s3 ffmpeg -hide_banner -encoders | grep nvenc`.
- [ ] **Step 4: commit** the Dockerfile.

## Task 4: probe endpoint (integration)

**Files:** `src/stremiosrv/transcode/probe.py`, `src/stremiosrv/api/hls.py`, `tests/test_hls_int.py`

- [ ] Implement `GET /hlsv2/probe?mediaURL=…` → shell `ffprobe -of json …` → map to the captured
      probe shape (incl. `isHdr`/`isDoVi` from side_data / color metadata). Wire `hls.router`.
- [ ] **Integration test (in image):** probe the bundled `samples/hevc.mkv` → assert
      `streams[0].track=="video"`, `codec=="hevc"`, `width==1920`.

## Task 5: media playlists (master/video0/audio0)

- [ ] Implement `/hlsv2/:id/master.m3u8` (lists video0/audio0/subtitle0) and `/hlsv2/:id/:track.m3u8`
      (fMP4 v7 VOD per captured shape; segment URLs carry `mediaURL&profile&maxWidth`).
- [ ] Integration: request master+video0 for the sample → assert `#EXTM3U`, `#EXT-X-MAP`, segment lines.

## Task 6: segment generation (init.mp4 + segmentN.m4s) — the ffmpeg run

**Files:** `src/stremiosrv/transcode/converter.py`

- [ ] Implement the converter: launch ffmpeg (argv from Task 2) producing fMP4; serve `init.mp4`
      and `segmentN.m4s`; manage one process per `id`; `/hlsv2/:id/destroy`; idle teardown.
- [ ] **Integration (GPU):** force a HEVC→H264 transcode of the sample via `nvenc-linux`; assert a
      valid `.m4s` is produced **and** `nvidia-smi --query-gpu=utilization.encoder` > 0 during it.

## Task 7: hwaccel-profiler + autodetect

- [ ] `profiler.py`: detect HW at boot (`nvidia-smi -L` → `nvenc-linux`, else `vaapi`); `/hwaccel-profiler`.
- [ ] Set the active `transcode_profile` in settings from the autodetect.

## Task 8: acceptance (in image, GPU)
- [ ] Unit: `test_fingerprint`, `test_ffmpeg_cmd` green; `ruff` clean.
- [ ] In the running image: probe + master + video0 + a real `.m4s` for the sample.
- [ ] **Forced HEVC→H264 via NVENC** produces a playable segment with GPU encoder util > 0.
- [ ] (Acceptance) a non-HEVC client (e.g. Firefox) plays an HEVC title through the running server.

---

## Self-review
- **Spec coverage:** covers the spec's Stage 3 (hlsv2/probe/profiler + capability fingerprint + NVENC/VAAPI).
- **Verifiable-anywhere first:** Tasks 1–2 (fingerprint, ffmpeg argv) are pure → unit-tested without GPU;
  Tasks 3–8 are image/GPU integration (the only place jellyfin-ffmpeg + NVENC exist).
- **Placeholders:** pure tasks have full code; integration tasks describe exact commands/assertions
  (the ffmpeg-output streaming + ffprobe→shape mapping are wired live against the tools, flagged).
- **Inputs:** the bundled `samples/hevc.mkv` (content-neutral) drives integration — no external content needed.
