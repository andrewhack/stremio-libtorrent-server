import shutil
import subprocess

import pytest

from stremiosrv.certcheck import _parse_enddate, cert_days_left, cert_san


def test_parse_enddate_future():
    days = _parse_enddate("notAfter=Dec 31 23:59:59 2099 GMT")
    assert days is not None and days > 25000  # decades out


def test_parse_enddate_garbage():
    assert _parse_enddate("notAfter=not a date") is None
    assert _parse_enddate("no-equals-sign") is None


def test_cert_days_left_missing_file():
    assert cert_days_left("/no/such/cert.pem") is None


def _make_cert(path, san: str) -> None:
    """A real self-signed cert with `san`, so the openssl invocation and output parsing are
    exercised end to end — that pairing is the part most likely to break, and a missing-file test
    proves nothing about it."""
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
         "-keyout", str(path) + ".key", "-out", str(path),
         "-subj", "/CN=test", "-addext", f"subjectAltName={san}"],
        check=True, capture_output=True, timeout=30,
    )


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl not on PATH")
def test_cert_san_reads_a_single_name(tmp_path):
    p = tmp_path / "c.pem"
    _make_cert(p, "DNS:stremio.example.com")
    assert "DNS:stremio.example.com" in cert_san(str(p))


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl not on PATH")
def test_cert_san_reads_multiple_names(tmp_path):
    p = tmp_path / "c.pem"
    _make_cert(p, "DNS:a.example.com,DNS:b.example.com")
    san = cert_san(str(p))
    assert "a.example.com" in san and "b.example.com" in san


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl not on PATH")
def test_cert_san_shared_wildcard_shape_is_rejected_by_authmode(tmp_path):
    """The real end-to-end path: a cert carrying the shared stremio.rocks name must withhold the
    password form. Guards the openssl output format as much as the comparison."""
    from stremiosrv.library.authmode import password_login_allowed

    p = tmp_path / "c.pem"
    _make_cert(p, "DNS:*.519b6502d940.stremio.rocks")
    san = cert_san(str(p))
    # Assert the read SUCCEEDED first. password_login_allowed(None) is also False, so without this
    # the test would pass just as happily if cert_san were completely broken — it could not tell
    # "recognised the shared cert" from "could not read the cert at all".
    assert san is not None, "cert_san failed to read a cert it should have parsed"
    assert "519b6502d940.stremio.rocks" in san
    assert password_login_allowed(san) is False


def test_cert_san_missing_file():
    assert cert_san("/no/such/cert.pem") is None
