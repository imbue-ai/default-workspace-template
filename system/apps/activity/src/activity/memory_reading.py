"""How much memory the workspace may use and how much it uses, read from the most truthful source it has.

Three sources, in order:

- ``/mngr-vol/.host-meminfo``: on a cloud workspace the VM rewrites this file several times a second with the
  container's limit (``MemTotal``) and what is left (``MemAvailable``). Under gVisor it is the only accurate
  reading: the sandbox's own ``/proc/meminfo`` freezes ``MemTotal`` at boot.
- The cgroup v2 files: ``memory.max`` is the limit; ``memory.current`` less the reclaimable ``inactive_file``
  page cache is what is in use (the figure ``docker stats`` reports).
- ``/proc/meminfo``: no limit of the workspace's own, so the machine's.

Whichever is used is named on the reading, so the page can say where its numbers came from.

Separately, the closing point: where something gets closed for lack of memory, and by whom. earlyoom reads only the
VM-published meminfo (a cloud workspace) or ``/proc/meminfo``, and acts once less than its ``-m`` share of that is
available. Under runc (a local workspace) ``/proc/meminfo`` is the whole machine's, so when the container's cgroup cap
sits below earlyoom's point, the kernel's own OOM killer acts first, at the cap. With no earlyoom running, only the
kernel acts.
"""

from collections.abc import Sequence
from enum import auto
from pathlib import Path
from typing import Final

from pydantic import Field

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure

HOST_MEMINFO_PATH: Final[Path] = Path("/mngr-vol/.host-meminfo")
CGROUP_DIR: Final[Path] = Path("/sys/fs/cgroup")
PROC_MEMINFO_PATH: Final[Path] = Path("/proc/meminfo")

BYTES_PER_KIB: Final[int] = 1024
_MEMINFO_TOTAL_FIELD: Final[str] = "MemTotal"
_MEMINFO_AVAILABLE_FIELD: Final[str] = "MemAvailable"
_CGROUP_UNLIMITED: Final[str] = "max"
_CGROUP_RECLAIMABLE_FIELD: Final[str] = "inactive_file"


class MemorySource(UpperCaseStrEnum):
    """Where a memory reading came from."""

    HOST_MEMINFO = auto()
    CGROUP = auto()
    PROC_MEMINFO = auto()


class MemoryCloser(UpperCaseStrEnum):
    """What closes something when memory runs out."""

    MEMORY_GUARD = auto()
    SYSTEM_LIMIT = auto()


class MemoryReading(FrozenModel):
    """The workspace's memory limit and current use, with the source they were read from."""

    limit_bytes: int = Field(description="The most the workspace may use")
    used_bytes: int = Field(description="What it uses now")
    source: MemorySource = Field(description="Which source the figures came from")
    source_detail: str = Field(description="The files and fields read, for the page's details")


class ClosingPoint(FrozenModel):
    """Where, in the reading's terms, something gets closed for lack of memory, by whom, and the total the closer
    scores processes against."""

    used_bytes: int = Field(description="The memory in use at which closing starts")
    closer: MemoryCloser = Field(description="Whether earlyoom or the kernel's cgroup limit acts first")
    min_available_percent: int | None = Field(description="earlyoom's -m threshold, when earlyoom is the closer")
    badness_total_kib: int = Field(description="The total memory the closer weighs oom_score_adj against, in KiB")
    detail: str = Field(description="How the point was worked out, for the page's details")


class MemorySources(FrozenModel):
    """The paths a reading is taken from; injectable so tests read a fake tree."""

    host_meminfo_path: Path = Field(description="The VM-published meminfo of a cloud workspace")
    cgroup_dir: Path = Field(description="The container's cgroup v2 directory")
    proc_meminfo_path: Path = Field(description="The kernel's meminfo")


DEFAULT_MEMORY_SOURCES: Final[MemorySources] = MemorySources(
    host_meminfo_path=HOST_MEMINFO_PATH, cgroup_dir=CGROUP_DIR, proc_meminfo_path=PROC_MEMINFO_PATH
)


@pure
def parse_kib_fields(text: str) -> dict[str, int]:
    """The numeric fields of a meminfo-style document (``Name:   123 kB``), in KiB."""
    values_kib: dict[str, int] = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        fields = rest.split()
        if fields and fields[0].isdigit():
            values_kib[key.strip()] = int(fields[0])
    return values_kib


@pure
def parse_space_separated_fields(text: str) -> dict[str, int]:
    """The numeric fields of a cgroup stat document (``name 123``), in bytes."""
    values: dict[str, int] = {}
    for line in text.splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[1].isdigit():
            values[fields[0]] = int(fields[1])
    return values


@pure
def reading_from_meminfo(text: str, source: MemorySource, path: Path) -> MemoryReading | None:
    values_kib = parse_kib_fields(text)
    total_kib = values_kib.get(_MEMINFO_TOTAL_FIELD)
    available_kib = values_kib.get(_MEMINFO_AVAILABLE_FIELD)
    if total_kib is None or available_kib is None:
        return None
    return MemoryReading(
        limit_bytes=total_kib * BYTES_PER_KIB,
        used_bytes=max(total_kib - available_kib, 0) * BYTES_PER_KIB,
        source=source,
        source_detail=f"{path}: {_MEMINFO_TOTAL_FIELD} minus {_MEMINFO_AVAILABLE_FIELD}",
    )


