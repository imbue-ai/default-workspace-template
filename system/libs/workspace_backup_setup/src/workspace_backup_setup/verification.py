"""The workspace's backup-verification flag in ``data/system/backup.toml``.

Whether a client checks the workspace's backups is a fact about the workspace that every client
should agree on, so it lives in the workspace's own backup config, as a key the backup service
tolerates and ignores.

The write is a careful text edit rather than a TOML re-render: ``backup.toml`` is a user-authored
file, so every other line (settings, comments, tables) is kept byte for byte. Only a top-level
``verification_disabled = ...`` line (one before the first table header) is replaced; absent one,
the key is inserted at the top of the file.
"""

import os
import tempfile
import tomllib
from pathlib import Path
from typing import Final

from loguru import logger

BACKUP_TOML_RELATIVE_PATH: Final[Path] = Path("data/system/backup.toml")
VERIFICATION_DISABLED_KEY: Final[str] = "verification_disabled"


def read_verification_disabled(workspace_root: Path) -> bool:
    """Whether the workspace's backup config turns verification off (absent or unreadable reads enabled)."""
    path = workspace_root / BACKUP_TOML_RELATIVE_PATH
    try:
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
    except FileNotFoundError:
        return False
    except (OSError, ValueError) as e:
        logger.warning(
            "Could not read the backup verification flag in {}: {}; reading verification as on",
            path,
            e,
        )
        return False
    return raw.get(VERIFICATION_DISABLED_KEY) is True


def _is_flag_line(line: str) -> bool:
    return line.split("=", 1)[0].strip() == VERIFICATION_DISABLED_KEY


def render_backup_toml_with_flag(existing_text: str, is_disabled: bool) -> str:
    """``backup.toml``'s text with the flag line set, every other line kept as it is.

    Only the root table's own flag line (one before the first ``[table]`` header) is replaced;
    the new line goes at the top so it always lands in the root table.
    """
    kept: list[str] = []
    is_in_root_table = True
    for line in existing_text.splitlines():
        if line.lstrip().startswith("["):
            is_in_root_table = False
        if is_in_root_table and _is_flag_line(line):
            continue
        kept.append(line)
    flag_line = f"{VERIFICATION_DISABLED_KEY} = {'true' if is_disabled else 'false'}"
    body = "\n".join([flag_line, *kept])
    return body if body.endswith("\n") else body + "\n"


def write_verification_disabled(workspace_root: Path, is_disabled: bool) -> None:
    """Set the flag, through a scratch file of its own so two clients toggling it never share one."""
    path = workspace_root / BACKUP_TOML_RELATIVE_PATH
    try:
        existing_text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        existing_text = ""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, scratch = tempfile.mkstemp(
        dir=path.parent, prefix=".backup.toml.", suffix=".tmp"
    )
    with os.fdopen(handle, "w", encoding="utf-8") as out:
        out.write(render_backup_toml_with_flag(existing_text, is_disabled))
    os.replace(scratch, path)
