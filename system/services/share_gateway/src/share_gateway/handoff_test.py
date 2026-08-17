from datetime import datetime
from datetime import timedelta
from datetime import timezone

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from share_gateway.handoff import HandoffVerificationError
from share_gateway.handoff import JwksCache
from share_gateway.handoff import SingleUseJtiRegistry
from share_gateway.handoff import verify_handoff_token

_DOMAIN = "host-" + "a" * 32 + "." + "b" * 32 + ".us1.imbueminds.com"
_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_KID = "kid-abc"


def _cache() -> JwksCache:
    return JwksCache("https://accounts.example.com/share/jwks.json", preloaded_keys_by_kid={_KID: _KEY.public_key()})


def _token(
    nonce: str = "n1",
    jti: str = "j1",
    kid: str = _KID,
    ttl_seconds: int = 60,
    email: str = "a@b.co",
    is_owner: bool = False,
) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": "u1",
        "email": email,
        "owner": is_owner,
        "aud": _DOMAIN,
        "jti": jti,
        "nonce": nonce,
        "iat": now,
        "exp": now + timedelta(seconds=ttl_seconds),
    }
    return jwt.encode(payload, _KEY, algorithm="RS256", headers={"kid": kid})


def test_valid_token_returns_email_and_not_owner_by_default() -> None:
    result = verify_handoff_token(_token(), "n1", _DOMAIN, _cache(), SingleUseJtiRegistry())
    assert result.email == "a@b.co"
    assert result.is_owner is False


def test_valid_token_carries_owner_claim() -> None:
    result = verify_handoff_token(_token(is_owner=True), "n1", _DOMAIN, _cache(), SingleUseJtiRegistry())
    assert result.is_owner is True


def test_expired_token_is_rejected() -> None:
    with pytest.raises(HandoffVerificationError):
        verify_handoff_token(_token(ttl_seconds=-10), "n1", _DOMAIN, _cache(), SingleUseJtiRegistry())


def test_nonce_mismatch_is_rejected() -> None:
    with pytest.raises(HandoffVerificationError):
        verify_handoff_token(_token(nonce="other"), "n1", _DOMAIN, _cache(), SingleUseJtiRegistry())


def test_jti_is_single_use() -> None:
    registry = SingleUseJtiRegistry()
    verify_handoff_token(_token(), "n1", _DOMAIN, _cache(), registry)
    with pytest.raises(HandoffVerificationError):
        verify_handoff_token(_token(), "n1", _DOMAIN, _cache(), registry)


def test_forged_signature_is_rejected() -> None:
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(timezone.utc)
    forged = jwt.encode(
        {"sub": "u1", "email": "a@b.co", "aud": _DOMAIN, "jti": "j9", "nonce": "n1",
         "iat": now, "exp": now + timedelta(seconds=60)},
        other_key,
        algorithm="RS256",
        headers={"kid": _KID},
    )
    with pytest.raises(HandoffVerificationError):
        verify_handoff_token(forged, "n1", _DOMAIN, _cache(), SingleUseJtiRegistry())


def test_garbage_token_is_rejected() -> None:
    with pytest.raises(HandoffVerificationError):
        verify_handoff_token("garbage", "n1", _DOMAIN, _cache(), SingleUseJtiRegistry())
