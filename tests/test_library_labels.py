import json
import sys
import threading

from stremiosrv.library import labels

LABEL = {"metaId": "m1", "videoId": "v1", "type": "series",
         "name": "Placeholder", "season": 1, "episode": 3,
         "poster": "https://example.com/p.jpg"}


def test_missing_file_is_empty(tmp_path):
    assert labels.load(str(tmp_path)) == {}


def test_put_then_load(tmp_path):
    labels.put(str(tmp_path), "AABB", LABEL)
    got = labels.load(str(tmp_path))
    assert got["aabb"]["name"] == "Placeholder"
    assert got["aabb"]["addedAt"] > 0


def test_infohash_key_is_lowercased(tmp_path):
    labels.put(str(tmp_path), "AABB", LABEL)
    assert "aabb" in labels.load(str(tmp_path))
    assert "AABB" not in labels.load(str(tmp_path))


def test_unknown_fields_are_dropped(tmp_path):
    """The payload comes from the browser. Storing whatever it sends would let a compromised page
    park arbitrary data — an authKey, say — in a file on the box."""
    labels.put(str(tmp_path), "aabb", {**LABEL, "evil": "x", "authKey": "secret"})
    stored = labels.load(str(tmp_path))["aabb"]
    assert "evil" not in stored and "authKey" not in stored
    assert "secret" not in (tmp_path / labels.LABELS_FILE).read_text(encoding="utf-8")


def test_put_overwrites(tmp_path):
    labels.put(str(tmp_path), "aabb", LABEL)
    labels.put(str(tmp_path), "aabb", {**LABEL, "episode": 4})
    assert labels.load(str(tmp_path))["aabb"]["episode"] == 4


def test_drop(tmp_path):
    labels.put(str(tmp_path), "aabb", LABEL)
    labels.drop(str(tmp_path), "AABB")
    assert labels.load(str(tmp_path)) == {}


def test_drop_of_absent_entry_is_quiet(tmp_path):
    labels.drop(str(tmp_path), "nope")


def test_corrupt_file_is_empty_not_fatal(tmp_path):
    (tmp_path / labels.LABELS_FILE).write_text("{not json", encoding="utf-8")
    assert labels.load(str(tmp_path)) == {}


def test_write_is_valid_json(tmp_path):
    labels.put(str(tmp_path), "aabb", LABEL)
    json.loads((tmp_path / labels.LABELS_FILE).read_text(encoding="utf-8"))


def test_no_temp_file_left_behind(tmp_path):
    labels.put(str(tmp_path), "aabb", LABEL)
    assert not list(tmp_path.glob("*.tmp"))


def test_concurrent_puts_do_not_lose_entries(tmp_path):
    """`put` is read-modify-write, and the download endpoint runs in uvicorn's threadpool — two
    downloads started together would otherwise race and one label would vanish, leaving a title
    silently displayed as unmatched.

    setswitchinterval is what gives this test teeth: at the default 5 ms the GIL hides the race, so
    an unsynchronised version passes and the test protects nothing.
    """
    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-9)
    try:
        start = threading.Barrier(24)

        def worker(i):
            start.wait()
            labels.put(str(tmp_path), f"{i:040x}", {**LABEL, "episode": i})

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(24)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(labels.load(str(tmp_path))) == 24
    finally:
        sys.setswitchinterval(old)


# --- labels learned at playback, and the file they record (1.6.14) ----------------------------

FILE = {"name": "Sample.Show.S01E03.mkv", "size": 4096}


def test_a_file_record_is_a_basename_and_a_positive_size():
    """labels.json is a plain file on the owner's box and can be edited by hand: anything but this
    exact shape reads as no file, and that label answers as one learned before files were."""
    assert labels.file_record(FILE) == FILE
    assert labels.file_record({**FILE, "extra": 1}) == FILE
    for bad in (None, "a.mkv", [], {"name": "", "size": 5}, {"name": "a.mkv", "size": 0},
                {"name": "a.mkv", "size": True}, {"name": "a.mkv", "size": "5"},
                {"size": 5}, {"name": 7, "size": 5}):
        assert labels.file_record(bad) is None, bad


def test_a_learned_label_is_written_with_its_file(tmp_path):
    root = str(tmp_path)
    assert labels.learn(root, "AABB", {"metaId": "m1", "type": "series", "season": 1,
                                       "episode": 3, "videoId": "m1:1:3", "file": FILE})
    stored = labels.load(root)["aabb"]
    assert stored["file"] == FILE
    assert (stored["metaId"], stored["season"], stored["episode"]) == ("m1", 1, 3)
    assert stored["addedAt"] > 0


def test_a_learned_label_never_replaces_one_written_in_between(tmp_path):
    """The torrent was unlabelled when the state was read, but a download started from the page can
    label it before the learned label is written -- and the page's label is the owner's choice."""
    root = str(tmp_path)
    labels.put(root, "aabb", LABEL)
    assert labels.learn(root, "aabb", {"metaId": "m9", "type": "movie", "file": FILE}) is False
    assert labels.load(root)["aabb"]["metaId"] == "m1"


def test_a_malformed_file_is_left_out_of_a_learned_label(tmp_path):
    root = str(tmp_path)
    assert labels.learn(root, "aabb", {"metaId": "m1", "type": "movie",
                                       "file": {"name": "", "size": 5}})
    assert "file" not in labels.load(root)["aabb"]


def test_a_label_gains_the_file_its_own_video_plays_from(tmp_path):
    """And nothing else in it changes: the page wrote its name and poster, and the owner chose
    them."""
    root = str(tmp_path)
    labels.put(root, "aabb", LABEL)
    before = labels.load(root)["aabb"]
    assert labels.add_file(root, "aabb", {"metaId": "m1", "season": 1, "episode": 3,
                                          "file": FILE})
    assert labels.load(root)["aabb"] == {**before, "file": FILE}


def test_a_file_is_added_to_its_own_videos_label_only_and_only_once(tmp_path):
    root = str(tmp_path)
    labels.put(root, "aabb", LABEL)
    own = {"metaId": "m1", "season": 1, "episode": 3}
    assert labels.add_file(root, "aabb", {**own, "episode": 4, "file": FILE}) is False
    assert labels.add_file(root, "ccdd", {**own, "file": FILE}) is False  # no label at all
    assert labels.add_file(root, "aabb", {**own, "file": {"name": "", "size": 5}}) is False
    assert labels.add_file(root, "aabb", {**own, "file": FILE})
    assert labels.add_file(root, "aabb", {**own, "file": {"name": "b.mkv", "size": 9}}) is False
    assert labels.load(root)["aabb"]["file"] == FILE
    assert "ccdd" not in labels.load(root)


def test_the_page_cannot_set_a_file(tmp_path):
    """Only the server records which file a label means; `put` stores what the browser sends."""
    root = str(tmp_path)
    labels.put(root, "aabb", {**LABEL, "file": FILE})
    assert "file" not in labels.load(root)["aabb"]
