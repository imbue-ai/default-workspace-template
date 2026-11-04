"""What the app's routes answer: the overview reading the page polls, the longer histories, the disk view's
figures, and the health probe."""

from typing import Literal

from imbue.imbue_common.frozen_model import FrozenModel
from pydantic import Field


class MemoryFigures(FrozenModel):
    """Memory in bytes, the figures the memory guard acts on (``oom_priority.meminfo``)."""

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


class CpuFigures(FrozenModel):
    """The CPU in use, from the resource sampler's latest reading (``cpu.read_recent``)."""

    usage: float = Field(
        description="The share of the workspace's CPU capacity in use, 0 to 1"
    )
    cores: int = Field(description="The cores the workspace may use")
    processes: int = Field(
        description="The processes running, as many as the snapshot's walk of /proc found"
    )
    sampled_at: float | None = Field(
        description="When the sampler took the reading, epoch seconds; null when there is no fresh reading"
    )


class StopTarget(FrozenModel):
    """What the Stop button beside an item stops (``POST /api/stop`` takes the same two fields)."""

    kind: Literal["chat", "checkers", "app"] = Field(
        description="chat: stop the chat's agent; checkers: stop the code checkers a chat started; app: quit one of "
        "the user's apps"
    )
    id: str = Field(
        description="The chat's id, the agent id whose code checkers, or the app's supervisord program"
    )


class UsageItem(FrozenModel):
    """One program group under a row of the breakdown: a chat, a code checker's chat, an app, or one of the
    workspace's own programs."""

    name: str = Field(
        description="The chat, the chat whose code checker it is, the app, or the workspace program"
    )
    bytes: int = Field(description="The memory it holds")
    cpu: float | None = Field(
        default=None,
        description="Its share of the CPU capacity, 0 to 1; null without a reading",
    )
    stop: StopTarget | None = Field(
        default=None,
        description="What its Stop button stops; null for an item that cannot be stopped from here",
    )


class UsageRow(FrozenModel):
    """One row of the page's breakdown of what is using the workspace (``usage``)."""

    id: str = Field(
        description="The row's key: chats, checkers, workspace, apps, shared, scratch or system"
    )
    label: str = Field(description="The row's heading")
    hint: str = Field(description="Small grey text beside the heading; empty for none")
    bytes: int = Field(description="The memory the row holds, its items' together")
    cpu: float | None = Field(
        default=None,
        description="The row's share of the CPU capacity, 0 to 1; null for a row with no processes or no reading",
    )
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


class StretchCause(FrozenModel):
    """One of the chats or programs a busy stretch's CPU is credited to."""

    name: str = Field(
        description="The chat's name, the app's display name, or the program"
    )
    share: float = Field(description="Its share of the stretch's CPU, 0 to 1")


class EventRecord(FrozenModel):
    """One recent event: a program stopped to free memory, or a stretch of high CPU."""

    at: str = Field(
        description="When it happened (a stop) or started (a stretch), ISO 8601"
    )
    kind: Literal["stop", "busy"] = Field(
        description="stop: the memory guard stopped a program; busy: a CPU stretch"
    )
    what: str = Field(description="What happened, in plain words")
    duration_seconds: int | None = Field(
        default=None, description="How long a stretch lasted; null for a stop"
    )
    top: tuple[StretchCause, ...] = Field(
        default=(), description="Who a stretch's CPU is credited to, largest first"
    )


class HistorySample(FrozenModel):
    """One point of the graphs: the CPU and memory in use, as shares."""

    t: int = Field(description="When it was taken, epoch milliseconds")
    cpu: float | None = Field(
        description="The share of CPU capacity in use, 0 to 1; null when unknown"
    )
    memory: float | None = Field(
        description="The share of memory in use, 0 to 1; null when unknown"
    )


