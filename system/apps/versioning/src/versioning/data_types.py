from datetime import datetime
from enum import auto
from typing import Final

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from pydantic import Field


class VersioningError(Exception):
    """Base exception for every error raised by the versioning app."""

    ...


class AppNotFoundError(VersioningError, KeyError):
    """Raised when a requested app has no folder or no history."""

    def __init__(self, app_name: str) -> None:
        self.app_name = app_name
        super().__init__(f"No app found for '{app_name}'")


class GitReadError(VersioningError, OSError):
    """Raised when git cannot be read (bad repo, bad sha, command failure)."""

    ...


class RestoreError(VersioningError, RuntimeError):
    """Raised when a restore cannot be performed safely."""

    ...


class SummaryGenerationError(VersioningError, ValueError):
    """Raised when Claude's response cannot be read as the requested record."""

    ...


class VersionKind(UpperCaseStrEnum):
    """What kind of change a commit records, from the user's point of view."""

    BUILD = auto()
    CHANGE = auto()
    FIX = auto()
    HARDEN = auto()
    RESTORE = auto()
    PORT = auto()


# Kinds that represent something the user asked for; these are milestones on the
# main timeline. HARDEN (turn-end machinery commits) hides behind "More versions".
MILESTONE_KINDS: Final[frozenset[VersionKind]] = frozenset(
    {VersionKind.BUILD, VersionKind.CHANGE, VersionKind.FIX, VersionKind.RESTORE, VersionKind.PORT}
)


class ChangeKind(UpperCaseStrEnum):
    """How one file changed within a version."""

    ADDED = auto()
    EDITED = auto()
    REMOVED = auto()


class FileChange(FrozenModel):
    """One file's change within a version, in user-facing terms."""

    display_path: str = Field(description="The file's path shown to the user, relative to the app folder")
    change_kind: ChangeKind = Field(description="Whether the file was added, edited, or removed")


class ChangeStats(FrozenModel):
    """How big one version's change was, counted from its diff."""

    files_changed: int = Field(description="How many files the version touched")
    lines_changed: int = Field(description="Lines added plus lines removed across those files")


class TrailerBlock(FrozenModel):
    """The parsed Versioning-* trailers of one commit message."""

    app_name: str | None = Field(default=None, description="Value of Versioning-App")
    request: str | None = Field(
        default=None, description="Value of Versioning-Request: the change described in plain language"
    )
    kind: VersionKind | None = Field(default=None, description="Value of Versioning-Kind")
    restored_from_sha: str | None = Field(default=None, description="Value of Versioning-Restored-From")
    ported_from_sha: str | None = Field(default=None, description="Value of Versioning-Ported-From")


class AppRef(FrozenModel):
    """One versionable app: its folder in the repo and how it is served."""

    name: str = Field(description="The app's registered service name, e.g. 'science-explorer'")
    package_dir: str = Field(description="Repo-relative folder holding the app, e.g. 'system/apps/science_explorer'")
    title: str = Field(description="Human-readable app name shown in the UI")
    program: str | None = Field(
        default=None, description="Supervisord program that serves the app, if it runs as a service"
    )
    icon: str | None = Field(
        default=None,
        description="The app's registered SVG icon markup, when it has one. Authored by a skill, so untrusted: "
        "every surface that draws it must sanitize it first",
    )


class CommitRecord(FrozenModel):
    """One git commit that touched an app's folder."""

    sha: str = Field(description="Full commit sha")
    author: str = Field(description="Author name, which identifies the chat session that made it")
    authored_at: datetime = Field(description="When the commit was made (UTC)")
    subject: str = Field(description="First line of the commit message")
    body: str = Field(description="Rest of the commit message, may be empty")
    trailers: TrailerBlock = Field(description="The parsed Versioning-* trailer block")


class VersionNode(FrozenModel):
    """One user-facing version. One commit is one version: no grouping anywhere."""

    sha: str = Field(description="The commit this version is; also the node's stable id")
    raw_title: str = Field(description="The Versioning-Request trailer when present, else the commit subject")
    is_titled_by_request: bool = Field(
        default=False, description="Whether raw_title came from a Versioning-Request trailer"
    )
    kind: VersionKind | None = Field(default=None, description="The version's kind, when recorded")
    authored_at: datetime = Field(description="When the commit was made")
    parent_sha: str | None = Field(
        default=None, description="The version this one was built on, None for the first"
    )
    restored_from_sha: str | None = Field(
        default=None, description="For a restore version, the version it restored the app to"
    )
    ported_from_sha: str | None = Field(
        default=None, description="For a brought-back feature, the version it came from"
    )
    is_current: bool = Field(default=False, description="Whether this is the version the app is on now")
    is_set_aside: bool = Field(
        default=False, description="Whether a later restore moved history off this version's arm"
    )
    is_milestone: bool = Field(
        default=True, description="Whether this belongs on the main timeline (vs behind 'More versions')"
    )
    change_stats: ChangeStats | None = Field(
        default=None, description="How big this version's change was, when the diff could be read"
    )


class AppHistory(FrozenModel):
    """The full derived version tree for one app."""

    app: AppRef = Field(description="The app this history belongs to")
    nodes: tuple[VersionNode, ...] = Field(description="All version nodes, oldest first")


class VersionSummary(FrozenModel):
    """The cached plain-language description of one version."""

    sha: str = Field(description="The commit this summary describes")
    title: str = Field(description="A milestone name of a few words, e.g. 'Follow-up questions'")
    description: str = Field(description="One or two sentences describing what changed in this version")


class AssistOutcome(FrozenModel):
    """One exchange with the version helper: the answer, and the change if it made one."""

    answer: str = Field(description="The helper's plain-language reply, shown in the conversation")
    new_version_sha: str | None = Field(
        default=None, description="The new version recorded when the helper changed the app"
    )


class RestorePreview(FrozenModel):
    """What a restore would do, shown before the user confirms."""

    target_sha: str = Field(description="The commit the app folder would be restored to")
    changed_file_count: int = Field(description="How many files in the app folder would change")
    set_aside_node_count: int = Field(description="How many later versions would be set aside")
    diff_stat: str = Field(description="Raw `git diff --stat` output between the target and now")


class RestoreResult(FrozenModel):
    """What actually happened when a restore was applied."""

    restore_commit_sha: str = Field(description="The new commit that recorded the restore")
    is_service_restarted: bool = Field(description="Whether the app's service was restarted afterwards")
    is_dependency_sync_run: bool = Field(
        description="Whether dependencies were re-synced because the app's pyproject changed"
    )
