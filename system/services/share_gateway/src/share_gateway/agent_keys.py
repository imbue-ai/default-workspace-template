"""The one check of a visiting agent's key that needs a key library: that it is exactly an Ed25519 OpenSSH key."""

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import load_ssh_public_key
from share_grants.agent_grants import fingerprint_of_public_key_line


class AgentKeyError(ValueError):
    """Raised when a public key is not a usable Ed25519 OpenSSH key."""


def canonical_public_key_line(raw_line: str) -> tuple[str, str]:
    """``(key line, fingerprint)`` for an Ed25519 OpenSSH public key line; any comment is dropped.

    Raises AgentKeyError for anything that is not exactly an Ed25519 key.
    """
    parts = raw_line.strip().split()
    if len(parts) < 2 or parts[0] != "ssh-ed25519":
        raise AgentKeyError("public key must be an ssh-ed25519 OpenSSH public key line")
    line = f"{parts[0]} {parts[1]}"
    try:
        key = load_ssh_public_key(line.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise AgentKeyError(f"public key could not be parsed: {exc}") from exc
    if not isinstance(key, Ed25519PublicKey):
        raise AgentKeyError("public key must be an Ed25519 key")
    return line, fingerprint_of_public_key_line(line)
