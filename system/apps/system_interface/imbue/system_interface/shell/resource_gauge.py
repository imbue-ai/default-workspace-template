"""The tray's resource gauge: how much room the workspace has left (disk, memory, CPU), the user's choice of when
the gauge is shown, and the warnings raised when room runs short.

The settings live in ``resource_gauge.json`` under the shell's state directory. The readings come from where the
System Monitor reads them: memory through the memory guard's reader (``oom_priority.meminfo``), the disk through
``disk_headroom`` (which sees a cloud machine's disk quota), and CPU from the resource sampler's latest reading.
One monitor thread takes a reading every few seconds, whether or not a window is open, and pushes the gauge to
every window over the shell's socket when what a window draws changes (and once a minute regardless, so a window
can tell a gauge that stopped updating from one with nothing new to say).

The disk warns on the space left, not the share used: 10% of a large disk is plenty and 10% of a small one is
not. It also warns when the disk is on course to fill within minutes, whatever is left. Memory and CPU warn on a
share in use; CPU is judged on its mean over the last two minutes, since a moment at full use is normal. A
warning clears only once its resource is comfortably back past its level, so a value hovering at the level does
not raise it again and again.

A raised warning reaches three places. The user gets one Imbue Studio notification per warning (retried until it
is delivered, and remembered across a restart of the shell in ``resource_gauge_state.json``, so a restart does
not send it again). The agents get a short notice file their prompt hook reads
(``system/scripts/agent_resource_notice.sh``), so they stop adding to the problem. And a program the memory
guard stopped (the shed ledger) is reported to the user, who would otherwise only see that something vanished.
"""

import json
import threading
import time
from abc import ABC
from abc import abstractmethod
from collections import deque
from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import Final

import httpx
from disk_headroom.space import read_disk_space
from latchkey_access.gateway import GatewayAccess
from loguru import logger
from oom_priority import resource_sampler
from oom_priority.ledger import RECORD_TYPE_PROCESS_SHED
from oom_priority.ledger import read_records
from oom_priority.meminfo import read_meminfo
from oom_priority.paths import shed_ledger_path
from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.enums import LowerCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.system_interface.shell.errors import ShellStateError
from imbue.system_interface.shell.state_files import STATE_FILES_LOCK
from imbue.system_interface.shell.state_files import parse_versioned_document
from imbue.system_interface.shell.state_files import read_json_object
from imbue.system_interface.shell.state_files import write_json_atomic

RESOURCE_GAUGE_FILENAME: Final[str] = "resource_gauge.json"
RESOURCE_GAUGE_FILE_VERSION: Final[int] = 1
RESOURCE_GAUGE_STATE_FILENAME: Final[str] = "resource_gauge_state.json"
RESOURCE_GAUGE_STATE_FILE_VERSION: Final[int] = 1
# What the agents' prompt hook reads; present only while a warning is raised, and rewritten every reading so the
# hook can ignore one a dead shell left behind.
AGENT_NOTICE_FILENAME: Final[str] = "resource_agent_notice.txt"
RESOURCE_GAUGE_ROUTE: Final[str] = "/api/resource-gauge"
RESOURCE_GAUGE_SETTINGS_ROUTE: Final[str] = "/api/resource-gauge/settings"

GIB: Final[int] = 1024**3

# The least and most disk space left the user can pick as the warning level, in GB.
MIN_DISK_WARNING_FREE_GB: Final[float] = 0.5
MAX_DISK_WARNING_FREE_GB: Final[float] = 500.0
DEFAULT_DISK_WARNING_FREE_GB: Final[float] = 5.0
# A disk warning clears once this many times its level is free again.
DISK_CLEAR_FACTOR: Final[float] = 1.2
# A disk with this share of its warning level left is critical: writes are about to fail.
DISK_CRITICAL_FACTOR: Final[float] = 0.2
# A disk on course to fill within this long warns whatever is left.
DISK_FILLING_FAST_SECONDS: Final[float] = 15 * 60.0
# The fill rate is the change in free space over this window, once it holds at least the minimum span.
DISK_RATE_WINDOW_SECONDS: Final[float] = 5 * 60.0
DISK_RATE_MIN_SPAN_SECONDS: Final[float] = 60.0
# A projection further out than this says nothing useful and is left out.
DISK_PROJECTION_HORIZON_SECONDS: Final[float] = 24 * 3600.0