@pure
def reading_from_cgroup(max_text: str, current_text: str, stat_text: str, cgroup_dir: Path) -> MemoryReading | None:
    limit_text = max_text.strip()
    current = current_text.strip()
    if limit_text == _CGROUP_UNLIMITED or not limit_text.isdigit() or not current.isdigit():
        return None
    reclaimable = parse_space_separated_fields(stat_text).get(_CGROUP_RECLAIMABLE_FIELD, 0)
    return MemoryReading(
        limit_bytes=int(limit_text),
        used_bytes=max(int(current) - reclaimable, 0),
        source=MemorySource.CGROUP,
        source_detail=(
            f"{cgroup_dir}/memory.max; in use is memory.current minus {_CGROUP_RECLAIMABLE_FIELD} from memory.stat"
        ),
    )


@pure
def parse_min_available_percent(earlyoom_argv: Sequence[str]) -> int | None:
    """earlyoom's ``-m PERCENT[,KILL_PERCENT]`` threshold, or None when its argv gives none."""
    for index, argument in enumerate(earlyoom_argv):
        value = earlyoom_argv[index + 1] if argument == "-m" and index + 1 < len(earlyoom_argv) else None
        if value is None and argument.startswith("-m") and len(argument) > 2:
            value = argument[2:]
        if value is not None:
            percent = value.split(",")[0].strip()
            return int(percent) if percent.isdigit() else None
    return None


@pure
def closing_point(
    reading: MemoryReading, earlyoom_meminfo_text: str | None, earlyoom_argv: Sequence[str] | None
) -> ClosingPoint:
    """Where closing starts, from the reading, the meminfo earlyoom reads, and earlyoom's own argv."""
    limit_kib = reading.limit_bytes // BYTES_PER_KIB
    min_available_percent = parse_min_available_percent(earlyoom_argv) if earlyoom_argv is not None else None
    guard_values_kib = parse_kib_fields(earlyoom_meminfo_text or "")
    guard_total_kib = guard_values_kib.get(_MEMINFO_TOTAL_FIELD)
    kernel = ClosingPoint(
        used_bytes=reading.limit_bytes,
        closer=MemoryCloser.SYSTEM_LIMIT,
        min_available_percent=None,
        badness_total_kib=limit_kib,
        detail="the kernel closes the process with the highest oom_score once the limit is reached",
    )
    if min_available_percent is None or guard_total_kib is None:
        return kernel
    guard_badness_total_kib = guard_total_kib + guard_values_kib.get("SwapTotal", 0)
    guard_point_bytes = guard_total_kib * BYTES_PER_KIB * (100 - min_available_percent) // 100
    if reading.source is MemorySource.CGROUP and reading.limit_bytes <= guard_point_bytes:
        return kernel
    guard_used_bytes = (
        reading.limit_bytes * (100 - min_available_percent) // 100
        if reading.source is not MemorySource.CGROUP
        else guard_point_bytes
    )
    return ClosingPoint(
        used_bytes=min(guard_used_bytes, reading.limit_bytes),
        closer=MemoryCloser.MEMORY_GUARD,
        min_available_percent=min_available_percent,
        badness_total_kib=guard_badness_total_kib,
        detail=f"earlyoom acts once less than {min_available_percent}% of the memory it reads is available"
        + (
            "; it reads the whole machine's memory, so this point assumes nothing outside the workspace is using it"
            if reading.source is MemorySource.CGROUP
            else ""
        ),
    )


def read_earlyoom_meminfo(sources: MemorySources) -> str | None:
    """The meminfo earlyoom itself reads: the VM-published file when present, else the kernel's."""
    return _read_text(sources.host_meminfo_path) or _read_text(sources.proc_meminfo_path)


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text()
    except OSError:
        return None


def read_memory(sources: MemorySources) -> MemoryReading | None:
    """The first source that answers, or None when none does (not a Linux host, say)."""
    host_text = _read_text(sources.host_meminfo_path)
    if host_text is not None:
        host_reading = reading_from_meminfo(host_text, MemorySource.HOST_MEMINFO, sources.host_meminfo_path)
        if host_reading is not None:
            return host_reading
    max_text = _read_text(sources.cgroup_dir / "memory.max")
    current_text = _read_text(sources.cgroup_dir / "memory.current")
    if max_text is not None and current_text is not None:
        stat_text = _read_text(sources.cgroup_dir / "memory.stat") or ""
        cgroup_reading = reading_from_cgroup(max_text, current_text, stat_text, sources.cgroup_dir)
        if cgroup_reading is not None:
            return cgroup_reading
    proc_text = _read_text(sources.proc_meminfo_path)
    if proc_text is None:
        return None
    return reading_from_meminfo(proc_text, MemorySource.PROC_MEMINFO, sources.proc_meminfo_path)
