"""Ed25519 request-envelope auth for the owner-exec service.

Every exec request is signed with a private key whose public half is in the
workspace's ``~/.ssh/authorized_keys`` -- so authorization is exactly SSH's:
possession of a key the workspace trusts, not possession of a session cookie.
The signed envelope binds the HTTP method, path, a digest of the body, the
workspace's own audience (its share domain), a unix timestamp, and a nonce, so
a captured envelope cannot be replayed against another workspace, against a
different request, or after its short window.

The gateway's owner-session forward_auth sits in front of this as defense in
depth, but the signature is the real gate: it is verified here regardless.
"""

import base64
import hashlib
import threading
import time
from collections.abc import Sequence

from cryptography.exceptions import InvalidSignature
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding
from cryptography.hazmat.primitives.serialization import PublicFormat
from cryptography.hazmat.primitives.serialization import load_ssh_public_key

# How far a request timestamp may be from now (seconds). Bounds both clock skew
# and how long a captured envelope stays replayable within the nonce window.
TIMESTAMP_WINDOW_SECONDS = 60

_ENVELOPE_VERSION = "v1"


class ExecAuthError(ValueError):
    """Raised when an exec request envelope fails any verification step."""


class NonceCache:
    """Remembers recently seen nonces so a signed envelope cannot be replayed.

    Entries older than the timestamp window are pruned on each use, so the
    cache never grows beyond the requests seen within one window.
    """

    def __init__(self, window_seconds: float = TIMESTAMP_WINDOW_SECONDS) -> None:
        self._window_seconds = window_seconds
        self._seen_at_by_nonce: dict[str, float] = {}
        self._lock = threading.Lock()

    def claim(self, nonce: str, now: float) -> bool:
        """Record ``nonce``; return False when it was already used within the window."""
        with self._lock:
            self._seen_at_by_nonce = {
                seen_nonce: seen_at
                for seen_nonce, seen_at in self._seen_at_by_nonce.items()
                if now - seen_at < self._window_seconds
            }
            if nonce in self._seen_at_by_nonce:
                return False
            self._seen_at_by_nonce[nonce] = now
            return True


def build_signing_string(
    method: str, path: str, body: bytes, audience: str, timestamp: str, nonce: str
) -> bytes:
    """The canonical bytes an exec request signs (and the server re-derives)."""
    body_digest = hashlib.sha256(body).hexdigest()
    lines = [_ENVELOPE_VERSION, method.upper(), path, body_digest, audience, timestamp, nonce]
    return "\n".join(lines).encode("utf-8")


def _raw_public_bytes(key: Ed25519PublicKey) -> bytes:
    return key.public_bytes(Encoding.Raw, PublicFormat.Raw)


def parse_authorized_ed25519_keys(authorized_keys_text: str) -> list[Ed25519PublicKey]:
    """Load every Ed25519 public key from an authorized_keys file body.

    Non-Ed25519 keys and unparseable lines are skipped: the workspace may also
    authorize RSA/ECDSA keys for plain SSH, but exec only accepts Ed25519.
    """
    keys: list[Ed25519PublicKey] = []
    for raw_line in authorized_keys_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            loaded = load_ssh_public_key(line.encode("utf-8"))
        except (ValueError, UnsupportedAlgorithm):
            continue
        if isinstance(loaded, Ed25519PublicKey):
            keys.append(loaded)
    return keys


def verify_exec_envelope(
    *,
    method: str,
    path: str,
    body: bytes,
    audience: str,
    signature_b64: str,
    public_key_ssh: str,
    timestamp: str,
    nonce: str,
    authorized_keys: Sequence[Ed25519PublicKey],
    nonce_cache: NonceCache,
    now: float,
) -> None:
    """Verify a signed exec envelope, raising :class:`ExecAuthError` on any failure.

    Steps, in order: the presented key must be one of ``authorized_keys``; the
    timestamp must be within the window; the nonce must be fresh; and the
    signature must verify over the canonical signing string.
    """
    try:
        presented = load_ssh_public_key(public_key_ssh.strip().encode("utf-8"))
    except (ValueError, UnsupportedAlgorithm) as exc:
        raise ExecAuthError(f"presented public key is not a usable OpenSSH key: {exc}") from exc
    if not isinstance(presented, Ed25519PublicKey):
        raise ExecAuthError("presented public key is not Ed25519")
    presented_raw = _raw_public_bytes(presented)
    if not any(presented_raw == _raw_public_bytes(candidate) for candidate in authorized_keys):
        raise ExecAuthError("presented public key is not authorized on this workspace")

    try:
        request_timestamp = float(timestamp)
    except ValueError as exc:
        raise ExecAuthError("timestamp is not a number") from exc
    if abs(now - request_timestamp) > TIMESTAMP_WINDOW_SECONDS:
        raise ExecAuthError("request timestamp is outside the allowed window")

    if not nonce:
        raise ExecAuthError("nonce is empty")
    if not nonce_cache.claim(nonce, now):
        raise ExecAuthError("nonce was already used (replay)")

    try:
        signature = base64.b64decode(signature_b64, validate=True)
    except (ValueError, TypeError) as exc:
        raise ExecAuthError("signature is not valid base64") from exc
    signing_string = build_signing_string(method, path, body, audience, timestamp, nonce)
    try:
        presented.verify(signature, signing_string)
    except InvalidSignature as exc:
        raise ExecAuthError("signature does not verify") from exc


def current_unix_time() -> float:
    return time.time()