# The lowest and highest warning level the user can pick for memory and CPU, as a percentage of the whole.
MIN_WARNING_PERCENT: Final[int] = 50
MAX_WARNING_PERCENT: Final[int] = 99
# How far below its level memory or CPU must fall before its warning clears.
WARNING_CLEAR_MARGIN_PERCENT: Final[float] = 5.0
# How often the monitor takes a reading: the resource sampler's own interval.
SAMPLE_INTERVAL_SECONDS: Final[float] = resource_sampler.SAMPLE_INTERVAL_SECONDS
# The span the CPU warning averages over.
CPU_WARNING_WINDOW_SECONDS: Final[float] = 120.0
# A sampler reading older than this is no reading (the System Monitor's own rule): the sampler is down or the
# machine was paused.
CPU_READING_FRESH_SECONDS: Final[float] = 30.0
CPU_RECENT_PATH: Final[Path] = resource_sampler.STATE_DIR / resource_sampler.RECENT_FILENAME

# A shown share must move this many points before the windows are told, so a bar a few pixels wide is not
# redrawn for a change nobody can see.
PUSH_PERCENT_STEP: Final[float] = 2.0
# The windows are told at least this often, changed or not: their proof the gauge is still being updated.
PUSH_HEARTBEAT_SECONDS: Final[float] = 60.0
# How long a notification that could not be delivered waits before it is tried again.
NOTIFY_RETRY_SECONDS: Final[float] = 60.0
# How many of the programs the memory guard stopped the gauge lists, and from how far back.
RECENT_STOPS_LIMIT: Final[int] = 3
RECENT_STOPS_WINDOW_SECONDS: Final[float] = 24 * 3600.0

SYSTEM_MONITOR_APP_NAME: Final[str] = "system-monitor"
# The workspace message the desktop delivers when the notification is opened; the System Monitor's manifest
# handles it by showing the view the message names.
SHOW_SYSTEM_MONITOR_MESSAGE_TYPE: Final[str] = "minds:show-system-monitor"
AGENT_ID_ENV: Final[str] = "MNGR_AGENT_ID"
NOTIFY_TIMEOUT_SECONDS: Final[float] = 10.0


class GaugeVisibility(LowerCaseStrEnum):
    """When the tray shows the gauge's bars; a warning is shown either way."""

    ALWAYS = "always"
    WHEN_WARNING = "when_warning"


class GaugeResource(LowerCaseStrEnum):
    """One of the resources the gauge shows; its value names the System Monitor view about it."""

    CPU = "cpu"
    MEMORY = "memory"
    DISK = "disk"


class AlertSeverity(LowerCaseStrEnum):
    WARNING = "warning"
    CRITICAL = "critical"


class ResourceGaugeSettings(FrozenModel):
    """The user's choices for the gauge: when its bars are shown, and each resource's warning level (None: no
    warning)."""

    visibility: GaugeVisibility = Field(
        default=GaugeVisibility.WHEN_WARNING,
        description="Whether the tray always shows the gauge's bars, or shows nothing until a warning is raised",
    )
    disk_warning_free_gb: float | None = Field(
        default=DEFAULT_DISK_WARNING_FREE_GB,
        ge=MIN_DISK_WARNING_FREE_GB,
        le=MAX_DISK_WARNING_FREE_GB,
        description="The disk space left, in GB, at or below which a warning is raised; None for no disk warning",
    )
    memory_warning_percent: int | None = Field(
        default=None,
        ge=MIN_WARNING_PERCENT,
        le=MAX_WARNING_PERCENT,
        description="The share of memory in use that raises a warning; None for no memory warning",
    )
    cpu_warning_percent: int | None = Field(
        default=None,
        ge=MIN_WARNING_PERCENT,
        le=MAX_WARNING_PERCENT,
        description="The share of the CPU in use, averaged over two minutes, that raises a warning; None for none",
    )


DEFAULT_RESOURCE_GAUGE_SETTINGS: Final[ResourceGaugeSettings] = ResourceGaugeSettings()


class _ResourceGaugeDocument(ResourceGaugeSettings):
    """The whole of ``resource_gauge.json``: the settings under a format version."""

    version: int = Field(description="The file format version")


class ResourceGaugeSettingsStore(MutableModel):
    """Reads and writes ``resource_gauge.json`` under the shell's state lock."""

    state_directory: Path = Field(frozen=True, description="The shell's state directory")

    def _path(self) -> Path:
        return self.state_directory / RESOURCE_GAUGE_FILENAME

    def read(self) -> ResourceGaugeSettings:
        """The stored settings; the defaults when nothing usable is stored (logged when something unusable is)."""
        with STATE_FILES_LOCK:
            raw = read_json_object(self._path())
        document = parse_versioned_document(raw, _ResourceGaugeDocument, RESOURCE_GAUGE_FILE_VERSION, self._path())
        if document is None:
            return DEFAULT_RESOURCE_GAUGE_SETTINGS
        return ResourceGaugeSettings.model_validate(document.model_dump(exclude={"version"}))

    def write(self, settings: ResourceGaugeSettings) -> None:
        document = {"version": RESOURCE_GAUGE_FILE_VERSION, **settings.model_dump(mode="json")}
        with STATE_FILES_LOCK:
            write_json_atomic(self._path(), document)


