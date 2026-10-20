"""The bundled web player runs hls.js without its Web Worker (1.6.47).

The web build's minified hls.js cannot start its demuxer worker -- the code it builds the worker
from fails with "ReferenceError: e is not defined" -- so every HLS playback starts by falling back
to the inline demuxer. With audio in renditions of its own (1.6.47's multi-track transcode), that
fallback can leave the player with no audio buffer, and the start fails. The image switches the
worker off (docker/web-player-hls-worker-off.sh); these tests run the script on a fake web build.
"""
from __future__ import annotations

import hashlib
import pathlib
import shutil
import subprocess

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCRIPT = _ROOT / "docker" / "web-player-hls-worker-off.sh"
_SH = shutil.which("sh")
needs_sh = pytest.mark.skipif(_SH is None or shutil.which("md5sum") is None,
                              reason="the script needs sh and md5sum")

_HASH = "74efd1a5d75ef3d804abe81c69bd4033c7cd49b1"
_OLD_REV = "165fbc8c67139ea9111ed733d5e5d5c1"
_LOADER_ENTRY = f'{{url:"{_HASH}/scripts/loader.js",revision:"0123456789abcdef0123456789abcdef"}}'
_ON = "debug:!1,enableWorker:!0"
_OFF = "debug:!1,enableWorker:!1"
_MAIN = f"var a=1;var HLS_CONFIG={{{_ON},lowLatencyMode:!1,backBufferLength:30}};var z=2;"


def _build(tmp_path: pathlib.Path, main: str = _MAIN, dirs: tuple = (_HASH,),
           precached: bool = True) -> pathlib.Path:
    build = tmp_path / "build"
    for d in dirs:
        scripts = build / d / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "main.js").write_text(main, encoding="utf-8")
    entries = [_LOADER_ENTRY]
    if precached:
        entries += [f'{{url:"{d}/scripts/main.js",revision:"{_OLD_REV}"}}' for d in dirs]
    build.mkdir(exist_ok=True)
    (build / "service-worker.js").write_text(f'precacheAndRoute([{",".join(entries)}]);',
                                             encoding="utf-8")
    return build


def _run(build: pathlib.Path) -> subprocess.CompletedProcess:
    return subprocess.run([_SH, _SCRIPT.as_posix(), build.as_posix()],
                          capture_output=True, text=True, timeout=30, check=False)


def _main(build: pathlib.Path) -> str:
    return (build / _HASH / "scripts" / "main.js").read_text(encoding="utf-8")


@needs_sh
def test_the_worker_is_switched_off_and_nothing_else_changes(tmp_path):
    build = _build(tmp_path)
    proc = _run(build)
    assert proc.returncode == 0, proc.stderr
    assert _main(build) == _MAIN.replace(_ON, _OFF)


@needs_sh
def test_the_service_worker_precaches_the_new_bundle(tmp_path):
    """With the old revision left in place, a browser with the service worker keeps running the
    bundle it cached, worker and all."""
    build = _build(tmp_path)
    assert _run(build).returncode == 0
    sw = (build / "service-worker.js").read_text(encoding="utf-8")
    rev = hashlib.md5(_main(build).encode("utf-8")).hexdigest()
    assert f'{{url:"{_HASH}/scripts/main.js",revision:"{rev}"}}' in sw
    assert _OLD_REV not in sw
    assert _LOADER_ENTRY in sw


@needs_sh
@pytest.mark.parametrize("main", ["var x={debug:!1,enableWorker:!1};", _MAIN + _MAIN],
                         ids=["setting-missing", "setting-twice"])
def test_the_build_fails_untouched_unless_the_setting_is_found_once(tmp_path, main):
    """A new web build that moved or renamed the setting must stop the image build, not ship a
    player that still starts the worker."""
    build = _build(tmp_path, main=main)
    assert _run(build).returncode != 0
    assert _main(build) == main


@needs_sh
@pytest.mark.parametrize("dirs", [(), (_HASH, "0" * 40)], ids=["no-build", "two-builds"])
def test_the_build_fails_unless_there_is_exactly_one_bundle(tmp_path, dirs):
    assert _run(_build(tmp_path, dirs=dirs)).returncode != 0


@needs_sh
def test_the_build_fails_untouched_when_the_bundle_is_not_precached(tmp_path):
    build = _build(tmp_path, precached=False)
    assert _run(build).returncode != 0
    assert _main(build) == _MAIN


def test_the_image_build_switches_the_worker_off_after_installing_the_loader():
    dockerfile = (_ROOT / "Dockerfile").read_text(encoding="utf-8")
    step = "sh docker/web-player-hls-worker-off.sh /srv/stremio-server/build"
    assert step in dockerfile
    assert dockerfile.index("sh docker/install-web-player-loader.sh") < dockerfile.index(step)
