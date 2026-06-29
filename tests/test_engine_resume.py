import time

import pytest

pytestmark = pytest.mark.integration

lt = pytest.importorskip("libtorrent")

# A tiny, legal, well-seeded torrent (Debian netinst). Replace infohash/magnet if the fixture rots.
DEBIAN_MAGNET = (
    "magnet:?xt=urn:btih:6f84758b0ddd8dc05840bf932a77935d8b5b8b93"
    "&dn=debian-12.6.0-amd64-netinst.iso"
)


def test_resume_file_written_and_skips_recheck(tmp_path):
    from stremiosrv.torrent.engine import Engine
    eng = Engine(listen_port=0, cache_root=str(tmp_path))
    h = eng.add(DEBIAN_MAGNET)
    # wait for metadata so save_resume_data has something to persist
    deadline = time.time() + 60
    while not h.has_metadata() and time.time() < deadline:
        time.sleep(0.5)
    assert h.has_metadata(), "metadata never arrived (network?)"
    eng.save_all_resume()
    ih = h.info_hash().lower()
    resume = tmp_path / ".resume" / f"{ih}.fastresume"
    deadline = time.time() + 20
    while not resume.exists() and time.time() < deadline:
        time.sleep(0.5)
    assert resume.exists()
    eng.shutdown()

    # re-add from resume -> must NOT enter a checking state
    eng2 = Engine(listen_port=0, cache_root=str(tmp_path))
    h2 = eng2.add(DEBIAN_MAGNET)
    time.sleep(2)
    state = str(h2.status().state)
    assert "checking" not in state.lower()
    eng2.shutdown()
