from stremiosrv.transcode.profiler import detect_profile


def test_detect_returns_str_or_none():
    p = detect_profile()
    assert p is None or isinstance(p, str)
    assert p in (None, "nvenc-linux", "vaapi-renderD128")
