"""What the app's routes answer: the stats reading the page polls, and the health probe."""

from typing import Literal

from imbue.imbue_common.frozen_model import FrozenModel
from pydantic import Field


class MemoryFigures(FrozenModel):
    """Memory in bytes, the figures the memory guard acts on (``memory.read_meminfo``)."""

    total: int = Field(description="All the memory the workspace may use")
    available: int = Field(
        description="What can still be used without stopping anything"
    )
    used: int = Field(description="total less available")


class DiskFigures(FrozenModel):
    """The workspace's disk in bytes (``memory.disk``)."""

    total: int = Field(description="The size of the disk")
    used: int = Field(description="What is on it")
    free: int = Field(description="What is left")


class UsageItem(FrozenModel):
    """One program group under a row of the breakdown: a chat, a code checker's chat, or an app."""

    name: str = Field(
        description="The chat, the chat whose code checker it is, or the app"
    )
    bytes: int = Field(description="The memory it holds")


class UsageRow(FrozenModel):
    """One row of the page's breakdown of what is using the memory (``usage``)."""

    id: str = Field(
        description="The row's key: chats, checkers, workspace, apps, shared, scratch or system"
    )
    label: str = Field(description="The row's heading")
    hint: str = Field(description="Small grey text beside the heading; empty for none")
    bytes: int = Field(description="The memory the row holds, its items' together")
    items: tuple[UsageItem, ...] = Field(
        description="What the row breaks down into, largest first; empty for a total"
    )
    limit: int | None = Field(
        default=None,
        description="The scratch row's limit, the size of the /tmp space held in memory; null elsewhere",
    )


class StopRecord(FrozenModel):
    """One program the memory guard stopped in the last 24 hours (``stops.recent_stops``)."""

    at: str = Field(description="When it was stopped, ISO 8601")
    what: str = Field(description="What it was, in plain words")
    command: str | None = Field(
        description="Its full command line, when the guard's log still names it"
    )


class HistorySample(FrozenModel):
    """One reading of the memory graph."""

    t: int = Field(description="When it was taken, epoch milliseconds")
    v: float = Field(description="The share of memory in use, 0 to 1")


class StatsResponse(FrozenModel):
    """The latest reading: everything the page shows."""

    memory: MemoryFigures = Field(description="Memory in use")
    disk: DiskFigures = Field(description="Disk in use")
    usage: tuple[UsageRow, ...] = Field(
        description="What is using the memory, as the page's breakdown rows"
    )
    usage_mode: Literal["exact", "scaled"] = Field(
        description="exact: rows are private memory plus Shared and System; scaled: resident sizes shrunk by one factor"
    )
    stops: tuple[StopRecord, ...] = Field(
        description="What the memory guard stopped in the last 24 hours, newest first"
    )
    history: tuple[HistorySample, ...] = Field(
        description="The memory graph's last 10 minutes, oldest first"
    )


class HealthResponse(FrozenModel):
    """What the health probe answers."""

    status: str = Field(description="Always 'ok'")
