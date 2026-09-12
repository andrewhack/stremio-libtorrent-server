"""Every API route must be reachable through nginx, or be deliberately listed as origin-only.

The image serves the web player as a static SPA and proxies the streaming API to uvicorn by an
explicit allowlist in docker/nginx-locations.inc. Anything neither on that list nor a web-player
file goes to /_unmatched: a 404, counted by shape in /stats.json. Until 1.6.7 it fell through to
index.html instead and answered **200 with HTML**, so a route added to FastAPI and forgotten here
did not fail -- it answered, plausibly, with a web page, and the client's `resp.json()` threw.

That is not hypothetical: /subtitleSignature shipped through unit tests, real-uvicorn checks and a
smoke test, and was still unreachable from the bundled player until the hermetic conformance gate
caught `text/html` on :12470. The 404 makes such a miss loud; this test keeps it from shipping.
"""
from __future__ import annotations

import pathlib
import re

from stremiosrv.app import create_app

_INC = pathlib.Path(__file__).resolve().parents[1] / "docker" / "nginx-locations.inc"

# Routes answered ONLY on the direct API origin (:11470), never through the player's nginx.
# Each needs a reason: this list is the place a "should the player see this?" decision gets made,
# not a place to silence the test.
# The appliance's config-web defaults to http://127.0.0.1:11470 (console-status/config.py), so
# everything it drives is answered on the direct origin and never needs the player's nginx.
_CONFIG_WEB = "appliance config-web calls :11470 directly"
ORIGIN_ONLY = {
    "/cache.json": _CONFIG_WEB,
    "/cache/remove": _CONFIG_WEB,
    "/pins.json": _CONFIG_WEB,
    "/{info_hash}/pin": _CONFIG_WEB,
    "/{info_hash}/unpin": _CONFIG_WEB,
    "/netcheck.json": _CONFIG_WEB,
    "/active.json": _CONFIG_WEB,
    "/transcode.json": "diagnostics; no client requests it through the player origin",
}


# Routes nginx reaches by rewrite rather than by a location of their own.
REWRITE_TARGETS = {
    "/_unmatched": "the @unmatched fallback: every path that is neither proxied nor a player file",
}


def _proxied_matchers() -> list:
    """Every location in the .inc that proxies to uvicorn, as (kind, value) predicates."""
    out = []
    for line in _INC.read_text(encoding="utf-8").splitlines():
        if "proxy_pass" not in line or not line.lstrip().startswith("location"):
            continue
        m = re.match(r'\s*location\s+(=|\^~|~\*?)?\s*"?([^"\s]+)"?\s*\{', line)
        assert m, f"unparsed location line: {line!r}"
        mod, value = (m.group(1) or ""), m.group(2)
        out.append((mod, value))
    return out


def _matches(path: str, mod: str, value: str) -> bool:
    if mod == "=":
        return path == value
    if mod.startswith("~"):
        return re.search(value, path) is not None
    return path.startswith(value)  # prefix, incl. ^~


def _concrete(path: str) -> str:
    """A FastAPI template -> a representative real URL, so prefix/regex rules can be tested."""
    path = path.replace("{info_hash}", "a" * 40)
    path = re.sub(r"\{idx(:int)?\}", "1", path)
    return path.replace("{ext}", "srt")


def test_every_api_route_is_proxied_or_declared_origin_only():
    api = sorted(
        r.path for r in create_app().routes
        if getattr(r, "path", "").startswith("/") and "methods" in dir(r)
        and r.path not in ("/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc")
    )
    matchers = _proxied_matchers()
    unreachable = [
        p for p in api
        if p not in ORIGIN_ONLY and p not in REWRITE_TARGETS
        and not any(_matches(_concrete(p), mod, val) for mod, val in matchers)
    ]
    assert not unreachable, (
        "these routes are not proxied by docker/nginx-locations.inc, so on the player origin they "
        f"return the counted 404 instead: {unreachable}. Add a location, or add them to "
        "ORIGIN_ONLY with a reason."
    )


def test_subtitle_signature_is_reachable_through_nginx():
    """The specific regression the hermetic gate caught. /subtitles. is a case-sensitive prefix, so
    it does NOT cover /subtitleSignature — this needs its own exact location."""
    matchers = _proxied_matchers()
    assert any(_matches("/subtitleSignature", mod, val) for mod, val in matchers)
    # and prove the near-miss is real, so the exact rule is not mistaken for redundant
    prefix = [(m, v) for m, v in matchers if v == "/subtitles."]
    assert prefix, "the /subtitles. prefix rule vanished — re-check this test's premise"
    assert not _matches("/subtitleSignature", *prefix[0])


