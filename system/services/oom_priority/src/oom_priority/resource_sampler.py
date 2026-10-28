"""Overall CPU and memory every few seconds, a day of history, and the busy stretches
with who caused them: what the system monitor app's graphs and events are drawn from
(``docs/system/specs/system-monitor.md``, part A).

CPU comes from per-process counters, the only CPU accounting that exists under gVisor
(its ``/proc/stat`` and load average read as zero). Each process's ``/proc/<pid>/stat``
carries its own CPU time and the time of every child it has reaped, so the busy time of
an interval is the change in the sum over every live process of own plus reaped-children
time: a process that exits between two samples drops out of the sum while its parent's
child counters grow by its whole lifetime, and nothing is counted twice or lost.

Stdlib-only (see ``paths``): the ``resource_sampler`` supervisord program runs this under
a plain ``python3``.
"""

import collections
import json
import math
import os
import uuid
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Final, NamedTuple

from oom_priority import registry, supervisor_rpc
from oom_priority.cgroup_meminfo import HOST_MEMINFO_PATH, write_replacing
from oom_priority.meminfo import PROC, read_meminfo

# Where the sampler writes and the app reads, relative to the repo root (the cwd of every
# supervised program): machine state the app rebuilds nothing from.
STATE_DIR: Final[Path] = Path("data/.state/system-monitor")
RECENT_FILENAME: Final[str] = "recent.json"
HISTORY_FILENAME: Final[str] = "history.jsonl"
STRETCH_EVENTS_RELATIVE_PATH: Final[Path] = (
    Path("events") / "cpu_stretches" / "events.jsonl"
)
RECENT_FILE_VERSION: Final[int] = 1
CGROUP_DIR: Final[Path] = Path("/sys/fs/cgroup")

SAMPLE_INTERVAL_SECONDS: Final[float] = 5.0
RECENT_WINDOW_SECONDS: Final[float] = 600.0
# A minute line holds the mean of a closed minute; the file keeps a day and is rewritten
# from two days down to one, so readers never see more than a day of gap-free lines.
MINUTE_MS: Final[int] = 60_000
HISTORY_KEEP_SECONDS: Final[float] = 24 * 3600.0
HISTORY_REWRITE_SECONDS: Final[float] = 2 * HISTORY_KEEP_SECONDS
# Per-process shares under this are left out of the recent file to keep it small.
MIN_PID_SHARE: Final[float] = 0.001

STRETCH_OPEN_USAGE: Final[float] = 0.90
STRETCH_OPEN_SECONDS: Final[float] = 60.0
STRETCH_CLOSE_USAGE: Final[float] = 0.70
STRETCH_CLOSE_SECONDS: Final[float] = 60.0
STRETCH_MIN_SECONDS: Final[float] = 120.0
ATTRIBUTION_INTERVAL_SECONDS: Final[float] = 30.0
STRETCH_TOP_COUNT: Final[int] = 3
STRETCH_TOP_MIN_SHARE: Final[float] = 0.05
STRETCH_EVENT_TYPE: Final[str] = "cpu_stretch"
STRETCH_EVENT_SOURCE: Final[str] = "cpu_stretches"
OTHER_ROOT: Final[str] = "other"
# How far up a ppid chain to look for a root before giving up.
_MAX_ANCESTRY: Final[int] = 64


class ProcessTicks(NamedTuple):
    """One process's CPU counters from ``/proc/<pid>/stat``: own plus reaped-children ticks, and its parent."""

    ticks: int
    ppid: int


def parse_stat(text: str) -> ProcessTicks | None:
    """The counters in one ``/proc/<pid>/stat`` line, or None when it is not one.

    The command name in parentheses can hold spaces and parentheses, so the fields are
    counted from the last ``)``: the parent pid is the second field after it and the four
    CPU times are the twelfth to fifteenth.
    """
    _, closing, rest = text.rpartition(")")
    if not closing:
        return None
    fields = rest.split()
    if len(fields) < 15:
        return None
    try:
        ppid = int(fields[1])
        ticks = sum(int(fields[index]) for index in (11, 12, 13, 14))
    except ValueError:
        return None
    return ProcessTicks(ticks, ppid)


