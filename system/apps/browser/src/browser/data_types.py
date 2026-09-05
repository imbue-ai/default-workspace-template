from enum import auto

from imbue.imbue_common.enums import LowerCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from pydantic import Field

from browser.primitives import BrowserName


class BrowserLifecycle(LowerCaseStrEnum):
    """Where a browser is in its life: registered but launching, up, or dead (the ``Lifecycle`` strings of ``session.py``)."""

    INIT = auto()
    RUNNING = auto()
    CRASHED = auto()


class BrowserController(LowerCaseStrEnum):
    """Who controls a browser right now (the ``ControlOwner`` strings of ``session.py``)."""

    HUMAN = auto()
    AGENT = auto()


class BrowserSnapshot(FrozenModel):
    """One browser as the instances adapter sees it: its name and the two facts its status derives from."""

    name: BrowserName = Field(
        description="The browser's name, which is its instance key"
    )
    lifecycle: BrowserLifecycle = Field(description="Launching, running, or crashed")
    controller: BrowserController = Field(
        description="Who holds control: the human (also when nobody does) or an agent"
    )
