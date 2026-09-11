"""The addon learns a title from playback, through the subtitles resource.

Only a download started from the library page writes a label, so everything that reached the box
through ordinary playback had no identity the addon could match -- no row on its title page, films
included. Whenever the app plays anything, from any addon, it asks every installed subtitles addon
for subtitles, passing the video id and the playing file's size and name. That is a join from a real
playback to a real file, not a guess from a folder name, so it is safe to record. The reply is an
empty subtitle list, which changes nothing in the player.
"""
from fastapi.testclient import TestClient

from stremiosrv import cache as cachemod
from stremiosrv.app import create_app
from stremiosrv.config import Settings
from stremiosrv.library import addon_model as am
from stremiosrv.library import labels as labelsmod
from stremiosrv.library import session as sessionmod
from stremiosrv.library import state as statemod

IH = "c3" * 20
OTHER_IH = "d4" * 20
GB = 1073741824
ORIGIN = "https://box.invalid:12470"
LAN = {"X-Forwarded-For": "192.168.1.20"}
EPISODE = "Sample.Show.S03E06.1080p.mkv"


def _cached(**kw):
    """An unlabelled torrent, as state.build reports one that ordinary playback left on disk.
    Neutral names on purpose: this repo is public and carries no media titles."""
    e = {"infoHash": IH, "name": "Sample.Show.S03E06.1080p", "state": "idle", "label": None,
         "files": [{"index": None, "name": EPISODE, "size": 6 * GB, "downloaded": 6 * GB}]}
    e.update(kw)
    return e


def _report(size=6 * GB, filename=EPISODE):
    """The `extra` a subtitles request carries, already split and decoded."""
    extra = {"videoHash": "0123456789abcdef", "videoSize": str(size)}
    if filename is not None:
        extra["filename"] = filename
    return extra


# --- what a player's report teaches ------------------------------------------------------------


def test_the_manifest_asks_for_subtitles_requests_on_films_and_episodes():
    by_name = {r["name"]: r for r in am.manifest("9.9.9")["resources"]}
    assert set(by_name["subtitles"]["types"]) == {"movie", "series"}
    assert by_name["subtitles"]["idPrefixes"] == ["tt"]


def test_an_episode_played_from_the_cache_is_labelled_with_that_episode():
    got = am.learn_labels({"entries": [_cached()]}, "series", "tt0000020:3:6", _report())
    assert got == [(IH, {"metaId": "tt0000020", "type": "series", "season": 3, "episode": 6,
                         "videoId": "tt0000020:3:6"})]


def test_a_film_is_labelled_as_a_film_even_while_it_is_still_arriving():
    """Identity belongs to the torrent, not to how much of it is here: the stream row still waits
    for the file to be complete before it offers anything."""
    e = _cached(files=[{"index": 0, "name": "Sample.Film.mkv", "size": 2 * GB, "downloaded": GB}])
    got = am.learn_labels({"entries": [e]}, "movie", "tt0000021",
                          _report(size=2 * GB, filename="Sample.Film.mkv"))
    assert got == [(IH, {"metaId": "tt0000021", "type": "movie"})]


def test_a_file_of_another_size_or_another_name_teaches_nothing():
    state = {"entries": [_cached()]}
    assert am.learn_labels(state, "series", "tt0000020:3:6", _report(size=6 * GB + 1)) == []
    assert am.learn_labels(state, "series", "tt0000020:3:6", _report(filename="Other.mkv")) == []


def test_the_name_is_compared_without_its_folder():
    """The player reports a bare file name, and so do the engine and the disk listing -- but a path
    on either side must not turn a match into a miss."""
    got = am.learn_labels({"entries": [_cached()]}, "series", "tt0000020:3:6",
                          _report(filename="Sample.Show.S03E06.1080p/" + EPISODE))
    assert [ih for ih, _ in got] == [IH]


def test_a_label_already_there_is_never_replaced():
    """A download from the page writes a label with a name and a poster; the owner chose it."""
    e = _cached(label={"metaId": "tt0000099", "type": "series", "season": 1, "episode": 1,
                       "name": "Chosen"})
    assert am.learn_labels({"entries": [e]}, "series", "tt0000020:3:6", _report()) == []


def test_without_a_file_name_the_size_alone_must_point_at_one_torrent():
    one, two = _cached(), _cached(infoHash=OTHER_IH)
    alone = am.learn_labels({"entries": [one]}, "series", "tt0000020:3:6", _report(filename=None))
    assert [ih for ih, _ in alone] == [IH]
    assert am.learn_labels({"entries": [one, two]}, "series", "tt0000020:3:6",
                           _report(filename=None)) == []