class StatsResponse(FrozenModel):
    """The latest reading: everything the overview shows."""

    cpu: CpuFigures = Field(description="CPU in use")
    memory: MemoryFigures = Field(description="Memory in use")
    disk: DiskFigures = Field(description="Disk in use")
    usage: tuple[UsageRow, ...] = Field(
        description="What is using the workspace, as the page's breakdown rows"
    )
    usage_mode: Literal["exact", "scaled"] = Field(
        description="exact: memory rows are anonymous memory plus Shared and System; scaled: resident sizes shrunk "
        "by one factor, then System for the rest"
    )
    stops: tuple[StopRecord, ...] = Field(
        description="What the memory guard stopped in the last 24 hours, newest first"
    )
    events: tuple[EventRecord, ...] = Field(
        description="The stops and CPU stretches of the last 24 hours, newest first"
    )
    history: tuple[HistorySample, ...] = Field(
        description="The last 10 minutes of both graphs, oldest first"
    )


class HistoryResponse(FrozenModel):
    """A longer span of both graphs, from the sampler's minute lines."""

    span: Literal["1h", "24h"] = Field(description="The span the samples cover")
    step_seconds: int = Field(description="How far apart the samples are")
    samples: tuple[HistorySample, ...] = Field(
        description="Oldest first; a gap in the readings is a gap here too"
    )


class DiskItem(FrozenModel):
    """One part of a row of the disk breakdown: a folder of the workspace, an app's data, a cache."""

    name: str = Field(description="The folder's or the app's name")
    hint: str = Field(
        default="", description="Small grey text beside the name; empty for none"
    )
    bytes: int = Field(description="What it takes")


class DiskRow(FrozenModel):
    """One row of what is on the disk, from an on-demand walk (``disk.scan``)."""

    id: str = Field(
        description="The row's key: workspace, uploads, apps, caches or other"
    )
    label: str = Field(description="The row's heading")
    hint: str = Field(description="Small grey text beside the heading; empty for none")
    bytes: int = Field(description="What the row takes")
    items: tuple[DiskItem, ...] = Field(
        default=(), description="What the row breaks down into, largest first"
    )


class DiskScan(FrozenModel):
    """The state of the on-demand walk of the disk and its last result."""

    state: Literal["idle", "scanning", "done", "failed"] = Field(
        description="idle: never run; scanning: running; done: a result; failed: the last walk could not read the disk"
    )
    scanned_at: str | None = Field(
        description="When the last walk ended, ISO 8601; null before the first"
    )
    rows: tuple[DiskRow, ...] = Field(
        description="The last walk's rows; empty before the first and after a failed one"
    )
    missing: tuple[str, ...] = Field(
        description="The rows the last walk could not measure"
    )


class BackupSummary(FrozenModel):
    """The backup service's state, read from its events and settings (``backups.summarize``)."""

    state: Literal["not_configured", "running", "up_to_date", "stale", "failing"] = (
        Field(
            description="not_configured: no repository; running: a backup is in progress; up_to_date: the last backup is "
            "recent; stale: it is older than expected; failing: the last backup failed"
        )
    )
    last_success_at: str | None = Field(
        description="When the last backup completed, ISO 8601; null for none"
    )
    last_failure_at: str | None = Field(
        description="When the last backup failed, ISO 8601; null for none"
    )
    interval_seconds: float = Field(description="How often backups run")
    failures_last_day: int = Field(
        description="How many backups failed in the last 24 hours"
    )
    readme_path: str = Field(description="The backup service's README, to learn more")


class DiskResponse(FrozenModel):
    """Everything the disk view shows."""

    disk: DiskFigures = Field(description="Disk in use")
    backups: BackupSummary = Field(description="The backup service's state")
    scan: DiskScan = Field(
        description="What is on the disk, when a walk has been asked for"
    )


class ScanResponse(FrozenModel):
    """What starting a walk of the disk answers."""

    scan: DiskScan = Field(description="The walk's state after the request")


class StopRequest(StopTarget):
    """What ``POST /api/stop`` takes: the target of one item's Stop button, as the item carries it."""


class StopResponse(FrozenModel):
    """What a stop answers once it has been carried out."""

    status: str = Field(description="Always 'ok'")


class HealthResponse(FrozenModel):
    """What the health probe answers."""

    status: str = Field(description="Always 'ok'")
