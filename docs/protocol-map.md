# Stremio Streaming-Server Protocol Map

Authoritative route surface that an unmodified Stremio client (TV apps, web player) calls on its
"streaming server". **Extracted directly from the reference bundle** `server.js` **v4.20.16**
(`docs/server-url.txt` → `https://dl.strem.io/server/v4.20.16/desktop/server.js`) via the route
registrations (`.get/.post/.use("/…")`). Line numbers are offsets in that minified bundle for
follow-up handler reading.

> Status legend per endpoint:
> - **route ✓** = registration confirmed in server.js (this pass).
> - **shape ⏳** = response body shape still to be captured as a live fixture (Stage 0 Task 0.3).

To reach **API parity**, each endpoint needs: method · path · params · response shape · status codes.
This document fixes **method + path** (done); **shape** is filled from captured fixtures.

---

## 1. Torrent playback (core — show-stoppers)

| Method | Path | server.js | Purpose | Notes |
|---|---|---|---|---|
| GET | `/:infoHash/:idx` and `/:infoHash/:idx/*` | 18420 | **Byte-range file stream** (direct play) | MUST honor HTTP Range (206, Content-Range, Accept-Ranges) + HEAD. Lazily creates the engine on first request. |
| GET | `/:infoHash/stats.json` | 18344 | per-torrent stats | downloaded/speed/peers/… (shape ⏳) |
| GET | `/:infoHash/:idx/stats.json` | 18346 | per-file stats | (shape ⏳) |
| GET | `/stats.json` | 18348 | global stats | `{}` when idle |
| GET | `/:infoHash/remove` | 18413 | drop a torrent engine | |
| GET | `/removeAll` | 18417 | drop all engines | |
| GET | `/favicon.ico` | 18342 | — | trivial |

> Note: torrent engines are created **lazily** by requesting `/:infoHash/:idx` (no mandatory POST
> create for torrents). `POST /create/:createKey` (96033) + `/stream/:key/:fileName` (96043/96053)
> are a **separate** local-file/url streaming flow, not the torrent path.

## 2. Transcode / HLS (hlsv2 sub-router — hardest parity surface)

`.use("/hlsv2", …)` (46592). Sub-routes (75764+):

| Method | Path (under `/hlsv2`) | server.js | Purpose |
|---|---|---|---|
| GET | `/:id/:track.m3u8` | 75764 | media playlist (video0/audio0/subtitle0) |
| GET | `/:id/:track/init.mp4` | 75779 | fMP4 init segment |
| GET | `/:id/:track/segment:sequenceNumber.:ext` | 75795 | media segments (.m4s/.ts/.vtt) |
| GET | `/:id/destroy` | 75869 | tear down converter |
| GET | `/:id/burn` | 75812 | (subtitle burn-in) |
| GET | `/probe` | 46637 | ffprobe a `mediaURL` |
| GET | `/hwaccel-profiler` | 46818 | supported HW accel profiles |

Query params observed on hlsv2 requests (from live logs): `mediaURL`, `videoCodecs` (repeatable),
`audioCodecs` (repeatable), `maxAudioChannels`, `maxWidth`. **master.m3u8** is requested too
(legacy route family below also exists).

### Legacy/alt HLS family (top-level `/:first/:second/…`, 46625+)
`master.m3u8` (46626), `hls.m3u8` (46625), `stream.m3u8` (46627), `stream-q-:quality.m3u8` (46628),
`stream-:stream.m3u8` (46629), `stream-q-:quality/:seg.ts` (46630), `stream-:stream/:seg.ts` (46631),
`mp4stream-q-:quality.m3u8` (46632), `mp4stream-q-:quality/:seg.mp4` (46633), `dlna` (46634),
`subs-:lang.m3u8` (46635), `thumb.jpg` (46636). Also `/:infoHash/:videoId/:playlist/:HLSSegment?` (46604).

## 3. Settings / info / control (handshake)

