from functools import cached_property
from pathlib import Path

from imbue.imbue_common.frozen_model import FrozenModel
from pydantic import AwareDatetime, Field, computed_field, field_validator

from terminal_app.errors import InvalidTerminalValueError
from terminal_app.primitives import (
    TerminalTitle,
    TmuxSessionId,
    TmuxSessionName,
    Workdir,
)


class TmuxSession(FrozenModel):
    """One session on the default tmux server, as ``tmux list-sessions`` reports it."""

    name: str = Field(description="The session name")
    session_id: str = Field(description="The immutable tmux id, such as $3")
    # tmux hands ids out afresh on every server, so the id alone names a session only for one
    # server's lifetime; with the creation time it names one for good.
    created_epoch: int | None = Field(
        description="When the session was created, as tmux's epoch seconds; None when tmux gave none"
    )
    last_activity: AwareDatetime | None = Field(
        description="When the session last saw activity, in UTC; None when tmux gave none"
    )


class TerminalSessionRecord(FrozenModel):
    """What the app remembers about a terminal beyond tmux: that it exists, what the user named it, and where its shell starts."""

    name: TmuxSessionName = Field(description="The tmux session name, which is the terminal's name")
    title: TerminalTitle | None = Field(
        description="The title the user gave it; None when the title derives from the name"
    )
    workdir: Workdir | None = Field(
        description="The directory a newly created session starts in; None for the default"
    )
    session_id: TmuxSessionId | None = Field(
        default=None,
        description="tmux's immutable id of the session backing this terminal; None while no session does (stopped, or never created or adopted)",
    )
    session_created: int | None = Field(
        default=None,
        description="When that session was created (tmux's epoch seconds), which tells it apart from a later server's session under the same id",
    )
    is_stopped: bool = Field(
        default=False,
        description="Whether the user stopped this terminal, so its session is not recreated at startup",
    )


class TerminalListing(FrozenModel):
    """One terminal as the app lists it: a live session, or a remembered one tmux no longer has."""

    name: TmuxSessionName = Field(description="The terminal's name, which is its tmux session name")
    title: TerminalTitle = Field(description="What its window is called")
    is_stopped: bool = Field(description="Whether no live session backs it")
    last_activity: AwareDatetime | None = Field(
        description="When its session last saw activity, in UTC; None when stopped or unknown"
    )


class TerminalStoreDocument(FrozenModel):
    """The whole of the terminal's ``instances.json``."""

    version: int = Field(description="The document format version")
    sessions: tuple[TerminalSessionRecord, ...] = Field(
        description="Every remembered terminal, in creation order"
    )


class TerminalPaths(FrozenModel):
    """Where the terminal app keeps its machine state (dispatch scripts and session id files), all under one directory."""

    state_dir: Path = Field(
        description="The app's state directory (data/.state/terminal under the repo root), absolute so the dispatch scripts can embed it"
    )

    @field_validator("state_dir")
    @classmethod
    def _require_an_absolute_state_dir(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise InvalidTerminalValueError(
                f"invalid state directory {str(value)!r}: it must be absolute, since the dispatch scripts embed it"
            )
        return value

    @computed_field
    @cached_property
    def commands_dir(self) -> Path:
        """The ttyd dispatch scripts; the dispatch snippet runs ``<commands_dir>/<key>.sh``."""
        return self.state_dir / "commands"

    @computed_field
    @cached_property
    def sessions_dir(self) -> Path:
        """One file per terminal the app created or adopted, named by terminal and holding the tmux session id and creation time the dispatch attaches by."""
        return self.state_dir / "sessions"

    @computed_field
    @cached_property
    def ttyd_index_path(self) -> Path:
        """Where the vendored OSC 52-capable ttyd web client is decompressed to."""
        return self.commands_dir / "index.html"
