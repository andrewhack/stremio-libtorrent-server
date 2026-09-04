import re

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
        # A name ending in `Html` -- variable or call -- is a fragment this file built. The
        # convention is only honest because `test_html_builders_escape_their_data` below finds
        # every such builder and checks it is assembled with esc().
        if e.endswith("Html") or re.match(r"^\w+Html\(", e):
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


def test_localstorage_buckets_are_unwrapped():
    """stremio-core persists every bucket as {uid, items}. Reading the wrapper made Object.values()
    return [uid, itemsObject], so every infohash join silently matched nothing — which is why
    on-disk titles stayed unrecognised and Continue watching offered Download for things already
    downloaded."""
    page = _page()
    assert "bucketItems" in page
    assert "raw.items !== undefined" in page


def test_streams_bucket_handles_a_struct_keyed_map():
    """Its HashMap key is a struct ({metaId, videoId}), which serialises as [key, value] pairs
    rather than an object."""
    assert "Array.isArray(entry)" in _page()


def test_release_names_are_matched_against_library_titles():
    """`streams` is never synced by stremio-core, so a browser that has not run the player has no
    infohash->title map at all. The torrent's own name is what a torrent client matches on."""
    page = _page()
    assert "nameMatcher" in page
    # Longest title first, so a short title that prefixes a longer one cannot win.
    assert "b.words.length - a.words.length" in page


def test_offline_detection_uses_the_client_labels():
    """Without this it saw only labels the SERVER wrote, so a title on disk from ordinary playback
    was never recognised and its card kept offering Download."""
    page = _page()
    body = page[page.index("function offlineMetaIds"):page.index("function renderContinue")]
    assert "withClientLabels(data.entries)" in body


def test_library_sources_are_merged_not_chosen():
    """The player's local bucket and the account's synced library are DIFFERENT sets. Preferring one
    made the same server look correct in one browser and empty in another, purely because they were
    reading different libraries."""
    page = _page()
    assert "Object.assign({}, remoteLibrary || {}, localLibrary())" in page


def test_catalog_metas_also_feed_the_name_matcher():
    """A title can live only in another device's local bucket, which no merging can reach. The
    catalog rows are already fetched for the board and carry names, so they cost nothing to use."""
    page = _page()
    assert "catalogMetas" in page
    assert "Object.assign({}, catalogMetas, storedLibrary())" in page


def test_download_opens_a_chooser_rather_than_grabbing_the_first_stream():
    """A series needs a season and an episode picked, and even a film has releases worth choosing
    between — which is exactly what the Stremio client asks for. Taking streams[0] was never right."""
    page = _page()
    assert "openDetail" in page
    body = page[page.index("function wireDownloadButtons"):page.index("function renderContinue")]
    assert "streams[0]" not in body, "the button still downloads the first stream blindly"


def test_series_detail_uses_the_meta_resource():
    """Seasons and episodes come from a meta-capable addon: meta/<type>/<id>.json -> meta.videos,
    each with `season`, `episode` and an id of the form <metaId>:<season>:<episode>."""
    page = _page()
    assert "metaAddons" in page and "'meta/'" in page
    assert "res.includes('meta')" in page
    assert "renderSeries" in page


def test_the_chosen_episode_is_recorded_on_the_download():
    """Without season/episode on the label, every episode of a series would be indistinguishable
    once downloaded."""
    page = _page()
    assert "season: v.season, episode: v.episode" in page


def test_releases_show_something_to_choose_between():
    """A list of identical rows is not a choice. Show the addon's own name plus whatever detail it
    gives — description, filename, size."""
    page = _page()
    assert "relLabel" in page
    assert "bh.videoSize" in page


def test_a_downloading_card_shows_speed_seeders_and_the_release():
    """A percentage alone does not say whether a download is moving, and two episodes of the same
    show look identical without the release name."""
    page = _page()
    assert "seeders" in page
    assert "e.downloadSpeed" in page
    assert "rel2" in page, "the chosen release is not shown on a downloading card"


