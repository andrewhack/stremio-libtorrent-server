"""The addon's two gates and its routes. A rejected request is a 404, never a 401: the surface does
not exist unless you are the owner."""
import json

from fastapi.testclient import TestClient

from stremiosrv import cache as cachemod
from stremiosrv.app import create_app
from stremiosrv.config import Settings
from stremiosrv.library import labels as labelsmod
from stremiosrv.library import session as sessionmod
from stremiosrv.library import state as statemod

LAN = {"X-Forwarded-For": "192.168.1.20"}


def _client(tmp_path, *, raise_server_exceptions=True, **kw):
    kw.setdefault("library_ui", True)
    kw.setdefault("cache_root", str(tmp_path))
    # `client=` is load-bearing: TestClient's default peer is the literal string "testclient",
    # which is not a loopback address, so client_ip would refuse to read X-Forwarded-For and the
    # guard would 404 every request -- every test in this file would then pass for the wrong
    # reason, including the ones asserting a refusal.
    return TestClient(create_app(settings=Settings(**kw)), client=("127.0.0.1", 45678),
                       raise_server_exceptions=raise_server_exceptions)


def _token(tmp_path):
    return sessionmod.ensure_addon_token(str(tmp_path))


def test_the_manifest_is_served_for_the_right_token(tmp_path):
    c = _client(tmp_path)
    r = c.get(f"/library/addon/{_token(tmp_path)}/manifest.json", headers=LAN)
    assert r.status_code == 200
    assert r.json()["id"] == "org.stremiosrv.library"
    # A manifest with no version is one Stremio will not install, and the server version does not
    # live on Settings -- it comes from the installed package's metadata, as health.py reads it.
    assert isinstance(r.json()["version"], str) and r.json()["version"]


def test_a_wrong_token_is_a_404_not_a_401(tmp_path):
    """A 401 confirms the route exists, which is exactly what the library guard refuses to do."""
    c = _client(tmp_path)
    _token(tmp_path)
    assert c.get("/library/addon/wrong/manifest.json", headers=LAN).status_code == 404


def test_a_non_ascii_token_is_refused_not_a_server_error(tmp_path):
    """hmac.compare_digest raises TypeError on a str with non-ASCII characters -- comparing bytes
    avoids it. A 500 here would tell a prober the route exists, which is exactly what the guard
    exists to prevent (raise_server_exceptions=False so the client sees the real response the app
    would send, rather than TestClient re-raising the unhandled exception)."""
    c = _client(tmp_path, raise_server_exceptions=False)
    _token(tmp_path)
    r = c.get("/library/addon/wrong-é/manifest.json", headers=LAN)
    assert r.status_code == 404


def test_a_client_outside_the_allowed_networks_is_refused(tmp_path):
    c = _client(tmp_path)
    r = c.get(f"/library/addon/{_token(tmp_path)}/manifest.json",
              headers={"X-Forwarded-For": "203.0.113.9"})
    assert r.status_code == 404


def test_the_addon_does_not_exist_when_the_library_flag_is_off(tmp_path):
    c = _client(tmp_path, library_ui=False)
    assert c.get("/library/addon/anything/manifest.json", headers=LAN).status_code == 404


def test_the_catalog_and_its_extra_form_both_answer(tmp_path):
    """Stremio appends extra segments (skip=…) whether or not the manifest asks for them, and a 404
    there empties the row with no error anywhere."""
    c = _client(tmp_path)
    t = _token(tmp_path)
    for path in (f"/library/addon/{t}/catalog/other/library.json",
                 f"/library/addon/{t}/catalog/other/library/skip=100.json"):
        r = c.get(path, headers=LAN)
        assert r.status_code == 200, path
        assert isinstance(r.json()["metas"], list)


def test_the_catalog_skips_by_the_amount_stremio_asks_for(tmp_path):
    """Stremio's paged grid re-requests the same page again once a row passes ~100 entries, asking
    for it with `skip=<n>` in the extra segment -- ignoring it would repeat the same titles."""
    index = {}
    for i in range(3):
        ih = f"{i}" * 40
        name = f"Title.{i}"
        d = tmp_path / name
        d.mkdir()
        (d / f"{name}.mkv").write_bytes(b"x" * 4096)
        index[name] = ih
    cachemod.save_name_index(str(tmp_path), index)
    c = _client(tmp_path)
    t = _token(tmp_path)
    full = c.get(f"/library/addon/{t}/catalog/other/library.json", headers=LAN).json()["metas"]
    skipped = c.get(f"/library/addon/{t}/catalog/other/library/skip=1.json",
                    headers=LAN).json()["metas"]
    assert len(full) == 3
    assert len(skipped) == 2


def test_an_unknown_catalog_is_an_empty_list_not_an_error(tmp_path):
    c = _client(tmp_path)
    r = c.get(f"/library/addon/{_token(tmp_path)}/catalog/other/nope.json", headers=LAN)
    assert r.status_code == 200
    assert r.json() == {"metas": []}


def test_a_malformed_meta_id_is_refused_before_it_reaches_a_lookup(tmp_path):
    c = _client(tmp_path)
    t = _token(tmp_path)
    r = c.get(f"/library/addon/{t}/meta/other/stremiosrv:..%2F..%2Fetc.json", headers=LAN)
    assert r.status_code == 404


