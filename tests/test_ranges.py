from stremiosrv.stream.ranges import parse_range


def test_parse_range_basic():
    assert parse_range("bytes=0-1023", 5000) == (0, 1023)


def test_parse_range_open_end():
    assert parse_range("bytes=1000-", 5000) == (1000, 4999)


def test_parse_range_suffix():
    assert parse_range("bytes=-500", 5000) == (4500, 4999)


def test_parse_range_none():
    assert parse_range(None, 5000) == (0, 4999)


def test_parse_range_clamps_end():
    assert parse_range("bytes=0-999999", 5000) == (0, 4999)
