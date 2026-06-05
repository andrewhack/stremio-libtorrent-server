from stremiosrv.torrent.trackers import DEFAULT_TRACKERS, merge_trackers


def test_defaults_present_when_empty():
    assert merge_trackers() == DEFAULT_TRACKERS


def test_existing_come_first():
    out = merge_trackers(["udp://x/announce"])
    assert out[0] == "udp://x/announce"
    assert DEFAULT_TRACKERS[0] in out


def test_extra_appended_and_deduped():
    out = merge_trackers(["udp://a/announce"], ["udp://a/announce", "udp://b/announce"])
    assert out.count("udp://a/announce") == 1
    assert "udp://b/announce" in out


def test_drops_empty_strings():
    assert "" not in merge_trackers(["", "udp://a/announce"])