def test_speed_and_seeders_update_between_polls():
    """These numbers only mean something if they move. The poll refreshes them in place rather
    than waiting for a board rebuild, which no longer happens."""
    page = _page()
    start = page.index("function refreshOfflineMarks")
    # Slice to the NEXT function, not to a name that happens to sit earlier in the file — that
    # produced a negative range and a test that could only ever fail.
    nxt = min(x for x in (page.find(chr(10) + "  function ", start + 10),
                          page.find(chr(10) + "  async function ", start + 10)) if x > 0)
    body = page[start:nxt]
    assert "downloadSpeed" in body and "seeders" in body


def test_cards_carry_their_infohash_for_in_place_updates():
    """The poll finds the card to update by infohash; without it the numbers could never refresh."""
    assert 'data-infohash="${esc(e.infoHash' in _page()


def test_html_builders_escape_their_data():
    """`${somethingHtml}` is exempt from the interpolation scan because it is a fragment this file
    assembled. That exemption holds only while each of those builders escapes what it interpolates
    — otherwise the naming convention becomes a way to smuggle raw data into innerHTML.

    Enumerated, not listed: a hardcoded list silently stops covering the next builder someone adds.
    """
    page = _page()
    names = sorted(set(re.findall(r"const (\w+Html)\s*=", page)))
    assert len(names) >= 3, f"builder scan found only {names} — the convention is not being used"
    for name in names:
        start = page.index("const " + name)
        # Bound at the next top-level declaration. Cutting at the first ";" landed inside a
        # builder's own local variables, before any escaping had happened yet.
        ends = [x for x in (page.find(chr(10) + "  const ", start + 8),
                            page.find(chr(10) + "  function ", start + 8),
                            page.find(chr(10) + "  async function ", start + 8)) if x > 0]
        body = page[start:min(ends)] if ends else page[start:]
        assert "esc(" in body, f"{name} builds HTML without escaping anything"


def test_a_series_is_counted_not_ticked():
    """One episode on disk does not make a series 'offline'. Saying so is wrong in the way that
    matters most: it hides that the rest is missing — and it appeared next to a progress bar
    showing the same series still downloading."""
    page = _page()
    assert "onDiskCounts" in page
    assert "on disk" in page, "a series shows no count of what is held"


def test_a_series_keeps_its_download_button():
    """A film with a copy has nothing left to fetch; a series always might."""
    page = _page()
    body = page[page.index("const metaCardHtml"):page.index("function wireDownloadButtons")]
    assert "n > 0 && !series" in body, "the button is removed for series as well as films"


# --- Continue watching: exercise the shipped predicates, not a copy of them -------------------
# The row showed films nobody had started and dropped half-finished series, because it selected
# the whole library and sorted on `_mtime`. A string assertion cannot catch a wrong predicate, so
# this pulls the real source out of the page and runs it.

# Whole lines, not "up to the first semicolon": `ts` has a semicolon inside its own body.
_CONTINUE_SRC = re.compile(
    r"(const ts = [^\n]*)[\s\S]*?(const wstate = [\s\S]*?const watchedAt = [^\n]*)")


def _run_continue(items):
    """Filter+sort `items` with the page's own continue-watching predicates, via node."""
    import json
    import shutil
    import subprocess

    import pytest
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available; continue-watching predicates not exercised")
    m = _CONTINUE_SRC.search(_page())
    assert m, "continue-watching predicates not found in the page — did they move?"
    harness = f"""
{m.group(1)}
{m.group(2)}
const items = {json.dumps(items)};
const out = items
  .filter(i => i && i._id && (i.temp || !i.removed) && inProgress(i))
  .sort((a, b) => watchedAt(b) - watchedAt(a))
  .map(i => i._id);
console.log(JSON.stringify(out));
"""
    r = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_continue_row_excludes_titles_never_started():
    """A library entry with no watch progress is not "continue watching" — it is just a title
    someone added, and it used to outrank real progress purely on `_mtime`."""
    got = _run_continue([
        {"_id": "started", "_mtime": "2020-01-01T00:00:00Z",
         "state": {"timeOffset": 900, "lastWatched": "2020-01-01T00:00:00Z"}},
        {"_id": "never-played", "_mtime": "2030-01-01T00:00:00Z", "state": {"timeOffset": 0}},
        {"_id": "no-state-at-all", "_mtime": "2031-01-01T00:00:00Z"},
    ])
    assert got == ["started"]