class DeliveryRecord(FrozenModel):
    """What the user has already been told, kept across a restart of the shell so it is not said twice."""

    notified: tuple[GaugeResource, ...] = Field(
        default=(), description="The resources whose standing warning the user was notified of"
    )
    last_stop_timestamp: str | None = Field(
        default=None,
        description="The shed ledger timestamp of the newest stopped program the user was told of; None before "
        "the first reading, which takes the ledger as it stands as already known",
    )


class _DeliveryDocument(DeliveryRecord):
    """The whole of ``resource_gauge_state.json``."""

    version: int = Field(description="The file format version")


class DeliveryStore(MutableModel):
    """Reads and writes ``resource_gauge_state.json`` under the shell's state lock."""

    state_directory: Path = Field(frozen=True, description="The shell's state directory")

    def _path(self) -> Path:
        return self.state_directory / RESOURCE_GAUGE_STATE_FILENAME

    def read(self) -> DeliveryRecord:
        with STATE_FILES_LOCK:
            raw = read_json_object(self._path())
        document = parse_versioned_document(raw, _DeliveryDocument, RESOURCE_GAUGE_STATE_FILE_VERSION, self._path())
        if document is None:
            return DeliveryRecord()
        return DeliveryRecord.model_validate(document.model_dump(exclude={"version"}))

    def write(self, record: DeliveryRecord) -> None:
        document = {"version": RESOURCE_GAUGE_STATE_FILE_VERSION, **record.model_dump(mode="json")}
        with STATE_FILES_LOCK:
            write_json_atomic(self._path(), document)


class AmountReading(FrozenModel):
    """How much of a resource measured in bytes is in use and left, out of how much the workspace can use."""

    used_bytes: int = Field(ge=0, description="Bytes in use")
    free_bytes: int = Field(ge=0, description="Bytes the workspace can still use")
    total_bytes: int = Field(gt=0, description="Bytes the workspace can use")

    @property
    def percent(self) -> float:
        return 100.0 * self.used_bytes / self.total_bytes


class DiskReading(AmountReading):
    """The workspace's disk, with when it fills at the rate it has been filling."""

    seconds_to_full: float | None = Field(
        default=None,
        ge=0,
        description="How long until the disk is full at the rate of the last few minutes; None when it is not "
        "filling, the rate is not known yet, or that is more than a day away",
    )


class CpuReading(FrozenModel):
    """The resource sampler's latest CPU reading."""

    percent: float = Field(ge=0, description="The share of the cores in use at the latest reading, in percent")
    recent_percent: float = Field(
        ge=0, description="The mean share of the cores in use over the warning window, in percent"
    )
    cores: int = Field(ge=1, description="How many cores the workspace can use")


class ResourceReadings(FrozenModel):
    """One reading of each resource; None where it could not be read."""

    cpu: CpuReading | None = Field(description="The CPU, None when the sampler has no fresh reading")
    memory: AmountReading | None = Field(description="Memory, None when it could not be read")
    disk: DiskReading | None = Field(description="The workspace's disk, None when it could not be read")


NO_READINGS: Final[ResourceReadings] = ResourceReadings(cpu=None, memory=None, disk=None)


class ResourceAlert(FrozenModel):
    """A raised warning, in the words the tray, the card and the notification all use."""

    resource: GaugeResource = Field(description="The resource the warning is about")
    severity: AlertSeverity = Field(description="Warning, or critical when the resource is all but gone")
    headline: str = Field(description="A few words naming the problem, for the tray")
    detail: str = Field(description="The figures behind it, in a short sentence fragment")


class StoppedProgram(FrozenModel):
    """A program the memory guard stopped to free memory."""

    at: str = Field(description="When it was stopped: the shed ledger's sortable ISO 8601 timestamp")
    what: str = Field(description="What it was, in plain words")


class ResourceGaugeView(FrozenModel):
    """What ``GET /api/resource-gauge`` answers and ``resource_gauge_changed`` pushes."""

    settings: ResourceGaugeSettings = Field(description="The user's choices for the gauge")
    readings: ResourceReadings = Field(description="The latest reading of each resource")
    alerts: tuple[ResourceAlert, ...] = Field(description="The raised warnings, the most severe first")
    recent_stops: tuple[StoppedProgram, ...] = Field(
        description="The programs the memory guard stopped in the last day, newest first, a few at most"
    )


