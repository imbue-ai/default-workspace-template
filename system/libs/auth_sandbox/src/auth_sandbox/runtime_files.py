"""The files under the sandbox's tmpfs: where each one is, how a key is read, and how a private file is written."""

import os
import secrets
from pathlib import Path

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr_latchkey.core import BROWSER_STATE_FILENAME
from pydantic import Field, SecretStr

from auth_sandbox.errors import MissingMaterialError
from auth_sandbox.primitives import (
    LATCHKEY_DIRNAME,
    LATCHKEY_KEY_FILENAME,
    RUNTIME_DIR,
    SCREEN_TOKEN_SHA256_FILENAME,
    SESSION_SETTINGS_FILENAME,
)


class SandboxPaths(FrozenModel):
    """The layout under the sandbox's runtime directory (``/run/auth-sandbox`` in the image)."""

    runtime_dir: Path = Field(
        default=RUNTIME_DIR,
        description="The tmpfs root every user-specific file lives under",
    )

    @property
    def latchkey_key_path(self) -> Path:
        """The session's latchkey encryption key, which ``auth-sandbox-prepare`` writes from the session file."""
        return self.runtime_dir / LATCHKEY_KEY_FILENAME

    @property
    def session_settings_path(self) -> Path:
        """The session's fingerprint settings, which ``auth-sandbox-prepare`` keeps for the sign-in."""
        return self.runtime_dir / SESSION_SETTINGS_FILENAME

    @property
    def screen_token_sha256_path(self) -> Path:
        """The Screen's owner token digest, which the claim watcher writes and the Screen's gate reads."""
        return self.runtime_dir / SCREEN_TOKEN_SHA256_FILENAME

    @property
    def latchkey_directory(self) -> Path:
        """The sandbox's ``LATCHKEY_DIRECTORY``: the template config, the browser state and the credentials, all
        under the session's key."""
        return self.runtime_dir / LATCHKEY_DIRNAME

    @property
    def latchkey_browser_state_path(self) -> Path:
        return self.latchkey_directory / BROWSER_STATE_FILENAME


def read_key(path: Path) -> SecretStr:
    """A base64 key as ``auth-sandbox-prepare`` wrote it, without the trailing newline.

    Raises:
        MissingMaterialError: when the file is absent or empty, naming the path and nothing of its contents.
    """
    try:
        text = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError as e:
        raise MissingMaterialError(f"no key at {path}") from e
    except OSError as e:
        raise MissingMaterialError(
            f"the key at {path} cannot be read: {e.strerror}"
        ) from e
    if not text:
        raise MissingMaterialError(f"the key file at {path} is empty")
    return SecretStr(text)


def write_private_file(path: Path, content: bytes) -> None:
    """Replace ``path`` with a file only its owner can read, through a rename, so no reader (owner-exec, the
    Screen's gate, latchkey) ever sees it half written."""
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as file:
            file.write(content)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
