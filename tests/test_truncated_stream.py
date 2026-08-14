"""End-to-end: a stream that ends early must not spam the ASGI error log.

The unit tests in test_fileserver.py assert `wait_and_read` returns instead of raising, but they
call the generator directly — they never exercise the ASGI/uvicorn layer where the real damage
happens. Once the range route has declared `Content-Length: end-start+1` (playback.py), a
short body makes uvicorn raise `RuntimeError("Response content shorter than Content-Length")`
(httptools_impl.py) *before* it marks the response complete, which surfaces as
'Exception in ASGI application' + an ExceptionGroup traceback, and nginx logs 'upstream
prematurely closed connection'. That is exactly what production showed while the unit tests
were green, so this test drives a real uvicorn server over a real socket.
"""
import contextvars
import logging
import threading
import time

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.responses import StreamingResponse

from stremiosrv.app import (
    _INCOMPLETE_MSG,
    _TRUNCATED_BODY_MSG,
    SuppressClientDisconnect,
    _all_benign_stream_end,
    _DropTruncationFollowup,
    _is_truncated_body,
    _truncated,
)

DECLARED = 100  # what the route promises in Content-Length
SENT = 10       # what the starved stream actually manages to yield


def _truncating_app():
    """Mirrors the playback range route: promises the full range, then the stream ends early
    (peer-starved piece / handle evicted mid-stream) having yielded fewer bytes."""
    app = FastAPI()

    @app.get("/short")
    def short():
        def gen():
            yield b"A" * SENT
            return  # wait_and_read's early `return` — DECLARED-SENT bytes never arrive

        return StreamingResponse(
            gen(),
            status_code=206,
            headers={
                "Content-Length": str(DECLARED),
                "Content-Range": f"bytes 0-{DECLARED - 1}/1000",
                "Accept-Ranges": "bytes",
            },
        )

    return SuppressClientDisconnect(app)


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)


@pytest.fixture
def uvicorn_log():
    """Capture everything uvicorn reports for the request, at ERROR level and above."""
    handler = _Capture()
    logger = logging.getLogger("uvicorn.error")
    logger.addHandler(handler)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)