@pure
def format_gigabytes(byte_count: int) -> str:
    """A byte count as GB the way the gauge says it: one decimal under 10, none from there."""
    gigabytes = byte_count / GIB
    return f"{gigabytes:.0f} GB" if gigabytes >= 10 else f"{gigabytes:.1f} GB"


@pure
def format_duration(seconds: float) -> str:
    """A span of time in the roughest unit that says it: 'under a minute', '12 minutes', '3 hours'."""
    minutes = seconds / 60.0
    if minutes < 1:
        return "under a minute"
    if minutes < 90:
        whole = round(minutes)
        return "1 minute" if whole == 1 else f"{whole} minutes"
    hours = round(minutes / 60.0)
    return f"{hours} hours"


@pure
def estimate_seconds_to_full(samples: Sequence[tuple[float, int]], free_bytes: int) -> float | None:
    """How long until the disk is full, from ``(time, free bytes)`` samples oldest first and the space left now.

    None when the samples span too little time to say, the disk is not filling, or it fills more than a day out.
    """
    if len(samples) < 2:
        return None
    first_time, first_free = samples[0]
    last_time, last_free = samples[-1]
    span = last_time - first_time
    if span < DISK_RATE_MIN_SPAN_SECONDS:
        return None
    rate = (first_free - last_free) / span
    if rate <= 0:
        return None
    seconds = free_bytes / rate
    return seconds if seconds <= DISK_PROJECTION_HORIZON_SECONDS else None


@pure
def _is_disk_filling_fast(disk: DiskReading) -> bool:
    return disk.seconds_to_full is not None and disk.seconds_to_full <= DISK_FILLING_FAST_SECONDS


@pure
def _is_disk_warned(disk: DiskReading, level_gb: float, is_raised: bool) -> bool:
    level_bytes = level_gb * GIB
    if disk.free_bytes <= level_bytes or _is_disk_filling_fast(disk):
        return True
    return is_raised and disk.free_bytes <= level_bytes * DISK_CLEAR_FACTOR


@pure
def _is_share_warned(percent: float, level: int, is_raised: bool) -> bool:
    return percent >= level or (is_raised and percent > level - WARNING_CLEAR_MARGIN_PERCENT)


@pure
def next_warnings(
    raised: frozenset[GaugeResource], readings: ResourceReadings, settings: ResourceGaugeSettings
) -> frozenset[GaugeResource]:
    """The warnings raised after this reading. A resource past its level raises one, and a raised one clears only
    once its resource is comfortably back (see the module docstring). A resource with no level never warns; one
    with no reading keeps whatever it had."""
    result: set[GaugeResource] = set()
    disk_level = settings.disk_warning_free_gb
    if disk_level is not None:
        is_raised = GaugeResource.DISK in raised
        if is_raised if readings.disk is None else _is_disk_warned(readings.disk, disk_level, is_raised):
            result.add(GaugeResource.DISK)
    memory_level = settings.memory_warning_percent
    if memory_level is not None:
        is_raised = GaugeResource.MEMORY in raised
        if (
            is_raised
            if readings.memory is None
            else _is_share_warned(readings.memory.percent, memory_level, is_raised)
        ):
            result.add(GaugeResource.MEMORY)
    cpu_level = settings.cpu_warning_percent
    if cpu_level is not None:
        is_raised = GaugeResource.CPU in raised
        if is_raised if readings.cpu is None else _is_share_warned(readings.cpu.recent_percent, cpu_level, is_raised):
            result.add(GaugeResource.CPU)
    return frozenset(result)


@pure
def describe_alert(
    resource: GaugeResource, readings: ResourceReadings, settings: ResourceGaugeSettings
) -> ResourceAlert:
    """A raised warning in words: what is wrong, and the figures behind it."""
    match resource:
        case GaugeResource.DISK:
            disk = readings.disk
            if disk is None:
                return ResourceAlert(
                    resource=resource,
                    severity=AlertSeverity.WARNING,
                    headline="Disk space is low",
                    detail="The disk cannot be read right now",
                )
            level_bytes = (settings.disk_warning_free_gb or 0.0) * GIB
            is_low = disk.free_bytes <= level_bytes * DISK_CLEAR_FACTOR
            is_critical = disk.free_bytes <= level_bytes * DISK_CRITICAL_FACTOR
            detail = f"{format_gigabytes(disk.free_bytes)} left"
            if disk.seconds_to_full is not None:
                detail += f", about {format_duration(disk.seconds_to_full)} at this rate"
            if is_critical:
                headline = "Disk is almost full"
            elif is_low:
                headline = "Disk space is low"
            else:
                headline = "Disk is filling up fast"
            return ResourceAlert(
                resource=resource,
                severity=AlertSeverity.CRITICAL if is_critical else AlertSeverity.WARNING,
                headline=headline,
                detail=detail,
            )
        case GaugeResource.MEMORY:
            memory = readings.memory
            return ResourceAlert(
                resource=resource,
                severity=AlertSeverity.WARNING,
                headline="Memory is nearly full",
                detail=f"{format_gigabytes(memory.free_bytes)} left of {format_gigabytes(memory.total_bytes)}"
                if memory is not None
                else "Memory cannot be read right now",
            )
        case GaugeResource.CPU:
            cpu = readings.cpu
            return ResourceAlert(
                resource=resource,
                severity=AlertSeverity.WARNING,
                headline="CPU is busy",
                detail=f"{cpu.recent_percent:.0f}% in use over the last two minutes"
                if cpu is not None
                else "No CPU reading right now",
            )


