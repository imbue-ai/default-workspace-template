"""The app record: one tracked file per user app, ``system/config/apps/<name>.toml``.

A record says which directory under ``system/apps/`` is the app, the port its main program
listens on, where the app came from, and the repositories it is kept in step with. It is one
file per app so that two branches that each add an app never edit the same file. Every app
under ``system/apps/`` without a record is a built-in app.
"""

import json
import tomllib
from collections.abc import Iterable
from enum import auto
from pathlib import Path
from typing import Final
from typing import Literal

from pydantic import Field
from pydantic import ValidationError

from app_manifest.errors import AppRecordNotFoundError
from app_manifest.errors import RecordLoadError
from app_manifest.primitives import AppName
from imbue.imbue_common.enums import LowerCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel

RECORDS_DIRECTORY: Final[Path] = Path("system/config/apps")
_APPS_DIRECTORY: Final[str] = "system/apps"
# The files in an app's directory that are a function of other files there: on a conflict the upstream side is
# taken and the file is made again, and a difference in one alone does not make an installed app modified.
REGENERABLE_FILENAMES: Final[frozenset[str]] = frozenset({"uv.lock"})
LOWEST_PORT: Final[int] = 1
HIGHEST_PORT: Final[int] = 65535
_RECORD_HEADER: Final[str] = (
    "# This app's record, kept by app-install: which directory is the app, its port, and where it came from.\n"
    "# Change it with the app-install command rather than by hand.\n"
)


class AppOrigin(LowerCaseStrEnum):
    """Where a user app came from."""

    # Built in this workspace.
    LOCAL = auto()
    # Added from someone else's published repository.
    INSTALLED = auto()


class RecordReference(FrozenModel):
    """Something elsewhere in this workspace that uses the app, and how."""

    path: str = Field(description="A repo-root-relative file or directory that uses the app")
    note: str = Field(default="", description="One line saying which of the app's surfaces it uses")


class AppRecord(FrozenModel):
    """What this workspace records about one user app."""

    name: AppName = Field(description="The manifest's name, and the record file's stem")
    package: str = Field(min_length=1, description="The app's directory name under system/apps/")
    port: int = Field(ge=LOWEST_PORT, le=HIGHEST_PORT, description="The loopback port of the app's main program")
    origin: AppOrigin = Field(description="Whether this workspace built the app or installed it")
    upstream_url: str = Field(default="", description="For an installed app, the repository it came from")
    upstream_commit: str = Field(default="", description="The upstream commit last taken")
    upstream_tree: str = Field(default="", description="That commit's tree")
    upstream_version: str = Field(default="", description="The app's version at that commit")
    dev_repo_url: str = Field(default="", description="The app's private dev repository, when it has one")
    published_repo_url: str = Field(default="", description="The repository this workspace publishes the app to")
    published_commit: str = Field(default="", description="The commit last published from this workspace")
    published_version: str = Field(default="", description="The version last published from this workspace")
    published_visibility: Literal["", "private", "public"] = Field(
        default="",
        description="Who can see the published repository, as this workspace last set it; empty when it never did",
    )
    verified_template: str = Field(
        default="", description="The template version this workspace last ran the app's tests against successfully"
    )
    references: tuple[RecordReference, ...] = Field(
        default=(), description="Paths elsewhere in this workspace that use the app"
    )


def app_directory_prefix(record: AppRecord) -> str:
    """The app's directory relative to the workspace repo's root, which is the prefix ``git subtree`` knows it by."""
    return f"{_APPS_DIRECTORY}/{record.package}"


def join_marker_pattern(record: AppRecord) -> str:
    """The ``git log --grep`` pattern of the commits that join the app's directory to a history of its own.

    ``git subtree`` leaves such a commit when an app is installed or updated (a squash) and when its first split
    is made with ``--rejoin``. It starts a later split from the newest one made with ``--rejoin``; a squash only
    tells it which upstream commit the app's history builds on. An app whose workspace history holds either is
    linked; the pattern is the one ``git subtree`` itself searches with.
    """
    return f"^git-subtree-dir: {app_directory_prefix(record)}/*$"


