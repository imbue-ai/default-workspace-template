"""The sign-in session's start-up material, which the chrome writes in one file and ``auth-sandbox-prepare`` installs.

One key per sign-in dialog: the chrome mints ``K``, which is both the sandbox store's ``LATCHKEY_ENCRYPTION_KEY`` and
the key the credentials travel to the workspace VM under, so nothing in the sandbox re-encrypts. The session file
(``/run/auth-sandbox/session.json``) carries ``K``, the workspace machine's browser state already encrypted under
``K`` (or null), and the two settings the browser's fingerprint is pinned from:

    {"latchkey_key": "<K>", "browser_state": "<base64>" | null, "workspace_host_id": "<host-...>" | null,
     "timezone": "<IANA>" | null}

Prepare writes ``K`` to ``latchkey_key``, the state into the latchkey directory, the settings to
``session_settings.json`` for the sign-in, and deletes the session file.
"""

import base64
import binascii
import json
import re
from pathlib import Path
from typing import Final

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from pydantic import Field, SecretBytes, SecretStr, ValidationError

from auth_sandbox.errors import MissingMaterialError, SessionFileError
from auth_sandbox.runtime_files import SandboxPaths, write_private_file

_LATCHKEY_KEY_FIELD: Final[str] = "latchkey_key"
_BROWSER_STATE_FIELD: Final[str] = "browser_state"
_WORKSPACE_HOST_ID_FIELD: Final[str] = "workspace_host_id"
_TIMEZONE_FIELD: Final[str] = "timezone"
_LATCHKEY_KEY_BYTES: Final[int] = 32
# An IANA zone name's shape ("UTC", "America/Argentina/Buenos_Aires", "Etc/GMT+5"); the value ends up in the
# browser launcher's environment, so nothing else is let through.
_TIMEZONE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"[A-Za-z][A-Za-z0-9_+-]*(/[A-Za-z0-9_+-]+)*"
)


class SessionSettings(FrozenModel):
    """What the sign-in pins the browser's fingerprint from, kept by ``auth-sandbox-prepare``."""

    workspace_host_id: str | None = Field(
        description="The workspace being signed in, which seeds the fingerprint"
    )
    timezone: str | None = Field(
        description="The user's IANA zone, which the browser reports in place of the region's"
    )


class SessionMaterial(FrozenModel):
    """The session file, parsed."""

    latchkey_key: SecretStr = Field(description="K: 32 random bytes, base64")
    browser_state: SecretBytes | None = Field(
        description="The workspace's browser state, encrypted under K, when the chrome sent one"
    )
    settings: SessionSettings = Field(
        description="The fingerprint settings the sign-in reads"
    )


def _decode_base64(value: str, name: str) -> bytes:
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as e:
        raise SessionFileError(f"the session file's {name!r} is not base64") from e


def _optional_string(document: dict[str, object], name: str) -> str | None:
    value = document.get(name)
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise SessionFileError(f"the session file's {name!r} must be a string or null")
    return value


@pure
def parse_session_file(text: str) -> SessionMaterial:
    """Parse the session file; a field it does not know is ignored, an optional field may be absent.

    Raises:
        SessionFileError: when the text is not that shape; the message never quotes the key.
    """
    try:
        document = json.loads(text)
    except json.JSONDecodeError as e:
        raise SessionFileError(
            f"the session file is not valid JSON: {e.msg} at line {e.lineno}"
        ) from e
    if not isinstance(document, dict):
        raise SessionFileError("the session file must hold a JSON object")
    latchkey_key = document.get(_LATCHKEY_KEY_FIELD)
    if not isinstance(latchkey_key, str) or not latchkey_key:
        raise SessionFileError(
            f"the session file's {_LATCHKEY_KEY_FIELD!r} must be a non-empty string"
        )
    if len(_decode_base64(latchkey_key, _LATCHKEY_KEY_FIELD)) != _LATCHKEY_KEY_BYTES:
        raise SessionFileError(
            f"the session file's {_LATCHKEY_KEY_FIELD!r} must be {_LATCHKEY_KEY_BYTES} bytes"
        )
    encoded_state = _optional_string(document, _BROWSER_STATE_FIELD)
    browser_state = (
        None
        if encoded_state is None
        else SecretBytes(_decode_base64(encoded_state, _BROWSER_STATE_FIELD))
    )
    timezone = _optional_string(document, _TIMEZONE_FIELD)
    if timezone is not None and _TIMEZONE_PATTERN.fullmatch(timezone) is None:
        raise SessionFileError(
            f"the session file's {_TIMEZONE_FIELD!r} is not an IANA zone name"
        )
    return SessionMaterial(
        latchkey_key=SecretStr(latchkey_key),
        browser_state=browser_state,
        settings=SessionSettings(
            workspace_host_id=_optional_string(document, _WORKSPACE_HOST_ID_FIELD),
            timezone=timezone,
        ),
    )


def read_and_remove_session_file(path: Path) -> SessionMaterial:
    """Read the session file and delete it, parseable or not: it carries the key, which is needed once."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as e:
        raise MissingMaterialError(f"no session file at {path}") from e
    except (OSError, UnicodeDecodeError) as e:
        path.unlink(missing_ok=True)
        raise SessionFileError(f"the session file at {path} cannot be read") from e
    path.unlink(missing_ok=True)
    return parse_session_file(text)


def install_session(material: SessionMaterial, paths: SandboxPaths) -> None:
    """Write the browser state (when the session has one), the settings, and the key, in that order: a key on disk
    means the rest of the session is in place, and the sign-in commands refuse to start without it."""
    paths.latchkey_directory.mkdir(parents=True, exist_ok=True)
    if material.browser_state is not None:
        write_private_file(
            paths.latchkey_browser_state_path,
            material.browser_state.get_secret_value(),
        )
    write_private_file(
        paths.session_settings_path,
        material.settings.model_dump_json().encode("utf-8"),
    )
    write_private_file(
        paths.latchkey_key_path,
        (material.latchkey_key.get_secret_value() + "\n").encode("utf-8"),
    )


def read_session_settings(path: Path) -> SessionSettings:
    """The settings ``auth-sandbox-prepare`` kept.

    Raises:
        MissingMaterialError: when prepare has not run.
        SessionFileError: when the file is not the settings prepare writes.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as e:
        raise MissingMaterialError(f"no session settings at {path}") from e
    except (OSError, UnicodeDecodeError) as e:
        raise SessionFileError(f"the session settings at {path} cannot be read") from e
    try:
        return SessionSettings.model_validate_json(text)
    except ValidationError as e:
        raise SessionFileError(
            f"the session settings at {path} are malformed: {e.error_count()} errors"
        ) from e
