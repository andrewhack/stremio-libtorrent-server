from stremiosrv.library import authmode


def test_shared_wildcard_forbids_password_login():
    """Every box that fetches the stremio.rocks cert gets the SAME private key, served
    unauthenticated over cleartext HTTP. An on-path attacker can present a valid cert for the box's
    own hostname, so a password form there is a harvesting opportunity."""
    assert authmode.password_login_allowed("DNS:*.519b6502d940.stremio.rocks") is False


def test_shared_wildcard_detected_with_whitespace_and_case():
    assert authmode.password_login_allowed("  dns:*.519B6502D940.stremio.rocks  ") is False


def test_own_cert_allows_password_login():
    assert authmode.password_login_allowed("DNS:stremio.example.com") is True


def test_multi_san_cert_allows_password_login():
    assert authmode.password_login_allowed(
        "DNS:stremio.example.com, DNS:www.example.com") is True


def test_shared_wildcard_among_other_names_still_forbids():
    """A cert that merely INCLUDES the shared name still lets anyone holding the public key answer
    for that name, so an exact whole-string compare would be the wrong test."""
    assert authmode.password_login_allowed(
        "DNS:stremio.example.com, DNS:*.519b6502d940.stremio.rocks") is False


def test_unknown_san_allows_but_is_not_the_shared_one():
    """A SAN we do not recognise is either a bring-your-own cert or a change in Stremio's issuance.
    Allow it — the operator chose it — but it is not the known-public key."""
    assert authmode.password_login_allowed("DNS:*.something-else.stremio.rocks") is True


def test_missing_san_is_treated_as_unsafe():
    """Cannot read the cert -> cannot prove the key is not shared. Fail closed."""
    assert authmode.password_login_allowed(None) is False
    assert authmode.password_login_allowed("") is False


def test_is_shared_cert_distinguishes_unreadable_from_shared():
    """Both refuse the password form, but they are different facts about the operator's setup: one
    says "your key is public", the other says "I could not read your certificate". Reporting the
    wrong one sends them to fix something that is not broken."""
    assert authmode.is_shared_cert("DNS:*.519b6502d940.stremio.rocks") is True
    assert authmode.is_shared_cert("DNS:stremio.example.com") is False
    assert authmode.is_shared_cert(None) is False
    assert authmode.is_shared_cert("") is False
