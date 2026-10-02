"""The bundled web player's loader seeds the streaming-server URL once, then keeps the user's pick.

The web build's own loader.js re-applied the seeded URL (SERVER_URL, via localStorage.json) every
5 s and reloaded the page, so a URL picked in Settings snapped back within seconds; with no
SERVER_URL the seed is 127.0.0.1:11470, which traps a browser on another machine. The image now
installs docker/web-player-loader.js over it (docker/install-web-player-loader.sh).

The loader tests run it in a fake browser under node (tests/fixtures/web_player_loader_harness.js);
the install tests run the shell script on a fake web build. Each set skips where its tool is absent.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import re
import shutil
import subprocess

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_DOCKER = _ROOT / "docker"
_LOADER = _DOCKER / "web-player-loader.js"
_INSTALL = _DOCKER / "install-web-player-loader.sh"
_HARNESS = pathlib.Path(__file__).resolve().parent / "fixtures" / "web_player_loader_harness.js"

_NODE = shutil.which("node")
_SH = shutil.which("sh")
needs_node = pytest.mark.skipif(_NODE is None, reason="node runs the loader; not installed here")
needs_sh = pytest.mark.skipif(_SH is None or shutil.which("md5sum") is None,
                              reason="the install script needs sh and md5sum")

SEED = "https://stremio.example:12470/"
OLD_SEED = "https://old.example:12470/"
APP_DEFAULT = "http://127.0.0.1:11470/"
PICKED = "http://192.0.2.10:11470/"
OTHER = "http://192.0.2.20:11470/"
MARK = "stremiosrv_seeded_server_url"
FAR = "2099-12-31T23:59:59.999Z"


def _profile(url: str) -> dict:
    return {"auth": None, "addons": [{"manifest": {"id": "seeded.addon"}}],
            "settings": {"interfaceLanguage": "eng", "streamingServerUrl": url}}


def _seed_file(url: str = SEED) -> dict:
    """localStorage.json as the entrypoint leaves it: every 127.0.0.1:11470 replaced by SERVER_URL."""
    return {"schema_version": 16, "installation_id": "seeded",
            "streaming_server_urls": {"uid": None, "items": {url: FAR}},
            "profile": _profile(url)}


def _run(storage: dict | None = None, after: tuple = (), seed_file: dict | None = None,
         env_ok: bool = True, origin: str = "http://stremio.example:8080/") -> dict:
    scenario = {"seedFile": seed_file or _seed_file(), "envOk": env_ok, "origin": origin,
                "storage": storage or {}, "afterLoad": list(after)}
    proc = subprocess.run([_NODE, str(_HARNESS), str(_LOADER)], input=json.dumps(scenario),
                          capture_output=True, text=True, timeout=30, check=False)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _url(result: dict) -> str:
    return result["storage"]["profile"]["settings"]["streamingServerUrl"]


def _servers(result: dict) -> set[str]:
    return set(result["storage"]["streaming_server_urls"]["items"])


# --- the loader -------------------------------------------------------------------------------

@needs_node
def test_a_first_visit_is_seeded_without_a_reload():
    r = _run()
    assert _url(r) == SEED
    assert _servers(r) == {SEED}
    assert r["storage"]["profile"]["addons"] == _profile(SEED)["addons"]  # the seed file's own
    assert r["storage"]["schema_version"] == 16
    assert r["reloads"] == 0


@needs_node
def test_a_url_picked_after_the_seed_is_kept():
    """The bug: the old loader put the seed back within 5 s and reloaded the page."""
    r = _run(after=({"set": ["profile", _profile(PICKED)]}, "tick", "tick"))
    assert _url(r) == PICKED
    assert r["reloads"] == 0


@needs_node
def test_a_url_picked_in_an_earlier_session_is_kept():
    r = _run(storage={"profile": _profile(PICKED), MARK: SEED,
                      "streaming_server_urls": {"uid": None, "items": {SEED: FAR, PICKED: FAR}}},
             after=("tick",))
    assert _url(r) == PICKED
    assert r["reloads"] == 0


@needs_node
def test_a_changed_server_url_is_applied_once_then_left_alone():
    first = _run(storage={"profile": _profile(PICKED), MARK: OLD_SEED,
                          "streaming_server_urls": {"uid": None,
                                                    "items": {OLD_SEED: FAR, PICKED: FAR}}})
    assert _url(first) == SEED
    assert first["storage"][MARK] == SEED
    assert first["reloads"] == 1
    # the page reloads; after that, a pick sticks again
    again = _run(storage=first["storage"], after=({"set": ["profile", _profile(OTHER)]}, "tick"))
    assert _url(again) == OTHER
    assert again["reloads"] == 0


@needs_node
def test_the_apps_own_default_is_replaced_by_the_seed():
    """The app writes 127.0.0.1:11470 when it boots before the loader has seeded it, and after a
    profile reset. That is never this server, so the seed goes back in."""
    r = _run(storage={"profile": _profile(SEED), MARK: SEED,
                      "streaming_server_urls": {"uid": None, "items": {SEED: FAR}}},
             after=({"set": ["profile", _profile(APP_DEFAULT)]}, "tick"))
    assert _url(r) == SEED
    assert r["reloads"] == 1


@needs_node
def test_without_server_url_the_default_seed_does_not_trap_the_user():
    r = _run(seed_file=_seed_file(APP_DEFAULT))
    assert _url(r) == APP_DEFAULT
    picked = _run(seed_file=_seed_file(APP_DEFAULT), storage=r["storage"],
                  after=({"set": ["profile", _profile(PICKED)]}, "tick", "tick"))
    assert _url(picked) == PICKED
    assert picked["reloads"] == 0


@needs_node
def test_seeding_keeps_the_servers_the_user_saved():
    r = _run(storage={"profile": _profile(APP_DEFAULT),
                      "streaming_server_urls": {"uid": None, "items": {PICKED: FAR, OTHER: FAR}}})
    assert _url(r) == SEED
    assert _servers(r) == {SEED, PICKED, OTHER}


@needs_node
def test_without_server_url_env_the_page_origin_is_the_seed():
    r = _run(env_ok=False, origin="http://lan-box:8080/#/settings")
    assert _url(r) == "http://lan-box:8080/"
    assert "http://lan-box:8080/" in _servers(r)
    assert r["reloads"] == 0


@needs_node
def test_a_browser_the_old_loader_already_seeded_needs_no_reload():
    r = _run(storage={"profile": _profile(SEED),
                      "streaming_server_urls": {"uid": None, "items": {SEED: FAR}}},
             after=("tick",))
    assert _url(r) == SEED
    assert r["storage"][MARK] == SEED
    assert r["reloads"] == 0


@needs_node
def test_the_loader_keeps_checking_for_the_apps_default():
    r = _run()
    assert r["intervals"] == 1


# --- the install script -----------------------------------------------------------------------

_HASH = "74efd1a5d75ef3d804abe81c69bd4033c7cd49b1"
_OLD_REV = "052c685047bfdf1754270bd047d1d4c1"
_MAIN_ENTRY = f'{{url:"{_HASH}/scripts/main.js",revision:"0123456789abcdef0123456789abcdef"}}'


def _build(tmp_path: pathlib.Path, dirs: tuple = (_HASH,), precached: bool = True) -> pathlib.Path:
    build = tmp_path / "build"
    for d in dirs:
        scripts = build / d / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "loader.js").write_text("the web build's own loader", encoding="utf-8")
        (scripts / "loader.js.map").write_text("{}", encoding="utf-8")
        (scripts / "main.js").write_text("main", encoding="utf-8")
    entries = [_MAIN_ENTRY]
    if precached:  # each build's loader precached, so only the one-loader rule can refuse two
        entries += [f'{{url:"{d}/scripts/loader.js",revision:"{_OLD_REV}"}}' for d in dirs]
    build.mkdir(exist_ok=True)
    (build / "service-worker.js").write_text(f'precacheAndRoute([{",".join(entries)}]);',
                                             encoding="utf-8")
    return build


def _install(build: pathlib.Path) -> subprocess.CompletedProcess:
    return subprocess.run([_SH, _INSTALL.as_posix(), build.as_posix(), _LOADER.as_posix()],
                          capture_output=True, text=True, timeout=30, check=False)


@needs_sh
def test_install_replaces_the_loader_and_its_precache_revision(tmp_path):
    build = _build(tmp_path)
    proc = _install(build)
    assert proc.returncode == 0, proc.stderr
    target = build / _HASH / "scripts" / "loader.js"
    assert target.read_bytes() == _LOADER.read_bytes()
    assert not (build / _HASH / "scripts" / "loader.js.map").exists()
    sw = (build / "service-worker.js").read_text(encoding="utf-8")
    rev = hashlib.md5(_LOADER.read_bytes()).hexdigest()  # workbox's own revision scheme
    assert f'{{url:"{_HASH}/scripts/loader.js",revision:"{rev}"}}' in sw
    assert _OLD_REV not in sw
    assert _MAIN_ENTRY in sw


@needs_sh
@pytest.mark.parametrize("dirs", [(), (_HASH, "0" * 40)], ids=["no-build", "two-builds"])
def test_install_fails_unless_there_is_exactly_one_loader(tmp_path, dirs):
    proc = _install(_build(tmp_path, dirs=dirs))
    assert proc.returncode != 0


@needs_sh
def test_install_fails_before_touching_anything_when_the_loader_is_not_precached(tmp_path):
    """A service worker that still caches the old loader would keep serving it; refuse instead."""
    build = _build(tmp_path, precached=False)
    proc = _install(build)
    assert proc.returncode != 0
    assert (build / _HASH / "scripts" / "loader.js").read_text(encoding="utf-8") == \
        "the web build's own loader"


# --- wiring -----------------------------------------------------------------------------------

def test_the_image_build_installs_the_loader():
    dockerfile = (_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert ("sh docker/install-web-player-loader.sh /srv/stremio-server/build "
            "docker/web-player-loader.js") in dockerfile


def test_browsers_revalidate_the_loader_on_every_load():
    """The loader keeps its URL across image releases, so without this a browser can run an older
    copy for days on heuristic caching. The first regex location that matches it decides."""
    path = f"/{_HASH}/scripts/loader.js"
    text = (_DOCKER / "nginx-locations.inc").read_text(encoding="utf-8")
    regex_lines = [ln for ln in text.splitlines() if re.match(r"\s*location\s+~", ln)]
    matching = [ln for ln in regex_lines
                if re.search(re.match(r'\s*location\s+~\*?\s*"?([^"\s]+)"?', ln).group(1), path)]
    assert matching, "no regex location serves the loader"
    assert 'add_header Cache-Control "no-cache"' in matching[0]
    assert "proxy_pass" not in matching[0]
