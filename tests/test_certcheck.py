from stremiosrv.certcheck import _parse_enddate, cert_days_left


def test_parse_enddate_future():
    days = _parse_enddate("notAfter=Dec 31 23:59:59 2099 GMT")
    assert days is not None and days > 25000  # decades out


def test_parse_enddate_garbage():
    assert _parse_enddate("notAfter=not a date") is None
    assert _parse_enddate("no-equals-sign") is None


def test_cert_days_left_missing_file():
    assert cert_days_left("/no/such/cert.pem") is None
