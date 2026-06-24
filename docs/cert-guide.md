# TLS Cert Guide (all-in-one)

The all-in-one image serves the web player + API over **HTTP :8080** (no cert) and **HTTPS :12470**
(needs a cert). This is, in most cases, a **LAN solution** — pick the simplest option that works for
your clients. Ordered easiest → most robust:

## 1. Self-signed (default, zero config)
If no cert is mounted, the container **auto-generates a self-signed cert** on first boot, so HTTPS
works immediately.
- **Browser web player:** load `https://<host>:12470`, accept the one-time "not secure" warning → it
  works (same-origin after that).
- **Native TV/desktop apps:** often **reject** self-signed certs (strict TLS). For those, use a real
  cert (options 2–3) **or** just use the HTTP/LAN path below.
- Override the CN with `-e DOMAIN=<host-or-ip>`.

## 2. Bring-your-own cert (best — works for all clients)
Put a **full-chain + private key** PEM at `<data-dir>/certificates.pem` (the cache dir mounted at
`/root/.stremio-server`). The container uses it as-is; no warnings, native apps accept it.
```sh
cat fullchain.pem privkey.pem > /your/data-dir/certificates.pem
```

## 3. Let's Encrypt (trusted, but has a catch)
- **HTTP-01** (the common method) needs **port 80 reachable from the internet** — frequently
  unavailable on home connections (ISP block / CGNAT / no forward). If you *can* expose 80, issue with
  certbot/acme.sh and drop the result at `<data-dir>/certificates.pem` (option 2).
- **DNS-01** avoids port 80 but needs your **DNS provider's API token**. Use this if 80 is blocked.

## Simplest path for a regular LAN user: skip TLS
Point your client at the **HTTP** origin — no cert at all:
```
http://<host>:8080        # web player + API
```
Browsers won't complain (it's http), and native LAN apps that allow a custom http server URL work
directly. Use HTTPS only when you need remote access.

## Client ↔ cert matrix
| Client | Self-signed (:12470) | Real cert (:12470) | HTTP (:8080, LAN) |
|---|---|---|---|
| Browser web player | ✅ (one-time warning) | ✅ | ✅ |
| Native TV / desktop app | ⚠️ often rejected | ✅ | ✅ (if it allows custom http URL) |

**`SERVER_URL` must match the origin clients use** — e.g. `SERVER_URL=https://host:12470` for HTTPS,
or `http://host:8080` for LAN HTTP. It seeds the web player's streaming-server target.
