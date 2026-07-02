"""Mid-stream client-disconnect log suppression: the outermost ASGI wrapper swallows
disconnect-ONLY exception groups (so the noisy 'Exception in ASGI application' clears) while any
group containing a genuine error still propagates and gets logged."""
import asyncio

import pytest

from stremiosrv.app import SuppressClientDisconnect, _all_client_disconnect


def test_cancellation_group_is_disconnect():
    grp = BaseExceptionGroup("x", [asyncio.CancelledError()])
    assert _all_client_disconnect(grp) is True


def test_named_disconnect_leaves_are_disconnect():
    grp = BaseExceptionGroup("x", [ConnectionResetError(), BrokenPipeError()])
    assert _all_client_disconnect(grp) is True


def test_real_error_is_not_disconnect():
    assert _all_client_disconnect(BaseExceptionGroup("x", [RuntimeError("boom")])) is False
    assert _all_client_disconnect(RuntimeError("boom")) is False


def test_mixed_group_is_not_disconnect():
    grp = BaseExceptionGroup("x", [ConnectionResetError(), RuntimeError("boom")])
    assert _all_client_disconnect(grp) is False


def test_wrapper_swallows_disconnect():
    async def app(scope, receive, send):
        raise BaseExceptionGroup("disconnect", [ConnectionResetError()])
    # Must NOT raise for an http disconnect.
    asyncio.run(SuppressClientDisconnect(app)({"type": "http"}, None, None))


def test_wrapper_reraises_real_error():
    async def app(scope, receive, send):
        raise RuntimeError("boom")
    with pytest.raises(RuntimeError):
        asyncio.run(SuppressClientDisconnect(app)({"type": "http"}, None, None))


def test_wrapper_only_suppresses_http():
    async def app(scope, receive, send):
        raise BaseExceptionGroup("x", [ConnectionResetError()])
    with pytest.raises(BaseExceptionGroup):
        asyncio.run(SuppressClientDisconnect(app)({"type": "lifespan"}, None, None))
