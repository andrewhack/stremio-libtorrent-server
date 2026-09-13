"""The bundled web player must keep applying the server URL the entrypoint seeds.

loader.js (in the web build) sends `HEAD server_url.env` and applies localStorage.json's server URL
only when that answers 2xx; otherwise it ignores SERVER_URL and uses the page's own origin. Until
1.6.7 no such file existed and nginx answered the HEAD with the web player's index.html (200) --
the seed worked by accident. Once unknown paths 404, the file has to exist.
"""
from __future__ import annotations

import pathlib
import re

_ENTRYPOINT = pathlib.Path(__file__).resolve().parents[1] / "docker" / "entrypoint.sh"
_WRITE = ": > /srv/stremio-server/build/server_url.env"


def _seed_block() -> str:
    text = _ENTRYPOINT.read_text(encoding="utf-8")
    m = re.search(r'if \[ -f "\$SEED_SRC" \]; then\n(.*?)\nfi\n', text, re.S)
    assert m, "the localStorage seed block moved -- re-check this test's premise"
    return m.group(1)


def test_seed_writes_server_url_env_into_the_web_build():
    assert _WRITE in _seed_block()


def test_server_url_env_is_written_whether_or_not_server_url_is_set():
    """Today's behaviour, pinned: the player applies the seeded URL in both cases (SERVER_URL's, or
    the default 127.0.0.1 one). Writing the file only when SERVER_URL is set would change what an
    install without SERVER_URL sees."""
    block = _seed_block()
    assert block.index(_WRITE) < block.index('if [ -n "${SERVER_URL}" ]; then')


def test_the_app_gets_the_server_url_the_player_is_seeded_with():
    """/proxy counts a page on SERVER_URL's host as the server's own (1.6.9). The IPADDRESS branch
    sets SERVER_URL inside this script, and a variable set there reaches uvicorn only if exported
    -- after the seed block, which may append a slash."""
    text = _ENTRYPOINT.read_text(encoding="utf-8")
    assert "export SERVER_URL\n" in text
    export = text.index("export SERVER_URL\n")
    assert text.index('if [ -f "$SEED_SRC" ]; then') < export < text.index("uvicorn stremiosrv.app")
