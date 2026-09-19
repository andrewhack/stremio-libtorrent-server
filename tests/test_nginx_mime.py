"""nginx must serve the web player's wasm as application/wasm, and compress it.

nginx 1.18's own mime.types has no wasm entry. Without one of ours the module goes out as
application/octet-stream: browsers then compile it only after the whole download, with a console
warning, and gzip skips it, because gzip_types lists types. The mapping lives in a `types` block at
http level, beside the include, where it adds to nginx's list. The same block inside a server
would replace that list -- nginx-locations.inc is included in both servers -- and html, js and css
would all go out as application/octet-stream.
"""
from __future__ import annotations

import pathlib
import re

_DOCKER = pathlib.Path(__file__).resolve().parents[1] / "docker"
_CONF = _DOCKER / "nginx-allinone.conf"
_LOCATIONS = _DOCKER / "nginx-locations.inc"
_WASM = re.compile(r"\btypes\s*\{[^}]*\bapplication/wasm\s+wasm\s*;[^}]*\}")
_TYPES = re.compile(r"\btypes\s*\{")


def _uncommented(path: pathlib.Path) -> str:
    return re.sub(r"#[^\n]*", "", path.read_text(encoding="utf-8"))


def _block_end(text: str, open_brace: int) -> int:
    """Index of the `}` that closes the `{` at `open_brace`. A brace inside a quoted string --
    `return 200 '}'` -- is text, not structure, and a backslash escapes the next character."""
    depth, quote, i = 0, None, open_brace
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise AssertionError("unbalanced braces in the nginx config")


def _http_scopes() -> tuple[str, list[str]]:
    """(the http block's own directives, the body of each server block inside it)."""
    text = _uncommented(_CONF)
    m = re.search(r"\bhttp\s*\{", text)
    assert m, "no http block in nginx-allinone.conf -- re-check this test's premise"
    body = text[m.end():_block_end(text, m.end() - 1)]
    servers = []
    while s := re.search(r"\bserver\s*\{", body):
        end = _block_end(body, s.end() - 1)
        servers.append(body[s.end():end])
        body = body[:s.start()] + body[end + 1:]
    assert servers, "no server block inside http -- re-check this test's premise"
    return body, servers


def test_wasm_is_mapped_at_http_level():
    http_level, _servers = _http_scopes()
    assert _WASM.search(http_level)


def test_gzip_covers_wasm():
    http_level, _servers = _http_scopes()
    m = re.search(r"\bgzip_types\b([^;]*);", http_level)
    assert m, "no gzip_types at http level"
    assert "application/wasm" in m.group(1).split()


def test_no_types_block_in_a_server_scope():
    """A types block in a server, or in the locations file both servers include, would replace
    nginx's whole list for that server instead of adding to it."""
    _http_level, servers = _http_scopes()
    for server in servers:
        assert not _TYPES.search(server)
    assert not _TYPES.search(_uncommented(_LOCATIONS))


def test_a_quoted_brace_is_not_structure():
    """A `}` inside a quoted string used to close the block early, so a types block after it
    escaped the server scope the guard checks."""
    text = "server { return 200 '}'; types { application/wasm wasm; } }"
    assert _block_end(text, text.index("{")) == len(text) - 1
