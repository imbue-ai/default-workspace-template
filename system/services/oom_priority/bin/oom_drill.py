#!/usr/bin/env python3
"""A live OOM drill: check that earlyoom sheds in the predicted order.

Starts one sleeper per requested band (each holding about ``--sleeper-percent``
of MemTotal at that ``oom_score_adj``), then a hog at ``oom_score_adj`` -1000
that grows in small steps until earlyoom has shed the last sleeper. Every
second it snapshots each process's ``oom_score_adj`` and RSS; each kill (from
the shed ledger, with earlyoom's own log line for detail) is judged against the
snapshot taken just before it: the victim must have had the highest predicted
badness among the processes earlyoom could pick,

    VmRSS + VmSwap + VmPTE + oom_score_adj * (MemTotal + SwapTotal) / 1000

with ``--avoid`` names 300 points lower -- the imbue-ai earlyoom fork's
scoring, which on a Linux kernel is also the kernel's own. A wrong victim, or
a built-in service (adj <= 80) shed while something at adj >= 900 remained,
stops the drill at once and frees the hog's memory.

Stdlib-only and self-contained, so it can be sent into a workspace inside the
command (``mngr exec`` does not pass its stdin through):
``mngr exec <agent> "echo $(base64 < oom_drill.py | tr -d '\\n') | base64 -d | python3 - --bands 1000,900,600,300,75,25"``.
It must run as a process allowed to set -1000 (root under gVisor, or anywhere
with ``CAP_SYS_RESOURCE``). Prints one JSON verdict on stdout and exits 0 only
when the drill passed.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from collections import deque
from pathlib import Path
from typing import Final, NamedTuple

PROC: Final[Path] = Path("/proc")
DEFAULT_LEDGER: Final[Path] = Path(
    "/home/user/workspace/data/.state/oom_priority/events/shed.jsonl"
)
DEFAULT_EARLYOOM_LOG: Final[Path] = Path("/var/log/supervisor/earlyoom-stderr.log")
# What --prefer/--avoid are worth, in oom_score_adj points.
AVOID_ADJ: Final[int] = -300
UNKILLABLE_ADJ: Final[int] = -1000
# A built-in service, the primary agent or never-kill infrastructure.
PROTECTED_MAX_ADJ: Final[int] = 80
# Agent subprocesses and Chromium.
EXPENDABLE_MIN_ADJ: Final[int] = 900
MEMINFO_FIELDS: Final[tuple[str, ...]] = (
    "MemTotal",
    "MemFree",
    "MemAvailable",
    "AnonPages",
    "Shmem",
    "Cached",
)

_SLEEPER_CODE: Final[str] = """
import signal, sys
with open("/proc/self/oom_score_adj", "w") as f:
    f.write(sys.argv[1])
block = bytearray(int(sys.argv[2]) * 1024)
for i in range(0, len(block), 4096):
    block[i] = 1
print("ready", flush=True)
signal.pause()
"""

# Reads one line per step from stdin, allocates that many KiB, and acks.
_HOG_CODE: Final[str] = """
import sys
with open("/proc/self/oom_score_adj", "w") as f:
    f.write("-1000")
blocks = []
print("ready", flush=True)
for line in sys.stdin:
    block = bytearray(int(line) * 1024)
    for i in range(0, len(block), 4096):
        block[i] = 1
    blocks.append(block)
    print("ok", flush=True)
