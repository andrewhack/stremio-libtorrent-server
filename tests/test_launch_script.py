"""The launcher must build a complete `docker run`.

`docker/launch.sh` is what README.md and docs/DEVOPS.md tell an operator to run, and it is one long
backslash-continued command. A blank line anywhere inside that continuation ends the command early
and silently: `docker run` is then called with no image and none of the health/label/log flags, and
the remaining flags are executed as shell commands. That is how the launcher stayed broken from
v0.2.21 to v1.6.11 with every unit test green.

So this runs the real script with a stub `docker` first on PATH and asserts the invocation it
builds, rather than scanning the source for a blank line -- a lexical rule cannot tell a real
continuation from a backslash inside a comment or a string, and flags 25 harmless lines today.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

LAUNCH = Path(__file__).resolve().parents[1] / "docker" / "launch.sh"

# Git Bash provides one on Windows; a machine without it cannot run the launcher either.
pytestmark = pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX sh on PATH")

# Each invocation of the stub is one record: a header line, then one line per argument, so an
# argument containing spaces (--health-cmd's) survives the round trip.
MARK = "--- docker ---"
STUB = f"""#!/bin/sh
{{
  echo "{MARK}"
  for a in "$@"; do echo "$a"; done
}} >> "$PWD/docker-args.txt"
exit 0
"""


def _run(tmp_path: Path) -> subprocess.CompletedProcess:
    (tmp_path / "bin").mkdir()
    stub = tmp_path / "bin" / "docker"
    stub.write_text(STUB, newline="\n")
    stub.chmod(0o755)
    shutil.copyfile(LAUNCH, tmp_path / "launch.sh")

    env = dict(os.environ)
    env.update(
        NAME="stremio-test",
        IMAGE="stremio-test:dev",
        DATA="/data-never-touched",
        IPADDRESS="",  # skip the host-IP probe: the script uses ${IPADDRESS-...}, not :-
    )
    return subprocess.run(
        ["sh", "-c", 'PATH="$PWD/bin:$PATH"; export PATH; exec sh ./launch.sh'],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60,
    )


def _invocations(tmp_path: Path) -> list[list[str]]:
    text = (tmp_path / "docker-args.txt").read_text()
    return [b.strip("\n").split("\n") for b in text.split(MARK) if b.strip()]


def test_the_launcher_runs_the_container_with_health_labels_and_log_limits(tmp_path):
    proc = _run(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "[launch] started stremio-test" in proc.stdout

    runs = [i for i in _invocations(tmp_path) if i[:2] == ["run", "-d"]]
    assert len(runs) == 1, _invocations(tmp_path)
    argv = runs[0]

    assert argv[-1] == "stremio-test:dev"            # the image, and it is the last argument
    assert "--health-cmd" in argv
    assert "--label" in argv and "monitor.enabled=true" in argv
    assert "monitor.health.port=11470" in argv
    assert "--log-opt" in argv
