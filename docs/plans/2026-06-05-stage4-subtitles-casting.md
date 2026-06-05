# Stage 4 — Subtitles · opensubHash · Casting (Implementation Plan)

**Goal:** Match the streaming-server's subtitle + opensub-hash + casting surface so subtitle addons
(OpenSubtitles etc.) and cast targets work against our server.

**Tech Stack:** Python/FastAPI, jellyfin-ffmpeg (embedded-sub extraction), pytest. Verifiable-anywhere
parts (opensubHash, srt→vtt) are pure; embedded-sub listing is image integration.

| File | Responsibility |
|---|---|
| `src/stremiosrv/subs/opensub.py` | **pure**: OpenSubtitles hash (filesize + 64-bit sums of head/tail 64 KiB) |
| `src/stremiosrv/subs/convert.py` | **pure**: SRT → WebVTT text conversion |
| `src/stremiosrv/api/subs.py` | routes: `/opensubHash`, `/:hash/:idx/subtitles.json`, `/:hash/:idx/subtitles.vtt` |
| `src/stremiosrv/api/casting.py` | `/casting` (DLNA renderer list; minimal/empty for parity) |
| `tests/test_opensub.py`, `test_convert.py` | unit (pure) |

## Task 1: OpenSubtitles hash (PURE — verifiable anywhere)
- [ ] `opensubtitles_hash(path) -> str` (16-char lowercase hex): `hash = (filesize + Σ int64le(first 64KiB)
      + Σ int64le(last 64KiB)) mod 2^64`. Unit test with a 128 KiB zero file → `"0000000000020000"`,
      and a file with a single leading `1` int64 → `+1`.
- [ ] `GET /opensubHash?videoUrl=…|mediaURL=…` → `{"result": "<hash>"}` (computes over the local/served file).

## Task 2: Subtitle serving + SRT→VTT (pure convert + integration listing)
- [ ] `srt_to_vtt(text) -> str` (pure): prepend `WEBVTT`, convert `,`→`.` in timestamps. Unit-tested.
- [ ] `GET /:hash/:idx/subtitles.json` → list embedded subtitle tracks (from ffprobe streams where
      `track=="subtitle"`), shape `{ "subtitles": [{id,lang,...}] }`.
- [ ] `GET /:hash/:idx/subtitles.vtt?track=N` → extract the embedded track via ffmpeg `-f webvtt` and serve.

## Task 3: Casting (parity stub)
- [ ] `GET /casting` → `[]` (no DLNA discovery yet) — present so clients don't 404; document as stub.

## Task 4: Acceptance
- [ ] Unit: opensubHash + srt→vtt green; ruff clean.
- [ ] In-image: `/opensubHash` over a generated file matches a reference computed independently;
      `/:hash/:idx/subtitles.json` lists an embedded track from a muxed test file.

## Self-review
- opensubHash + srt→vtt are pure → unit-tested without GPU/ffmpeg; embedded-sub listing/extraction is
  the only image-integration part. Casting is an explicit stub (logged, not silently empty).
