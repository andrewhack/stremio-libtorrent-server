def pieces_for_range(start: int, end: int, piece_length: int) -> list[int]:
    """Inclusive list of piece indices covering byte range [start, end]."""
    return list(range(start // piece_length, end // piece_length + 1))


def priority_plan(active_piece: int, readahead: int, total_pieces: int) -> dict[int, int]:
    """libtorrent piece priorities (0=skip .. 7=top).

    The playhead piece + a readahead window get top priority; everything else gets a low
    baseline so the engine still completes the torrent without starving the playhead
    ("head & holes").
    """
    plan = {p: 1 for p in range(total_pieces)}
    for p in range(active_piece, min(active_piece + readahead + 1, total_pieces)):
        plan[p] = 7
    return plan
