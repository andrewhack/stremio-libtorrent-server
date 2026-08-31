

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
        # esc()/fmt()/Math.* are safe by construction. posterHtml() is an HTML *builder* that
        # escapes both of its arguments — allowed here only because
        # `test_poster_builder_escapes_both_arguments` below pins that, so this exemption cannot
        # quietly become false.
        if e.startswith(("esc(", "Math.", "fmt(", "posterHtml(")):
            continue
        # A ternary whose branches are themselves template literals: its own `${...}` were already
        # collected separately by the scanner above, so judge them there, not here.
        if "?" in e and "`" in e:
            continue
        # A ternary choosing between two static string literals — no data reaches the output.
        if "?" in e and "${" not in e and "`" not in e:
            branches = e.split("?", 1)[1]
            if all(part.strip()[:1] in ("'", '"') for part in branches.split(":") if part.strip()):
                continue
        offenders.append(e)
    assert not offenders, f"unescaped interpolations into innerHTML: {offenders}"


def test_the_escape_scanner_actually_catches_something():
    """Guards the guard: if `_interpolations` silently returned nothing, the test above would pass
    on any page at all."""
    found = _interpolations(_page())
    assert len(found) > 5, f"scanner found only {len(found)} interpolations — it is not working"
    assert any(e.strip().startswith("esc(") for e in found)


def test_page_resolves_streams_in_the_browser():
    """The server never contacts an addon — that is what keeps it content-neutral. The page must
    therefore build the /stream/ URL itself and hand the server only a magnet."""
    page = _page()
    assert "/library/api/download" in page
    # The addon's own transportUrl is rewritten into a /stream/ request, in the browser.
    assert "transportUrl" in page
    assert "'stream/'" in page


def test_page_filters_addons_by_resource():
    """Only addons whose manifest declares `stream` for this type are queried, so a catalog-only
    addon is not fanned out to on every click."""
    page = _page()
    assert "resources" in page and "'stream'" in page


def test_page_reads_the_library_bucket():
    assert "localStorage.getItem('library')" in _page()


def test_page_offers_magnet_paste():
    assert 'id="magnet"' in _page()


def test_page_joins_downloads_to_titles_through_the_streams_bucket():
    """`streams` is how an infohash the server holds becomes a title the owner recognises, for
    anything played through the client rather than downloaded from this page."""
    page = _page()
    assert "localStorage.getItem('streams')" in page
    assert "offlineMetaIds" in page


def test_poster_builder_escapes_both_arguments():
    """`posterHtml(url, name)` is exempted from the interpolation scan above because it builds HTML
    itself. That exemption is only honest while it escapes what it is handed — a poster URL and a
    title both come from third parties."""
    page = _page()
    body = page[page.index("const posterHtml"):page.index("const cardHtml")]
    assert "esc(url)" in body, "posterHtml does not escape the URL it is given"
    assert "esc(name)" in body, "posterHtml does not escape the name it is given"


def test_page_falls_back_to_the_stremio_api_for_library_and_addons():
    """A browser that has only ever run the desktop or TV app has an EMPTY `library` bucket on this
    origin, so reading localStorage alone shows an empty library to a correctly signed-in owner.
    The account data has to come from the API in that case."""
    page = _page()
    assert "datastoreGet" in page, "no library fallback: 'Your library' stays empty off-device"
    assert "libraryItem" in page
    assert "addonCollectionGet" in page, "no addon fallback: every Download would find no sources"


def test_api_fallback_checks_the_error_body_not_the_status():
    """Same trap as the server side: api.strem.io answers failures with HTTP 200 and an error body."""
    page = _page()
    body = page[page.index("const stremioApi"):page.index("const localLibrary")]
    assert "b.error" in body, "browser-side API helper trusts the HTTP status"


def test_badge_means_pinned_not_merely_present():
    """Ordinary cached files from playback are not downloads. Marking them with the same check as a
    kept download tells the owner they have something they do not."""
    page = _page()
    assert "e.pinned && !downloading" in page


def test_no_remove_button_where_the_server_cannot_act():
    assert "e.removable === false" in _page()


