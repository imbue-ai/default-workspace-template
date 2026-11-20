"""The launches the shell's recovery page runs while there is no interface to run them from (desktop contracts.md
section 5.1): a terminal session for the page to frame, and an agent asked to repair what the page is about."""

from collections.abc import Sequence
from enum import auto
from typing import Final

from app_manifest.registry import RegistryLaunchPath
from pydantic import Field

from imbue.imbue_common.enums import LowerCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.system_interface.shell.data_types import AppInventoryEntry
from imbue.system_interface.shell.data_types import effective_launch_paths
from imbue.system_interface.shell.desktop_document import launcher_order

TERMINAL_APP_NAME: Final[str] = "terminal"
# The terminal's launch path that allocates a session and answers its page (system/apps/terminal/app.toml).
TERMINAL_NEW_LAUNCH_ID: Final[str] = "new"


class RecoveryReason(LowerCaseStrEnum):
    """Why the shell serves its recovery page: its frontend is not built, or the read of the desktops and apps the
    frontend boots from failed."""

    INTERFACE_NOT_BUILT = auto()
    DESKTOPS_NOT_LOADED = auto()


class RecoveryLaunchPurpose(LowerCaseStrEnum):
    """What the recovery page asks to launch."""

    TERMINAL = auto()
    REPAIR_AGENT = auto()


class RecoveryLaunchRequest(FrozenModel):
    """The body of ``POST /api/recovery/launch``."""

    reason: RecoveryReason = Field(description="What the page is about, which the repair agent is told")
    purpose: RecoveryLaunchPurpose = Field(description="A terminal session, or an agent asked to repair it")


class RecoveryLaunchAnswer(FrozenModel):
    """What ``POST /api/recovery/launch`` answers: where the launch's page is."""

    label: str = Field(description="The app's origin label")
    path: str = Field(description="The page the launch opened")
    display_name: str = Field(description="The app's name")


class RecoveryLaunch(FrozenModel):
    """A launch the recovery page runs: the app, its launch path, and the params it is run with."""

    entry: AppInventoryEntry = Field(description="The app the launch runs on")
    launch_path: RegistryLaunchPath = Field(description="The launch path it runs")
    params: dict[str, str] = Field(description="The params it is run with")


@pure
def terminal_launch(entries: Sequence[AppInventoryEntry]) -> RecoveryLaunch | None:
    """A new session of the terminal app, or None when no terminal is registered or it offers no ``new``."""
    entry = next((candidate for candidate in entries if candidate.row.name == TERMINAL_APP_NAME), None)
    if entry is None:
        return None
    launch_path = next((path for path in effective_launch_paths(entry.row) if path.id == TERMINAL_NEW_LAUNCH_ID), None)
    if launch_path is None:
        return None
    return RecoveryLaunch(entry=entry, launch_path=launch_path, params={})


@pure
def repair_agent_launch(entries: Sequence[AppInventoryEntry], message: str) -> RecoveryLaunch | None:
    """``message`` sent through the first launch path, in launcher order, that sends typed text (a ``text_param``;
    a draft row is skipped, since the repair has to be sent); None when no app offers one. That is the launcher's
    primary text action, the main chat on a stock workspace, where the desktop sends an ask that is about no app."""
    by_name = {candidate.row.name: candidate for candidate in entries}
    primary_text_row = next(
        (
            (row, launch_path)
            for row in launcher_order([candidate.row for candidate in entries])
            if not row.internal
            for launch_path in effective_launch_paths(row)
            if launch_path.text_param is not None
        ),
        None,
    )
    if primary_text_row is None:
        return None
    row, launch_path = primary_text_row
    return RecoveryLaunch(
        entry=by_name[row.name], launch_path=launch_path, params={str(launch_path.text_param): message}
    )
