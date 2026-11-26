"""The files that hold a workspace's backup credentials: ``restic.env``, the copies of it kept aside, and the
credentials of a setup that has not finished.

``data/.secrets/restic.env`` is what the backup service runs on. A ``restic.env`` that is replaced or turned off
is kept beside it as ``restic.env.<timestamp>``, the stamp the Imbue Studio desktop gives its own archived copies,
and is where the password of a repository the workspace used before is found again. The credentials of a setup
under way sit in ``restic.env.pending`` (mode 0600) until the repository is ready.

The parser of ``restic.env`` is the backup service's own (``host_backup.config``).
"""

import hashlib
import os
import re
import tempfile
from collections.abc import Mapping
from datetime import datetime, timezone
from enum import auto
from pathlib import Path
from typing import Final

from host_backup.config import RESTIC_ENV_PATH, parse_restic_env_file
from imbue.imbue_common.enums import UpperCaseStrEnum

PENDING_ENV_RELATIVE_PATH: Final[Path] = RESTIC_ENV_PATH.with_name(
    RESTIC_ENV_PATH.name + ".pending"
)
SECRET_FILE_MODE: Final[int] = 0o600
# The stamp a ``restic.env`` moved aside carries, the one the Imbue Studio desktop gives its own archived copies.
ENV_ARCHIVE_TIMESTAMP_FORMAT: Final[str] = "%Y%m%dT%H%M%SZ"
_ARCHIVE_NAME: Final[re.Pattern[str]] = re.compile(
    re.escape(RESTIC_ENV_PATH.name) + r"\.(\d{8}T\d{6}Z)(?:-(\d+))?"
)

REPOSITORY_KEY: Final[str] = "RESTIC_REPOSITORY"
PASSWORD_KEY: Final[str] = "RESTIC_PASSWORD"
REST_USERNAME_KEY: Final[str] = "RESTIC_REST_USERNAME"
REST_PASSWORD_KEY: Final[str] = "RESTIC_REST_PASSWORD"
_REST_REPOSITORY_PREFIX: Final[str] = "rest:"

_INSTALLED_ENV_HEADER: Final[str] = (
    "# Written by this workspace's backup-setup (system/libs/workspace_backup_setup). The workspace made\n"
    "# RESTIC_PASSWORD itself; it is the repository's only key.\n"
)
_PENDING_REQUEST_PREFIX: Final[str] = "# backup-setup request "
# A credential written into a repository's address, between the scheme and the host. A password may itself
# hold an "@", so the host begins after the last one.
_ADDRESS_CREDENTIAL: Final[re.Pattern[str]] = re.compile(r"(?<=://)[^/]*@")


class BackupDestinationKind(UpperCaseStrEnum):
    """Where a workspace's backups go."""

    NONE = auto()
    IMBUE_CLOUD = auto()
    OWN_STORAGE = auto()


def env_digest(env_text: str) -> str:
    """The digest a client compares its copy of the credentials by: the SHA-256 of the text, in hex."""
    return hashlib.sha256(env_text.encode("utf-8")).hexdigest()


