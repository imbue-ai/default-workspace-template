"""Every process in the workspace, read from ``/proc``: its parent, names, memory, and shedding priority.

Memory is read exactly as earlyoom's fork reads it (``oom_priority.badness``), so the page and the shedder agree on
sizes and on which processes exist at all: a process without an mm (a kernel thread, or a zombie) is left out, and
under gVisor the anonymous total of ``smaps`` stands in for its inflated ``VmRSS``.
"""

from pathlib import Path
from typing import Final

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from oom_priority.badness import ProcessSample
from oom_priority.badness import parse_smaps_anonymous
from oom_priority.badness import parse_status_memory

PROC_DIR: Final[Path] = Path("/proc")
# The command names a browser process runs under (Chromium, and Fortress, its stealth fork).
BROWSER_COMMAND_NAMES: Final[frozenset[str]] = frozenset({"chromium", "chrome", "tilion", "headless_shell"})


class ProcessReading(FrozenModel):
    """One process as ``/proc`` describes it."""

    pid: int = Field(description="The process id")
    parent_pid: int = Field(description="The parent's process id (0 for the root of the namespace)")
    command_name: str = Field(description="The kernel's short name for it (``comm``)")
    command_line: str = Field(description="Its full command line, arguments joined by spaces")
    rss_kib: int = Field(description="Resident memory as earlyoom counts it, in KiB")
    swap_kib: int = Field(description="Its swapped-out memory, which earlyoom also counts, in KiB")
    page_table_kib: int = Field(description="Its page tables, which earlyoom also counts, in KiB")
    oom_score_adj: int = Field(description="Its shedding priority: higher is shed sooner; -1000 is never shed")


@pure
def parse_parent_pid(status_text: str) -> int:
    for line in status_text.splitlines():
        key, _, value = line.partition(":")
        if key == "PPid" and value.strip().isdigit():
            return int(value.strip())
    return 0


def read_process(pid: int, proc_dir: Path) -> ProcessReading | None:
    """One process, or None when it exited mid-read or has no memory of its own to count."""
    process_dir = proc_dir / str(pid)
    try:
        status_text = (process_dir / "status").read_text()
        command_name = (process_dir / "comm").read_text().rstrip("\n")
        oom_score_adj = int((process_dir / "oom_score_adj").read_text())
        command_line_bytes = (process_dir / "cmdline").read_bytes()
    except (OSError, ValueError):
        return None
    memory = parse_status_memory(status_text)
    rss_kib = memory.vm_rss_kib
    if rss_kib is None:
        return None
    if rss_kib and not memory.has_rss_anon:
        # smaps lists mapped files' paths, which need not be UTF-8.
        try:
            rss_kib = parse_smaps_anonymous((process_dir / "smaps").read_text(errors="replace"))
        except (OSError, ValueError):
            return None
    return ProcessReading(
        pid=pid,
        parent_pid=parse_parent_pid(status_text),
        command_name=command_name,
        command_line=command_line_bytes.replace(b"\0", b" ").decode(errors="replace").strip(),
        rss_kib=rss_kib,
        swap_kib=memory.vm_swap_kib,
        page_table_kib=memory.vm_pte_kib,
        oom_score_adj=oom_score_adj,
    )


def read_process_table(proc_dir: Path) -> list[ProcessReading]:
    """Every process that could be read, in pid order."""
    try:
        entries = [entry.name for entry in proc_dir.iterdir() if entry.name.isdigit()]
    except OSError:
        return []
    readings = (read_process(int(name), proc_dir) for name in sorted(entries, key=int))
    return [reading for reading in readings if reading is not None]


@pure
def as_badness_sample(process: ProcessReading) -> ProcessSample:
    """The process as earlyoom's scoring model takes it."""
    return ProcessSample(
        pid=process.pid,
        comm=process.command_name,
        oom_score_adj=process.oom_score_adj,
        rss_kib=process.rss_kib,
        vm_swap_kib=process.swap_kib,
        vm_pte_kib=process.page_table_kib,
    )