"""


# NamedTuples rather than pydantic models: the drill runs under a bare python3.
class ProcessSample(NamedTuple):
    pid: int
    comm: str
    oom_score_adj: int
    # None for a process without an mm (no VmRSS line): a kernel thread.
    vm_rss_kib: int | None
    vm_swap_kib: int
    vm_pte_kib: int


class RankedProcess(NamedTuple):
    pid: int
    comm: str
    oom_score_adj: int
    badness_kib: int


class KillJudgement(NamedTuple):
    # "right": the victim was the top predicted badness (within the
    # tolerance); "wrong": something else was; "unpredicted": the victim was
    # not in the snapshot (it started after it), so there is nothing to judge.
    verdict: str
    victim_badness_kib: int | None
    top: RankedProcess | None
    is_protected_shed_before_expendable: bool


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
        if sample.vm_rss_kib is None or sample.oom_score_adj == UNKILLABLE_ADJ:
            continue
        adj = sample.oom_score_adj
        if avoid_regex is not None and avoid_regex.search(sample.comm):
            adj += AVOID_ADJ
        badness = (
            sample.vm_rss_kib
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


class Snapshot(NamedTuple):
    taken_at: float
    samples: list[ProcessSample]


def last_snapshot_with(pid: int, snapshots: "deque[Snapshot]") -> Snapshot | None:
    """The newest snapshot in which ``pid`` still held memory: the state just
    before earlyoom killed it. A later snapshot either lacks it or has it as an
    unreaped zombie, which has no VmRSS."""
    for snapshot in reversed(snapshots):
        if any(
            sample.pid == pid and sample.vm_rss_kib is not None
            for sample in snapshot.samples
        ):
            return snapshot
    return None


def judge_kill(
    victim_pid: int, ranking: list[RankedProcess], tolerance_kib: int
) -> KillJudgement:
    """Whether ``victim_pid`` was the right pick given the snapshot's ranking.

    The snapshot is up to a second old, so a victim within ``tolerance_kib`` of
    the top still counts as right: two processes that close are a tie the
    snapshot cannot break.
    """
    top = ranking[0] if ranking else None
    victim = next((process for process in ranking if process.pid == victim_pid), None)
    is_protected_shed_before_expendable = (
        victim is not None
        and victim.oom_score_adj <= PROTECTED_MAX_ADJ
        and any(
            process.oom_score_adj >= EXPENDABLE_MIN_ADJ
            for process in ranking
            if process.pid != victim_pid
        )
    )
    if victim is None or top is None:
        return KillJudgement("unpredicted", None, top, False)
    verdict = (
        "right" if victim.badness_kib >= top.badness_kib - tolerance_kib else "wrong"
    )
    return KillJudgement(
        verdict, victim.badness_kib, top, is_protected_shed_before_expendable
    )


def parse_status_memory(text: str) -> tuple[int | None, int, int]:
    """``(VmRSS, VmSwap, VmPTE)`` in KiB from a /proc/<pid>/status body; a missing
    VmSwap/VmPTE (gVisor serves neither) reads as 0."""
    values: dict[str, int] = {}
    for line in text.splitlines():
        name, _, rest = line.partition(":")
        if name in ("VmRSS", "VmSwap", "VmPTE"):
            fields = rest.split()
            if fields and fields[0].isdigit():
                values[name] = int(fields[0])
    return values.get("VmRSS"), values.get("VmSwap", 0), values.get("VmPTE", 0)


def parse_avoid_regex(cmdline: list[str]) -> re.Pattern[str] | None:
    """The ``--avoid`` regex from earlyoom's argv, or None."""
    for index, argument in enumerate(cmdline):
        if argument == "--avoid" and index + 1 < len(cmdline):
            return re.compile(cmdline[index + 1])
        if argument.startswith("--avoid="):
            return re.compile(argument.split("=", 1)[1])
    return None


def read_meminfo() -> dict[str, int]:
    values: dict[str, int] = {}
    for line in (PROC / "meminfo").read_text().splitlines():
        name, _, rest = line.partition(":")
        fields = rest.split()
        if fields and fields[0].isdigit():
            values[name] = int(fields[0])
    return values


def snapshot_processes() -> list[ProcessSample]:
    samples: list[ProcessSample] = []
    for entry in PROC.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            comm = (entry / "comm").read_text().rstrip("\n")
            adj = int((entry / "oom_score_adj").read_text())
            rss, swap, pte = parse_status_memory((entry / "status").read_text())
        except (OSError, ValueError):
            continue
        samples.append(ProcessSample(int(entry.name), comm, adj, rss, swap, pte))
    return samples


def find_earlyoom() -> tuple[int, list[str]]:
    for entry in PROC.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if (entry / "comm").read_text().strip() == "earlyoom":
                argv = (entry / "cmdline").read_bytes().split(b"\0")
                return int(entry.name), [
                    argument.decode() for argument in argv if argument
                ]
        except OSError:
            continue
    raise SystemExit("oom_drill: earlyoom is not running")


