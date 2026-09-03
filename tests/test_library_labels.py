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
