"""Regression: /hlsv2 path parameters must not name a file outside the transcode job directory.

`%2e%2e` survives the normalisation most HTTP clients apply, so it arrives at the route as a literal
`..`. Joined onto the job base that resolved to <cache_root>, and `certificates.pem` — the server's
TLS private key — sits directly there. `GET /hlsv2/%2e%2e/certificates.pem` answered 200 with the
key. `filename` had the same hole from the other direction.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from stremiosrv.app import create_app
from stremiosrv.transcode.converter import Converter

SECRET = "-----BEGIN PRIVATE KEY-----test-key-----END PRIVATE KEY-----"


def _client(tmp_path):
    (tmp_path / "certificates.pem").write_text(SECRET)
    (tmp_path / "transcode").mkdir(parents=True, exist_ok=True)
    conv = Converter(str(tmp_path), None)
    app = create_app(converter=conv)
    app.state.settings.cache_root = str(tmp_path)
    return TestClient(app)


def test_encoded_dotdot_in_job_id_cannot_read_the_tls_key(tmp_path):
    r = _client(tmp_path).get("/hlsv2/%2e%2e/certificates.pem")
    assert r.status_code != 200, "path traversal served a file outside the transcode dir"
    assert SECRET not in r.text


def test_encoded_dotdot_uppercase_is_also_rejected(tmp_path):
    r = _client(tmp_path).get("/hlsv2/%2E%2E/certificates.pem")
    assert r.status_code != 200
    assert SECRET not in r.text


def test_encoded_dotdot_in_filename_cannot_escape_the_job_dir(tmp_path):
    r = _client(tmp_path).get("/hlsv2/somejob/%2e%2e%2fcertificates.pem")
    assert r.status_code != 200
    assert SECRET not in r.text


def test_destroy_cannot_delete_outside_the_transcode_dir(tmp_path):
    """/destroy now removes a directory, so its id is the most dangerous parameter here."""
    keep = tmp_path / "stremio-cache"
    keep.mkdir()
    (keep / "payload.bin").write_bytes(b"important")
    client = _client(tmp_path)
    r = client.get("/hlsv2/%2e%2e%2fstremio-cache/destroy")
    assert r.status_code != 200 or (keep / "payload.bin").exists()
    assert (keep / "payload.bin").exists(), "traversal via /destroy deleted a directory outside transcode"
    assert (tmp_path / "certificates.pem").exists()