def test_a_malformed_meta_id_is_byte_identical_to_a_route_that_does_not_exist(tmp_path):
    """Same guarantee `_guard` already gives the token check, extended to the id parse: a hand
    written `detail` string, however similar to Starlette's own, is a difference a prober can see
    without ever holding a working token. The id here must stay inside one path segment (unlike the
    `..%2F..%2F` fixture above, which Starlette's router rejects before our handler ever runs, by a
    decoded "/" not matching the route at all) so this actually reaches parse_id's `None` branch."""
    c = _client(tmp_path)
    t = _token(tmp_path)
    bad_id = "stremiosrv:" + "z" * 40  # 40 chars but not hex -- parse_id refuses it
    malformed = c.get(f"/library/addon/{t}/meta/other/{bad_id}.json", headers=LAN)
    missing = c.get("/this-route-does-not-exist-anywhere", headers=LAN)
    assert malformed.status_code == missing.status_code == 404
    assert malformed.json() == missing.json()


def test_a_meta_id_we_do_not_hold_is_an_empty_object_not_an_error(tmp_path):
    c = _client(tmp_path)
    t = _token(tmp_path)
    r = c.get(f"/library/addon/{t}/meta/other/stremiosrv:{'a' * 40}.json", headers=LAN)
    assert r.status_code == 200
    assert r.json() == {"meta": {}}


def test_streams_for_an_unknown_tt_id_are_an_empty_list(tmp_path):
    c = _client(tmp_path)
    t = _token(tmp_path)
    r = c.get(f"/library/addon/{t}/stream/series/tt0000009:1:1.json", headers=LAN)
    assert r.status_code == 200
    assert r.json() == {"streams": []}


def test_stream_does_not_build_library_state_for_an_id_it_can_never_answer(tmp_path, monkeypatch):
    """state.build walks the whole cache directory, and Stremio asks every installed addon for
    streams on every title the user opens -- held or not. An id that is neither ours (`stremiosrv:`)
    nor Stremio's own `tt…` shape can never be answered by this addon, so building state for it is a
    full disk scan with no possible use."""
    calls = []

    def _tracking_build(*_a, **_k):
        calls.append(1)
        return {"entries": []}

    monkeypatch.setattr(statemod, "build", _tracking_build)
    c = _client(tmp_path)
    t = _token(tmp_path)
    r = c.get(f"/library/addon/{t}/stream/movie/someotheraddon12345.json", headers=LAN)
    assert r.status_code == 200
    assert r.json() == {"streams": []}
    assert calls == []


def test_the_install_url_is_built_from_the_request(tmp_path):
    """The app may reach the box by IP, by name or through the appliance's address."""
    c = _client(tmp_path)
    t = _token(tmp_path)
    r = c.get(f"/library/addon/{t}/manifest.json",
              headers={**LAN, "Host": "box.invalid:12470", "X-Forwarded-Proto": "https"})
    assert r.status_code == 200
    assert json.loads(r.text)["id"] == "org.stremiosrv.library"


def test_a_refused_request_is_byte_identical_to_a_route_that_does_not_exist(tmp_path):
    """The guard answers every refusal with a 404 so that a registered-but-refused route cannot be
    told apart from one that was never registered at all. A prober cannot read the source -- the
    response bytes are all they have -- so if the two bodies differed by even one character (a
    capital letter, a trailing word) that difference alone would tell them the route exists and is
    worth attacking further."""
    c = _client(tmp_path)
    refused = c.get("/library/addon/wrong/manifest.json", headers=LAN)
    missing = c.get("/this-route-does-not-exist-anywhere", headers=LAN)
    assert refused.status_code == missing.status_code == 404
    assert refused.json() == missing.json()


def test_x_forwarded_proto_from_a_non_loopback_peer_does_not_break_the_route(tmp_path):
    """`client_ip` (netguard.py) trusts `X-Forwarded-For` only when the direct peer is loopback --
    otherwise the header is just something the caller typed. `_origin` applies the same rule to
    `X-Forwarded-Proto`. This client's peer is on the LAN and passes the network guard on its own
    address, without being loopback, so the header must be ignored rather than crash or otherwise
    change what an ordinary unknown-id lookup returns."""
    c = TestClient(create_app(settings=Settings(library_ui=True, cache_root=str(tmp_path))),
                   client=("192.168.1.20", 45678))
    t = _token(tmp_path)
    r = c.get(f"/library/addon/{t}/stream/series/tt0000009:1:1.json",
              headers={"X-Forwarded-Proto": "https"})
    assert r.status_code == 200
    assert r.json() == {"streams": []}


def test_x_forwarded_proto_from_a_non_loopback_peer_does_not_set_the_stream_scheme(tmp_path):
    """The empty-list case above cannot show which scheme `_origin` picked -- an empty list carries
    no URL to inspect. This gives the lookup a matching, labelled title on disk so a real stream URL
    comes back, from the same non-loopback peer with the same forged header, and checks the URL
    itself: if the fix regressed and trusted the header again, this would build `https://…` even
    though the request arrived as plain http.
    """
    ih = "b" * 40
    d = tmp_path / "Sample.Movie.2020"
    d.mkdir()
    (d / "Sample.Movie.2020.mkv").write_bytes(b"x" * 4096)
    cachemod.save_name_index(str(tmp_path), {"Sample.Movie.2020": ih})
    labelsmod.put(str(tmp_path), ih, {"metaId": "tt0000009", "type": "movie", "name": "Sample Movie"})

    c = TestClient(create_app(settings=Settings(library_ui=True, cache_root=str(tmp_path))),
                   client=("192.168.1.20", 45678))
    t = _token(tmp_path)
    r = c.get(f"/library/addon/{t}/stream/movie/tt0000009.json",
              headers={"X-Forwarded-Proto": "https"})
    assert r.status_code == 200
    streams = r.json()["streams"]
    assert len(streams) == 1
    assert streams[0]["url"].startswith("http://")
