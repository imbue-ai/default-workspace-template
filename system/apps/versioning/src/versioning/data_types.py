from datetime import datetime
from enum import auto

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from pydantic import Field


class VersioningError(Exception):
    """Base exception for every error raised by the versioning app."""

    ...


class AppNotFoundError(VersioningError, KeyError):
    def __init__(self, app_name: str) -> None:
        self.app_name = app_name
        super().__init__(f"No app found for '{app_name}'")


class GitReadError(VersioningError, OSError):
    ...


class RestoreError(VersioningError, RuntimeError):
    ...


class SummaryGenerationError(VersioningError, ValueError):
    ...


class VersionKind(UpperCaseStrEnum):
    BUILD = auto()
    CHANGE = auto()
    FIX = auto()
    HARDEN = auto()
    RESTORE = auto()
    PORT = auto()


class ChangeKind(UpperCaseStrEnum):
    ADDED = auto()
    EDITED = auto()
    REMOVED = auto()


class FileChange(FrozenModel):
    display_path: str
    change_kind: ChangeKind


class ChangeStats(FrozenModel):
    files_changed: int
    lines_changed: int


class TrailerBlock(FrozenModel):
    """The parsed Versioning-* trailers of one commit message."""

    app_name: str | None = None
    request: str | None = None
    kind: VersionKind | None = None
    restored_from_sha: str | None = None
    ported_from_sha: str | None = None


class AppRef(FrozenModel):
    name: str
    package_dir: str
    title: str
    program: str | None = None
    # Authored by a skill, so untrusted: every surface that draws it must sanitize it.
    icon: str | None = None


class CommitRecord(FrozenModel):
    sha: str
    # The author name identifies the chat session that made the commit.
    author: str
    authored_at: datetime
    subject: str
    body: str
    trailers: TrailerBlock


class VersionNode(FrozenModel):
    """One user-facing version. One commit is one version: no grouping anywhere."""

    sha: str
    raw_title: str
    is_titled_by_request: bool = False
    kind: VersionKind | None = None
    authored_at: datetime
    parent_sha: str | None = None
    restored_from_sha: str | None = None
    ported_from_sha: str | None = None
    is_current: bool = False
    # Whether a later restore moved history off this version's arm.
    is_set_aside: bool = False
    change_stats: ChangeStats | None = None


class AppHistory(FrozenModel):
    app: AppRef
    nodes: tuple[VersionNode, ...] = Field(description="All version nodes, oldest first")


class VersionSummary(FrozenModel):
    sha: str
    title: str
    description: str


class AssistOutcome(FrozenModel):
    answer: str
    new_version_sha: str | None = None


class RestorePreview(FrozenModel):
    target_sha: str
    changed_file_count: int
    set_aside_node_count: int
    diff_stat: str


class RestoreResult(FrozenModel):
    restore_commit_sha: str
    is_service_restarted: bool
    is_dependency_sync_run: bool
