"""Repo and image hygiene: where a deployment's own settings live, and what the image needs from
its base.

`.env` is where compose reads a deployment's variables from, and it can hold a VPN private key, so
it must never be committed or sent to the build. `.env.example` documents those variables, and
stays complete because the test reads the compose files themselves.

The entrypoint and the server run programs the base image provides (nginx, node, openssl, curl,
ffmpeg, ...). A base rebuilt without one of them used to produce an image that built fine and failed
at startup, or only when Docker's health check ran; the build now checks for them.
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CHECK = ROOT / "docker" / "check-runtime-tools.sh"
TOOLS = ["nginx", "node", "openssl", "curl", "timeout", "sed", "date", "tr", "ffmpeg", "ffprobe"]

needs_git = pytest.mark.skipif(shutil.which("git") is None or not (ROOT / ".git").exists(),
                               reason="needs a git checkout")
needs_sh = pytest.mark.skipif(shutil.which("sh") is None, reason="needs sh")


@needs_git
def test_a_local_env_file_is_ignored_and_the_example_is_not():
    def ignored(name):
        return subprocess.run(["git", "check-ignore", "-q", name], cwd=ROOT).returncode == 0

    assert ignored(".env")
    assert not ignored(".env.example")


def test_a_local_env_file_never_reaches_the_build_context():
    lines = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert ".env" in [ln.strip() for ln in lines]


def _compose_variables():
    names = set()
    for f in ROOT.glob("compose*.yaml"):
        names |= set(re.findall(r"\$\{([A-Z_][A-Z0-9_]*)", f.read_text(encoding="utf-8")))
    return names


def _example():
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    pairs = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, _, value = line.partition("=")
        pairs[name] = value
    return pairs


def test_the_example_names_every_variable_the_compose_files_read():
    assert _compose_variables(), "found no variables in the compose files"
    assert _compose_variables() <= set(_example())


def test_the_example_carries_no_values():
    """An example with a value in it is one copy-paste from a real key, address or path being
    committed. Every variable is listed empty; what it means is said in its comment."""
    assert all(v == "" for v in _example().values()), _example()


def _stubs(tmp_path, tools):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for t in tools:
        p = bin_dir / t
        p.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        p.chmod(0o755)
    return bin_dir


def _check(bin_dir):
    return subprocess.run([shutil.which("sh"), str(CHECK)], env={"PATH": str(bin_dir)},
                          capture_output=True, text=True, timeout=30)


@needs_sh
def test_the_check_passes_when_the_base_provides_every_tool(tmp_path):
    r = _check(_stubs(tmp_path, TOOLS))
    assert r.returncode == 0, r.stderr


@needs_sh
@pytest.mark.parametrize("gone", ["openssl", "curl", "ffprobe"])
def test_the_check_fails_naming_a_missing_tool(tmp_path, gone):
    r = _check(_stubs(tmp_path, [t for t in TOOLS if t != gone]))
    assert r.returncode != 0
    assert gone in r.stderr


def test_the_image_build_runs_the_check_before_installing_anything():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    step = "sh docker/check-runtime-tools.sh"
    assert step in dockerfile
    assert dockerfile.index("COPY docker ./docker") < dockerfile.index(step)
    assert dockerfile.index(step) < dockerfile.index("RUN uv sync")
