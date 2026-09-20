"""The certificate the container keeps, and the one it replaces.

`docker/entrypoint.sh` asks this before calling the certificate service, so a wrong answer either
serves an expired certificate (TVs refuse it) or calls a third party on every single restart.
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "docker" / "cert-reuse.sh"
HEALTH = Path(__file__).resolve().parents[1] / "src" / "stremiosrv" / "health.py"
ZONE = "519b6502d940.stremio.rocks"

pytestmark = pytest.mark.skipif(
    shutil.which("sh") is None or shutil.which("openssl") is None,
    reason="needs sh and openssl on PATH",
)


def _make_pem(path: Path, san: str, days: int, subj: str = "/CN=test") -> None:
    """A real certificate, in the layout the entrypoint writes: certificate then private key in one
    file. Reading the certificate out of that pairing is part of what has to work."""
    crt, key = str(path) + ".crt", str(path) + ".key"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", str(days),
         "-keyout", key, "-out", crt, "-subj", subj,
         "-addext", f"subjectAltName={san}"],
        check=True, capture_output=True, timeout=30,
    )
    path.write_bytes(Path(crt).read_bytes() + Path(key).read_bytes())


def _keeps(cert, zone: str = ZONE) -> bool:
    proc = subprocess.run(["sh", str(SCRIPT), str(cert), zone],
                          capture_output=True, timeout=30)
    return proc.returncode == 0


def test_a_valid_certificate_for_the_zone_is_kept(tmp_path):
    p = tmp_path / "certificates.pem"
    _make_pem(p, f"DNS:*.{ZONE}", days=60)
    assert _keeps(p)


def test_a_certificate_about_to_expire_is_not_kept(tmp_path):
    """The point of the window: one with three days left is replaced now, while the box still has
    a working certificate to fall back on, rather than on the day it dies."""
    p = tmp_path / "certificates.pem"
    _make_pem(p, f"DNS:*.{ZONE}", days=3)
    assert not _keeps(p)


def test_a_certificate_for_another_name_is_not_kept(tmp_path):
    """The self-signed fallback lands at the same path and never expires. Keeping it would mean
    never fetching the trusted one that TVs need."""
    p = tmp_path / "certificates.pem"
    _make_pem(p, "DNS:localhost", days=3650)
    assert not _keeps(p)


def test_a_missing_certificate_is_not_kept(tmp_path):
    assert not _keeps(tmp_path / "nothing.pem")


def test_an_unreadable_certificate_is_not_kept(tmp_path):
    """Inconclusive has to mean fetch, not keep."""
    p = tmp_path / "certificates.pem"
    p.write_text("not a certificate\n")
    assert not _keeps(p)


def test_an_empty_zone_keeps_nothing(tmp_path):
    """Otherwise the match degenerates and every certificate looks like the right one."""
    p = tmp_path / "certificates.pem"
    _make_pem(p, f"DNS:*.{ZONE}", days=60)
    assert not _keeps(p, zone="")


def test_the_zone_in_the_subject_does_not_count(tmp_path):
    """Only the SAN decides what a TLS client will accept. A certificate that merely mentions the
    zone in its subject cannot serve the magic-DNS host, so keeping it would mean never fetching
    the one that can."""
    p = tmp_path / "certificates.pem"
    _make_pem(p, "DNS:localhost", days=60, subj=f"/CN={ZONE}")
    assert not _keeps(p)


def test_a_near_miss_zone_does_not_count(tmp_path):
    """The dots in the zone are regex wildcards unless the match is a fixed string, so a name one
    character off would otherwise read as ours."""
    p = tmp_path / "certificates.pem"
    _make_pem(p, "DNS:*.519b6502d940Xstremio.rocks", days=60)
    assert not _keeps(p)


def test_the_zone_inside_a_longer_name_does_not_count(tmp_path):
    """evil.<zone>.attacker.example ends in somebody else's domain, so it is not ours however much
    of the zone it repeats."""
    p = tmp_path / "certificates.pem"
    _make_pem(p, f"DNS:evil.{ZONE}.attacker.example", days=60)
    assert not _keeps(p)


def test_a_single_host_inside_the_zone_is_not_kept(tmp_path):
    """Only the wildcard survives an IPADDRESS change, which is the case this check exists for: a
    certificate naming one dashed-IP host stops answering the moment that address moves."""
    p = tmp_path / "certificates.pem"
    _make_pem(p, f"DNS:1-2-3-4.{ZONE}", days=60)
    assert not _keeps(p)


def test_one_matching_name_among_several_is_enough(tmp_path):
    """A SAN usually lists more than one name, and only one of them has to be ours."""
    p = tmp_path / "certificates.pem"
    _make_pem(p, f"DNS:other.example,DNS:*.{ZONE}", days=60)
    assert _keeps(p)


def test_a_certificate_is_replaced_before_the_server_calls_it_degraded(tmp_path):
    """Twenty days is the case that matters: the server's own health check starts reporting the
    certificate degraded at fourteen, so it has to be replaced above that, not below it."""
    p = tmp_path / "certificates.pem"
    _make_pem(p, f"DNS:*.{ZONE}", days=20)
    assert not _keeps(p)


def test_the_window_stays_above_the_health_check_threshold():
    """The two constants live in different languages and cannot import each other. If the window
    ever drops below the alarm, a restart would hand back a certificate the server is already
    calling degraded -- and the restart is the one thing an operator would try."""
    window = int(re.search(r"^RENEW_WITHIN=(\d+)", SCRIPT.read_text(), re.M).group(1))
    warn_days = int(re.search(r"^CERT_WARN_DAYS = (\d+)", HEALTH.read_text(), re.M).group(1))
    assert window > warn_days * 86400, (
        f"reuse window {window}s is not above the {warn_days}-day health alarm"
    )
