"""The trusted certificate is taken from what the fetch wrote, never from how the fetch exited.

Stremio's `certificate.js --action fetch` exits 0 even after all five attempts against the
certificate service failed. The entrypoint trusted that exit code and copied a file that was never
written, and under `set -e` the failed copy ended the container: during an outage of that service a
box crash-looped whenever the failures came back quickly, and fell back to a self-signed
certificate only when they were slow enough for the 30 s timeout to fire first.

A failed fetch also says why, in one REASON<TAB>DETAIL line: certificate.js only says "failed".
"""
import http.server
import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest
from test_cert_reuse import ZONE, _make_pem

ROOT = Path(__file__).resolve().parents[1]
FETCH = ROOT / "docker" / "cert-fetch.sh"
REUSE = ROOT / "docker" / "cert-reuse.sh"
ENTRYPOINT = ROOT / "docker" / "entrypoint.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("sh") is None or shutil.which("openssl") is None or shutil.which("curl") is None,
    reason="needs sh, openssl and curl on PATH",
)


def _closed_port_url() -> str:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}/api/certificateGet"


class _Stub:
    """Stremio's certificate service, answering every POST with one configured reply."""

    def __init__(self, status: int, body: bytes, delay: float = 0.0):
        stub = self
        self.hits = 0

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                stub.hits += 1
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                time.sleep(delay)
                self.send_response(status)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/api/certificateGet"

    def close(self):
        self.server.shutdown()


def _fetch(tmp_path: Path, fake: str, probe_url: str | None = None,
           probe_timeout: str = "15") -> tuple[int, Path, str]:
    """Run cert-fetch.sh with `fake` standing in for certificate.js; the fetch directory is
    tmp_path/work, the certificate the server serves is tmp_path/certificates.pem. The probe goes
    to `probe_url`, by default a closed local port (never the real service)."""
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    (work / "fake.sh").write_text(fake, encoding="utf-8")
    cert = tmp_path / "certificates.pem"
    env = {"PATH": os.environ["PATH"], "CERT_FETCH_DIR": str(work),
           "CERT_FETCH_CMD": "sh fake.sh", "IPADDRESS": "192.168.5.124",
           "CERT_PROBE_URL": probe_url or _closed_port_url(), "CERT_PROBE_TIMEOUT": probe_timeout}
    proc = subprocess.run(["sh", str(FETCH), str(cert), ZONE], capture_output=True, env=env,
                          text=True, encoding="utf-8", timeout=60)
    return proc.returncode, cert, proc.stdout


def test_a_fetch_that_wrote_nothing_failed_whatever_it_exited(tmp_path):
    """The outage: the service answered 502, certificate.js gave up and exited 0."""
    rc, cert, _ = _fetch(tmp_path, "echo 'Error fetching certificate'; exit 0\n")
    assert rc != 0
    assert not cert.exists()


def test_a_fetched_wildcard_for_the_zone_is_installed(tmp_path):
    good = tmp_path / "good.pem"
    _make_pem(good, f"DNS:*.{ZONE}", days=90)
    rc, cert, _ = _fetch(tmp_path, f"cp '{good}' certificates.pem\n")
    assert rc == 0
    assert cert.read_bytes() == good.read_bytes()


@pytest.mark.parametrize("written", ["not a certificate", None])
def test_a_fetch_that_wrote_something_unusable_failed(tmp_path, written):
    other = tmp_path / "other.pem"
    _make_pem(other, "DNS:localhost", days=90)
    body = (f"printf '{written}' > certificates.pem\n" if written
            else f"cp '{other}' certificates.pem\n")  # a certificate for another name
    rc, cert, _ = _fetch(tmp_path, body)
    assert rc != 0
    assert not cert.exists()


def test_a_failed_fetch_leaves_the_served_certificate_alone(tmp_path):
    served = tmp_path / "certificates.pem"
    _make_pem(served, f"DNS:*.{ZONE}", days=10)
    before = served.read_bytes()
    rc, _, _ = _fetch(tmp_path, "exit 0\n")
    assert rc != 0
    assert served.read_bytes() == before


