"""The certificate status file, and the one plain log line, written for every attempt."""
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "docker" / "cert-status.sh"
HOST = "192-168-5-124.519b6502d940.stremio.rocks"

pytestmark = pytest.mark.skipif(shutil.which("sh") is None, reason="needs sh")


def _write(tmp_path, *args: str) -> tuple[dict, str]:
    f = tmp_path / "cert-status.json"
    proc = subprocess.run(["sh", str(SCRIPT), str(f), *args], capture_output=True, text=True,
                          encoding="utf-8", timeout=30)
    assert proc.returncode == 0, proc.stderr
    return json.loads(f.read_text(encoding="utf-8")), proc.stdout


def test_waiting_records_every_field(tmp_path):
    nxt = int(time.time()) + 1800
    data, _ = _write(tmp_path, "self-signed", "waiting", "refused",
                     "General HTTPS Certificate DNS Error", HOST, str(nxt))
    assert data["source"] == "self-signed" and data["state"] == "waiting"
    assert data["reason"] == "refused"
    assert data["detail"] == "General HTTPS Certificate DNS Error"
    assert data["host"] == HOST
    assert data["nextTry"] == time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(nxt))
    assert data["lastTry"] and data["writtenAt"]


def test_ok_writes_nulls_and_logs_nothing(tmp_path):
    data, out = _write(tmp_path, "own", "ok", "", "", "", "")
    assert data["source"] == "own" and data["state"] == "ok"
    assert data["reason"] is None and data["detail"] is None and data["host"] is None
    assert data["lastTry"] is None and data["nextTry"] is None
    assert out == ""


def test_a_hostile_detail_still_makes_valid_json(tmp_path):
    data, _ = _write(tmp_path, "self-signed", "waiting", "refused", 'say "no"\\ back\tslash\nend',
                     HOST, str(int(time.time()) + 60))
    assert data["detail"].startswith('say "no"\\ back')


@pytest.mark.parametrize(("state", "reason", "expected"), [
    ("waiting", "refused", "refused " + HOST + " (General HTTPS Certificate DNS Error)"),
    ("waiting", "unavailable", "is not answering"),
    ("waiting", "no-internet", "cannot reach Stremio's certificate service"),
    ("renewing", "refused", "could not renew the trusted certificate"),
])
def test_the_log_line_says_what_and_when(tmp_path, state, reason, expected):
    _, out = _write(tmp_path, "self-signed", state, reason, "General HTTPS Certificate DNS Error",
                    HOST, str(int(time.time()) + 1800))
    assert out.startswith("[cert] ") and expected in out and " UTC" in out
    if state == "waiting":
        assert "no trusted certificate yet" in out and "port 8080" in out


def test_no_temporary_file_is_left_behind(tmp_path):
    _write(tmp_path, "own", "ok", "", "", "", "")
    assert sorted(os.listdir(tmp_path)) == ["cert-status.json"]
