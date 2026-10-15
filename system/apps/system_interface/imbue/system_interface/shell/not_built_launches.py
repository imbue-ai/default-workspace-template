"""The launches the not-built placeholder runs while there is no interface to run them from (desktop contracts.md
section 5.1): a terminal session for the page to frame, and an agent asked to repair the interface."""

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


class NotBuiltLaunchPurpose(LowerCaseStrEnum):
    """What the placeholder asks to launch."""

    TERMINAL = auto()
    REPAIR_AGENT = auto()


class NotBuiltLaunchRequest(FrozenModel):
    """The body of ``POST /api/not-built/launch``."""

    purpose: NotBuiltLaunchPurpose = Field(description="A terminal session, or an agent asked to repair the interface")


class NotBuiltLaunch(FrozenModel):
    """A launch the placeholder runs: the app, its launch path, and the params it is run with."""

    entry: AppInventoryEntry = Field(description="The app the launch runs on")
    launch_path: RegistryLaunchPath = Field(description="The launch path it runs")
    params: dict[str, str] = Field(description="The params it is run with")


@pure
def terminal_launch(entries: Sequence[AppInventoryEntry]) -> NotBuiltLaunch | None:
    """A new session of the terminal app, or None when no terminal is registered or it offers no ``new``."""
    entry = next((candidate for candidate in entries if candidate.row.name == TERMINAL_APP_NAME), None)
    if entry is None:
        return None
    launch_path = next((path for path in effective_launch_paths(entry.row) if path.id == TERMINAL_NEW_LAUNCH_ID), None)
    if launch_path is None:
        return None
    return NotBuiltLaunch(entry=entry, launch_path=launch_path, params={})


@pure
def repair_agent_launch(entries: Sequence[AppInventoryEntry], message: str) -> NotBuiltLaunch | None:
    """``message`` sent through the workspace's primary text action (the launcher's first free-text row that sends
    rather than drafts, Chat's New Chat on a stock workspace), or None when no app offers one."""
    by_name = {candidate.row.name: candidate for candidate in entries}
    for row in launcher_order([candidate.row for candidate in entries]):
        if row.internal:
            continue
        for launch_path in effective_launch_paths(row):
            if launch_path.text_param is not None:
                return NotBuiltLaunch(
                    entry=by_name[row.name], launch_path=launch_path, params={str(launch_path.text_param): message}
                )
    return None
