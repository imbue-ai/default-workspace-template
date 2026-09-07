from flask import Flask
from flask import Response

from share_gateway.session_cookie import PARTITIONED_SESSION_COOKIE_NAME
from share_gateway.session_cookie import SESSION_COOKIE_NAME
from share_gateway.session_cookie import SessionIdentity
from share_gateway.session_cookie import mint_session_cookie_value
from share_gateway.session_cookie import set_session_cookie
from share_gateway.session_cookie import strip_session_cookie
from share_gateway.session_cookie import verify_session_cookie_value
from share_gateway.session_cookie import verify_session_from_cookies

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


def test_set_session_cookie_sets_a_plain_copy_and_a_partitioned_copy() -> None:
    app = Flask(__name__)
    with app.test_request_context():
        response = Response(status=302)
        set_session_cookie(response, "cookie-value", _DOMAIN)
        set_cookies = response.headers.getlist("Set-Cookie")
    by_name = {header.split("=", 1)[0]: header for header in set_cookies}
    assert set(by_name) == {SESSION_COOKIE_NAME, PARTITIONED_SESSION_COOKIE_NAME}
    for header in by_name.values():
        assert "=cookie-value;" in header
        assert "SameSite=None" in header
        assert "Secure" in header
        assert "HttpOnly" in header
        assert f"Domain={_DOMAIN}" in header
    # The plain copy is what a top-level visit (Safari included) keeps; only
    # the iframe copy carries the CHIPS attribute.
    assert "Partitioned" not in by_name[SESSION_COOKIE_NAME]
    assert by_name[PARTITIONED_SESSION_COOKIE_NAME].endswith("; Partitioned")


def test_verify_session_from_cookies_accepts_whichever_copy_verifies() -> None:
    value = mint_session_cookie_value(_SECRET, "bob@example.com", _DOMAIN, is_owner=False)
    expected = SessionIdentity("bob@example.com", is_owner=False)
    assert verify_session_from_cookies(_SECRET, {SESSION_COOKIE_NAME: value}, _DOMAIN) == expected
    assert verify_session_from_cookies(_SECRET, {PARTITIONED_SESSION_COOKIE_NAME: value}, _DOMAIN) == expected
    # A stale plain copy must not mask a valid partitioned one.
    both = {SESSION_COOKIE_NAME: "garbage", PARTITIONED_SESSION_COOKIE_NAME: value}
    assert verify_session_from_cookies(_SECRET, both, _DOMAIN) == expected
    assert verify_session_from_cookies(_SECRET, {}, _DOMAIN) is None
    assert verify_session_from_cookies(_SECRET, {SESSION_COOKIE_NAME: "garbage"}, _DOMAIN) is None


def test_strip_session_cookie_removes_only_ours() -> None:
    header = "a=1; imbue_machine_session=xyz; b=2"
    assert strip_session_cookie(header) == "a=1; b=2"
    both_copies = "a=1; imbue_machine_session=xyz; imbue_machine_session_partitioned=xyz; b=2"
    assert strip_session_cookie(both_copies) == "a=1; b=2"
    assert strip_session_cookie("imbue_machine_session=xyz") == ""
    assert strip_session_cookie("a=1; b=2") == "a=1; b=2"
    assert strip_session_cookie("") == ""
