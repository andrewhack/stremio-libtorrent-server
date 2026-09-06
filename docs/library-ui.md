# Library download UI

An **opt-in**, authenticated page at `/library` that turns the server into a real torrent client for
your own content: pick a title, download it in full, keep it, and manage what is on disk *as titles*
rather than as torrent folder names.

It is off unless you turn it on:

```sh
docker run ... -e STREMIOSRV_LIBRARY_UI=true ...
```

Then open `https://<your-server>:12470/library/`.

## Why it lives on the player's origin

The container already serves the Stremio web player and the streaming API on one origin. The library
page is a **path** on that same origin, not a new port — so there is nothing extra to forward, and
nothing extra to get a certificate for.

It also means the page can read the session the web player already stored in this origin's
`localStorage`. On a device where you are signed in to the player, the library page signs you in
with no password at all.

## Signing in

Two paths, in this order:

1. **The player's own session.** The page reads the authKey the web player stored on this origin and
   exchanges it for a session cookie. Nothing is typed.
2. **Your Stremio email and password**, if this device has never opened the player here. The
   password is relayed to Stremio, used once, and discarded — it is never stored and never logged.

Either way the server checks the account against the one that owns this box. The **first** account to
sign in claims it; every later sign-in must match. Set `STREMIOSRV_LIBRARY_OWNER` to pin it
explicitly, or delete `library-ui.json` from the data volume to re-pair.

> A valid Stremio account is not the same thing as *your* Stremio account. Without that ownership
> check, anyone with any Stremio login could open your library — which is why the check exists and
> why there is no way to switch it off.

### When the password form is not offered

If the server is using the shared `*.stremio.rocks` certificate that `IPADDRESS` fetches, the page
will not offer the password form, and says so.

That certificate is convenient and genuinely trusted by browsers — but **every installation gets the
same private key**, and that key can be fetched by anyone, unauthenticated. Someone positioned on
the network can therefore present a valid certificate for your server's hostname. That is tolerable
for a session key the browser already holds; it is not tolerable for an account password you type
in. Install your own certificate (see [cert-guide.md](cert-guide.md)) and both paths are available.

The same message appears, worded differently, if the certificate simply could not be read.

## What the page shows

Horizontal shelves of posters, in the shape the Stremio client uses:

| Shelf | What is in it |
|---|---|
| **Downloading** | In progress, with the bar drawn on the artwork. Hidden when nothing is running. |
| **Downloaded** | Complete and kept, marked with a check. |
| **Your library** | Your Stremio library. Items already on disk are marked; the rest have a **Download** button. |
| **Other on disk** | Everything the server is holding that is **not** matched to a title. |

That last shelf is not optional and does not hide itself. Pasted magnets, torrents played before the
UI existed, and anything whose title could not be resolved all consume disk, and a view that only
listed recognised titles would let the disk fill invisibly.

Titles and artwork come from the Stremio data already in your browser — your library, and the record
of which stream played which video. The server stores a small `labels.json` beside the cache for the
downloads you start here, so they are still labelled on a different device.

## The library as a Stremio addon

Turning the library on also registers it as a Stremio **addon**. The install address is on the page
itself, in the **"Watch this library in Stremio"** panel below the shelves — add it once, in the app,
under Addons → Add addon, and it produces two surfaces there:

- a **My Library** row on the board, one card per title the server holds;
- a **My Library** entry in a normal title's stream list, beside every other source, whenever the box
  already holds that title.

An entry the server has no Stremio identity for — a pasted magnet, or anything played before the
library existed — still shows up on that board row, but under its on-disk **folder name** and with
**no poster**: the addon can only name a title from `labels.json`, and guessing an identity from a
folder name would risk putting the wrong film behind a right-looking row.

The install address carries a token. **New link**, in the same panel, mints a fresh one and retires
the old — every device that installed the previous address stops working immediately, which is what
you want the moment a link has leaked or a device is retired.

> **Installing the addon writes its URL into your Stremio account.** That is how a second device or a
> TV inherits the install with nothing retyped — and also how that URL leaves this device and comes
> to rest in Stremio's own storage. So the token alone is not treated as the boundary: the addon also
> checks that the request comes from a **private network**, and this check trusts the
> `X-Forwarded-For` header — which this image's own nginx sets to the real client address. If you put
> **your own** reverse proxy in front of this server for remote access, it will typically overwrite
> that header with *its own* address, and its own address is on your LAN. Every visitor arriving
> through such a proxy then reads as local, and the token left in the URL becomes the only thing
> between the addon and the internet. Prefer a VPN back to the LAN over fronting this port with a
> reverse proxy, or make sure whatever you front it with forwards the original client address rather
> than replacing it.
>
> A request either check rejects gets back a plain 404 — the same one a route that does not exist
> would return — never a 401, which would at least confirm the path is real.

Reachable from the LAN only, and — like the page itself — off entirely unless `STREMIOSRV_LIBRARY_UI`
is turned on.

## How a download is started

The page asks **your own addons** for streams, in the browser, exactly as the Stremio client does,
and sends the server only a magnet link. The server never contacts an addon. Only addons that
declare the `stream` resource for that type are asked, and one addon that fails to answer is named
rather than emptying the list.

A magnet box is there for anything an addon does not serve.

A download is a **pin**: fully downloaded, never evicted, kept, and seeding. The existing disk guard
applies, so a download that would leave no room for normal streaming is refused with a message
saying how much space is needed. While anything is being watched, background downloads yield the
bandwidth (`STREMIOSRV_IDLE_DOWNLOAD_RATE_LIMIT`).

**Remove** unpins the torrent, stops it, deletes its files and forgets its label.

## Reaching it from outside the LAN

The page requires HTTPS — its session cookie is `Secure` — so it refuses plain HTTP unless you set
`STREMIOSRV_LIBRARY_ALLOW_HTTP=true`, which is for a trusted LAN or a VPN and nothing else.

For access from outside, the options are the ordinary ones: your own domain and certificate, a VPN
back to the LAN, or a tunnel. See [cert-guide.md](cert-guide.md).

## Files it keeps

Both live in the data volume and are protected from cache eviction:

| File | Contents |
|---|---|
| `library-ui.json` | The owning account id and current sessions. Owner-readable only. |
| `labels.json` | infohash → title, for downloads started from the page. |

Delete `library-ui.json` to sign every device out and re-pair the server with a fresh account.