def test_authkey_survives_a_reload():
    """It was a closure variable, so a password sign-in's key vanished on the next page load and
    the account data could never be fetched again — which is what kept 'Your library' empty."""
    page = _page()
    assert "stremiosrv_library_authkey" in page
    assert "rememberAuthKey" in page


def test_an_existing_session_cookie_is_honoured():
    """Without probing the cookie first, a device with no player data was sent back to the sign-in
    form on every single reload even though its session was still valid."""
    page = _page()
    body = page[page.index("async function signIn"):page.index("async function passwordLogin")]
    assert "/library/api/state" in body


def test_empty_library_says_why():
    """A silently empty shelf is indistinguishable from a broken fetch. It must name which one."""
    assert "libraryError" in _page()


def test_continue_watching_items_are_not_filtered_out():
    """In Stremio's model `removed` does not mean deleted: an item auto-added by playing something
    is `temp`, and those Continue-Watching entries carry removed:true. Filtering on `!removed`
    alone discards nearly the whole row."""
    assert "i.temp || !i.removed" in _page()


def test_board_renders_addon_catalogs_not_a_saved_library():
    """The client's home board is built from the INSTALLED addons' catalogs, in the order they
    declare them -- that is what produces "Popular - Movie", "Popular - Series", "Featured - ...".
    A list of saved library items is a different surface."""
    page = _page()
    assert "catalogRows" in page and "'catalog/'" in page
    assert "res.includes('catalog')" in page


def test_catalogs_requiring_an_extra_are_skipped():
    """Cinemeta's `New` catalog requires a genre and its `last-videos` requires ids: asking without
    them returns nothing, which is why the client does not show those rows either."""
    assert "e.isRequired" in _page()


def test_catalog_row_count_is_capped():
    """Every row is a network call. An addon collection with many catalogs must not fan out into a
    request storm on page load."""
    assert "CATALOG_ROW_CAP" in _page()


def test_a_failing_catalog_row_says_so():
    """One catalog that will not answer must not blank the board or fail silently."""
    assert "Could not load: " in _page()


def test_page_cannot_render_blank_on_a_script_error():
    """Both panels used to start hidden, so any error painted an entirely blank page with no clue
    what broke -- which is exactly what happened in one browser and not another."""
    page = _page()
    assert '<div id="auth" class="panel">' in page, "auth panel must not start hidden"
    assert "window.addEventListener('error'" in page


def test_page_is_not_cacheable(tmp_path):
    """A cached page makes every redeploy unverifiable: the person testing cannot tell whether they
    are running the new build, so a fixed bug and an unfixed one look the same."""
    c = TestClient(create_app(settings=Settings(library_ui=True, cache_root=str(tmp_path))),
                   base_url="https://testserver")
    r = c.get("/library/", headers={"X-Forwarded-Proto": "https"})
    assert "no-store" in r.headers.get("cache-control", "")


def test_the_poll_does_not_rebuild_the_board():
    """The 5s poll used to call renderBoard, which re-fetches every catalog and rewrites the DOM —
    the page visibly reloaded its rows over and over. The poll now only flips the offline markers."""
    page = _page()
    assert "refreshOfflineMarks" in page
    body = page[page.index("  function render(data) {"):page.index("  async function loadState()")]
    # A CALL, not the word: the comment in there explains why renderBoard must not be called, and
    # matching the bare name flagged that comment as the violation it was warning about.
    code = chr(10).join(ln for ln in body.splitlines() if not ln.strip().startswith("//"))
    assert "renderBoard(" not in code, "render() still rebuilds the whole board on every poll"


def test_on_disk_entries_are_named_from_the_players_own_records():
    """A title being watched right now arrives from the server with no label, and was shown under
    'Other on disk' as a raw torrent folder name. The player already knows what it is."""
    page = _page()
    assert "withClientLabels" in page and "streamIndex" in page


def test_data_loading_errors_have_somewhere_to_show():
    """`libraryError` lost its only render site when the library shelf was replaced by the board,
    so a failed fetch became invisible again."""
    page = _page()
    assert 'id="status"' in page
    assert "$('status').textContent" in page


def test_continue_watching_sort_tolerates_an_unparsable_timestamp():
    """Date parsing is stricter in some browsers; NaN in a comparator makes the sort incoherent."""
    assert "Number.isNaN(n) ? 0 : n" in _page()
