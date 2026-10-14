"""earlyoom's victim choice, as the imbue-ai fork computes it: the badness of every process it could kill.

The fork scores each process ``VmRSS + VmSwap + VmPTE + oom_score_adj * (MemTotal + SwapTotal) / 1000`` and kills the
highest. Under gVisor, whose ``VmRSS`` counts whole mapped ranges, it counts the ``smaps`` Anonymous total instead
(a Linux kernel prints ``RssAnon`` and is read as is). A process matching earlyoom's ``--avoid`` regex is scored 300
points lower rather than skipped, and an ``oom_score_adj`` of -1000 is never killed. A process without an mm (a
kernel thread or zombie: no ``VmRSS`` line, or gVisor's ``VmSize`` of 0) is not a candidate, except a zombie leader
whose threads still run, which counts as 0.

The drill (``bin/oom_drill.py``) checks real kills against this prediction, and other callers predict from it, so all
read the same model. Stdlib-only (see ``paths``): the drill runs under a plain ``python3``.
"""

import re
from pathlib import Path
from typing import Final, NamedTuple

AVOID_ADJ: Final[int] = -300
UNKILLABLE_ADJ: Final[int] = -1000
EARLYOOM_COMMAND_NAME: Final[str] = "earlyoom"


class ProcessSample(NamedTuple):
    pid: int
    comm: str
    oom_score_adj: int
    # The resident memory the fork's badness counts (see StatusMemory), or None
    # for a process without an mm.
    rss_kib: int | None
    vm_swap_kib: int
    vm_pte_kib: int


class StatusMemory(NamedTuple):
    # None for a process without an mm, as the fork reads it: no VmRSS line
    # (Linux: a kernel thread or a zombie), or a VmSize of 0 (gVisor: a zombie
    # or an exiting task). A gVisor zombie leader whose threads still run reads
    # 0, since gVisor cannot list its task directory to reach their memory.
    vm_rss_kib: int | None
    vm_swap_kib: int
    vm_pte_kib: int
    # Linux prints RssAnon; gVisor does not, and its VmRSS counts whole mapped
    # ranges, so there the fork counts the smaps Anonymous total instead.
    has_rss_anon: bool


class RankedProcess(NamedTuple):
    pid: int
    comm: str
    oom_score_adj: int
    badness_kib: int


def predict_ranking(
    samples: list[ProcessSample],
    total_kib: int,
    avoid_regex: re.Pattern[str] | None,
    excluded_pids: set[int],
) -> list[RankedProcess]:
    """The processes earlyoom could pick, highest predicted badness first."""
    ranked: list[RankedProcess] = []
    for sample in samples:
        if sample.pid == 1 or sample.pid in excluded_pids:
            continue
        if sample.rss_kib is None or sample.oom_score_adj == UNKILLABLE_ADJ:
            continue
        adj = sample.oom_score_adj
        if avoid_regex is not None and avoid_regex.search(sample.comm):
            adj += AVOID_ADJ
        badness = (
            sample.rss_kib
            + sample.vm_swap_kib
            + sample.vm_pte_kib
            + adj * total_kib // 1000
        )
        ranked.append(
            RankedProcess(
                pid=sample.pid,
                comm=sample.comm,
                oom_score_adj=sample.oom_score_adj,
                badness_kib=badness,
            )
        )
    return sorted(ranked, key=lambda process: process.badness_kib, reverse=True)


def parse_status_memory(text: str) -> StatusMemory:
    """The memory counters of a /proc/<pid>/status body, in KiB; a missing
    VmSwap/VmPTE (gVisor serves neither) reads as 0."""
    values: dict[str, int] = {}
    for line in text.splitlines():
        name, _, rest = line.partition(":")
        if name in ("VmRSS", "VmSwap", "VmPTE", "VmSize", "Threads", "RssAnon"):
            fields = rest.split()
            if fields and fields[0].isdigit():
                values[name] = int(fields[0])
    rss = values.get("VmRSS")
    if rss is not None and values.get("VmSize") == 0:
        rss = 0 if values.get("Threads", 1) > 1 else None
    return StatusMemory(
        vm_rss_kib=rss,
        vm_swap_kib=values.get("VmSwap", 0),
        vm_pte_kib=values.get("VmPTE", 0),
        has_rss_anon="RssAnon" in values,
    )


def parse_smaps_anonymous(text: str) -> int:
    """The sum of a /proc/<pid>/smaps body's Anonymous lines, in KiB."""
    return sum(
        int(line.split()[1])
        for line in text.splitlines()
        if line.startswith("Anonymous:")
    )


def parse_avoid_regex(cmdline: list[str]) -> re.Pattern[str] | None:
    """The ``--avoid`` regex from earlyoom's argv, or None."""
    for index, argument in enumerate(cmdline):
        if argument == "--avoid" and index + 1 < len(cmdline):
            return re.compile(cmdline[index + 1])
        if argument.startswith("--avoid="):
            return re.compile(argument.split("=", 1)[1])
    return None


def snapshot_processes(proc: Path) -> list[ProcessSample]:
    """Every process the fork could score, with the memory it would count."""
    samples: list[ProcessSample] = []
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            comm = (entry / "comm").read_text().rstrip("\n")
            adj = int((entry / "oom_score_adj").read_text())
            memory = parse_status_memory((entry / "status").read_text())
            rss = memory.vm_rss_kib
            if rss and not memory.has_rss_anon:
                # smaps lists mapped files' paths, which need not be UTF-8.
                rss = parse_smaps_anonymous((entry / "smaps").read_text(errors="replace"))
        except (OSError, ValueError):
            continue
        samples.append(
            ProcessSample(
                int(entry.name), comm, adj, rss, memory.vm_swap_kib, memory.vm_pte_kib
            )
        )
    return samples


def find_earlyoom_argv(proc: Path) -> tuple[int, list[str]] | None:
    """The running earlyoom's pid and argv, or None when none runs."""
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if (entry / "comm").read_text().strip() != EARLYOOM_COMMAND_NAME:
                continue
            argv = (entry / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        return int(entry.name), [argument.decode(errors="replace") for argument in argv if argument]
    return None