def modifying_paths(differing_paths: Iterable[str]) -> tuple[str, ...]:
    """Of the paths in which an installed app's directory differs from what was installed, the ones that make
    the app modified: every one that is not a regenerable file."""
    return tuple(path for path in differing_paths if Path(path).name not in REGENERABLE_FILENAMES)


def record_path(repo_root: Path, name: str) -> Path:
    """Where the record of the app registered as ``name`` is kept."""
    return repo_root / RECORDS_DIRECTORY / f"{name}.toml"


def _toml_string(value: str) -> str:
    # A JSON string is a valid TOML basic string: both escape the quote, the backslash and the control characters.
    return json.dumps(value, ensure_ascii=False)


def render_record(record: AppRecord) -> str:
    """The record as the TOML text its file holds, the same text for the same record."""
    lines = [
        _RECORD_HEADER.rstrip("\n"),
        f"name = {_toml_string(record.name)}",
        f"package = {_toml_string(record.package)}",
        f"port = {record.port}",
        f"origin = {_toml_string(record.origin.value)}",
        f"upstream_url = {_toml_string(record.upstream_url)}",
        f"upstream_commit = {_toml_string(record.upstream_commit)}",
        f"upstream_tree = {_toml_string(record.upstream_tree)}",
        f"upstream_version = {_toml_string(record.upstream_version)}",
        f"dev_repo_url = {_toml_string(record.dev_repo_url)}",
        f"published_repo_url = {_toml_string(record.published_repo_url)}",
        f"published_commit = {_toml_string(record.published_commit)}",
        f"published_version = {_toml_string(record.published_version)}",
        f"published_visibility = {_toml_string(record.published_visibility)}",
        f"verified_template = {_toml_string(record.verified_template)}",
    ]
    for reference in record.references:
        lines.extend(
            ["", "[[references]]", f"path = {_toml_string(reference.path)}", f"note = {_toml_string(reference.note)}"]
        )
    return "\n".join(lines) + "\n"


def parse_record(text: str, source: Path) -> AppRecord:
    """The record ``text`` holds; ``source`` is named in the error when it is not one."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise RecordLoadError(f"app record {source} is not valid TOML: {e}") from e
    try:
        record = AppRecord.model_validate(data)
    except ValidationError as e:
        first = e.errors()[0]
        location = ".".join(str(part) for part in first["loc"]) or "<root>"
        raise RecordLoadError(f"app record {source} is invalid: field {location!r}: {first['msg']}") from e
    if source.stem != record.name:
        raise RecordLoadError(
            f"app record {source} names the app {str(record.name)!r}; its file must be named after it"
        )
    return record


def load_record(repo_root: Path, name: str) -> AppRecord:
    """The record of the app registered as ``name``; raises AppRecordNotFoundError when it has none."""
    path = record_path(repo_root, name)
    if not path.is_file():
        raise AppRecordNotFoundError(f"no app record for {name!r} (expected {path.relative_to(repo_root)})")
    return parse_record(path.read_text(encoding="utf-8"), path)


def load_all_records(repo_root: Path) -> tuple[AppRecord, ...]:
    """Every app record of the workspace, by name; none when the workspace has no user app."""
    directory = repo_root / RECORDS_DIRECTORY
    if not directory.is_dir():
        return ()
    return tuple(
        parse_record(path.read_text(encoding="utf-8"), path)
        for path in sorted(directory.glob("*.toml"))
        if path.is_file()
    )


def write_record(repo_root: Path, record: AppRecord) -> Path:
    """Write ``record`` to its file, creating the records directory when this is the workspace's first user app."""
    path = record_path(repo_root, record.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_record(record), encoding="utf-8")
    return path
