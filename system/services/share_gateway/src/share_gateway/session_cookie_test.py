from datetime import datetime
from datetime import timedelta
from datetime import timezone

import jwt
from flask import Flask
from flask import Response

from share_gateway.identity import RequesterIdentity
from share_gateway.session_cookie import PARTITIONED_SESSION_COOKIE_NAME
from share_gateway.session_cookie import SESSION_COOKIE_NAME
from share_gateway.session_cookie import mint_session_cookie_value
from share_gateway.session_cookie import set_session_cookie
from share_gateway.session_cookie import strip_session_cookie
from share_gateway.session_cookie import verify_session_cookie_value
from share_gateway.session_cookie import verify_session_from_cookies
from share_gateway.testing import set_cookies_by_name

_DOMAIN = "host-aaaa.bbbb.us1.imbueminds.com"
_SECRET = "signing-secret-77f1"
_BOB = RequesterIdentity(user_id="user-bob-4471", email="bob@example.com", is_owner=False)
_OWNER = RequesterIdentity(
    user_id="user-owner-9c21",
    email="owner@example.com",
    is_owner=True,
    display_name="Owner Person",
    avatar_url="https://accounts.example.com/users/user-owner-9c21/avatar/abc",
)


def test_session_cookie_roundtrips_the_whole_identity_record() -> None:
    value = mint_session_cookie_value(_SECRET, _OWNER, _DOMAIN)
    assert verify_session_cookie_value(_SECRET, value, _DOMAIN) == _OWNER


def test_session_cookie_omits_absent_profile_fields() -> None:
    value = mint_session_cookie_value(_SECRET, _BOB, _DOMAIN)
    claims = jwt.decode(value, _SECRET, algorithms=["HS256"], audience=_DOMAIN)
    assert "display_name" not in claims
    assert "avatar_url" not in claims
    identity = verify_session_cookie_value(_SECRET, value, _DOMAIN)
    assert identity == _BOB
    assert identity is not None
    assert identity.display_name is None
    assert identity.avatar_url is None


def test_session_cookie_rejects_wrong_secret_domain_and_garbage() -> None:
    value = mint_session_cookie_value(_SECRET, _BOB, _DOMAIN)
    assert verify_session_cookie_value("other-secret", value, _DOMAIN) is None
    assert verify_session_cookie_value(_SECRET, value, "other." + _DOMAIN) is None
    assert verify_session_cookie_value(_SECRET, "garbage", _DOMAIN) is None
    assert verify_session_cookie_value(_SECRET, "", _DOMAIN) is None


def test_legacy_cookie_without_a_user_id_is_no_session() -> None:
    # A cookie minted before the record carried a user id (email + owner only)
    # must not open a session: the visitor re-runs the handoff instead.
    now = datetime.now(timezone.utc)
    legacy = jwt.encode(
        {"email": "bob@example.com", "owner": False, "aud": _DOMAIN, "iat": now, "exp": now + timedelta(hours=1)},
        _SECRET,
        algorithm="HS256",
    )
    assert verify_session_cookie_value(_SECRET, legacy, _DOMAIN) is None


def test_set_session_cookie_sets_a_plain_copy_and_a_partitioned_copy() -> None:
    app = Flask(__name__)
    with app.test_request_context():
        response = Response(status=302)
        set_session_cookie(response, "cookie-value", _DOMAIN)
        by_name = set_cookies_by_name(response)
    assert set(by_name) == {SESSION_COOKIE_NAME, PARTITIONED_SESSION_COOKIE_NAME}
    for header in by_name.values():
        assert "=cookie-value;" in header
        assert "Secure" in header
        assert "HttpOnly" in header
        assert f"Domain={_DOMAIN}" in header
    # The plain copy is what a top-level visit (Safari included) keeps: Lax,
    # so a foreign site's subresource requests never carry it. Only the iframe
    # copy is SameSite=None, and only it carries the CHIPS attribute.
    assert "SameSite=Lax" in by_name[SESSION_COOKIE_NAME]
    assert "Partitioned" not in by_name[SESSION_COOKIE_NAME]
    assert "SameSite=None" in by_name[PARTITIONED_SESSION_COOKIE_NAME]
    assert "Partitioned" in by_name[PARTITIONED_SESSION_COOKIE_NAME]


def test_verify_session_from_cookies_accepts_whichever_copy_verifies() -> None:
    value = mint_session_cookie_value(_SECRET, _BOB, _DOMAIN)
    assert verify_session_from_cookies(_SECRET, {SESSION_COOKIE_NAME: value}, _DOMAIN) == _BOB
    assert verify_session_from_cookies(_SECRET, {PARTITIONED_SESSION_COOKIE_NAME: value}, _DOMAIN) == _BOB
    # A stale plain copy must not mask a valid partitioned one.
    both = {SESSION_COOKIE_NAME: "garbage", PARTITIONED_SESSION_COOKIE_NAME: value}
    assert verify_session_from_cookies(_SECRET, both, _DOMAIN) == _BOB
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
