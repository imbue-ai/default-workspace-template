import pytest
from cryptography.hazmat.primitives.asymmetric.rsa import generate_private_key
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from share_grants.agent_grants import fingerprint_of_public_key_line

from share_gateway.agent_keys import AgentKeyError, canonical_public_key_line

# The public key and fingerprint of the owner-exec profile's shared test vectors, so the fingerprint rule here is
# checked against the one the daemon uses as keyid.
_VECTOR_PUBLIC_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIIqI4910CfGV/VLbLTy6XXLKZwm/HZQSG/N0iAG0D29c"
_VECTOR_FINGERPRINT = "SHA256:fe85JkIjo8VPe+XqXJGH5Mau1EMFdK1OdKvJUFicyA8"


def test_canonical_public_key_line_drops_the_comment_and_fingerprints_like_openssh() -> None:
    line, fingerprint = canonical_public_key_line(_VECTOR_PUBLIC_KEY + " agent@laptop")
    assert line == _VECTOR_PUBLIC_KEY
    assert fingerprint == _VECTOR_FINGERPRINT
    assert fingerprint_of_public_key_line(line) == _VECTOR_FINGERPRINT


def test_canonical_public_key_line_refuses_other_key_types_and_garbage() -> None:
    rsa_public = generate_private_key(public_exponent=65537, key_size=2048).public_key()
    rsa_line = rsa_public.public_bytes(Encoding.OpenSSH, PublicFormat.OpenSSH).decode("ascii")
    with pytest.raises(AgentKeyError):
        canonical_public_key_line(rsa_line)
    with pytest.raises(AgentKeyError):
        canonical_public_key_line("ssh-ed25519 not-base64!!")
    with pytest.raises(AgentKeyError):
        canonical_public_key_line("")
