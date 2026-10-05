"""The trusted certificate is taken from what the fetch wrote, never from how the fetch exited.

Stremio's `certificate.js --action fetch` exits 0 even after all five attempts against the
certificate service failed. The entrypoint trusted that exit code and copied a file that was never
written, and under `set -e` the failed copy ended the container: during an outage of that service a
box crash-looped whenever the failures came back quickly, and fell back to a self-signed
certificate only when they were slow enough for the 30 s timeout to fire first.
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from test_cert_reuse import ZONE, _make_pem

ROOT = Path(__file__).resolve().parents[1]
FETCH = ROOT / "docker" / "cert-fetch.sh"
REUSE = ROOT / "docker" / "cert-reuse.sh"
ENTRYPOINT = ROOT / "docker" / "entrypoint.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("sh") is None or shutil.which("openssl") is None,
    reason="needs sh and openssl on PATH",
)


def _fetch(tmp_path: Path, fake: str) -> tuple[int, Path]:
    """Run cert-fetch.sh with `fake` standing in for certificate.js; the fetch directory is
    tmp_path/work, the certificate the server serves is tmp_path/certificates.pem."""
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    (work / "fake.sh").write_text(fake, encoding="utf-8")
    cert = tmp_path / "certificates.pem"
    env = {"PATH": os.environ["PATH"], "CERT_FETCH_DIR": str(work),
           "CERT_FETCH_CMD": "sh fake.sh"}
    proc = subprocess.run(["sh", str(FETCH), str(cert), ZONE], capture_output=True, env=env,
                          timeout=60)
    return proc.returncode, cert


def test_a_fetch_that_wrote_nothing_failed_whatever_it_exited(tmp_path):
    """The outage: the service answered 502, certificate.js gave up and exited 0."""
    rc, cert = _fetch(tmp_path, "echo 'Error fetching certificate'; exit 0\n")
    assert rc != 0
    assert not cert.exists()


def test_a_fetched_wildcard_for_the_zone_is_installed(tmp_path):
    good = tmp_path / "good.pem"
    _make_pem(good, f"DNS:*.{ZONE}", days=90)
    rc, cert = _fetch(tmp_path, f"cp '{good}' certificates.pem\n")
    assert rc == 0
    assert cert.read_bytes() == good.read_bytes()


@pytest.mark.parametrize("written", ["not a certificate", None])
def test_a_fetch_that_wrote_something_unusable_failed(tmp_path, written):
    other = tmp_path / "other.pem"
    _make_pem(other, "DNS:localhost", days=90)
    body = (f"printf '{written}' > certificates.pem\n" if written
            else f"cp '{other}' certificates.pem\n")  # a certificate for another name
    rc, cert = _fetch(tmp_path, body)
    assert rc != 0
    assert not cert.exists()


def test_a_failed_fetch_leaves_the_served_certificate_alone(tmp_path):
    served = tmp_path / "certificates.pem"
    _make_pem(served, f"DNS:*.{ZONE}", days=10)
    before = served.read_bytes()
    rc, _ = _fetch(tmp_path, "exit 0\n")
    assert rc != 0
    assert served.read_bytes() == before


def test_a_file_left_by_an_earlier_fetch_is_not_taken_for_a_new_one(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    _make_pem(work / "certificates.pem", f"DNS:*.{ZONE}", days=90)
    rc, cert = _fetch(tmp_path, "exit 0\n")
    assert rc != 0
    assert not cert.exists()


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
