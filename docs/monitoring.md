# Dependency monitoring

Two external dependencies are watched so they don't break silently.

## 1. TLS cert (the `*.stremio.rocks` shared wildcard, or a BYO cert)
The trusted-HTTPS-for-TVs path relies on a cert that can lapse (esp. the shared
`*.519b6502d940.stremio.rocks` wildcard from Stremio's free cert service). The streaming server's
**`/health`** now reports a **`cert`** component:

- only present when a cert actually exists (dev/test stays `healthy`),
- `ok` while the cert has ≥ 14 days left, `degraded` (HTTP 503) once it's within 14 days or unreadable,
- `certDaysLeft` is included in the body for visibility.

The **Application Health Monitor** already polls `/health`, so a lapsing cert surfaces as a Telegram
alert with no extra wiring. If `*.stremio.rocks` ever dies, switch to the bring-your-own-cert path
(drop a full-chain+key PEM as `certificates.pem`, leave `IPADDRESS` unset).

## 2. Stremio protocol drift
The server reimplements Stremio's closed streaming-server API, so a new Stremio client could expect a
changed protocol. Two detectors (`scripts/`, stdlib only, exit non-zero on a problem → wire to cron):

- **`synthetic_playback.py`** — end-to-end "did it actually break": adds a CC torrent (Big Buck Bunny),
  HEADs the stream (expects `206` + `Content-Range` + `Content-Type`), and shape-checks `/settings` +
  `/stats.json`. Run against the live server.
  `SERVER=https://stremio.karadimov.info:12470 python3 scripts/synthetic_playback.py`
- **`check_stremio_releases.py`** — earliest warning: polls GitHub for new `Stremio/stremio-web`
  (and the fork) tags; alerts on a new release so we re-test the protocol before users hit it.

### Scheduling (cron — adjust to taste / move to Ansible)
```cron
# protocol smoke test, hourly
0 * * * * SERVER=https://stremio.karadimov.info:12470 python3 /path/scripts/synthetic_playback.py || /path/notify.sh "stremio playback FAILED"
# upstream release watch, daily
0 9 * * * python3 /path/scripts/check_stremio_releases.py || /path/notify.sh "new Stremio release — re-test protocol"
```
Replace `notify.sh` with your Telegram/AHM hook (the scripts only print + set exit code).