| Method | Path | server.js | Purpose |
|---|---|---|---|
| GET | `/settings` | 46772 | read server settings (transcode*, bt*, cache…) (shape ⏳) |
| POST | `/settings` | 46782 | write partial settings |
| GET | `/network-info` | 46741 | network/IP info (shape ⏳) |
| GET | `/device-info` | 46755 | device info (shape ⏳) |
| GET | `/get-https` | 46801 | HTTPS/cert provisioning flow |
| GET | `/status` | 75852 | status |
| GET | `/heartbeat` | 46790 | keepalive |
| use | `/casting/` | 46691 | casting sub-router (SSDP/DLNA) |
| use | `/proxy` | 46837 | **proxy external streams** (non-torrent / debrid HTTP) |
| use | `/local-addon` | 46798 | local addon sub-router |

## 4. Subtitles

| Method | Path | server.js | Purpose |
|---|---|---|---|
| GET | `/subtitles.:ext` | 46721 | convert/serve subtitles (e.g. .vtt) |
| GET | `/subtitlesTracks` | 46693 | list subtitle tracks |
| GET | `/opensubHash` | 46706 | **OpenSubtitles hash** — must match algorithm exactly |
| GET | `/tracks/:url` | 46644 | track extraction |

## 5. Built-in addon (manifest) — 91xxx

| Method | Path | server.js | Purpose |
|---|---|---|---|
| GET | `/manifest.json` | 91812 | addon manifest |
| GET | `/:resource/:type/:id/:extra?.json` | 91814 | addon resource (catalog/meta/stream) |

> Likely the server's bundled local addon. Confirm whether clients depend on it or only on
> external addons (Torrentio etc.). If only external → this is **out of scope** (parity not required).

## 6. Archive streaming (niche)

`.use("/rar" /zip /tar /tgz /7zip /ftp /nzb)` (46918–46928) — stream media out of archive/container
torrents. Low priority; many torrents aren't archived. Defer unless fixtures show client reliance.

## 7. YouTube / casting/convert (likely out of scope)

`/yt/:id` (46681), `/yt/:id.json` (46672), casting module `/:devID` (83821), `/convert:ext?`,
`/transcode:ext?` (83821). Not needed for torrent streaming to TVs; defer/skip.

---

## Parity priority (what to implement, in order)

1. **§1 file serving + stats + lazy create** (show-stopper; Stage 2).
2. **§3 settings/network-info/device-info/status/heartbeat** (handshake; Stage 1/2).
3. **§2 hlsv2 transcode + probe + hwaccel-profiler** (Stage 3).
4. **§3 `/proxy`** for non-torrent/debrid streams (Stage 3 — parity for direct/debrid addons).
5. **§4 subtitles + opensubHash** (Stage 4).
6. **§5 built-in addon, §6 archives, §7 yt/cast** — confirm need; likely skip.

## Still TODO in Stage 0 (needs the running server — Task 0.3)
Capture **response bodies** (shape ⏳) for: `/settings`, `/network-info`, `/device-info`,
`/:hash/:idx/stats.json`, `/hwaccel-profiler`, `/hlsv2/probe`, a `master.m3u8` + `video0.m3u8`,
and the Range response headers of `/:hash/:idx`. These become the conformance fixtures.

---

## Captured shapes (Task 0.3 — first pass)

Fixtures in `tests/fixtures/`. **Confirmed:**

- `GET /settings` → `{"options":[…UI descriptors…], "values":{…}, "baseUrl":"http://<ip>:11470"}`.
  `values` keys include: `serverVersion, appPath, cacheRoot, cacheSize, btMaxConnections,
  btHandshakeTimeout, btRequestTimeout, btDownloadSpeedSoftLimit, btDownloadSpeedHardLimit,
  btMinPeersForStable, remoteHttps, localAddonEnabled, transcodeHorsepower, transcodeMaxBitRate,
  transcodeConcurrency, transcodeTrackConcurrency, transcodeHardwareAccel, transcodeProfile,
  allTranscodeProfiles, transcodeMaxWidth, proxyStreamsEnabled, btProfile`.
  **Our `/settings` must return all three keys (`options`/`values`/`baseUrl`), not a flat dict.**
- `GET /network-info` → `{"availableInterfaces":["<ip>", …]}`.
- `GET /device-info` → `{"availableHardwareAccelerations":["nvenc-linux","vaapi-renderD128", …]}`.
- `GET /stats.json` (idle) → `{}`.
- `GET /casting/` → `[]` (array).
- `GET /opensubHash?videoUrl=…` → `{"error":null,"result":{"size":<bytes>,"hash":"<16hex>"}}`.
- `GET /:hash/:idx` (Range) → `206 Partial Content` with `Accept-Ranges: bytes`,
  `Content-Range: bytes A-B/TOTAL`, `Content-Type: <per-file>`, plus DLNA headers
  (`transferMode.dlna.org: Streaming`, `contentFeatures.dlna.org: …`), `Connection: keep-alive`.

