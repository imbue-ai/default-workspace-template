from enum import StrEnum
from functools import cached_property
from pathlib import Path

from app_instances.primitives import InstanceTitle
from imbue.imbue_common.frozen_model import FrozenModel
from pydantic import AwareDatetime, Field, computed_field, field_validator

from terminal_app.errors import InvalidTerminalValueError
from terminal_app.primitives import ClientTty, TmuxSessionName, Workdir


class TmuxHookKind(StrEnum):
    """Which tmux hook fired; the values are the wire strings the hook script has always posted."""

    SESSION_CHANGED = "session-changed"
    SESSION_RENAMED = "session-renamed"


class TmuxHookEvent(FrozenModel):
    """What the tmux hook script posts to the app's own ``/tmux-hook``.

    Names and ids are plain strings here because a hand-made tmux session may carry any name;
    each handler validates what it needs.
    """

    kind: TmuxHookKind = Field(description="Which hook fired")
    client_tty: str = Field(
        description="The switching client's pty for session-changed; empty for a rename"
    )
    session_name: str = Field(description="The session's (new) name")
    session_id: str = Field(description="The session's immutable tmux id, such as $3")


class TmuxSession(FrozenModel):
    """One session on the default tmux server, as ``tmux list-sessions`` reports it."""

    name: str = Field(description="The session name")
    session_id: str = Field(description="The immutable tmux id, such as $3")
    last_activity: AwareDatetime | None = Field(
        description="When the session last saw activity, in UTC; None when tmux gave none"
    )


class TmuxClient(FrozenModel):
    """One client attached to the default tmux server, as ``tmux list-clients`` reports it."""

    client_tty: ClientTty = Field(description="The pty the client is attached through")
    session_name: str = Field(description="The session the client currently shows")
    session_id: str = Field(description="That session's immutable tmux id")


class TerminalSessionRecord(FrozenModel):
    """What the app remembers about a terminal beyond tmux: that it exists, what the user named it, and where its shell starts."""

    name: TmuxSessionName = Field(
        description="The tmux session name, which is the instance key"
    )
    title: InstanceTitle | None = Field(
        description="The title the user gave it; None when the title derives from the name"
    )
    workdir: Workdir | None = Field(
        description="The directory a newly created session starts in; None for the default"
    )


class TerminalStoreDocument(FrozenModel):
    """The whole of the terminal's ``instances.json``."""

    version: int = Field(description="The document format version")
    sessions: tuple[TerminalSessionRecord, ...] = Field(
        description="Every remembered terminal, in creation order"
    )


class TerminalPaths(FrozenModel):
    """Where the terminal app keeps its machine state (the dispatch scripts and the pty-to-tab records), all under one directory.

    The store of remembered terminals is app data rather than machine state and lives under
    ``data/.apps/terminal/`` instead (contracts.md section 17).
    """

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
    def clients_dir(self) -> Path:
        """One file per attached tab, named by tab id and holding the client's pty."""
        return self.commands_dir / "clients"

    @computed_field
    @cached_property
    def ttyd_index_path(self) -> Path:
        """Where the vendored OSC 52-capable ttyd web client is decompressed to."""
        return self.commands_dir / "index.html"