def read_process_ticks(proc: Path = PROC) -> dict[int, ProcessTicks]:
    """Every live process's counters; an entry that cannot be read (gone mid-walk, not ours) is skipped."""
    ticks_by_pid: dict[int, ProcessTicks] = {}
    try:
        entries = list(proc.iterdir())
    except OSError:
        return ticks_by_pid
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            parsed = parse_stat((entry / "stat").read_text())
        except OSError:
            continue
        if parsed is not None:
            ticks_by_pid[int(entry.name)] = parsed
    return ticks_by_pid


def read_cores(cgroup_dir: Path = CGROUP_DIR, cpu_count: int | None = None) -> int:
    """The cores the workspace may use: the cgroup's ``cpu.max`` quota where there is one, else the CPU count.

    Under gVisor there is no ``cpu.max`` and the CPU count already is the quota. Never fewer than 1.
    """
    fallback = max(1, cpu_count if cpu_count is not None else os.cpu_count() or 1)
    try:
        quota_text, _, period_text = (cgroup_dir / "cpu.max").read_text().partition(" ")
    except OSError:
        return fallback
    quota_text, period_text = quota_text.strip(), period_text.strip()
    if not quota_text.isdigit() or not period_text.isdigit() or int(period_text) == 0:
        return fallback
    return max(1, math.ceil(int(quota_text) / int(period_text)))


class CpuReading(NamedTuple):
    """One interval's CPU: the share of capacity in use, and each process's share of capacity."""

    usage: float
    by_pid: dict[int, float]


def cpu_reading(
    before: Mapping[int, ProcessTicks],
    now: Mapping[int, ProcessTicks],
    elapsed_seconds: float,
    cores: int,
    clk_tck: int,
) -> CpuReading:
    """The CPU used between two walks, as shares of the workspace's capacity over the interval."""
    capacity_ticks = clk_tck * elapsed_seconds * cores
    if capacity_ticks <= 0:
        return CpuReading(0.0, {})
    busy_ticks = sum(entry.ticks for entry in now.values()) - sum(
        entry.ticks for entry in before.values()
    )
    usage = min(1.0, max(0.0, busy_ticks / capacity_ticks))
    deltas = {
        pid: entry.ticks - (before[pid].ticks if pid in before else 0)
        for pid, entry in now.items()
    }
    # A process gone since the previous walk had its earlier ticks reported under its own pid,
    # and its whole lifetime now sits in its parent's child counters: take the part already
    # reported back from the nearest ancestor still alive, so the shares add up to the busy ticks.
    for pid, entry in before.items():
        if pid not in now:
            ancestor = _nearest_live_ancestor(entry.ppid, now, before)
            if ancestor is not None:
                deltas[ancestor] -= entry.ticks
    by_pid: dict[int, float] = {}
    for pid, delta in deltas.items():
        share = min(1.0, max(0.0, delta / capacity_ticks))
        if share >= MIN_PID_SHARE:
            by_pid[pid] = share
    return CpuReading(usage, by_pid)


def _nearest_live_ancestor(
    pid: int, now: Mapping[int, ProcessTicks], last: Mapping[int, ProcessTicks]
) -> int | None:
    """``pid`` itself when it is still alive, else its closest ancestor (by the parent pids last seen) that is."""
    for _ in range(_MAX_ANCESTRY):
        if pid in now:
            return pid
        entry = last.get(pid)
        if entry is None or pid <= 1:
            return None
        pid = entry.ppid
    return None


class Sample(NamedTuple):
    """One reading for the graphs: when, the CPU in use, and the memory in use, both as shares."""

    t_ms: int
    cpu: float
    memory: float


def render_recent(
    sampled_at: float,
    cores: int,
    cpu: CpuReading,
    memory: Mapping[str, int],
    samples: Iterable[Sample],
) -> str:
    return json.dumps(
        {
            "version": RECENT_FILE_VERSION,
            "sampled_at": sampled_at,
            "interval_seconds": SAMPLE_INTERVAL_SECONDS,
            "cores": cores,
            "cpu": {
                "usage": round(cpu.usage, 4),
                "by_pid": {
                    str(pid): round(share, 4) for pid, share in cpu.by_pid.items()
                },
            },
            "memory": dict(memory),
            "samples": [
                [sample.t_ms, round(sample.cpu, 4), round(sample.memory, 4)]
                for sample in samples
            ],
        }
    )