def test_the_clients_create_call_and_guess_index_are_proxied():
    """stremio-video POSTs /<ih>/create before streaming any addon stream that carries `sources` or
    no fileIdx, and stremio-core builds /<ih>/-1 for the same streams. Both used to fall through to
    the web player -- the POST as a 405, the GET as index.html -- and the stream never started."""
    matchers = _proxied_matchers()
    ih = "a" * 40
    for path in (f"/{ih}/create", f"/{ih}/-1"):
        assert any(_matches(path, mod, val) for mod, val in matchers), path
    # ...without loosening the anchor: the web build's own 40-hex asset dir must stay static.
    assert not any(_matches(f"/{ih}/scripts/main.js", mod, val) for mod, val in matchers)


def test_origin_only_entries_are_real_routes():
    """A stale exclusion is worse than none: it silently blesses a path that no longer exists."""
    api = {r.path for r in create_app().routes if getattr(r, "path", "").startswith("/")}
    assert set(ORIGIN_ONLY) <= api, f"ORIGIN_ONLY names routes that do not exist: {set(ORIGIN_ONLY) - api}"
    assert set(REWRITE_TARGETS) <= api, (
        f"REWRITE_TARGETS names routes that do not exist: {set(REWRITE_TARGETS) - api}")


def test_unauthenticated_routes_stay_origin_only():
    """The library UI adds an AUTHENTICATED namespace on the public origin. These routes have no
    auth at all and must never join it — proxying them through nginx would hand the cache list and
    pin controls to the internet."""
    for path in ("/cache.json", "/cache/remove", "/pins.json",
                 "/{info_hash}/pin", "/{info_hash}/unpin"):
        assert path in ORIGIN_ONLY, f"{path} must stay origin-only"


def test_library_routes_are_proxied_when_the_flag_is_on():
    """The test above builds `create_app()` with DEFAULT settings, where STREMIOSRV_LIBRARY_UI is
    off — so the /library routes are not registered and it cannot see them at all. Without this,
    every future library route could be added and forgotten in nginx-locations.inc while the
    allowlist test stayed green: the exact /subtitleSignature failure, reintroduced by a feature
    flag. Build the app with the flag ON and hold that namespace to the same rule."""
    from stremiosrv.config import Settings

    app = create_app(settings=Settings(library_ui=True))
    lib = sorted(
        r.path for r in app.routes
        if getattr(r, "path", "").startswith("/library") and "methods" in dir(r)
    )
    assert lib, "the flag is on but no /library route was registered — check the mount in app.py"
    matchers = _proxied_matchers()
    unreachable = [p for p in lib
                   if not any(_matches(_concrete(p), mod, val) for mod, val in matchers)]
    assert not unreachable, (
        f"library routes not proxied by docker/nginx-locations.inc: {unreachable}"
    )


def test_the_addon_paths_reach_the_app_through_the_include():
    """`location ^~ /library` already covers these -- asserted so that narrowing that prefix later
    cannot silently serve the web player's index.html to Stremio instead of a manifest."""
    matchers = _proxied_matchers()
    assert any(
        _matches("/library/addon/sometoken/manifest.json", mod, val) for mod, val in matchers
    )
    assert any(
        _matches("/library/addon/sometoken/catalog/other/library.json", mod, val)
        for mod, val in matchers
    )


def test_the_client_address_is_forwarded():
    """Without this header every proxied request arrives as nginx's own loopback address, and the
    addon's LAN check would inspect the proxy instead of the client."""
    assert "proxy_set_header X-Forwarded-For" in _INC.read_text(encoding="utf-8")


def _location_block(name: str) -> str:
    """The body of `location <name> { ... }` in the include (it has no nested braces)."""
    text = _INC.read_text(encoding="utf-8")
    m = re.search(r"^location " + re.escape(name) + r" \{(.*?)\}", text, re.M | re.S)
    assert m, f"no `location {name}` block in the include"
    return m.group(1)


def test_unmatched_paths_get_a_counted_404_not_the_web_player():
    """Every path that is neither proxied nor a web-player file must reach /_unmatched (404, counted
    in /stats.json), never index.html -- the fallback that hid a missing route for months."""
    fallback = _location_block("/")
    assert "/index.html" not in fallback
    assert "@unmatched" in fallback
    named = _location_block("@unmatched")
    assert "rewrite ^ /_unmatched? break;" in named
    assert "proxy_pass http://127.0.0.1:11470;" in named
    assert "X-Original-Method $request_method" in named
    assert "X-Original-URI $request_uri" in named
    assert 'proxy_set_header X-Forwarded-For "";' in named


def test_the_proxy_route_reaches_the_app_with_its_raw_path():
    """stremio-video's /proxy URLs carry the destination percent-encoded in the path. A prefix
    location with a bare proxy_pass (no URI part) forwards the request URI as the client sent it."""
    assert ("^~", "/proxy/") in _proxied_matchers()
    line = next(ln for ln in _INC.read_text(encoding="utf-8").splitlines()
                if ln.lstrip().startswith("location ^~ /proxy/"))
    assert line.rstrip().endswith("{ proxy_pass http://127.0.0.1:11470; }")
