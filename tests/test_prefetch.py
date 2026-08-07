"""Next-episode prefetch: the pure helpers (no libtorrent, no engine)."""
from stremiosrv.torrent.prefetch import (
    MIN_VIDEO_BYTES,
    TAIL_BYTES,
    head_pieces,
    natural_key,
    next_video_index,
    position_reached,
    tail_pieces,
)

MiB = 1024 * 1024
GiB = 1024 * MiB
PLEN = 4 * MiB


def test_position_reached_gate():
    assert position_reached(900, 1000, 0.9) is True
    assert position_reached(899, 1000, 0.9) is False
    assert position_reached(1000, 1000, 0.9) is True


def test_position_reached_disabled_on_bad_config():
    # A misconfigured fraction must DISABLE the feature, never fire at every position: a prefetch
    # that never runs is a non-event, one that runs constantly is a bandwidth bug.
    assert position_reached(900, 1000, 0.0) is False
    assert position_reached(900, 1000, 1.1) is False
    assert position_reached(900, 1000, -1.0) is False
    assert position_reached(900, 0, 0.9) is False


def test_natural_key_orders_unpadded_numbers():
    assert sorted(["Ep 10.mkv", "Ep 2.mkv", "Ep 1.mkv"], key=natural_key) == [
        "Ep 1.mkv", "Ep 2.mkv", "Ep 10.mkv",
    ]


def _pack(n, size=GiB):
    paths = [f"Show.S01E{i:02d}.mkv" for i in range(1, n + 1)]
    return paths, [size] * n


def test_next_video_index_ordinary_pack():
    paths, sizes = _pack(5)
    assert next_video_index(paths, sizes, 0) == 1
    assert next_video_index(paths, sizes, 3) == 4


def test_next_video_index_last_episode_returns_none():
    paths, sizes = _pack(5)
    assert next_video_index(paths, sizes, 4) is None


def test_next_video_index_single_video_is_a_movie():
    # A one-video torrent has no next file — that is how movies opt out with no metadata involved.
    assert next_video_index(["movie.mkv"], [2 * GiB], 0) is None


def test_next_video_index_follows_natural_order_not_file_order():
    paths = ["Show Ep 10.mkv", "Show Ep 2.mkv", "Show Ep 1.mkv"]
    sizes = [GiB, GiB, GiB]
    assert next_video_index(paths, sizes, 2) == 1   # Ep 1 -> Ep 2
    assert next_video_index(paths, sizes, 1) == 0   # Ep 2 -> Ep 10


def test_next_video_index_skips_small_files():
    paths = ["Show.S01E01.mkv", "sample.mkv", "Show.S01E02.mkv"]
    sizes = [GiB, 40 * MiB, GiB]   # sample is under a quarter of the current episode
    assert next_video_index(paths, sizes, 0) == 2


def test_next_video_index_ignores_non_video_files():
    paths = ["Show.S01E01.mkv", "Show.S01E01.srt", "Show.S01E02.mkv"]
    sizes = [GiB, GiB, GiB]        # even a large .srt must not be treated as an episode
    assert next_video_index(paths, sizes, 0) == 2


def test_next_video_index_none_when_current_not_eligible():
    paths = ["readme.txt", "Show.S01E01.mkv"]
    sizes = [GiB, GiB]
    assert next_video_index(paths, sizes, 0) is None


def test_next_video_index_out_of_range():
    paths, sizes = _pack(2)
    assert next_video_index(paths, sizes, 9) is None
    assert next_video_index(paths, sizes, -1) is None


def test_min_video_bytes_floor_applies_to_tiny_packs():
    paths = ["a1.mkv", "a2.mkv"]
    sizes = [MIN_VIDEO_BYTES - 1, MIN_VIDEO_BYTES - 1]
    assert next_video_index(paths, sizes, 0) is None


def test_head_pieces_fraction():
    pieces = head_pieces(0, 1000 * PLEN, PLEN, 0.05, 10 * GiB)
    assert pieces[0] == 0
    assert pieces[-1] == 49
    assert len(pieces) == 50


def test_head_pieces_respects_the_byte_cap():
    # A 4 GiB episode: 5% would be ~205 MiB, so the 128 MiB cap wins.
    assert len(head_pieces(0, 4 * GiB, PLEN, 0.05, 128 * MiB)) == 32


def test_head_pieces_unaligned_offset_covers_the_shared_first_piece():
    # The file starts mid-piece; that piece must be included or the head is unreadable.
    assert head_pieces(PLEN + 1000, 100 * PLEN, PLEN, 0.05, 10 * GiB)[0] == 1


def test_head_pieces_tiny_file_still_gets_one_piece():
    assert head_pieces(0, 1000, PLEN, 0.05, 10 * GiB) == [0]


def test_head_pieces_disabled_fraction():
    assert head_pieces(0, GiB, PLEN, 0.0, 128 * MiB) == []
    assert head_pieces(0, GiB, PLEN, -0.5, 128 * MiB) == []
    # A "5" meant as "5 percent" (STREMIOSRV_PREFETCH_NEXT_FRACTION is a fraction, not a percentage)
    # must disable the feature like any other misconfiguration, not silently widen towards the whole
    # file bounded only by max_bytes.
    assert head_pieces(0, GiB, PLEN, 1.1, 128 * MiB) == []
    assert head_pieces(0, 0, PLEN, 0.05, 128 * MiB) == []


def test_tail_pieces_covers_the_last_bytes():
    pieces = tail_pieces(0, 100 * PLEN, PLEN)
    assert pieces[-1] == 99
    assert len(pieces) == TAIL_BYTES // PLEN


def test_tail_pieces_file_smaller_than_the_tail_budget():
    assert tail_pieces(0, 1000, PLEN) == [0]
    assert tail_pieces(0, 0, PLEN) == []