**Corrections to the route table:**
- `/status` is **404 at top level** (`Cannot GET /status`) — the line-75852 route lives in a
  sub-router, not the root. Do **not** implement top-level `/status`.

**CONFIRMED — `GET /:infoHash/stats.json` (active engine) schema** (Stage 2 contract; fixture must be
SANITIZED — placeholder infoHash/name, redacted peer IPs):
```jsonc
{
  "infoHash": "<40hex>", "name": "<str>",
  "peers": 0, "unchoked": 0, "queued": 0, "unique": 0,
  "connectionTries": 0, "swarmPaused": false, "swarmConnections": 0, "swarmSize": 0,
  "selections": [],
  "wires": [ { "requests": 0, "address": "<ip:port>", "amInterested": false,
               "isSeeder": false, "downSpeed": 0, "upSpeed": 0 } ],
  "files": [ { "path": "<str>", "name": "<str>", "length": 0, "offset": 0, "__cacheEvents": true } ],
  "downloaded": 0, "uploaded": 0, "downloadSpeed": 0, "uploadSpeed": 0,
  "sources": [ { "numFound": 0, "numFoundUniq": 0, "numRequests": 0,
                 "url": "tracker:udp://…/announce" | "dht:<hash>", "lastStarted": "<iso8601>" } ],
  "peerSearchRunning": true,
  "opts": { "peerSearch": { "min": 40, "max": 150, "sources": ["tracker:…","dht:…"] },
            "dht": false, "tracker": false, "connections": 200,
            "handshakeTimeout": 5000, "timeout": 2000, "virtual": true,
            "swarmCap": { "minPeers": 20, "maxSpeed": 12582912 },
            "growler": { "flood": 0, "pulse": 52428800 },
            "path": "<cache-path>", "id": "-AZ5340-<rand>", "flood": 0, "pulse": <int> }
}
```
> Confirms the engine design: `opts.dht=false`/`tracker=false` (built-ins off) while `peerSearch.sources`
> drives DHT+tracker discovery. Our libtorrent impl must populate `peers/unchoked/swarmConnections/
> downloaded/downloadSpeed`, `files[]` (path/name/length/offset), and `wires[]` (per-peer).
> Peer-ID masquerades as Azureus (`-AZ5340-`).
>
> **`GET /:infoHash/:idx/stats.json`** = same object **plus** the per-file fields
> `streamProgress` (0..1), `streamName` (the selected file's name), `streamLen` (bytes).

**CONFIRMED — `GET /hlsv2/:id/:track.m3u8` media playlist** (Stage 3 contract; fMP4/CMAF):
```m3u8
#EXTM3U
#EXT-X-VERSION:7
#EXT-X-TARGETDURATION:1
#EXT-X-MEDIA-SEQUENCE:1
#EXT-X-PLAYLIST-TYPE:VOD
#EXT-X-MAP:URI="video0/init.mp4?mediaURL=<urlenc>&profile=<prof>&maxWidth=<n>"
#EXTINF:0.200033,
video0/segment1.m4s?mediaURL=<urlenc>&profile=<prof>&maxWidth=<n>
```
Key facts for parity:
- **fMP4 segments** (`init.mp4` via `#EXT-X-MAP` + `segmentN.m4s`), HLS **v7**, `VOD`.
- Every init/segment URL **carries the query** (`mediaURL`, `profile` e.g. `vaapi-renderD128`,
  `maxWidth`) — the server keys the converter off these, so they must round-trip on sub-requests.
- Captured against the bundled `samples/hevc.mkv` (the hwaccel test clip → short, one segment);
  a real title produces many `#EXTINF`/segment lines + `#EXT-X-ENDLIST`. Content-neutral, no scrub.

**Still ⏳ (nice-to-have, not blocking):**
- `master.m3u8` + `audio0.m3u8` bodies (same family), a real `.m4s`/`init.mp4` (binary), `/hlsv2/probe`
  success body. Capture before Stage 3.
- `/hwaccel-profiler` success body (informational — our server controls HW detection).