def test_continue_row_excludes_titles_flagged_watched():
    got = _run_continue([
        {"_id": "finished", "_mtime": "2030-01-01T00:00:00Z",
         "state": {"timeOffset": 5000, "flaggedWatched": 1}},
        {"_id": "midway", "_mtime": "2020-01-01T00:00:00Z", "state": {"timeOffset": 60}},
    ])
    assert got == ["midway"]


def test_continue_row_orders_by_last_watched_not_last_modified():
    """`_mtime` moves on any mutation — a sync, an add, a watched flag. Ordering on it put things
    at the top that had not been watched in months."""
    got = _run_continue([
        {"_id": "touched-recently-watched-long-ago", "_mtime": "2031-01-01T00:00:00Z",
         "state": {"timeOffset": 60, "lastWatched": "2020-01-01T00:00:00Z"}},
        {"_id": "watched-last-night", "_mtime": "2020-06-01T00:00:00Z",
         "state": {"timeOffset": 60, "lastWatched": "2030-01-01T00:00:00Z"}},
    ])
    assert got == ["watched-last-night", "touched-recently-watched-long-ago"]


def test_downloaded_section_does_not_depend_on_labels_json():
    """labels.json is a separate file in the cache root and it HAS been deleted in the field.
    A pin is what says the library downloaded something; the label is decoration on top."""
    assert "const ours = e => Boolean(e.label || e.pinned);" in _page()


# --- complete vs kept, and whether a release can fit ------------------------------------------

_COMPLETE_SRC = re.compile(r"(const isComplete = [^\n]*)")
_FITS_SRC = re.compile(r"(const sizeVerdict = [\s\S]*?\n  \};)")


