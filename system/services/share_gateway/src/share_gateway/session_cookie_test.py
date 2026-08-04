from share_gateway.session_cookie import mint_session_cookie_value
from share_gateway.session_cookie import strip_session_cookie
from share_gateway.session_cookie import verify_session_cookie_value

_DOMAIN = "host-aaaa.bbbb.us1.imbueminds.com"
_SECRET = "signing-secret-77f1"


def test_session_cookie_roundtrips() -> None:
    value = mint_session_cookie_value(_SECRET, "bob@example.com", _DOMAIN)
    assert verify_session_cookie_value(_SECRET, value, _DOMAIN) == "bob@example.com"


def test_session_cookie_rejects_wrong_secret_domain_and_garbage() -> None:
    value = mint_session_cookie_value(_SECRET, "bob@example.com", _DOMAIN)
    assert verify_session_cookie_value("other-secret", value, _DOMAIN) is None
    assert verify_session_cookie_value(_SECRET, value, "other." + _DOMAIN) is None
    assert verify_session_cookie_value(_SECRET, "garbage", _DOMAIN) is None
    assert verify_session_cookie_value(_SECRET, "", _DOMAIN) is None


def test_strip_session_cookie_removes_only_ours() -> None:
    header = f"a=1; imbue_machine_session=xyz; b=2"
    assert strip_session_cookie(header) == "a=1; b=2"
    assert strip_session_cookie("imbue_machine_session=xyz") == ""
    assert strip_session_cookie("a=1; b=2") == "a=1; b=2"
    assert strip_session_cookie("") == ""