# What the user can do about each resource, as the notification and the agents' notice say it.
_ADVICE: Final[Mapping[GaugeResource, str]] = {
    GaugeResource.DISK: "Open System Monitor to see what is using the disk. When it fills up, saving files fails.",
    GaugeResource.MEMORY: "Open System Monitor to see what is using it. Near the limit, busy programs are stopped "
    "to free memory.",
    GaugeResource.CPU: "Open System Monitor to see what is keeping it busy. Everything in the workspace runs slower "
    "meanwhile.",
}

_AGENT_ADVICE: Final[Mapping[GaugeResource, str]] = {
    GaugeResource.DISK: "Do not write large files (downloads, clones, caches, build output, copies of data) until "
    "there is room. `uv run --no-sync disk-headroom` shows the room left. Tell the user, and offer to free space.",
    GaugeResource.MEMORY: "Do not start memory-heavy work (builds, test suites, browsers) until there is room, or "
    "it is likely to be stopped. Free memory with the user per `.agents/shared/references/freeing-memory.md`.",
    GaugeResource.CPU: "Heavy work will run slowly, and slows the user's own. Check whether a job of yours is "
    "responsible before starting more.",
}


@pure
def describe_notification(alert: ResourceAlert) -> tuple[str, str]:
    """The Imbue Studio notification's title and message for a raised warning."""
    return f"{alert.headline}: {alert.detail[0].lower()}{alert.detail[1:]}", _ADVICE[alert.resource]


@pure
def describe_agent_notice(alerts: Sequence[ResourceAlert]) -> str:
    """What the agents are told while warnings stand; empty when none does."""
    if not alerts:
        return ""
    lines = ["Workspace resource notice (from the workspace's resource monitor):"]
    for alert in alerts:
        lines.append(f"- {alert.headline}: {alert.detail}. {_AGENT_ADVICE[alert.resource]}")
    return "\n".join(lines) + "\n"


@pure
def describe_stopped_program(record: Mapping[str, Any]) -> str:
    """A shed ledger record in plain words, as the System Monitor words one it has no command for."""
    agent_name = record.get("agent_name")
    if isinstance(agent_name, str) and agent_name:
        return f"the {agent_name.removesuffix('-Agent')} chat's agent"
    if record.get("comm") == "claude":
        return "a chat's agent"
    if record.get("is_worker"):
        return "a background agent"
    return "a background program"


@pure
def stopped_programs_of(records: Sequence[Mapping[str, Any]]) -> tuple[StoppedProgram, ...]:
    """The ledger's ``process_shed`` records as stopped programs, oldest first."""
    return tuple(
        StoppedProgram(at=record["timestamp"], what=describe_stopped_program(record))
        for record in records
        if record.get("type") == RECORD_TYPE_PROCESS_SHED and isinstance(record.get("timestamp"), str)
    )


@pure
def describe_stops_notification(stops: Sequence[StoppedProgram]) -> tuple[str, str]:
    """The notification for programs the memory guard just stopped."""
    if len(stops) == 1:
        what = stops[0].what
        title = f"Stopped {what} to free memory"
    else:
        title = f"Stopped {len(stops)} programs to free memory"
    return (
        title,
        "The workspace was running out of memory. A stopped chat starts again on its next message. Open System "
        "Monitor to see what is using memory.",
    )


