import json

import pytest

from stremiosrv.library import stremio_api as api


def _transport(payload):
    def send(url, body, timeout):
        send.seen = (url, json.loads(body.decode()), timeout)
        return json.dumps(payload).encode()
    return send


def test_result_is_returned():
    t = _transport({"result": {"_id": "u1", "email": "a@b.c"}})
    assert api.get_user("k", transport=t)["_id"] == "u1"
    assert t.seen[1] == {"authKey": "k"}


def test_error_body_raises_even_though_http_was_200():
    """The whole point. api.strem.io answers a rejected key with HTTP 200 and an error body, so a
    client that branches on the status code authenticates anybody."""
    t = _transport({"error": {"code": 1, "message": "Session does not exist"}})
    with pytest.raises(api.StremioApiError) as e:
        api.get_user("definitely-not-a-real-key", transport=t)
    assert e.value.code == 1


def test_missing_result_raises():
    with pytest.raises(api.StremioApiError):
        api.get_user("k", transport=_transport({}))


def test_non_object_response_raises():
    with pytest.raises(api.StremioApiError):
        api.get_user("k", transport=_transport([1, 2, 3]))


def test_malformed_json_raises():
    def send(url, body, timeout):
        return b"<html>not json</html>"
    with pytest.raises(api.StremioApiError):
        api.get_user("k", transport=send)


def test_login_sends_email_and_password_and_returns_result():
    t = _transport({"result": {"authKey": "AK", "user": {"_id": "u1"}}})
    assert api.login("a@b.c", "pw", transport=t)["authKey"] == "AK"
    assert t.seen[1] == {"email": "a@b.c", "password": "pw"}


def test_transport_failure_is_wrapped():
    def send(url, body, timeout):
        raise OSError("dns went away")
    with pytest.raises(api.StremioApiError):
        api.get_user("k", transport=send)


def test_error_detail_is_not_leaked_into_the_message_the_caller_shows():
    """Live probe: a wrong email answers `{"code":2,"message":"User not found","wrongEmail":true}`
    — an account-enumeration oracle. This client preserves code/message for LOGGING, so the API
    layer above it must collapse every failure to one opaque response; assert the extra field is
    never carried along as structured data that could be forwarded by accident."""
    t = _transport({"error": {"code": 2, "message": "User not found", "wrongEmail": True}})
    with pytest.raises(api.StremioApiError) as e:
        api.login("a@b.c", "pw", transport=t)
    assert not hasattr(e.value, "wrongEmail")
    assert e.value.code == 2