def load_recent_samples(
    path: Path, now: float, max_age_seconds: float = RECENT_WINDOW_SECONDS
) -> list[Sample]:
    """The samples a recent file holds, when it is fresh enough to continue from; else nothing."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict) or data.get("version") != RECENT_FILE_VERSION:
        return []
    sampled_at = data.get("sampled_at")
    if not isinstance(sampled_at, (int, float)) or now - sampled_at > max_age_seconds:
        return []
    samples: list[Sample] = []
    for raw in data.get("samples", []):
        if (
            isinstance(raw, list)
            and len(raw) == 3
            and all(isinstance(value, (int, float)) for value in raw)
        ):
            samples.append(Sample(int(raw[0]), float(raw[1]), float(raw[2])))
    return samples


class MinuteAggregator:
    """Folds samples into one line per closed minute: the minute's mean CPU and memory and its peak CPU."""

    def __init__(self) -> None:
        self._minute_end_ms: int | None = None
        self._cpu: list[float] = []
        self._memory: list[float] = []

    def add(self, sample: Sample) -> dict[str, float | int] | None:
        """Take a sample; when it starts a new minute, answer the closed minute's line."""
        minute_end_ms = (sample.t_ms // MINUTE_MS + 1) * MINUTE_MS
        closed = None
        if (
            self._minute_end_ms is not None
            and minute_end_ms != self._minute_end_ms
            and self._cpu
        ):
            closed = {
                "t": self._minute_end_ms,
                "cpu": round(sum(self._cpu) / len(self._cpu), 4),
                "cpu_max": round(max(self._cpu), 4),
                "memory": round(sum(self._memory) / len(self._memory), 4),
            }
            self._cpu, self._memory = [], []
        self._minute_end_ms = minute_end_ms
        self._cpu.append(sample.cpu)
        self._memory.append(sample.memory)
        return closed


def append_history_line(
    path: Path, line: Mapping[str, float | int], now_ms: int
) -> None:
    """Append one minute line; when the file has grown past two days, rewrite it keeping the last day."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as history_file:
        history_file.write(json.dumps(dict(line)) + "\n")
    if path.stat().st_size < _history_rewrite_bytes():
        return
    keep_from_ms = now_ms - int(HISTORY_KEEP_SECONDS * 1000)
    kept = [
        raw
        for raw in path.read_text().splitlines()
        if _history_line_time(raw) >= keep_from_ms
    ]
    write_replacing(path, "".join(raw + "\n" for raw in kept))


def _history_line_time(raw: str) -> int:
    try:
        parsed = json.loads(raw)
    except ValueError:
        return 0
    time_ms = parsed.get("t") if isinstance(parsed, dict) else None
    return int(time_ms) if isinstance(time_ms, (int, float)) else 0


def _history_rewrite_bytes() -> int:
    # About 70 bytes per line, one line per minute, two days of them.
    return int(70 * HISTORY_REWRITE_SECONDS / 60)


Root = dict[str, str]
RootsReader = Callable[[], dict[int, Root]]


def read_roots(
    supervisor_socket: Path = supervisor_rpc.SUPERVISOR_SOCKET,
    is_alive: Callable[[int], bool] = registry.is_process_alive,
) -> dict[int, Root]:
    """The processes a stretch's CPU is credited to: each registered agent's main pid and each supervised program's."""
    roots: dict[int, Root] = {}
    for program, pid in supervisor_rpc.supervised_pids(supervisor_socket).items():
        roots[pid] = {"kind": "program", "program": program}
    for agent_id, pids in registry.live_pids_by_agent_id(is_alive).items():
        for pid in pids:
            roots[pid] = {"kind": "chat", "agent_id": agent_id}
    return roots


def _root_key(
    pid: int,
    now: Mapping[int, ProcessTicks],
    last: Mapping[int, ProcessTicks],
    roots: Mapping[int, Root],
) -> int | str:
    """The root pid ``pid`` rolls up to through its ancestors, or ``other`` when none is a root."""
    for _ in range(_MAX_ANCESTRY):
        if pid in roots:
            return pid
        entry = now.get(pid) or last.get(pid)
        if entry is None or pid <= 1:
            return OTHER_ROOT
        pid = entry.ppid
    return OTHER_ROOT


def _iso_nanoseconds(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "000Z"


class StretchTracker:
    """The busy-stretch state machine: opens on sustained high usage, credits the CPU to the roots as it goes,
    and answers the stretch's event when it closes."""

    def __init__(self, roots_reader: RootsReader) -> None:
        self._roots_reader = roots_reader
        self._above_since: float | None = None
        self._below_since: float | None = None
        self._opened_at: float | None = None
        # Each sample's time and usage since the first one at or above the open threshold.
        self._usages: list[tuple[float, float]] = []
        self._accumulated: dict[int | str, int] = collections.defaultdict(int)
        self._root_labels: dict[int, Root] = {}
        self._last_ticks: dict[int, ProcessTicks] = {}
        self._last_fold_at: float = 0.0

    @property
    def is_open(self) -> bool:
        return self._opened_at is not None

    def feed(
        self, now: float, usage: float, ticks: Mapping[int, ProcessTicks], cores: int
    ) -> dict[str, object] | None:
        """Take one sample; answer the event of a stretch this sample closed, if any."""
        opened_at = self._opened_at
        if opened_at is None:
            self._track_opening(now, usage, ticks)
            return None
        self._usages.append((now, usage))
        if now - self._last_fold_at >= ATTRIBUTION_INTERVAL_SECONDS:
            self._fold(now, ticks)
        if usage < STRETCH_CLOSE_USAGE:
            self._below_since = now if self._below_since is None else self._below_since
        else:
            self._below_since = None
        below_since = self._below_since
        if below_since is None or now - below_since < STRETCH_CLOSE_SECONDS:
            return None
        return self._close(now, ticks, cores, opened_at, ended_at=below_since)

    def _track_opening(
        self, now: float, usage: float, ticks: Mapping[int, ProcessTicks]
    ) -> None:
        if usage < STRETCH_OPEN_USAGE:
            self._above_since = None
            self._usages = []
            return
        if self._above_since is None:
            self._above_since = now
        self._usages.append((now, usage))
        if now - self._above_since < STRETCH_OPEN_SECONDS:
            return
        self._opened_at = self._above_since
        self._above_since = None
        self._below_since = None
        self._accumulated = collections.defaultdict(int)
        self._root_labels = {}
        self._last_ticks = dict(ticks)
        self._last_fold_at = now

    def _fold(self, now: float, ticks: Mapping[int, ProcessTicks]) -> None:
        """Credit the CPU used since the last fold to the roots the processes roll up to."""
        roots = self._roots_reader()
        self._root_labels.update(roots)
        for pid, entry in ticks.items():
            previous = self._last_ticks.get(pid)
            delta = entry.ticks - (previous.ticks if previous is not None else 0)
            self._accumulated[_root_key(pid, ticks, self._last_ticks, roots)] += delta
        # A process gone since the last fold had its last counter credited already, and its whole
        # lifetime now sits in its parent's child counters, which the parent's root receives above:
        # take the part credited before back from that root. Its own root keeps what it earned.
        for pid, entry in self._last_ticks.items():
            if pid not in ticks:
                self._accumulated[
                    _root_key(entry.ppid, ticks, self._last_ticks, roots)
                ] -= entry.ticks
        self._last_ticks = dict(ticks)
        self._last_fold_at = now

    def _close(
        self,
        now: float,
        ticks: Mapping[int, ProcessTicks],
        cores: int,
        opened_at: float,
        ended_at: float,
    ) -> dict[str, object] | None:
        self._fold(now, ticks)
        # The stretch's own samples: from its first busy one up to the one that began its closing minute.
        usages = [usage for at, usage in self._usages if at < ended_at]
        self._usages = []
        self._opened_at = None
        self._below_since = None
        duration = ended_at - opened_at
        if duration < STRETCH_MIN_SECONDS:
            return None
        credited = {key: max(0, value) for key, value in self._accumulated.items()}
        total = sum(credited.values())
        shares = sorted(
            (
                (key, value / total)
                for key, value in credited.items()
                if total and key != OTHER_ROOT
            ),
            key=lambda pair: pair[1],
            reverse=True,
        )
        top = [
            {**self._root_labels.get(int(key), {}), "share": round(share, 3)}
            for key, share in shares[:STRETCH_TOP_COUNT]
            if share >= STRETCH_TOP_MIN_SHARE and isinstance(key, int)
        ]
        return {
            "timestamp": _iso_nanoseconds(datetime.fromtimestamp(now, timezone.utc)),
            "type": STRETCH_EVENT_TYPE,
            "event_id": f"cpu-stretch-{uuid.uuid4().hex}",
            "source": STRETCH_EVENT_SOURCE,
            "started_at": _iso_nanoseconds(
                datetime.fromtimestamp(opened_at, timezone.utc)
            ),
            "ended_at": _iso_nanoseconds(
                datetime.fromtimestamp(ended_at, timezone.utc)
            ),
            "duration_seconds": round(duration),
            "peak": round(max(usages), 4),
            "average": round(sum(usages) / len(usages), 4),
            "cores": cores,
            "top": top,
            "other_share": round(1 - sum(share for _, share in shares), 3)
            if total
            else 1.0,
        }


def append_event(path: Path, event: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as events_file:
        events_file.write(json.dumps(dict(event)) + "\n")


def _wall_clock() -> float:
    return datetime.now(timezone.utc).timestamp()


class Sampler:
    """The whole loop's state: the previous walk, the recent ring, the minute aggregator and the stretch tracker."""

    def __init__(
        self,
        state_dir: Path = STATE_DIR,
        proc: Path = PROC,
        cgroup_dir: Path = CGROUP_DIR,
        host_meminfo: Path = HOST_MEMINFO_PATH,
        roots_reader: RootsReader = read_roots,
        clock: Callable[[], float] = _wall_clock,
        clk_tck: int | None = None,
        cpu_count: int | None = None,
    ) -> None:
        self.state_dir = state_dir
        self.proc = proc
        self.cgroup_dir = cgroup_dir
        self.host_meminfo = host_meminfo
        self.clock = clock
        self.clk_tck = clk_tck if clk_tck is not None else os.sysconf("SC_CLK_TCK")
        self.cpu_count = cpu_count
        self.stretches = StretchTracker(roots_reader)
        self.minutes = MinuteAggregator()
        self.samples: collections.deque[Sample] = collections.deque()
        self._previous_ticks: dict[int, ProcessTicks] | None = None
        self._previous_at: float = 0.0
        for sample in load_recent_samples(state_dir / RECENT_FILENAME, self.clock()):
            self.samples.append(sample)

    def sample_once(self) -> dict[str, object] | None:
        """One round: walk, record, write the files; answer the event of a stretch that closed."""
        now = self.clock()
        ticks = read_process_ticks(self.proc)
        cores = read_cores(self.cgroup_dir, self.cpu_count)
        memory = read_meminfo(self.proc, self.host_meminfo, self.clock)
        reading = (
            cpu_reading(
                self._previous_ticks,
                ticks,
                now - self._previous_at,
                cores,
                self.clk_tck,
            )
            if self._previous_ticks is not None
            else None
        )
        self._previous_ticks, self._previous_at = ticks, now
        if reading is None:
            return None
        sample = Sample(
            int(now * 1000),
            reading.usage,
            memory["used"] / memory["total"] if memory["total"] else 0.0,
        )
        self.samples.append(sample)
        while (
            self.samples
            and sample.t_ms - self.samples[0].t_ms > RECENT_WINDOW_SECONDS * 1000
        ):
            self.samples.popleft()
        write_replacing(
            self.state_dir / RECENT_FILENAME,
            render_recent(now, cores, reading, memory, self.samples),
        )
        closed_minute = self.minutes.add(sample)
        if closed_minute is not None:
            append_history_line(
                self.state_dir / HISTORY_FILENAME, closed_minute, sample.t_ms
            )
        event = self.stretches.feed(now, reading.usage, ticks, cores)
        if event is not None:
            append_event(self.state_dir / STRETCH_EVENTS_RELATIVE_PATH, event)
        return event
