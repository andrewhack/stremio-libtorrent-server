"""Every API route must be reachable through nginx, or be deliberately listed as origin-only.

The image serves the web player as a static SPA and proxies the streaming API to uvicorn by an
explicit allowlist in docker/nginx-locations.inc. Anything not on that list falls through to
`location /` -> index.html, which returns **200 with HTML** — so a route added to FastAPI and
forgotten here does not 404. It answers, plausibly, with a web page, and the client's `resp.json()`
throws instead.

That is not hypothetical: /subtitleSignature shipped through unit tests, real-uvicorn checks and a
smoke test, and was still unreachable from the bundled player until the hermetic conformance gate
caught `text/html` on :12470. This test turns that verdict into an assertion, so the next route
cannot repeat it.
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
        if p not in ORIGIN_ONLY
        and not any(_matches(_concrete(p), mod, val) for mod, val in matchers)
    ]
    assert not unreachable, (
        "these routes are not proxied by docker/nginx-locations.inc, so on the player origin they "
        f"return the SPA's index.html (200 text/html) instead: {unreachable}. Add a location, or "
        "add them to ORIGIN_ONLY with a reason."
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


def test_origin_only_entries_are_real_routes():
    """A stale exclusion is worse than none: it silently blesses a path that no longer exists."""
    api = {r.path for r in create_app().routes if getattr(r, "path", "").startswith("/")}
    assert set(ORIGIN_ONLY) <= api, f"ORIGIN_ONLY names routes that do not exist: {set(ORIGIN_ONLY) - api}"