@pure
def parse_cpu_reading(data: Any, now: float) -> CpuReading | None:
    """The CPU reading a resource sampler's recent file holds, or None when it is not a fresh version-1 reading."""
    if not isinstance(data, dict) or data.get("version") != resource_sampler.RECENT_FILE_VERSION:
        return None
    sampled_at = data.get("sampled_at")
    if not isinstance(sampled_at, (int, float)) or now - sampled_at > CPU_READING_FRESH_SECONDS:
        return None
    cpu = data.get("cpu")
    usage = cpu.get("usage") if isinstance(cpu, dict) else None
    if not isinstance(usage, (int, float)):
        return None
    cores = data.get("cores")
    window_start_ms = (now - CPU_WARNING_WINDOW_SECONDS) * 1000
    recent = [
        float(raw[1])
        for raw in data.get("samples", [])
        if isinstance(raw, list)
        and len(raw) == 3
        and all(isinstance(value, (int, float)) for value in raw)
        and raw[0] >= window_start_ms
    ]
    recent_usage = sum(recent) / len(recent) if recent else float(usage)
    return CpuReading(
        percent=100.0 * max(float(usage), 0.0),
        recent_percent=100.0 * max(recent_usage, 0.0),
        cores=int(cores) if isinstance(cores, (int, float)) and cores >= 1 else 1,
    )


def read_cpu(path: Path = CPU_RECENT_PATH, clock: Callable[[], float] = time.time) -> CpuReading | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return parse_cpu_reading(data, clock())


def read_memory() -> AmountReading | None:
    try:
        meminfo = read_meminfo()
    except (OSError, KeyError, ValueError) as e:
        logger.debug("Could not read memory for the resource gauge: {}", e)
        return None
    total, used = meminfo["total"], max(meminfo["used"], 0)
    if total <= 0:
        return None
    return AmountReading(used_bytes=min(used, total), free_bytes=max(total - used, 0), total_bytes=total)


def read_disk(path: Path) -> DiskReading | None:
    try:
        space = read_disk_space(path)
    except (OSError, ValueError) as e:
        logger.debug("Could not read the disk for the resource gauge: {}", e)
        return None
    if space.total <= 0:
        return None
    return DiskReading(used_bytes=max(space.used, 0), free_bytes=max(space.free, 0), total_bytes=space.total)


def read_resource_readings(workspace_root: Path) -> ResourceReadings:
    """One reading of each resource, as the System Monitor reads them; the monitor adds the disk's projection."""
    return ResourceReadings(cpu=read_cpu(), memory=read_memory(), disk=read_disk(workspace_root))


class StopLedgerReader(MutableModel):
    """The shed ledger's stopped programs, read again only when the file has changed."""

    ledger_path: Path = Field(frozen=True, description="The shed ledger")

    _stamp: tuple[int, int] | None = PrivateAttr(default=None)
    _stops: tuple[StoppedProgram, ...] = PrivateAttr(default=())

    def read(self) -> tuple[StoppedProgram, ...]:
        """Every stopped program in the ledger, oldest first; nothing when there is no ledger."""
        try:
            status = self.ledger_path.stat()
        except OSError:
            self._stamp, self._stops = None, ()
            return ()
        stamp = (status.st_mtime_ns, status.st_size)
        if stamp != self._stamp:
            self._stops = stopped_programs_of(read_records(self.ledger_path))
            self._stamp = stamp
        return self._stops


def read_shed_ledger_stops() -> StopLedgerReader:
    """A reader of the workspace's own shed ledger."""
    return StopLedgerReader(ledger_path=shed_ledger_path())


class ResourceNotifierInterface(MutableModel, ABC):
    """Where the user is told of a warning or a stopped program."""

    @abstractmethod
    def notify(self, title: str, message: str, view: GaugeResource) -> bool:
        """Tell the user, pointing at the System Monitor view about ``view``; whether the notification was taken."""


