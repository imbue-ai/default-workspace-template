import base64

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding
from cryptography.hazmat.primitives.serialization import PublicFormat

from owner_exec.signing import ExecAuthError
from owner_exec.signing import NonceCache
from owner_exec.signing import build_signing_string
from owner_exec.signing import parse_authorized_ed25519_keys
from owner_exec.signing import verify_exec_envelope

_AUDIENCE = "host-abc.user.us1.imbueminds.com"


def _openssh_public(key: Ed25519PrivateKey) -> str:
    return key.public_key().public_bytes(Encoding.OpenSSH, PublicFormat.OpenSSH).decode("ascii")


def _sign(key: Ed25519PrivateKey, message: bytes) -> str:
    return base64.b64encode(key.sign(message)).decode("ascii")


def _verify(
    key: Ed25519PrivateKey,
    *,
    method: str = "POST",
    path: str = "/run",
    body: bytes = b"{}",
    audience: str = _AUDIENCE,
    timestamp: str = "1000",
    nonce: str = "n1",
    authorized: list[Ed25519PrivateKey] | None = None,
    nonce_cache: NonceCache | None = None,
    now: float = 1000.0,
    signature_b64: str | None = None,
    public_key_ssh: str | None = None,
) -> None:
    authorized_keys = [k.public_key() for k in (authorized if authorized is not None else [key])]
    message = build_signing_string(method, path, body, audience, timestamp, nonce)
    verify_exec_envelope(
        method=method,
        path=path,
        body=body,
        audience=audience,
        signature_b64=signature_b64 if signature_b64 is not None else _sign(key, message),
        public_key_ssh=public_key_ssh if public_key_ssh is not None else _openssh_public(key),
        timestamp=timestamp,
        nonce=nonce,
        authorized_keys=authorized_keys,
        nonce_cache=nonce_cache if nonce_cache is not None else NonceCache(),
        now=now,
    )


def test_valid_envelope_verifies() -> None:
    key = Ed25519PrivateKey.generate()
    _verify(key)  # does not raise


def test_unauthorized_key_is_rejected() -> None:
    signer = Ed25519PrivateKey.generate()
    other = Ed25519PrivateKey.generate()
    with pytest.raises(ExecAuthError, match="not authorized"):
        _verify(signer, authorized=[other])


def test_tampered_body_breaks_signature() -> None:
    key = Ed25519PrivateKey.generate()
    message = build_signing_string("POST", "/run", b"{}", _AUDIENCE, "1000", "n1")
    with pytest.raises(ExecAuthError, match="signature does not verify"):
        # Signature is over the original body but a different body is presented.
        _verify(key, body=b'{"evil":1}', signature_b64=_sign(key, message))


def test_wrong_audience_breaks_signature() -> None:
    key = Ed25519PrivateKey.generate()
    message = build_signing_string("POST", "/run", b"{}", "other.example.com", "1000", "n1")
    with pytest.raises(ExecAuthError, match="signature does not verify"):
        _verify(key, signature_b64=_sign(key, message))


def test_stale_timestamp_is_rejected() -> None:
    key = Ed25519PrivateKey.generate()
    with pytest.raises(ExecAuthError, match="outside the allowed window"):
        _verify(key, timestamp="1000", now=1100.0)


def test_replayed_nonce_is_rejected() -> None:
    key = Ed25519PrivateKey.generate()
    cache = NonceCache()
    _verify(key, nonce="reused", nonce_cache=cache)
    with pytest.raises(ExecAuthError, match="replay"):
        _verify(key, nonce="reused", nonce_cache=cache)


def test_garbage_signature_is_rejected() -> None:
    key = Ed25519PrivateKey.generate()
    with pytest.raises(ExecAuthError, match="not valid base64"):
        _verify(key, signature_b64="not base64 !!!")


def test_non_ed25519_presented_key_is_rejected() -> None:
    key = Ed25519PrivateKey.generate()
    with pytest.raises(ExecAuthError, match="not a usable OpenSSH key"):
        _verify(key, public_key_ssh="ssh-rsa AAAAgarbage")


def test_parse_authorized_keys_keeps_only_ed25519() -> None:
    ed = Ed25519PrivateKey.generate()
    authorized_text = "\n".join(
        [
            "# a comment",
            "",
            _openssh_public(ed),
            "ssh-rsa AAAAB3NzaC1yc2E-not-really-valid",
            "garbage line",
        ]
    )
    parsed = parse_authorized_ed25519_keys(authorized_text)
    assert len(parsed) == 1
    expected = ed.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    assert parsed[0].public_bytes(Encoding.Raw, PublicFormat.Raw) == expected