def write_atomically(path: Path, text: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, scratch = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    with os.fdopen(handle, "w", encoding="utf-8") as out:
        out.write(text)
    os.chmod(scratch, mode)
    os.replace(scratch, path)


def read_restic_env_text(workspace_root: Path) -> str | None:
    """The text of the ``restic.env`` in use; None when there is none."""
    try:
        return (
            (workspace_root / RESTIC_ENV_PATH)
            .read_bytes()
            .decode("utf-8", errors="replace")
        )
    except FileNotFoundError:
        return None


def repository_without_credentials(repository: str) -> str:
    """A repository's address with any credential written into it taken out."""
    return _ADDRESS_CREDENTIAL.sub("", repository)


def storage_of(env_values: Mapping[str, str]) -> dict[str, str]:
    """The repository and the variables of its storage: everything but the repository's password."""
    return {key: value for key, value in env_values.items() if key != PASSWORD_KEY}


def destination_kind_of(
    env_values: Mapping[str, str], workspace_id: str
) -> BackupDestinationKind:
    """Which destination a ``restic.env`` is for, read off the file.

    Imbue Cloud names each repository for its workspace and has restic sign in as the workspace's id, so it is
    a ``rest:`` address whose user name is this workspace's own id; a process that does not know its
    workspace's id compares the user name with the last part of the address instead. Anything else is storage
    of the user's own.
    """
    repository = env_values.get(REPOSITORY_KEY, "")
    username = env_values.get(REST_USERNAME_KEY, "")
    if not repository.startswith(_REST_REPOSITORY_PREFIX) or not username:
        return BackupDestinationKind.OWN_STORAGE
    expected_username = workspace_id or repository.rstrip("/").rpartition("/")[2]
    return (
        BackupDestinationKind.IMBUE_CLOUD
        if username == expected_username
        else BackupDestinationKind.OWN_STORAGE
    )


def _render_pairs(env_values: Mapping[str, str]) -> str:
    """One ``KEY=value`` line each, unquoted, as the backup service's parser reads them back."""
    return "".join(f"{key}={value}\n" for key, value in env_values.items())


def render_restic_env(env_values: Mapping[str, str]) -> str:
    """The text of a ``restic.env`` this library installs: the repository, its storage, and its password last."""
    ordered = {
        REPOSITORY_KEY: env_values.get(REPOSITORY_KEY, ""),
        **{
            key: value
            for key, value in env_values.items()
            if key not in (REPOSITORY_KEY, PASSWORD_KEY)
        },
        PASSWORD_KEY: env_values.get(PASSWORD_KEY, ""),
    }
    return _INSTALLED_ENV_HEADER + _render_pairs(ordered)


def write_pending_env(
    workspace_root: Path, request_id: str, env_values: Mapping[str, str]
) -> None:
    """Keep the credentials of the setup ``request_id`` asked for until they are installed."""
    write_atomically(
        workspace_root / PENDING_ENV_RELATIVE_PATH,
        f"{_PENDING_REQUEST_PREFIX}{request_id}\n" + _render_pairs(env_values),
        SECRET_FILE_MODE,
    )


def read_pending_env(workspace_root: Path, request_id: str) -> dict[str, str] | None:
    """The credentials kept for the setup ``request_id`` asked for; None when none are, or they are another's."""
    try:
        text = (workspace_root / PENDING_ENV_RELATIVE_PATH).read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    if text.partition("\n")[0] != f"{_PENDING_REQUEST_PREFIX}{request_id}":
        return None
    return parse_restic_env_file(text)


def _archive_path(workspace_root: Path, now: float) -> Path:
    """A name beside ``restic.env`` that no kept copy has yet."""
    stamp = datetime.fromtimestamp(now, timezone.utc).strftime(
        ENV_ARCHIVE_TIMESTAMP_FORMAT
    )
    path = workspace_root / RESTIC_ENV_PATH
    candidate = path.with_name(f"{path.name}.{stamp}")
    sequence = 0
    while candidate.exists():
        sequence += 1
        candidate = path.with_name(f"{path.name}.{stamp}-{sequence}")
    return candidate


def keep_restic_env_aside(workspace_root: Path, now: float) -> bool:
    """Move the ``restic.env`` in use to ``restic.env.<timestamp>``; whether there was one to move."""
    try:
        os.replace(workspace_root / RESTIC_ENV_PATH, _archive_path(workspace_root, now))
    except FileNotFoundError:
        return False
    return True


def drop_pending_env(workspace_root: Path, now: float) -> None:
    """Let go of the credentials of a setup that will not finish.

    Ones that hold a password are kept aside instead of deleted: the repository may already be initialised
    with it, and then it is the only key.
    """
    pending_path = workspace_root / PENDING_ENV_RELATIVE_PATH
    try:
        text = pending_path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        pending_path.unlink(missing_ok=True)
        return
    env_values = parse_restic_env_file(text)
    if (
        env_values.get(PASSWORD_KEY)
        and env_values.get(REPOSITORY_KEY)
        and env_values not in kept_envs(workspace_root)
    ):
        write_atomically(
            _archive_path(workspace_root, now),
            render_restic_env(env_values),
            SECRET_FILE_MODE,
        )
    pending_path.unlink(missing_ok=True)


def install_restic_env(workspace_root: Path, env_text: str, now: float) -> None:
    """Put ``env_text`` in place as ``restic.env``, keeping a copy of one with other contents beside it first.

    A file with the same contents is left alone, so its mtime does not move and the backup service starts no
    extra backup. The file is replaced in one step, so the backup service never finds it missing.
    """
    path = workspace_root / RESTIC_ENV_PATH
    existing_text = read_restic_env_text(workspace_root)
    if existing_text == env_text:
        os.chmod(path, SECRET_FILE_MODE)
        return
    if existing_text is not None:
        write_atomically(
            _archive_path(workspace_root, now), existing_text, SECRET_FILE_MODE
        )
    write_atomically(path, env_text, SECRET_FILE_MODE)


def kept_envs(workspace_root: Path) -> list[dict[str, str]]:
    """Every ``restic.env`` kept aside that names a repository and its password, the newest first."""
    directory = (workspace_root / RESTIC_ENV_PATH).parent
    stamped: list[tuple[str, int, Path]] = []
    try:
        candidates = list(directory.iterdir())
    except OSError:
        return []
    for candidate in candidates:
        match = _ARCHIVE_NAME.fullmatch(candidate.name)
        if match is not None:
            stamped.append((match.group(1), int(match.group(2) or 0), candidate))
    kept: list[dict[str, str]] = []
    for _stamp, _sequence, path in sorted(stamped, reverse=True):
        try:
            env_values = parse_restic_env_file(
                path.read_text(encoding="utf-8", errors="replace")
            )
        except OSError:
            continue
        if env_values.get(REPOSITORY_KEY) and env_values.get(PASSWORD_KEY):
            kept.append(env_values)
    return kept


def kept_env_for_destination(
    workspace_root: Path, destination: BackupDestinationKind, workspace_id: str
) -> dict[str, str] | None:
    """The newest ``restic.env`` kept aside for ``destination``; None when the workspace kept none."""
    for env_values in kept_envs(workspace_root):
        if destination_kind_of(env_values, workspace_id) is destination:
            return env_values
    return None


def kept_password_for_repository(workspace_root: Path, repository: str) -> str | None:
    """The password this workspace keeps for ``repository``: the one in use, else the newest kept aside."""
    in_use_text = read_restic_env_text(workspace_root)
    in_use = parse_restic_env_file(in_use_text) if in_use_text is not None else {}
    for env_values in (in_use, *kept_envs(workspace_root)):
        if env_values.get(REPOSITORY_KEY) == repository and env_values.get(
            PASSWORD_KEY
        ):
            return env_values[PASSWORD_KEY]
    return None