def test_the_same_file_in_two_torrents_labels_both_when_its_name_confirms_it():
    one, two = _cached(), _cached(infoHash=OTHER_IH)
    got = am.learn_labels({"entries": [one, two]}, "series", "tt0000020:3:6", _report())
    assert sorted(ih for ih, _ in got) == sorted([IH, OTHER_IH])


def test_reports_that_do_not_parse_teach_nothing():
    state = {"entries": [_cached()]}
    assert am.learn_labels(state, "series", "tt0000020", _report()) == []      # an episode needs S:E
    assert am.learn_labels(state, "movie", "tt0000020:3:6", _report()) == []   # a film has none
    assert am.learn_labels(state, "series", "kitsu:1:2", _report()) == []      # not a tt id
    assert am.learn_labels(state, "channel", "tt0000020", _report()) == []     # a type never labelled
    assert am.learn_labels(state, "series", "tt0000020:³:6", _report()) == []  # int() would raise
    assert am.learn_labels(state, "series", "tt0000020:3:6", {"videoSize": "12a"}) == []
    assert am.learn_labels(state, "series", "tt0000020:3:6", {"videoSize": "0"}) == []
    assert am.learn_labels(state, "series", "tt0000020:3:6", {}) == []


def test_orphans_and_entries_without_an_infohash_are_never_labelled():
    entries = [_cached(kind="orphan"), _cached(infoHash=None)]
    assert am.learn_labels({"entries": entries}, "series", "tt0000020:3:6", _report()) == []


def test_the_extra_segment_is_split_before_it_is_decoded():
    """stremio-core percent-encodes each extra value as a URI component, so a `&` inside a file
    name arrives as %26 -- split on the real separators first, decode after."""
    assert am.parse_extra("videoHash=abc&videoSize=123&filename=A%20%26%20B.S01E02.mkv") == {
        "videoHash": "abc", "videoSize": "123", "filename": "A & B.S01E02.mkv"}
    assert am.parse_extra("") == {}
    assert am.parse_extra("junk") == {}


def test_once_learned_the_episode_page_offers_the_local_copy():
    e = _cached()
    state = {"entries": [e]}
    [(_ih, label)] = am.learn_labels(state, "series", "tt0000020:3:6", _report())
    assert am.streams_for_meta_id(state, "tt0000020:3:6", ORIGIN) == []
    e["label"] = label
    assert [s["url"] for s in am.streams_for_meta_id(state, "tt0000020:3:6", ORIGIN)] == [
        f"{ORIGIN}/{IH}/0"]


# --- the route, through a real state build and labels.json --------------------------------------


def _client(tmp_path):
    # `client=` is load-bearing -- see test_addon_routes._client: without a loopback peer the guard
    # would 404 everything and every assertion below would pass for the wrong reason.
    return TestClient(create_app(settings=Settings(library_ui=True, cache_root=str(tmp_path))),
                      client=("127.0.0.1", 45678))


def _on_disk(tmp_path, name="Sample.Show.S03E06.1080p", ih=IH, size=4096):
    """What ordinary playback leaves behind: a folder, one video, a name-index entry -- and no
    label, because only a download started from the page writes one."""
    d = tmp_path / name
    d.mkdir()
    (d / f"{name}.mkv").write_bytes(b"x" * size)
    index = cachemod.load_name_index(str(tmp_path))
    index[name] = ih
    cachemod.save_name_index(str(tmp_path), index)
    return f"{name}.mkv", size


def _subs(c, token, video_id, extra, kind="series"):
    return c.get(f"/library/addon/{token}/subtitles/{kind}/{video_id}/{extra}.json", headers=LAN)


def test_playing_an_episode_puts_the_local_copy_on_its_page(tmp_path):
    fname, size = _on_disk(tmp_path)
    c = _client(tmp_path)
    t = sessionmod.ensure_addon_token(str(tmp_path))
    page = f"/library/addon/{t}/stream/series/tt0000030:3:6.json"
    assert c.get(page, headers=LAN).json() == {"streams": []}

    r = _subs(c, t, "tt0000030:3:6",
              f"videoHash=0123456789abcdef&videoSize={size}&filename={fname}")
    assert r.status_code == 200
    assert r.json() == {"subtitles": []}
    assert labelsmod.load(str(tmp_path))[IH]["metaId"] == "tt0000030"

    streams = c.get(page, headers=LAN).json()["streams"]
    assert len(streams) == 1
    assert streams[0]["url"].endswith(f"/{IH}/0")