def _run_js(src, expr):
    import json
    import shutil
    import subprocess

    import pytest
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    r = subprocess.run([node, "-e", f"{src}\nconsole.log(JSON.stringify({expr}));"],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def _complete_src():
    m = _COMPLETE_SRC.search(_page())
    assert m, "isComplete not found in the page"
    return m.group(1)


def _fits_src():
    m = _FITS_SRC.search(_page())
    assert m, "sizeVerdict not found in the page"
    return m.group(1)


def test_a_title_on_disk_reads_as_complete_even_unpinned():
    """A title the player streamed to the end is complete, and the card said nothing about it.

    The tick is NOT the place for this -- it means "kept", and
    test_badge_means_pinned_not_merely_present holds it to that. Completeness is stated in the
    sub-line instead, so the two facts cannot be confused for one another.
    """
    src = _complete_src()
    assert _run_js(src, "isComplete({state:'idle', progress:1, pinned:false})") is True
    assert _run_js(src, "isComplete({state:'seeding', progress:1, pinned:true})") is True
    assert _run_js(src, "isComplete({state:'downloading', progress:0.5})") is False
    assert _run_js(src, "isComplete({state:'idle', progress:0.4})") is False


def test_the_card_states_both_completeness_and_keptness():
    page = _page()
    assert "const stateWord = e =>" in page
    assert "'complete'" in page and "'cached'" in page and "'kept'" in page
    # the tick still means kept, and only kept
    assert "e.pinned && !downloading" in page


def test_a_release_that_cannot_fit_is_refused():
    """No room on the DISK is the only case that disables the button."""
    src = _fits_src()
    b = "{diskFree: 60e9, diskTotal: 80e9, cacheUsed: 0, cacheSize: 19327352832}"
    assert _run_js(src, f"sizeVerdict(4.5e9, {b})") == "ok"
    assert _run_js(src, f"sizeVerdict(59e9, {b})") == "nodisk"


def test_the_download_gate_does_not_reserve_the_cache_budget_the_way_a_pin_does():
    """It applied pins.pin_fits: free space must exceed the release PLUS the whole budget and ten
    percent. That is right for a PIN, which can never be evicted and therefore has to leave the
    entire budget free beside it for ordinary streaming. Applied to a download -- ordinary
    evictable cache since the want/pin split -- a 48 GiB budget demanded ~56.7 GB free ON TOP of
    the release, so a 10 GB file was greyed out on a box with 61 GB free.
    """
    src = _fits_src()
    b = "{diskFree: 61e9, diskTotal: 72e9, cacheUsed: 30e9, cacheSize: 51539607552}"
    assert _run_js(src, f"sizeVerdict(10e9, {b})") != "nodisk"
    # The reserve is real, though: a release that would leave the disk bare is still refused.
    assert _run_js(src, f"sizeVerdict(60e9, {b})") == "nodisk"


def test_the_reserve_scales_with_the_disk():
    """A flat floor is too little to hold back on a large disk and too much on a small one, so it
    is the larger of the two. Nothing fits when free space is already inside it."""
    src = _fits_src()
    big = "{diskFree: 30e9, diskTotal: 2e12, cacheUsed: 0, cacheSize: 19327352832}"
    assert _run_js(src, f"sizeVerdict(0, {big})") == "nodisk"      # 2% of 2 TB is 40 GB
    small = "{diskFree: 30e9, diskTotal: 40e9, cacheUsed: 0, cacheSize: 19327352832}"
    assert _run_js(src, f"sizeVerdict(1e9, {small})") == "ok"      # 2% of 40 GB is under the floor


def test_a_release_that_overruns_the_budget_is_offered_with_a_warning():
    """It fits on the disk, so refusing it would be wrong -- a download is ordinary cache and the
    owner may well want it anyway. But it will push the cache past its budget, so the evictor may
    reclaim it before they watch it, and they should see that before clicking rather than after.
    """
    src = _fits_src()
    b = "{diskFree: 60e9, cacheUsed: 17e9, cacheSize: 19327352832}"
    assert _run_js(src, f"sizeVerdict(6e9, {b})") == "overbudget"
    assert _run_js(src, f"sizeVerdict(1e9, {b})") == "ok"



def test_an_unknown_release_size_is_not_treated_as_refusal():
    """An unknown size is not a refusal on its own -- but it is not a free pass either.

    Refusing every release whose size no addon reported would grey out most of them. Yet when free
    space is already under the headroom, NOTHING fits. That gap is why a release far larger than
    the free space was still clickable.
    """
    src = _fits_src()
    roomy = "{diskFree: 60e9, cacheUsed: 0, cacheSize: 19327352832}"
    assert _run_js(src, f"sizeVerdict(0, {roomy})") == "ok"
    assert _run_js(src, "sizeVerdict(5e9, {})") == "ok"          # nothing to judge against
    cramped = "{diskFree: 1e9, cacheUsed: 0, cacheSize: 19327352832}"
    assert _run_js(src, f"sizeVerdict(0, {cramped})") == "nodisk"



def test_a_release_size_is_read_from_the_text_when_the_addon_omits_the_field():
    """behaviorHints.videoSize is usually absent; addons put the size in the description instead,
    which is where the gate has to read it or it stays inert for almost every release."""
    m = re.search(r"(const SIZE_RE = [\s\S]*?\n  \};)", _page())
    assert m, "releaseSize not found in the page"
    src = m.group(1)
    gb = 1024 ** 3
    assert _run_js(src, "releaseSize({behaviorHints:{videoSize: 123}})") == 123
    assert _run_js(src, "releaseSize({description:'Some.Release.mkv 356 4.21 GB'})") == round(4.21 * gb)
    assert _run_js(src, "releaseSize({title:'x 225.43 MB y'})") == round(225.43 * 1024 ** 2)
    assert _run_js(src, "releaseSize({description:'no size here'})") == 0


def test_the_sign_in_panel_links_to_the_player_rather_than_describing_it():
    """On a shared-cert box the panel cannot offer a password, so it sends the owner to the player
    to sign in there. It used to say "on this address" and leave them to construct it -- and the
    obvious guess is wrong: the player is ALSO served on a plain-HTTP port, so a browser reaching
    for that one over https (which it will, on a trusted hostname) fails with
    SSL_ERROR_RX_RECORD_TOO_LONG and nothing pointing at the port. The player is at the root of
    this very origin, so the panel links to it: same scheme, host and port.
    """
    page = _page()
    assert "a.href = '/';" in page
    assert "Open the player on this address" in page
    # built as a node, never interpolated into markup
    assert "document.createElement('a')" in page
    assert "introlink" in page


def test_keeping_is_a_deliberate_control_on_the_card():
    """Downloading no longer pins, so there has to be something that does -- and it must be the
    same act as the appliance's pin, not a second meaning of "kept"."""
    page = _page()
    assert "/library/api/pin" in page and "/library/api/unpin" in page
    assert "data-keep=" in page
    # Unpin is not Remove: the bytes stay, they merely become evictable again.
    assert "/library/api/remove" in page


def test_a_pack_shows_the_files_it_holds():
    """A season pack fills up from several places -- one episode downloaded here, another streamed
    by the player, possibly by different people. One card said nothing about that while the disk
    grew, so the card now lists each file it holds with that file's own size."""
    page = _page()
    assert "const childrenHtml = e =>" in page
    assert "e.children" in page
    assert 'class="kid' in page


def test_every_episode_a_pack_holds_is_ticked_not_just_one():
    """A season pack is ONE infohash, and the player's stream record maps an infohash to a single
    video -- so ticking by videoId marked at most one episode however many were on disk. The file
    names are what know which episodes a torrent actually holds."""
    page = _page()
    assert "function onDiskEpisodes(" in page
    assert "have.has(v.id)" not in page, "an episode tick still keys off videoId"
    assert "EPISODE_RE" in page


def test_a_download_already_running_is_counted_against_the_next_one():
    """Space a download has claimed but not written is in neither `df` nor the cache total. Judging
    the next download on those alone approves things there will be no room for once everything
    already in flight has finished -- which is exactly when the owner would find out."""
    src = _fits_src()
    # 30 GB free, 18 GiB budget, 29 GB already promised to a download in progress.
    busy = ("{diskFree: 30e9, diskTotal: 120e9, cacheUsed: 2e9, committed: 29e9,"
            " cacheSize: 19327352832}")
    assert _run_js(src, f"sizeVerdict(2e9, {busy})") == "nodisk"
    idle = ("{diskFree: 30e9, diskTotal: 120e9, cacheUsed: 2e9, committed: 0,"
            " cacheSize: 19327352832}")
    assert _run_js(src, f"sizeVerdict(2e9, {idle})") == "ok"


def test_committed_bytes_also_count_against_the_budget():
    """A download in flight lands in the cache, so it is already spending the budget."""
    src = _fits_src()
    b = "{diskFree: 200e9, cacheUsed: 2e9, committed: 15e9, cacheSize: 19327352832}"
    assert _run_js(src, f"sizeVerdict(4e9, {b})") == "overbudget"


_RELFILE_SRC = re.compile(r"(const releaseFile = [\s\S]*?\n  \};)")
_EPRE_SRC = re.compile(r"(const EPISODE_RE = [^\n]*)")


def _release_file_src():
    page = _page()
    ep, rf = _EPRE_SRC.search(page), _RELFILE_SRC.search(page)
    assert ep and rf, "releaseFile not found in the page"
    return ep.group(1) + "\n" + rf.group(1)


# Episode 5 is being fetched; episode 6 has boundary spill from it and nothing has asked for it;
# episode 7 is not on this disk at all. All three live in ONE torrent.
_PACK = ("{numFiles: 9, files: ["
         "{index: 4, name: 'Show.S01E05.mkv', progress: 0.3, wanted: true},"
         "{index: 5, name: 'Show.S01E06.mkv', progress: 0.01, wanted: false}]}")


def test_a_release_inside_a_busy_torrent_is_still_offered():
    """A pack is ONE infohash, so a button keyed on torrent identity showed whatever that torrent
    was doing: with episode 5 downloading, every OTHER episode of the pack read "Downloading" and
    was disabled. Nothing had asked for those files -- the torrent was busy, the episode was not.
    """
    src = _release_file_src()
    assert _run_js(src, f"releaseFile({_PACK}, {{}}, {{season:1, episode:5}}).index") == 4
    assert _run_js(src, f"releaseFile({_PACK}, {{}}, {{season:1, episode:6}}).wanted") is False
    # Held in no sense at all: this torrent's business is not this release's state.
    assert _run_js(src, f"releaseFile({_PACK}, {{}}, {{season:1, episode:7}})") is None


def test_an_explicit_fileidx_decides_before_the_episode_number():
    """An addon that points at one file inside a pack has said which; guessing from the episode
    number over the top of that would be second-guessing the only authority there is."""
    src = _release_file_src()
    assert _run_js(src, f"releaseFile({_PACK}, {{fileIdx: 5}}, {{season:1, episode:5}}).index") == 5
    assert _run_js(src, f"releaseFile({_PACK}, {{fileIdx: 8}}, {{}})") is None


def test_a_single_file_torrent_needs_no_matching():
    """That file IS the release. The length of `files` cannot say so on its own -- a pack with one
    episode selected also reports one file -- which is why the torrent's file count travels."""
    src = _release_file_src()
    movie = "{numFiles: 1, files: [{index: 0, name: 'Film.mkv', progress: 1, wanted: true}]}"
    assert _run_js(src, f"releaseFile({movie}, {{}}, {{}}).index") == 0


_DISK = ("{filesFrom: 'disk', files: ["
         "{index: null, name: 'Show.S01E05.mkv', progress: 1, wanted: false}]}")


def test_a_disk_listing_is_not_read_as_the_torrents_own_file_list():
    """After a restart the files come from a directory, which knows only what LANDED -- not the
    torrent's file order, and not how many files it has. Taking the single-file shortcut there
    would answer "yes, that is the release" for every episode of a pack holding one."""
    src = _release_file_src()
    # the episode that is actually on disk
    assert _run_js(src, f"releaseFile({_DISK}, {{}}, {{season:1, episode:5}}).name") \
        == "Show.S01E05.mkv"
    # a different episode of the same pack: not here, whatever the directory holds
    assert _run_js(src, f"releaseFile({_DISK}, {{}}, {{season:1, episode:6}})") is None
    # an addon's file index means nothing against a directory listing
    assert _run_js(src, f"releaseFile({_DISK}, {{fileIdx: 0}}, {{season:1, episode:5}}).name") \
        == "Show.S01E05.mkv"


def test_a_film_on_disk_is_still_recognised():
    """Nothing to match on and one file present: that file is the release. Answering "not held"
    would offer a re-download of something already on the disk."""
    src = _release_file_src()
    film = "{filesFrom: 'disk', files: [{index: null, name: 'Film.2019.mkv', progress: 1}]}"
    assert _run_js(src, f"releaseFile({film}, {{}}, {{}}).name") == "Film.2019.mkv"


def test_a_torrent_with_no_per_file_record_falls_back_to_itself():
    """Nothing in the session knows this torrent's files -- after a restart, say. Answering "not
    held" would offer a re-download of something already on the disk."""
    src = _release_file_src()
    assert _run_js(src, "releaseFile({files: []}, {}, {season:1, episode:5}) === undefined") is True


def test_the_release_button_states_the_file_not_the_torrent():
    page = _page()
    assert "const part = entry ? releaseFile(entry, st, want) : undefined;" in page
    assert "mine.state === 'downloading'" not in page, "the button still reads the torrent's state"


def test_keep_on_a_release_pins_instead_of_downloading_it_again():
    """Since the want/pin split a download does not pin, so a button labelled Keep that called the
    download route kept nothing at all -- it wanted a file already on disk. Both surfaces go
    through one keepTitle now, which is also the only copy of the 409 handling."""
    page = _page()
    assert "async function keepTitle(" in page
    assert "data-keep-hash=" in page
    assert "keepTitle(btn.dataset.keepHash, false)" in page


def test_a_refused_or_warned_button_explains_itself_on_hover():
    """A disabled or red button is a decision made on the owner's behalf. The reason was only in a
    line beneath the release, so hovering the button -- the thing that looked broken -- said
    nothing. Same sentence, both places."""
    page = _page()
    assert "const reason =" in page
    assert 'title="${esc(reason)}"' in page
    # and the line underneath is built from that same sentence, so the two cannot drift
    assert "${esc(reason)}</div>`;" in page


def test_the_on_disk_badge_counts_episodes_not_torrents():
    """A pack is one torrent holding many episodes, so counting entries said "1 on disk" whether
    three episodes were here or nine -- neither a file count nor a season count, but a count of
    something the owner has no reason to think about. It must use the same complete-files rule the
    episode ticks use, or the badge and the picker contradict each other."""
    page = _page()
    body = page[page.index("function onDiskCounts"):page.index("function onDiskEpisodes")]
    assert "e.children" in body, "still counting one per torrent"
    assert "progress" in body, "a part-fetched episode is not one the owner has"
