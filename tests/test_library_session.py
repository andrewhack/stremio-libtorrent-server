import json
import os

import pytest

from stremiosrv.library import session as S

USER = {"_id": "user-1", "email": "owner@example.com"}
OTHER = {"_id": "user-2", "email": "someone@example.com"}


def test_first_sign_in_pins_the_owner(tmp_path):
    assert S.claim_owner(str(tmp_path), USER, "") == "user-1"
    assert S.load_state(str(tmp_path))["owner_id"] == "user-1"


def test_second_account_is_rejected(tmp_path):
    S.claim_owner(str(tmp_path), USER, "")
    with pytest.raises(S.OwnerMismatch):
        S.claim_owner(str(tmp_path), OTHER, "")


def test_same_owner_signs_in_again(tmp_path):
    S.claim_owner(str(tmp_path), USER, "")
    assert S.claim_owner(str(tmp_path), USER, "") == "user-1"


def test_configured_owner_accepts_id_or_email(tmp_path):
    assert S.claim_owner(str(tmp_path), USER, "user-1") == "user-1"
    assert S.claim_owner(str(tmp_path), USER, "owner@example.com") == "user-1"


def test_configured_owner_rejects_a_stranger(tmp_path):
    with pytest.raises(S.OwnerMismatch):
        S.claim_owner(str(tmp_path), OTHER, "owner@example.com")


def test_configured_owner_overrides_a_wrongly_pinned_file(tmp_path):
    """The env var is the operator speaking now; a stale pin must not lock them out."""
    S.claim_owner(str(tmp_path), OTHER, "")
    assert S.claim_owner(str(tmp_path), USER, "user-1") == "user-1"


def test_account_with_no_id_is_rejected(tmp_path):
    """A malformed user object must not claim the box, and must not pin an empty owner — which
    would leave owner_matches() returning True for everyone thereafter."""
    with pytest.raises(S.OwnerMismatch):
        S.claim_owner(str(tmp_path), {"email": "owner@example.com"}, "")
    assert S.load_state(str(tmp_path))["owner_id"] == ""


def test_email_is_never_persisted(tmp_path):
    S.claim_owner(str(tmp_path), USER, "")
    raw = (tmp_path / S.STATE_FILE).read_text(encoding="utf-8")
    assert "owner@example.com" not in raw


def test_session_round_trip(tmp_path):
    sid = S.new_session(str(tmp_path))
    assert S.verify_session(str(tmp_path), sid)
    S.drop_session(str(tmp_path), sid)
    assert not S.verify_session(str(tmp_path), sid)


def test_unknown_session_is_rejected(tmp_path):
    assert not S.verify_session(str(tmp_path), "not-a-session")
    assert not S.verify_session(str(tmp_path), "")


def test_expired_session_is_rejected_and_reaped(tmp_path):
    sid = S.new_session(str(tmp_path), ttl=-1)
    assert not S.verify_session(str(tmp_path), sid)
    assert S.load_state(str(tmp_path))["sessions"] == {}


def test_sessions_survive_a_restart(tmp_path):
    sid = S.new_session(str(tmp_path))
    assert S.verify_session(str(tmp_path), sid)  # fresh read from disk each call


def test_corrupt_state_file_does_not_crash(tmp_path):
    (tmp_path / S.STATE_FILE).write_text("{not json", encoding="utf-8")
    assert S.load_state(str(tmp_path)) == {"owner_id": "", "sessions": {}}


def test_session_ids_are_unguessable(tmp_path):
    ids = {S.new_session(str(tmp_path)) for _ in range(20)}
    assert len(ids) == 20
    assert all(len(i) >= 32 for i in ids)


def test_saved_state_is_valid_json(tmp_path):
    S.claim_owner(str(tmp_path), USER, "")
    json.loads((tmp_path / S.STATE_FILE).read_text(encoding="utf-8"))


def test_no_temp_file_left_behind(tmp_path):
    S.claim_owner(str(tmp_path), USER, "")
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are not enforced on Windows")
def test_state_file_is_owner_only(tmp_path):
    """The file holds live session ids: anything that can read it can impersonate the owner.

    Skipped on Windows, so on a Windows dev box this property is asserted by nobody. It was checked
    once by hand on linux (python:3.12-slim): the same os.open(0o600) + os.replace sequence
    save_state uses yields mode 0o600, group/world bits 0o0. The skip hides a platform gap, not an
    unverified claim — but if save_state's write changes, re-run it on linux rather than trusting
    a green Windows run.
    """
    S.claim_owner(str(tmp_path), USER, "")
    mode = os.stat(tmp_path / S.STATE_FILE).st_mode & 0o777
    assert mode & 0o077 == 0, f"state file is group/world readable: {mode:o}"


def test_concurrent_sign_ins_keep_the_owner_pin_and_every_session():
    """The auth endpoints run in uvicorn's threadpool, so these mutators really are called
    concurrently. Unsynchronised, 16 concurrent new_session calls raised PermissionError from
    os.replace on Windows (every thread wrote the same `.tmp`), and where it did not raise it left
    invalid JSON — at which point load_state falls back to a blank owner_id, the pin is GONE, and
    the next account to sign in claims the box.

    setswitchinterval is what makes this catch the regression rather than describe it.
    """
    import sys
    import tempfile
    import threading

    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-9)
    try:
        d = tempfile.mkdtemp()
        S.claim_owner(d, USER, "")
        start = threading.Barrier(16)
        sids = []
        guard = threading.Lock()

        def worker():
            start.wait()
            sid = S.new_session(d)
            with guard:
                sids.append(sid)

        threads = [threading.Thread(target=worker) for _ in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        state = S.load_state(d)
        assert state["owner_id"] == "user-1", "owner pin lost to a concurrent write"
        assert len(sids) == 16, "a sign-in raised instead of returning"
        assert len(state["sessions"]) == 16, "sessions lost to a concurrent write"
    finally:
        sys.setswitchinterval(old)
