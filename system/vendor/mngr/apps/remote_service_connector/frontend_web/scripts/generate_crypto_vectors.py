"""Generate cross-language crypto test vectors for the web chrome.

The web chrome's crypto must be wire-compatible with
``imbue.imbue_common.secret_wrapping`` (argon2id KEK derivation, AES-256-GCM
nonce||ciphertext blobs, the key-bundle JSON shape) and with the dwt
owner-exec signed envelope (``owner_exec.signing.build_signing_string``).
This script produces ``src/crypto/test_vectors.json`` (committed) from the
Python implementations; the vitest suite asserts the TypeScript side matches.

Run from the repo root:

    uv run python apps/remote_service_connector/frontend_web/scripts/generate_crypto_vectors.py
"""

import base64
import hashlib
import json
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding
from cryptography.hazmat.primitives.serialization import PublicFormat
from loguru import logger
from pydantic import SecretStr

from imbue.imbue_common.secret_wrapping import KdfParameters
from imbue.imbue_common.secret_wrapping import decrypt_secrets
from imbue.imbue_common.secret_wrapping import derive_kek
from imbue.imbue_common.secret_wrapping import encrypt_secrets
from imbue.imbue_common.secret_wrapping import unwrap_dek
from imbue.imbue_common.secret_wrapping import wrap_dek

_OUTPUT_PATH = Path(__file__).parent.parent / "src" / "crypto" / "test_vectors.json"

# Deterministic inputs (the AEAD nonces are random, so the vectors carry the
# produced ciphertexts; decryption on the TS side is the deterministic check).
_PASSWORD = "correct horse battery staple"
_SALT = bytes(range(16))
_DEK = bytes(range(32))
_SECRETS_PLAINTEXT = (
    '{"restic_env":"export RESTIC_REPOSITORY=s3:endpoint/bucket\\n",'
    '"ssh_private_key":"-----BEGIN OPENSSH PRIVATE KEY-----\\nfake\\n-----END OPENSSH PRIVATE KEY-----\\n",'
    '"ssh_known_hosts":null}'
)

# Ed25519 exec-envelope vector (signatures are deterministic per RFC 8032).
_ED25519_SEED = bytes(range(32, 64))
_ENVELOPE = {
    "method": "POST",
    "path": "/run",
    "body": '{"command":["printf","hello"]}',
    "audience": "host-abc123.ownerlabel.us1.example.com",
    "timestamp": "1723150000",
    "nonce": "nonce-vector-1",
}


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _openssh_public_key_line(private_key: Ed25519PrivateKey) -> str:
    return private_key.public_key().public_bytes(Encoding.OpenSSH, PublicFormat.OpenSSH).decode("ascii")


def main() -> None:
    parameters = KdfParameters(salt=_SALT, time_cost=3, memory_kib=65536, parallelism=4)
    kek = derive_kek(SecretStr(_PASSWORD), parameters)
    wrapped = wrap_dek(kek, _DEK)
    assert unwrap_dek(kek, wrapped) == _DEK
    blob = encrypt_secrets(_DEK, _SECRETS_PLAINTEXT.encode("utf-8"))
    assert decrypt_secrets(_DEK, blob) == _SECRETS_PLAINTEXT.encode("utf-8")

    private_key = Ed25519PrivateKey.from_private_bytes(_ED25519_SEED)
    body_digest = hashlib.sha256(_ENVELOPE["body"].encode("utf-8")).hexdigest()
    signing_string = "\n".join(
        [
            "v1",
            _ENVELOPE["method"],
            _ENVELOPE["path"],
            body_digest,
            _ENVELOPE["audience"],
            _ENVELOPE["timestamp"],
            _ENVELOPE["nonce"],
        ]
    )
    signature = private_key.sign(signing_string.encode("utf-8"))

    vectors = {
        "kdf": {
            "password": _PASSWORD,
            "salt_b64": _b64(_SALT),
            "time_cost": parameters.time_cost,
            "memory_kib": parameters.memory_kib,
            "parallelism": parameters.parallelism,
            "kek_b64": _b64(kek),
        },
        "bundle": {
            "dek_b64": _b64(_DEK),
            "wrapped_dek_b64": _b64(wrapped),
        },
        "secrets": {
            "plaintext": _SECRETS_PLAINTEXT,
            "blob_b64": _b64(blob),
        },
        "exec_envelope": {
            **_ENVELOPE,
            "seed_b64": _b64(_ED25519_SEED),
            "public_key_openssh": _openssh_public_key_line(private_key),
            "signing_string": signing_string,
            "signature_b64": _b64(signature),
        },
    }
    _OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    _OUTPUT_PATH.write_text(json.dumps(vectors, indent=2) + "\n")
    logger.info("Wrote {}", _OUTPUT_PATH)


if __name__ == "__main__":
    main()