def test_a_file_name_holding_an_ampersand_still_matches(tmp_path):
    fname, size = _on_disk(tmp_path, name="Sample & Show.S01E02")
    c = _client(tmp_path)
    t = sessionmod.ensure_addon_token(str(tmp_path))
    encoded = fname.replace(" ", "%20").replace("&", "%26")
    _subs(c, t, "tt0000031:1:2", f"videoSize={size}&filename={encoded}")
    assert labelsmod.load(str(tmp_path))[IH]["videoId"] == "tt0000031:1:2"


def test_the_subtitles_route_refuses_like_the_rest_of_the_addon(tmp_path):
    c = _client(tmp_path)
    t = sessionmod.ensure_addon_token(str(tmp_path))
    assert c.get("/library/addon/wrong/subtitles/movie/tt0000032.json",
                 headers=LAN).status_code == 404
    outside = c.get(f"/library/addon/{t}/subtitles/movie/tt0000032.json",
                    headers={"X-Forwarded-For": "203.0.113.9"})
    assert outside.status_code == 404


def test_a_request_that_cannot_teach_anything_does_not_scan_the_disk(tmp_path, monkeypatch):
    """The app asks before it knows the file, and for ids no torrent here could answer. Neither may
    cost a state build: that walks the whole cache directory, once per playback."""
    _on_disk(tmp_path)
    calls = []
    real = statemod.build
    monkeypatch.setattr(statemod, "build", lambda *a, **k: calls.append(1) or real(*a, **k))
    c = _client(tmp_path)
    t = sessionmod.ensure_addon_token(str(tmp_path))
    for path in (f"/library/addon/{t}/subtitles/series/tt0000030:3:6.json",
                 f"/library/addon/{t}/subtitles/series/tt0000030:3:6/videoHash=abc.json",
                 f"/library/addon/{t}/subtitles/series/kitsu:1:2/videoSize=4096.json"):
        r = c.get(path, headers=LAN)
        assert r.status_code == 200, path
        assert r.json() == {"subtitles": []}, path
    assert calls == []
    assert labelsmod.load(str(tmp_path)) == {}


def test_a_label_the_page_wrote_survives_a_playback(tmp_path):
    fname, size = _on_disk(tmp_path)
    labelsmod.put(str(tmp_path), IH, {"metaId": "tt0000077", "type": "series", "season": 1,
                                      "episode": 1, "name": "Chosen"})
    c = _client(tmp_path)
    t = sessionmod.ensure_addon_token(str(tmp_path))
    _subs(c, t, "tt0000030:3:6", f"videoSize={size}&filename={fname}")
    assert labelsmod.load(str(tmp_path))[IH]["metaId"] == "tt0000077"


# --- seeing it work from outside -----------------------------------------------------------------


def test_the_route_counts_what_it_was_asked_and_what_it_learned(tmp_path):
    """Nothing about these requests is logged -- the names are the owner's library -- so counts in
    /stats.json are the only way to tell, from outside, whether the app is calling at all, whether
    it reports a file, and whether that file matched."""
    from stremiosrv import metrics

    metrics.reset()
    fname, size = _on_disk(tmp_path)
    c = _client(tmp_path)
    t = sessionmod.ensure_addon_token(str(tmp_path))
    c.get(f"/library/addon/{t}/subtitles/series/tt0000030:3:6.json", headers=LAN)  # no report yet
    _subs(c, t, "tt0000030:3:6", f"videoSize={size}&filename={fname}")
    _subs(c, t, "tt0000030:3:6", f"videoSize={size}&filename={fname}")  # labelled by now
    s = c.get("/stats.json").json()["playback"]
    assert (s["librarySubtitlesAsks"], s["librarySubtitlesReports"],
            s["libraryLabelsLearned"]) == (3, 2, 1)
    metrics.reset()


def test_a_learned_title_is_counted_in_the_container_log(tmp_path):
    """uvicorn surfaces only its own loggers. Without a handler of its own the count never reached
    `docker logs` -- zero lines on a live box right after a label was learned."""
    import logging

    fname, size = _on_disk(tmp_path)
    c = _client(tmp_path)
    t = sessionmod.ensure_addon_token(str(tmp_path))
    _subs(c, t, "tt0000030:3:6", f"videoSize={size}&filename={fname}")
    log = logging.getLogger("stremiosrv.library.addon")
    assert log.handlers, "no handler: uvicorn will not print this logger's INFO lines"
    assert log.getEffectiveLevel() <= logging.INFO