def set_own_adj(adj: int) -> None:
    (PROC / "self" / "oom_score_adj").write_text(f"{adj}\n")
    actual = int((PROC / "self" / "oom_score_adj").read_text())
    if actual != adj:
        raise SystemExit(
            f"oom_drill: could not set oom_score_adj {adj} (reads {actual})"
        )


def read_new_ledger_kills(ledger: Path, offset: int) -> tuple[list[dict], int]:
    """``process_shed`` records appended past byte ``offset``, and the new offset.
    A partial last line is left for the next read."""
    if not ledger.exists():
        return [], offset
    with open(ledger, "rb") as handle:
        handle.seek(offset)
        data = handle.read()
    complete, _, _partial = data.rpartition(b"\n")
    if not complete and not data.endswith(b"\n"):
        return [], offset
    records: list[dict] = []
    for line in complete.splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if record.get("type") == "process_shed":
            records.append(record)
    return records, offset + len(complete) + 1


def earlyoom_kill_line(log: Path, offset: int, pid: int) -> str | None:
    """earlyoom's own ``sending SIG... to process <pid>`` line, past ``offset``."""
    try:
        with open(log, "rb") as handle:
            handle.seek(offset)
            text = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    pattern = re.compile(rf"sending SIG\w+ to process {pid} ")
    for line in text.splitlines():
        if pattern.search(line):
            return line
    return None


