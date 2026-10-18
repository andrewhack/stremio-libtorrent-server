"""Stopping the server saves what the next start needs: every torrent's resume data and the DHT
routing table.

Engine.shutdown() does both, and until 1.6.45 nothing called it: a stop kept only what the periodic
saver (every 30 s) had written, while the README said the table is saved "on shutdown" too. The app's
lifespan now runs it when uvicorn stops, before the cache root is given back.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from stremiosrv import cache
from stremiosrv.app import create_app
from stremiosrv.config import Settings


class Engine:
    def __init__(self, order: list, fail: bool = False) -> None:
        self.order, self.fail = order, fail

    def shutdown(self) -> None:
        self.order.append("engine.shutdown")
        if self.fail:
            raise RuntimeError("libtorrent went away")


def _stop(tmp_path, engine, monkeypatch, order) -> None:
    monkeypatch.setattr(cache, "release_owner", lambda root: order.append("release_owner"))
    with TestClient(create_app(settings=Settings(cache_root=str(tmp_path)), engine=engine)):
        pass  # leaving the block is the stop: the lifespan's shutdown half runs


def test_a_stop_saves_through_the_engine_before_giving_the_cache_back(tmp_path, monkeypatch):
    order: list = []
    _stop(tmp_path, Engine(order), monkeypatch, order)
    assert order == ["engine.shutdown", "release_owner"]


def test_a_failing_save_still_gives_the_cache_back(tmp_path, monkeypatch, capsys):
    """The claim must go back whatever happened, or a recreated container waits it out."""
    order: list = []
    _stop(tmp_path, Engine(order, fail=True), monkeypatch, order)
    assert order == ["engine.shutdown", "release_owner"]
    assert "libtorrent went away" in capsys.readouterr().err, "a failed save is said, not swallowed"


@pytest.mark.parametrize("engine", [None])
def test_without_an_engine_a_stop_still_gives_the_cache_back(tmp_path, monkeypatch, engine):
    order: list = []
    _stop(tmp_path, engine, monkeypatch, order)
    assert order == ["release_owner"]
