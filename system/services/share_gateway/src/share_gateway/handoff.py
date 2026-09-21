"""Verification of the broker's handoff JWT at the gateway's login callback.

The broker (at the accounts domain) mints a 60-second RS256 token
``{sub, email, owner, display_name?, avatar_url?, aud: <workspace-domain>, jti,
nonce}`` and redirects the visitor here. Verification: signature against the
broker's published JWKS (cached; refreshed once on an unknown ``kid``),
audience must be exactly this workspace's domain, ``nonce`` must match the
state this gateway minted for the pending login, and each ``jti`` is
single-use. The verified claims become the requester's identity record, which
the session cookie then carries.
"""

import threading
import time

import httpx
import jwt
from jwt import algorithms as jwt_algorithms

from share_gateway.identity import RequesterIdentity

_HANDOFF_ALGORITHM = "RS256"
_JWKS_FETCH_TIMEOUT_SECONDS = 10.0
_JTI_RETENTION_SECONDS = 300.0


class HandoffVerificationError(ValueError):
    """Raised when a handoff token fails any verification step."""


class JwksCache:
    """Fetches and caches the broker's JWKS, refreshing once when a kid is unknown."""

    def __init__(self, jwks_url: str, preloaded_keys_by_kid: dict[str, object] | None = None) -> None:
        # preloaded_keys_by_kid lets tests (and pre-warmed callers) inject keys
        # without a network fetch; unknown kids still trigger a refresh.
        self._jwks_url = jwks_url
        self._keys_by_kid: dict[str, object] = dict(preloaded_keys_by_kid or {})
        self._lock = threading.Lock()

    def _refresh(self) -> None:
        response = httpx.get(self._jwks_url, timeout=_JWKS_FETCH_TIMEOUT_SECONDS)
        response.raise_for_status()
        fresh_keys: dict[str, object] = {}
        for key_entry in response.json().get("keys", []):
            kid = key_entry.get("kid")
            if kid and key_entry.get("kty") == "RSA":
                fresh_keys[str(kid)] = jwt_algorithms.RSAAlgorithm.from_jwk(key_entry)
        self._keys_by_kid = fresh_keys

    def key_for_kid(self, kid: str) -> object:
        """The public key for ``kid``; raises HandoffVerificationError when unknown even after refresh."""
        with self._lock:
            cached = self._keys_by_kid.get(kid)
            if cached is not None:
                return cached
            try:
                self._refresh()
            except (httpx.HTTPError, ValueError) as exc:
                raise HandoffVerificationError(f"could not fetch broker JWKS: {exc}") from exc
            refreshed = self._keys_by_kid.get(kid)
            if refreshed is None:
                raise HandoffVerificationError(f"broker JWKS has no key {kid!r}")
            return refreshed


class SingleUseJtiRegistry:
    """Remembers recently seen token ids so a replayed handoff token is rejected."""

    def __init__(self) -> None:
        self._seen_at_by_jti: dict[str, float] = {}
        self._lock = threading.Lock()

    def claim(self, jti: str) -> bool:
        """Record ``jti``; False when it was already used within the retention window."""
        now = time.monotonic()
        with self._lock:
            self._seen_at_by_jti = {
                seen_jti: seen_at
                for seen_jti, seen_at in self._seen_at_by_jti.items()
                if now - seen_at < _JTI_RETENTION_SECONDS
            }
            if jti in self._seen_at_by_jti:
                return False
            self._seen_at_by_jti[jti] = now
            return True


def verify_handoff_token(
    token: str,
    expected_nonce: str,
    workspace_domain: str,
    jwks_cache: JwksCache,
    jti_registry: SingleUseJtiRegistry,
) -> RequesterIdentity:
    """Verify a handoff token end to end and return the visitor's identity record."""
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        raise HandoffVerificationError(f"malformed handoff token: {exc}") from exc
    kid = header.get("kid")
    if not isinstance(kid, str) or not kid:
        raise HandoffVerificationError("handoff token has no kid")
    public_key = jwks_cache.key_for_kid(kid)
    try:
        claims = jwt.decode(
            token,
            public_key,
            algorithms=[_HANDOFF_ALGORITHM],
            audience=workspace_domain,
        )
    except jwt.PyJWTError as exc:
        raise HandoffVerificationError(f"handoff token rejected: {exc}") from exc
    if claims.get("nonce") != expected_nonce:
        raise HandoffVerificationError("handoff token nonce does not match the pending login")
    jti = claims.get("jti")
    if not isinstance(jti, str) or not jti:
        raise HandoffVerificationError("handoff token has no jti")
    if not jti_registry.claim(jti):
        raise HandoffVerificationError("handoff token was already used")
    user_id = claims.get("sub")
    if not isinstance(user_id, str) or not user_id:
        raise HandoffVerificationError("handoff token has no sub")
    email = claims.get("email")
    if not isinstance(email, str) or not email:
        raise HandoffVerificationError("handoff token has no email")
    return RequesterIdentity(
        user_id=user_id,
        email=email,
        is_owner=bool(claims.get("owner", False)),
        display_name=_optional_text_claim(claims, "display_name"),
        avatar_url=_optional_text_claim(claims, "avatar_url"),
    )


def _optional_text_claim(claims: dict[str, object], name: str) -> str | None:
    value = claims.get(name)
    return value if isinstance(value, str) and value else None
