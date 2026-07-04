from stremiosrv.torrent.trackers import (
    DEFAULT_TRACKERS,
    is_tracker_url,
    merge_trackers,
    parse_tracker_string,
)


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


def test_defaults_are_valid_and_unique():
    assert DEFAULT_TRACKERS, "default tracker list must not be empty"
    assert all(is_tracker_url(t) for t in DEFAULT_TRACKERS)
    assert len(DEFAULT_TRACKERS) == len(set(DEFAULT_TRACKERS))


# --- merge order: existing -> defaults -> env -> live -> client extra ---


def test_env_and_live_injected_in_order():
    out = merge_trackers(
        ["udp://magnet/announce"],
        ["udp://client/announce"],
        env=["udp://env/announce"],
        live=["udp://live/announce"],
    )
    assert out[0] == "udp://magnet/announce"
    # env comes after the built-in defaults, live after env, client extra last
    assert out.index("udp://env/announce") > out.index(DEFAULT_TRACKERS[-1])
    assert out.index("udp://live/announce") > out.index("udp://env/announce")
    assert out.index("udp://client/announce") > out.index("udp://live/announce")


def test_dedup_across_all_groups():
    dup = DEFAULT_TRACKERS[0]
    out = merge_trackers([dup], [dup], env=[dup], live=[dup])
    assert out.count(dup) == 1


# --- parse_tracker_string ---


def test_parse_handles_comma_space_newline():
    raw = "udp://a/announce, udp://b/announce\nhttps://c/announce\tudp://d/announce"
    assert parse_tracker_string(raw) == [
        "udp://a/announce",
        "udp://b/announce",
        "https://c/announce",
        "udp://d/announce",
    ]


def test_parse_drops_invalid_and_dedupes():
    raw = "udp://a/announce  garbage  # a comment  udp://a/announce  magnet:?xt=urn:btih:zzz"
    assert parse_tracker_string(raw) == ["udp://a/announce"]


def test_parse_empty_is_empty_list():
    assert parse_tracker_string("") == []
    assert parse_tracker_string(None) == []


def test_is_tracker_url_schemes():
    assert is_tracker_url("udp://t/announce")
    assert is_tracker_url("https://t/announce")
    assert is_tracker_url("wss://t")
    assert not is_tracker_url("magnet:?xt=urn:btih:x")
    assert not is_tracker_url("tracker.example.com")
    assert not is_tracker_url("")