def _serve(app):
    """Run a real uvicorn server on an ephemeral port; yields the bound port.

    `log_config=None` is load-bearing: uvicorn's default dictConfig re-declares the 'uvicorn.error'
    logger and drops handlers attached before startup, which silently blinds the assertion."""
    config = uvicorn.Config(
        app, host="127.0.0.1", port=0, log_level="error", access_log=False, log_config=None,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    if not server.started:
        server.should_exit = True
        raise RuntimeError("uvicorn did not start")
    return server, thread


def test_truncated_stream_logs_no_asgi_error(uvicorn_log):
    """A deliberately-truncated stream is normal operation on a peer-starved box: the player just
    re-requests the range. It must not produce an ASGI error traceback in the log."""
    server, thread = _serve(_truncating_app())
    port = server.servers[0].sockets[0].getsockname()[1]
    truncated = False
    try:
        try:
            httpx.get(f"http://127.0.0.1:{port}/short", timeout=10)
        except httpx.RemoteProtocolError:
            # "peer closed connection without sending complete message body" — the wire behaviour
            # we intend to keep. The player re-requests the range and succeeds once more is cached.
            truncated = True
        time.sleep(0.3)  # let run_asgi finish logging
    finally:
        server.should_exit = True
        thread.join(timeout=15)

    # Guard: prove the test actually exercised the short-body path rather than passing vacuously.
    assert truncated, "expected a truncated response — the short-body path was never hit"
    errors = [r.getMessage().strip() for r in uvicorn_log.records if r.levelno >= logging.ERROR]
    assert errors == [], f"truncated stream produced ASGI error log: {errors}"


class _FakeHandle:
    """Reports the piece as present, but the file is gone from disk — the evictor removing a torrent
    mid-stream (fileserver.py's `except Exception` path). Production evicted 5 items an hour before
    the ASGI error, so this is the real shape, and it truncates instantly instead of waiting out
    the 120s peer-starvation budget."""

    def has_metadata(self):
        return True

    def is_active(self):
        return True

    def focus_file(self, idx):
        pass

    def refocus(self):
        pass

    def file_size(self, idx):
        return 1_000_000

    def file_path(self, idx):
        return "gone.mkv"

    def piece_length(self):
        return 262144

    def file_offset(self, idx):
        return 0

    def num_pieces(self):
        return 100

    def have_piece(self, i):
        return True  # engine says it's downloaded...

    def boost_piece(self, p, ms):
        pass

    def note_read_position(self, pos, total):
        pass


class _FakeEngine:
    def __init__(self, save_path):
        self._save_path = save_path
        self._h = _FakeHandle()

    def get(self, info_hash):
        return self._h

    def save_path(self):
        return self._save_path  # ...but nothing is here, so open() raises FileNotFoundError

    def active_torrent_count(self):
        return 1

    def note_stream_open(self, h):
        pass

    def note_stream_close(self, h):
        pass


def test_real_playback_route_truncation_logs_no_asgi_error(uvicorn_log, tmp_path):
    """The genuine wiring: the real range route sets Content-Length, the real wait_and_read bails
    out, and the wrapper must keep that off the error log."""
    from stremiosrv.app import create_app

    app = SuppressClientDisconnect(create_app(engine=_FakeEngine(str(tmp_path))))
    server, thread = _serve(app)
    port = server.servers[0].sockets[0].getsockname()[1]
    truncated = False
    try:
        try:
            httpx.get(f"http://127.0.0.1:{port}/{'ab' * 20}/0",
                      headers={"Range": "bytes=0-99999"}, timeout=15)
        except httpx.RemoteProtocolError:
            truncated = True
        time.sleep(0.3)
    finally:
        server.should_exit = True
        thread.join(timeout=15)

    assert truncated, "expected the real route to truncate — the short-body path was never hit"
    errors = [r.getMessage().strip() for r in uvicorn_log.records if r.levelno >= logging.ERROR]
    assert errors == [], f"real playback route produced ASGI error log: {errors}"


# --- the suppression must stay narrow: anything that isn't our own truncation still gets logged ---


def test_truncation_predicate_matches_only_the_short_body_error():
    assert _is_truncated_body(RuntimeError(_TRUNCATED_BODY_MSG)) is True
    # The sibling means we computed a range wrong — a real bug, must NOT be suppressed.
    assert _is_truncated_body(RuntimeError("Response content longer than Content-Length")) is False
    assert _is_truncated_body(RuntimeError("boom")) is False
    # Subclasses of RuntimeError carrying the same text are not uvicorn's error.
    class Sneaky(RuntimeError):
        pass
    assert _is_truncated_body(Sneaky(_TRUNCATED_BODY_MSG)) is False


def test_benign_stream_end_accepts_truncation_and_disconnect():
    assert _all_benign_stream_end(BaseExceptionGroup("x", [RuntimeError(_TRUNCATED_BODY_MSG)]))
    assert _all_benign_stream_end(BaseExceptionGroup("x", [ConnectionResetError()]))
    # Nested groups (anyio wraps more than one level deep under load).
    nested = BaseExceptionGroup("o", [BaseExceptionGroup("i", [RuntimeError(_TRUNCATED_BODY_MSG)])])
    assert _all_benign_stream_end(nested)


def test_benign_stream_end_rejects_real_errors():
    assert _all_benign_stream_end(BaseExceptionGroup("x", [RuntimeError("boom")])) is False
    # A genuine error riding along with a truncation must still propagate.
    mixed = BaseExceptionGroup("x", [RuntimeError(_TRUNCATED_BODY_MSG), ValueError("real")])
    assert _all_benign_stream_end(mixed) is False
    # A bare (ungrouped) error must be judged on its own merits too.
    assert _all_benign_stream_end(RuntimeError("boom")) is False
    assert _all_benign_stream_end(RuntimeError(_TRUNCATED_BODY_MSG)) is True


def test_log_filter_only_drops_the_followup_for_a_truncated_request():
    """The filter is contextvar-scoped: an unrelated incomplete response is still reported."""
    filt = _DropTruncationFollowup()

    def record(msg):
        return logging.LogRecord("uvicorn.error", logging.ERROR, __file__, 0, msg, None, None)

    # No truncation flagged on this context -> uvicorn's complaint is genuine, keep it.
    assert filt.filter(record(_INCOMPLETE_MSG)) is True

    def flagged():
        _truncated.set(True)
        return filt.filter(record(_INCOMPLETE_MSG)), filt.filter(record("something else"))

    dropped, unrelated = contextvars.copy_context().run(flagged)
    assert dropped is False      # our own truncation -> silenced
    assert unrelated is True     # every other uvicorn error still gets through
