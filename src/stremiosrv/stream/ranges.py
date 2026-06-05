def parse_range(header: str | None, total: int) -> tuple[int, int]:
    """Return inclusive (start, end) byte offsets for an HTTP Range header.

    Supports `bytes=A-B`, `bytes=A-` (open end), `bytes=-N` (suffix), and no header
    (whole file). Clamps the end to the last byte.
    """
    if not header or not header.startswith("bytes="):
        return (0, total - 1)
    spec = header[len("bytes="):].split(",")[0].strip()
    start_s, _, end_s = spec.partition("-")
    if start_s == "":  # suffix: bytes=-N -> last N bytes
        n = int(end_s)
        return (max(0, total - n), total - 1)
    start = int(start_s)
    end = int(end_s) if end_s else total - 1
    return (start, min(end, total - 1))