def test_a_file_left_by_an_earlier_fetch_is_not_taken_for_a_new_one(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    _make_pem(work / "certificates.pem", f"DNS:*.{ZONE}", days=90)
    rc, cert, _ = _fetch(tmp_path, "exit 0\n")
    assert rc != 0
    assert not cert.exists()


# --- why a fetch failed ---

def _reason(tmp_path, stub=None, **kw) -> tuple[str, str]:
    rc, _, out = _fetch(tmp_path, "echo noise from certificate.js; exit 0\n",
                        probe_url=stub.url if stub else None, **kw)
    assert rc != 0
    reason, _, detail = out.rstrip("\n").partition("\t")
    return reason, detail


@pytest.mark.parametrize(("status", "body", "reason", "detail"), [
    (546, b'{"error":{"message":"General HTTPS Certificate DNS Error"}}', "refused",
     "General HTTPS Certificate DNS Error"),
    (403, b'{"error":{"message":"forbidden"}}', "refused", "forbidden"),
    (502, b"error code: 502", "unavailable", "HTTP 502"),
    (200, b'{"result":{}}', "other", "HTTP 200"),
])
def test_a_failed_fetch_says_why(tmp_path, status, body, reason, detail):
    stub = _Stub(status, body)
    try:
        assert _reason(tmp_path, stub) == (reason, detail)
        assert stub.hits == 1  # one probe, nothing more
    finally:
        stub.close()


def test_no_connection_is_no_internet(tmp_path):
    assert _reason(tmp_path) == ("no-internet", "")


def test_no_answer_in_time_is_unavailable(tmp_path):
    stub = _Stub(200, b"{}", delay=3)
    try:
        assert _reason(tmp_path, stub, probe_timeout="1") == ("unavailable",
                                                             "no answer within 1s")
    finally:
        stub.close()


def test_a_long_message_is_capped(tmp_path):
    stub = _Stub(546, b'{"error":{"message":"' + b"x" * 400 + b'"}}')
    try:
        assert len(_reason(tmp_path, stub)[1]) == 160
    finally:
        stub.close()


def test_a_non_ascii_message_never_splits_a_character(tmp_path):
    """cut counts bytes: a long multi-byte message was cut mid-character, the status file stopped
    being UTF-8, and /health then read it as missing -- healthy while serving self-signed."""
    # one ASCII byte, then two-byte characters: the 160th byte falls in the middle of one
    stub = _Stub(546, ('{"error":{"message":"x' + "ü" * 200 + '"}}').encode("utf-8"))
    try:
        detail = _reason(tmp_path, stub)[1]  # decoding the output as UTF-8 must not fail
        assert detail.startswith("xü") and len(detail.encode("utf-8")) <= 160
    finally:
        stub.close()


def test_success_prints_nothing_and_asks_nothing_more(tmp_path):
    good = tmp_path / "good.pem"
    _make_pem(good, f"DNS:*.{ZONE}", days=90)
    stub = _Stub(546, b"{}")
    try:
        rc, _, out = _fetch(tmp_path, f"echo noise; cp '{good}' certificates.pem\n",
                            probe_url=stub.url)
        assert rc == 0 and out == "" and stub.hits == 0
    finally:
        stub.close()


# --- keeping a still-valid trusted certificate when the fetch fails ---

def _reuse(cert: Path, window: str | None = None) -> bool:
    args = ["sh", str(REUSE), str(cert), ZONE] + ([window] if window is not None else [])
    return subprocess.run(args, capture_output=True, timeout=30).returncode == 0


def test_a_certificate_inside_the_renewal_window_still_counts_as_valid_at_window_zero(tmp_path):
    """Ten days left is too few to skip the fetch, and plenty to keep serving when it fails."""
    p = tmp_path / "certificates.pem"
    _make_pem(p, f"DNS:*.{ZONE}", days=10)
    assert not _reuse(p)
    assert _reuse(p, "0")


def test_an_expired_certificate_is_not_valid_at_window_zero(tmp_path):
    p = tmp_path / "certificates.pem"
    _make_pem(p, f"DNS:*.{ZONE}", days=1)
    assert not _reuse(p, "86400")  # expires inside a day: the window arithmetic still applies


def test_the_entrypoint_judges_the_fetch_by_its_file():
    text = ENTRYPOINT.read_text(encoding="utf-8")
    assert "node certificate.js --action fetch); then" not in text
    assert "sh /srv/app/docker/cert-fetch.sh" in text
    assert 'cert-reuse.sh "$CERT" "$SROCKS_ZONE" 0' in text
