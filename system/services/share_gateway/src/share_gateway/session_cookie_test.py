from flask import Flask
from flask import Response

from share_gateway.session_cookie import SessionIdentity
from share_gateway.session_cookie import mint_session_cookie_value
from share_gateway.session_cookie import set_session_cookie
from share_gateway.session_cookie import strip_session_cookie
from share_gateway.session_cookie import verify_session_cookie_value

_DOMAIN = "host-aaaa.bbbb.us1.imbueminds.com"
_SECRET = "signing-secret-77f1"


def test_session_cookie_roundtrips() -> None:
    value = mint_session_cookie_value(_SECRET, "bob@example.com", _DOMAIN, is_owner=False)
    assert verify_session_cookie_value(_SECRET, value, _DOMAIN) == SessionIdentity("bob@example.com", is_owner=False)


def test_session_cookie_carries_owner_flag() -> None:
    value = mint_session_cookie_value(_SECRET, "owner@example.com", _DOMAIN, is_owner=True)
    identity = verify_session_cookie_value(_SECRET, value, _DOMAIN)
    assert identity is not None
    assert identity.email == "owner@example.com"
    assert identity.is_owner is True


def test_session_cookie_rejects_wrong_secret_domain_and_garbage() -> None:
    value = mint_session_cookie_value(_SECRET, "bob@example.com", _DOMAIN, is_owner=False)
    assert verify_session_cookie_value("other-secret", value, _DOMAIN) is None
    assert verify_session_cookie_value(_SECRET, value, "other." + _DOMAIN) is None
    assert verify_session_cookie_value(_SECRET, "garbage", _DOMAIN) is None
    assert verify_session_cookie_value(_SECRET, "", _DOMAIN) is None


def test_set_session_cookie_is_partitioned_samesite_none_secure() -> None:
    app = Flask(__name__)
    with app.test_request_context():
        response = Response(status=302)
        set_session_cookie(response, "cookie-value", _DOMAIN)
        set_cookie = response.headers["Set-Cookie"]
    assert "SameSite=None" in set_cookie
    assert "Secure" in set_cookie
    assert "Partitioned" in set_cookie
    assert f"Domain={_DOMAIN}" in set_cookie


def test_strip_session_cookie_removes_only_ours() -> None:
    header = "a=1; imbue_machine_session=xyz; b=2"
    assert strip_session_cookie(header) == "a=1; b=2"
    assert strip_session_cookie("imbue_machine_session=xyz") == ""
    assert strip_session_cookie("a=1; b=2") == "a=1; b=2"
    assert strip_session_cookie("") == ""