class GatewayResourceNotifier(ResourceNotifierInterface):
    """Posts to the Imbue Studio feed as the services agent, the way the routines runner posts; opening the entry
    shows the System Monitor on the resource's view."""

    environ: Mapping[str, str] = Field(frozen=True, description="The process environment the gateway is named in")

    def notify(self, title: str, message: str, view: GaugeResource) -> bool:
        gateway = GatewayAccess.from_environ(dict(self.environ))
        agent_id = self.environ.get(AGENT_ID_ENV, "")
        if gateway is None or not agent_id:
            logger.debug("Skipped a resource notification: the latchkey gateway environment is not set")
            return False
        payload = {
            "title": title,
            "message": message,
            "destination": {"type": SHOW_SYSTEM_MONITOR_MESSAGE_TYPE, "view": str(view)},
        }
        url = gateway.url(f"/minds-api-proxy/api/v1/agents/{agent_id}/notifications")
        try:
            response = httpx.post(
                url,
                json=payload,
                headers={"Content-Type": "application/json", **gateway.headers()},
                timeout=NOTIFY_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as e:
            logger.warning("Could not send a resource notification: {}", e)
            return False
        if response.status_code != 200:
            logger.warning("Imbue Studio refused a resource notification: HTTP {}", response.status_code)
            return False
        return True


@pure
def _shown_percents(readings: ResourceReadings) -> tuple[float | None, float | None, float | None]:
    return (
        readings.cpu.percent if readings.cpu is not None else None,
        readings.memory.percent if readings.memory is not None else None,
        readings.disk.percent if readings.disk is not None else None,
    )


@pure
def is_push_due(previous: ResourceGaugeView | None, current: ResourceGaugeView) -> bool:
    """Whether the windows draw ``current`` differently from the view they were last sent."""
    if previous is None:
        return True
    if (
        previous.settings != current.settings
        or [(alert.resource, alert.severity, alert.headline) for alert in previous.alerts]
        != [(alert.resource, alert.severity, alert.headline) for alert in current.alerts]
        or previous.recent_stops != current.recent_stops
    ):
        return True
    if current.settings.visibility != GaugeVisibility.ALWAYS:
        return False
    for before, after in zip(_shown_percents(previous.readings), _shown_percents(current.readings), strict=True):
        if (before is None) != (after is None):
            return True
        if before is not None and after is not None and abs(before - after) >= PUSH_PERCENT_STEP:
            return True
    return False


class ResourceGaugeMonitor(MutableModel):
    """Takes a reading every ``interval_seconds`` on a thread of its own, keeps the view every window reads, and
    tells the windows, the user and the agents what changed."""

    model_config = {"arbitrary_types_allowed": True}

    settings_store: ResourceGaugeSettingsStore = Field(frozen=True, description="Where the settings are kept")
    delivery_store: DeliveryStore = Field(frozen=True, description="Where what the user was told is kept")
    read_readings: Callable[[], ResourceReadings] = Field(frozen=True, description="Takes one reading")
    read_stops: Callable[[], Sequence[StoppedProgram]] = Field(
        frozen=True, description="Every program the memory guard stopped, oldest first"
    )
    notifier: ResourceNotifierInterface | None = Field(
        frozen=True, description="Where the user is told; None in a preview or a test, which tell nobody"
    )
    push: Callable[[ResourceGaugeView], None] = Field(frozen=True, description="Sends the view to every window")
    agent_notice_path: Path | None = Field(
        frozen=True, description="The notice file the agents' prompt hook reads; None to write none"
    )
    clock: Callable[[], float] = Field(default=time.time, frozen=True, description="Seconds since the epoch")
    interval_seconds: float = Field(default=SAMPLE_INTERVAL_SECONDS, frozen=True, description="Time between readings")

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _settings: ResourceGaugeSettings | None = PrivateAttr(default=None)
    _delivery: DeliveryRecord | None = PrivateAttr(default=None)
    _readings: ResourceReadings = PrivateAttr(default=NO_READINGS)
    _warnings: frozenset[GaugeResource] = PrivateAttr(default=frozenset())
    _stops: tuple[StoppedProgram, ...] = PrivateAttr(default=())
    _disk_samples: deque[tuple[float, int]] = PrivateAttr(default_factory=deque)
    _pushed: ResourceGaugeView | None = PrivateAttr(default=None)
    _pushed_at: float = PrivateAttr(default=0.0)
    _retry_at: float = PrivateAttr(default=0.0)
    _stop: threading.Event = PrivateAttr(default_factory=threading.Event)
    _wake: threading.Event = PrivateAttr(default_factory=threading.Event)
    _thread: threading.Thread | None = PrivateAttr(default=None)

    def start(self) -> None:
        thread = threading.Thread(target=self._run, daemon=True, name="shell-resource-gauge")
        self._thread = thread
        thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def view(self) -> ResourceGaugeView:
        with self._lock:
            return self._view_locked()

    def update_settings(self, settings: ResourceGaugeSettings) -> ResourceGaugeView:
        """Keep the new settings and judge the last reading against them at once, so a changed level shows without
        waiting for the next reading. A notification it calls for is sent by the monitor's own thread, never by
        the request that saved the settings."""
        self.settings_store.write(settings)
        with self._lock:
            self._settings = settings
            self._warnings = next_warnings(self._warnings, self._readings, settings)
            view = self._view_locked()
        self._push(view, is_forced=True)
        self._wake.set()
        return view

    def take_reading(self) -> None:
        """One round: read, judge, tell the windows, the user and the agents. What the thread does every interval."""
        now = self.clock()
        raw = self.read_readings()
        stops = tuple(self.read_stops())
        with self._lock:
            settings = self._current_settings()
            readings = raw.model_copy_update(to_update(raw.field_ref().disk, self._with_projection(raw.disk, now)))
            self._readings = readings
            self._warnings = next_warnings(self._warnings, readings, settings)
            self._stops = stops
            view = self._view_locked()
        self._push(view, is_forced=False)
        self._write_agent_notice(view.alerts)
        self._tell_user(view, stops, now)

    def _current_settings(self) -> ResourceGaugeSettings:
        if self._settings is None:
            self._settings = self.settings_store.read()
        return self._settings

    def _view_locked(self) -> ResourceGaugeView:
        settings = self._current_settings()
        alerts = [
            describe_alert(resource, self._readings, settings)
            for resource in (GaugeResource.DISK, GaugeResource.MEMORY, GaugeResource.CPU)
            if resource in self._warnings
        ]
        alerts.sort(key=lambda alert: alert.severity != AlertSeverity.CRITICAL)
        return ResourceGaugeView(
            settings=settings,
            readings=self._readings,
            alerts=tuple(alerts),
            recent_stops=self._recent_stops(self._stops),
        )

    def _recent_stops(self, stops: Sequence[StoppedProgram]) -> tuple[StoppedProgram, ...]:
        cutoff = datetime.fromtimestamp(self.clock() - RECENT_STOPS_WINDOW_SECONDS, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        )
        recent = [stop for stop in stops if stop.at >= cutoff]
        return tuple(reversed(recent[-RECENT_STOPS_LIMIT:]))

    def _with_projection(self, disk: DiskReading | None, now: float) -> DiskReading | None:
        if disk is None:
            return None
        samples = self._disk_samples
        samples.append((now, disk.free_bytes))
        while samples and now - samples[0][0] > DISK_RATE_WINDOW_SECONDS:
            samples.popleft()
        return disk.model_copy_update(
            to_update(disk.field_ref().seconds_to_full, estimate_seconds_to_full(tuple(samples), disk.free_bytes))
        )

    def _push(self, view: ResourceGaugeView, is_forced: bool) -> None:
        now = self.clock()
        with self._lock:
            is_due = is_forced or is_push_due(self._pushed, view) or now - self._pushed_at >= PUSH_HEARTBEAT_SECONDS
            if not is_due:
                return
            self._pushed, self._pushed_at = view, now
        self.push(view)

    def _write_agent_notice(self, alerts: Sequence[ResourceAlert]) -> None:
        path = self.agent_notice_path
        if path is None:
            return
        notice = describe_agent_notice(alerts)
        try:
            if notice:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(notice, encoding="utf-8")
            else:
                path.unlink(missing_ok=True)
        except OSError as e:
            logger.debug("Could not update the agents' resource notice: {}", e)

    def _read_delivery(self) -> DeliveryRecord:
        if self._delivery is None:
            self._delivery = self.delivery_store.read()
        return self._delivery

    def _keep_delivery(self, record: DeliveryRecord) -> None:
        if record == self._delivery:
            return
        self._delivery = record
        try:
            self.delivery_store.write(record)
        except ShellStateError as e:
            # A full disk is the likeliest reason, and the very thing being reported: what was delivered is still
            # remembered for the life of this process.
            logger.warning("Could not record the resource notifications delivered: {}", e)

    def _tell_user(self, view: ResourceGaugeView, stops: Sequence[StoppedProgram], now: float) -> None:
        """Send what the user has not been told yet: each standing warning once, and each newly stopped program."""
        record = self._read_delivery()
        standing = {alert.resource for alert in view.alerts}
        notified = tuple(resource for resource in record.notified if resource in standing)
        latest_stop = stops[-1].at if stops else None
        # On the first reading in this workspace, what the ledger already holds is history, not news.
        known_stop = (latest_stop or "") if record.last_stop_timestamp is None else record.last_stop_timestamp
        record = record.model_copy_update(
            to_update(record.field_ref().notified, notified),
            to_update(record.field_ref().last_stop_timestamp, known_stop),
        )
        self._keep_delivery(record)

        notifier = self.notifier
        if notifier is None or now < self._retry_at:
            return
        is_failed = False
        for alert in view.alerts:
            if alert.resource in record.notified:
                continue
            title, message = describe_notification(alert)
            if notifier.notify(title, message, alert.resource):
                record = record.model_copy_update(
                    to_update(record.field_ref().notified, (*record.notified, alert.resource))
                )
                self._keep_delivery(record)
            else:
                is_failed = True
        new_stops = [stop for stop in stops if stop.at > (record.last_stop_timestamp or "")]
        if new_stops:
            title, message = describe_stops_notification(new_stops)
            if notifier.notify(title, message, GaugeResource.MEMORY):
                self._keep_delivery(
                    record.model_copy_update(to_update(record.field_ref().last_stop_timestamp, new_stops[-1].at))
                )
            else:
                is_failed = True
        if is_failed:
            self._retry_at = now + NOTIFY_RETRY_SECONDS

    def _run(self) -> None:
        while not self._stop.is_set():
            self.take_reading()
            self._wake.wait(self.interval_seconds)
            self._wake.clear()
