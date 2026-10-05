"""The loop that keeps asking for the trusted certificate, and switches only when it changed."""
import json
import os
import shutil
import socket
import subprocess
from pathlib import Path

import pytest
from test_cert_reuse import ZONE, _make_pem

ROOT = Path(__file__).resolve().parents[1]
RETRY = ROOT / "docker" / "cert-retry.sh"
HOST = "192-168-5-124." + ZONE

pytestmark = pytest.mark.skipif(
    shutil.which("sh") is None or shutil.which("openssl") is None or shutil.which("curl") is None,
    reason="needs sh, openssl and curl")


def _closed_port_url() -> str:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}/api/certificateGet"


def _run(tmp_path: Path, state: str, fake: str, served: Path | None, attempts: int = 2):
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    (work / "fake.sh").write_text(fake, encoding="utf-8")
    cert = tmp_path / "certificates.pem"
    if served is not None:
        shutil.copy(served, cert)
    status = tmp_path / "cert-status.json"
    marker = tmp_path / "restarted"
    env = {"PATH": os.environ["PATH"], "CERT_FETCH_DIR": str(work), "CERT_FETCH_CMD": "sh fake.sh",
           "CERT_PROBE_URL": _closed_port_url(), "CERT_RETRY_INTERVAL": "0",
           "CERT_RETRY_MAX": str(attempts), "CERT_RESTART_CMD": f"touch {marker.as_posix()}",
           "IPADDRESS": "192.168.5.124"}
    proc = subprocess.run(["sh", str(RETRY), str(cert), ZONE, str(status), state, HOST, "1"],
                          capture_output=True, text=True, encoding="utf-8", env=env, timeout=120)
    assert proc.returncode == 0, proc.stderr
    data = json.loads(status.read_text(encoding="utf-8")) if status.exists() else None
    return marker.exists(), data, proc.stdout


def test_a_failing_service_keeps_the_box_waiting_and_says_so(tmp_path):
    restarted, data, out = _run(tmp_path, "waiting", "exit 0\n", served=None)
    assert not restarted
    assert data["state"] == "waiting" and data["source"] == "self-signed"
    assert data["reason"] == "no-internet" and data["host"] == HOST
    assert out.count("[cert] no trusted certificate yet") == 2


def test_a_certificate_that_arrives_restarts_the_server_once(tmp_path):
    good = tmp_path / "good.pem"
    _make_pem(good, f"DNS:*.{ZONE}", days=90)
    selfsigned = tmp_path / "self.pem"
    _make_pem(selfsigned, "DNS:localhost", days=3650)
    restarted, _, out = _run(tmp_path, "waiting", f"cp '{good.as_posix()}' certificates.pem\n",
                             served=selfsigned, attempts=5)
    assert restarted
    assert "[cert] trusted certificate received -> restarting to serve it" in out
    assert (tmp_path / "certificates.pem").read_bytes() == good.read_bytes()


def test_the_same_certificate_again_never_restarts(tmp_path):
    """The shared wildcard itself near expiry: the service keeps handing back what is served."""
    same = tmp_path / "same.pem"
    _make_pem(same, f"DNS:*.{ZONE}", days=20)
    restarted, data, _ = _run(tmp_path, "renewing", f"cp '{same.as_posix()}' certificates.pem\n",
                              served=same, attempts=3)
    assert not restarted
    assert data["state"] == "renewing" and data["source"] == "stremio.rocks"
    assert data["reason"] == "other"


def test_a_newer_certificate_during_renewal_restarts(tmp_path):
    old = tmp_path / "old.pem"
    _make_pem(old, f"DNS:*.{ZONE}", days=20)
    new = tmp_path / "new.pem"
    _make_pem(new, f"DNS:*.{ZONE}", days=90)
    restarted, _, _ = _run(tmp_path, "renewing", f"cp '{new.as_posix()}' certificates.pem\n",
                           served=old)
    assert restarted
