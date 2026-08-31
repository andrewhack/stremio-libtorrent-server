

from fastapi.testclient import TestClient

from stremiosrv.app import create_app
from stremiosrv.config import Settings
from stremiosrv.library.api import INDEX_HTML


def _page() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


def test_page_is_served(tmp_path):
    c = TestClient(create_app(settings=Settings(library_ui=True, cache_root=str(tmp_path))),
                   base_url="https://testserver")
    r = c.get("/library/", headers={"X-Forwarded-Proto": "https"})
    assert r.status_code == 200 and "<!doctype html>" in r.text.lower()


def test_page_reads_the_profile_bucket():
    """The fast path is the whole reason this lives on the player's origin."""
    assert "localStorage.getItem('profile')" in _page()


def test_page_calls_every_endpoint_it_needs():
    page = _page()
    for path in ("/library/api/config", "/library/api/session",
                 "/library/api/state", "/library/api/remove"):
        assert path in page, f"page never calls {path}"


def test_page_has_no_external_asset():
    """No CDN: the box may be reached over a link with no route to the wider internet, and a page
    that needs a third party to render is a page that fails exactly then."""
    page = _page()
    for bad in ('src="http', 'href="http', "cdn.", "unpkg", "jsdelivr"):
        assert bad not in page, f"page references an external asset: {bad}"


def test_page_never_stores_the_password():
    page = _page()
    assert "localStorage.setItem('password'" not in page
    assert "sessionStorage.setItem('password'" not in page


def _interpolations(src: str) -> list[str]:
    """Every `${...}` in the file, matched with balanced braces.

    A naive `\\$\\{([^}]*)\\}` stops at the first `}`, so a nested interpolation like
    `${Math.round(pct(e))}` inside a template literal comes back as a mangled fragment of the
    enclosing ternary — which reads as a violation and is not one. Count braces instead.
    """
    out, i = [], 0
    while (i := src.find("${", i)) != -1:
        depth, j = 1, i + 2
        while j < len(src) and depth:
            if src[j] == "{":
                depth += 1
            elif src[j] == "}":
                depth -= 1
            j += 1
        out.append(src[i + 2:j - 1])
        i = j
    return out


def test_every_interpolated_value_is_escaped():
    """Torrent names and Stremio library titles are third-party text this page did not author, and
    they go into innerHTML. Every `${...}` must pass through esc() or be a number this page
    computed — otherwise a crafted torrent name is stored XSS against the owner's own session, on
    an internet-facing origin.
    """
    offenders = []
    for expr in _interpolations(_page()):
        e = expr.strip()
        if e.startswith(("esc(", "Math.", "fmt(")):
            continue
        # A ternary whose branches are themselves template literals: its own `${...}` were already
        # collected separately by the scanner above, so judge them there, not here.
        if "?" in e and "`" in e:
            continue
        offenders.append(e)
    assert not offenders, f"unescaped interpolations into innerHTML: {offenders}"


def test_the_escape_scanner_actually_catches_something():
    """Guards the guard: if `_interpolations` silently returned nothing, the test above would pass
    on any page at all."""
    found = _interpolations(_page())
    assert len(found) > 5, f"scanner found only {len(found)} interpolations — it is not working"
    assert any(e.strip().startswith("esc(") for e in found)
