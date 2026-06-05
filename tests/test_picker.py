from stremiosrv.torrent.picker import pieces_for_range, priority_plan


def test_pieces_for_range():
    assert pieces_for_range(0, 2 * 1024 * 1024 - 1, 1024 * 1024) == [0, 1]


def test_pieces_for_range_single():
    assert pieces_for_range(10, 20, 1024 * 1024) == [0]


def test_priority_plan_head_and_readahead():
    plan = priority_plan(active_piece=10, readahead=3, total_pieces=100)
    assert all(plan[p] == 7 for p in range(10, 14))
    assert plan[50] == 1
    assert plan[0] == 1


def test_priority_plan_clamps_tail():
    plan = priority_plan(active_piece=98, readahead=5, total_pieces=100)
    assert plan[98] == 7 and plan[99] == 7
    assert len(plan) == 100