def file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _judge_record(
    record: dict,
    sleepers: dict[int, tuple[subprocess.Popen[str], int]],
    snapshots: "deque[Snapshot]",
    total_kib: int,
    avoid_regex: re.Pattern[str] | None,
    excluded: set[int],
    tolerance_kib: int,
    earlyoom_log: Path,
    log_offset: int,
) -> tuple[dict, str]:
    """One kill's report entry, and the failure it amounts to ("" if none).

    ``excluded`` also holds every earlier victim: two kills can land between
    the same pair of snapshots, and the second must not be judged against the
    first victim."""
    pid = int(record.get("pid", 0))
    snapshot = last_snapshot_with(pid, snapshots)
    samples = snapshot.samples if snapshot is not None else snapshots[-1].samples
    judgement = judge_kill(
        pid, predict_ranking(samples, total_kib, avoid_regex, excluded), tolerance_kib
    )
    current = read_meminfo()
    kill = {
        "pid": pid,
        "comm": record.get("comm"),
        "band": sleepers[pid][1] if pid in sleepers else None,
        "ledger": record,
        "earlyoom_log_line": earlyoom_kill_line(earlyoom_log, log_offset, pid),
        "snapshot_age_seconds": round(time.time() - snapshot.taken_at, 2)
        if snapshot is not None
        else None,
        "judgement": {
            "verdict": judgement.verdict,
            "victim_badness_kib": judgement.victim_badness_kib,
            "top": judgement.top._asdict() if judgement.top else None,
            "is_protected_shed_before_expendable": judgement.is_protected_shed_before_expendable,
        },
        "meminfo": {field: current.get(field) for field in MEMINFO_FIELDS},
    }
    if judgement.verdict == "wrong":
        predicted = judgement.top.pid if judgement.top else None
        return (
            kill,
            f"wrong victim: pid {pid} ({record.get('comm')}), predicted pid {predicted}",
        )
    if judgement.is_protected_shed_before_expendable:
        return (
            kill,
            f"protected pid {pid} ({record.get('comm')}) shed while a process at adj >= {EXPENDABLE_MIN_ADJ} remained",
        )
    return kill, ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--bands",
        required=True,
        help="Comma-separated oom_score_adj per sleeper, e.g. 1000,900,600,25",
    )
    parser.add_argument(
        "--sleeper-percent",
        type=float,
        default=3.0,
        help="Each sleeper's size, in percent of MemTotal",
    )
    parser.add_argument(
        "--step-percent",
        type=float,
        default=0.5,
        help="The hog's growth per step, in percent of MemTotal",
    )
    parser.add_argument(
        "--timeout", type=float, default=1200.0, help="Give up after this many seconds"
    )
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--earlyoom-log", type=Path, default=DEFAULT_EARLYOOM_LOG)
    args = parser.parse_args()
    bands = [int(band) for band in args.bands.split(",")]

    set_own_adj(UNKILLABLE_ADJ)
    meminfo = read_meminfo()
    total_kib = meminfo["MemTotal"] + meminfo.get("SwapTotal", 0)
    tolerance_kib = max(1024, total_kib // 1000)
    earlyoom_pid, earlyoom_argv = find_earlyoom()
    avoid_regex = parse_avoid_regex(earlyoom_argv)
    ledger_offset = file_size(args.ledger)
    log_offset = file_size(args.earlyoom_log)

    sleepers: dict[int, tuple[subprocess.Popen[str], int]] = {}
    for band in bands:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                _SLEEPER_CODE,
                str(band),
                str(int(meminfo["MemTotal"] * args.sleeper_percent / 100)),
            ],
            stdout=subprocess.PIPE,
            text=True,
        )
        assert proc.stdout is not None
        if proc.stdout.readline().strip() != "ready":
            raise SystemExit(f"oom_drill: the band-{band} sleeper did not start")
        sleepers[proc.pid] = (proc, band)
    hog = subprocess.Popen(
        [sys.executable, "-c", _HOG_CODE],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert hog.stdin is not None and hog.stdout is not None
    if hog.stdout.readline().strip() != "ready":
        raise SystemExit(
            "oom_drill: the hog did not start (can this process set oom_score_adj -1000?)"
        )
    step_kib = int(meminfo["MemTotal"] * args.step_percent / 100)
    excluded = {earlyoom_pid, os.getpid(), hog.pid}

    kills: list[dict] = []
    failure = ""
    shed_sleepers: set[int] = set()
    # A kill is judged against the last snapshot that still has its victim,
    # which may be a few snapshots back by the time its ledger line is read.
    snapshots: deque[Snapshot] = deque(
        [Snapshot(time.time(), snapshot_processes())], maxlen=30
    )
    deadline = time.monotonic() + args.timeout
    try:
        while not failure and len(shed_sleepers) < len(sleepers):
            if time.monotonic() > deadline:
                failure = f"timed out after {args.timeout:.0f}s with sleepers left"
                break
            records, ledger_offset = read_new_ledger_kills(args.ledger, ledger_offset)
            for record in records:
                kill, record_failure = _judge_record(
                    record,
                    sleepers,
                    snapshots,
                    total_kib,
                    avoid_regex,
                    excluded,
                    tolerance_kib,
                    args.earlyoom_log,
                    log_offset,
                )
                kills.append(kill)
                excluded.add(kill["pid"])
                if kill["band"] is not None:
                    shed_sleepers.add(kill["pid"])
                failure = failure or record_failure
            is_every_sleeper_gone = all(
                proc.poll() is not None or pid in shed_sleepers
                for pid, (proc, _) in sleepers.items()
            )
            if is_every_sleeper_gone:
                # The last ledger line lands just after its sleeper dies.
                hog.kill()
            elif hog.poll() is not None:
                failure = "the hog died"
            elif records:
                # Let the kill settle before growing again, so earlyoom picks one
                # victim at a time, and see the settled state before the next.
                time.sleep(2)
                snapshots.append(Snapshot(time.time(), snapshot_processes()))
            else:
                hog.stdin.write(f"{step_kib}\n")
                hog.stdin.flush()
                hog.stdout.readline()
            if time.time() - snapshots[-1].taken_at >= 1:
                snapshots.append(Snapshot(time.time(), snapshot_processes()))
            time.sleep(0.1)
    finally:
        hog.kill()
        for proc, _ in sleepers.values():
            proc.kill()

    sleeper_bands_in_shed_order = [
        kill["band"] for kill in kills if kill["band"] is not None
    ]
    passed = not failure and len(sleeper_bands_in_shed_order) == len(bands)
    if not failure and not passed:
        failure = "not every sleeper was shed"
    verdict = {
        "passed": passed,
        "failure": failure,
        "mem_total_kib": meminfo["MemTotal"],
        "swap_total_kib": meminfo.get("SwapTotal", 0),
        "tolerance_kib": tolerance_kib,
        "earlyoom": {"pid": earlyoom_pid, "argv": earlyoom_argv},
        "bands": bands,
        "sleeper_bands_in_shed_order": sleeper_bands_in_shed_order,
        "kills": kills,
    }
    print(json.dumps(verdict, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
