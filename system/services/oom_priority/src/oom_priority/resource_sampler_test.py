"""Tests for the resource sampler: the stat parser, the busy-ticks sum, the cores, the files, the minute lines,
and the busy-stretch state machine with its attribution."""

import json
from pathlib import Path
from typing import Any

from oom_priority import resource_sampler as rs

_CLK_TCK = 100


def _stat_line(
    pid: int, ppid: int, own: int, children: int, comm: str = "python3"
) -> str:
    # The fields after the command: state, ppid, pgrp, session, tty, tpgid, flags, minflt, cminflt,
    # majflt, cmajflt, utime, stime, cutime, cstime, then the rest.
    return f"{pid} ({comm}) S {ppid} 1 1 0 -1 4194560 10 20 0 0 {own} 0 {children} 0 20 0 1 0 5 0 0\n"


def _write_proc(proc: Path, processes: dict[int, tuple[int, int, int]]) -> None:
    """``processes``: pid -> (ppid, own ticks, reaped-children ticks)."""
    for pid, (ppid, own, children) in processes.items():
        entry = proc / str(pid)
        entry.mkdir(parents=True, exist_ok=True)
        (entry / "stat").write_text(_stat_line(pid, ppid, own, children))
    proc.mkdir(exist_ok=True)
    (proc / "meminfo").write_text("MemTotal: 8000 kB\nMemAvailable: 3000 kB\n")


def _ticks(processes: dict[int, tuple[int, int, int]]) -> dict[int, rs.ProcessTicks]:
    return {
        pid: rs.ProcessTicks(own + children, ppid)
        for pid, (ppid, own, children) in processes.items()
    }


def test_the_stat_parser_counts_from_the_last_parenthesis_so_a_command_with_spaces_is_fine() -> (
    None
):
    assert rs.parse_stat(
        _stat_line(4121, 7, own=117, children=187, comm="claude (code) x")
    ) == rs.ProcessTicks(304, 7)
    assert rs.parse_stat("garbage") is None
    assert rs.parse_stat("1 (x) S 0 1") is None


def test_the_walk_reads_every_numeric_entry_and_skips_what_it_cannot(
    tmp_path: Path,
) -> None:
    _write_proc(tmp_path, {1: (0, 5, 0), 42: (1, 10, 3)})
    (tmp_path / "self").mkdir()
    (tmp_path / "43").mkdir()

    assert rs.read_process_ticks(tmp_path) == {
        1: rs.ProcessTicks(5, 0),
        42: rs.ProcessTicks(13, 1),
    }


def test_busy_ticks_are_the_change_in_the_sum_so_an_exited_child_is_neither_lost_nor_counted_twice() -> (
    None
):
    # Between the two walks, process 30 (a child of 20) used 50 more ticks, then exited and was reaped:
    # its 80 lifetime ticks moved into 20's children counter. 20 itself used 10, 1 used 0, and 40 is new
    # with 20 ticks of its own.
    before = _ticks({1: (0, 100, 0), 20: (1, 200, 0), 30: (20, 30, 0)})
    now = _ticks({1: (0, 100, 0), 20: (1, 210, 80), 40: (20, 20, 0)})

    reading = rs.cpu_reading(
        before, now, elapsed_seconds=5.0, cores=2, clk_tck=_CLK_TCK
    )

    # 10 (20's own) + 80 (30's whole life, less the 30 already counted) + 20 (40) = 80 busy ticks of 1000.
    assert reading.usage == 0.08
    # 20's share holds its own 10 and the 50 of 30's life not yet reported under 30, so the shares add up.
    assert reading.by_pid == {20: 0.06, 40: 0.02}
    assert sum(reading.by_pid.values()) == reading.usage


def test_usage_is_clamped_and_shares_under_the_floor_are_left_out() -> None:
    before = _ticks({1: (0, 100, 0)})
    now = _ticks({1: (0, 5000, 0), 2: (1, 0, 0)})

    reading = rs.cpu_reading(
        before, now, elapsed_seconds=1.0, cores=1, clk_tck=_CLK_TCK
    )

    assert reading.usage == 1.0
    assert reading.by_pid == {1: 1.0}
    assert rs.cpu_reading(now, before, 1.0, 1, _CLK_TCK).usage == 0.0


def test_cores_come_from_the_cgroup_quota_rounded_up_else_the_cpu_count(
    tmp_path: Path,
) -> None:
    (tmp_path / "cpu.max").write_text("150000 100000\n")
    assert rs.read_cores(tmp_path, cpu_count=8) == 2
    (tmp_path / "cpu.max").write_text("max 100000\n")
    assert rs.read_cores(tmp_path, cpu_count=8) == 8
    assert rs.read_cores(tmp_path / "missing", cpu_count=3) == 3
    (tmp_path / "cpu.max").write_text("0 0\n")
    assert rs.read_cores(tmp_path, cpu_count=0) == 1


def test_the_recent_file_round_trips_and_a_stale_one_is_ignored(tmp_path: Path) -> None:
    samples = [rs.Sample(1_000_000, 0.3, 0.6), rs.Sample(1_005_000, 0.35, 0.61)]
    rendered = rs.render_recent(
        1005.0,
        4,
        rs.CpuReading(0.35, {42: 0.1234}),
        {"total": 8, "available": 3, "used": 5},
        samples,
    )
    path = tmp_path / "recent.json"
    rs.write_replacing(path, rendered)

    data = json.loads(path.read_text())
    assert data["cpu"] == {"usage": 0.35, "by_pid": {"42": 0.1234}}
    assert rs.load_recent_samples(path, now=1200.0) == samples
    assert rs.load_recent_samples(path, now=1005.0 + 601.0) == []
    assert rs.load_recent_samples(tmp_path / "missing.json", now=0.0) == []


def test_a_minute_line_holds_the_closed_minute_s_mean_and_peak() -> None:
    minutes = rs.MinuteAggregator()

    assert minutes.add(rs.Sample(60_000, 0.2, 0.5)) is None
    assert minutes.add(rs.Sample(65_000, 0.6, 0.5)) is None
    # The first sample of the next minute closes the previous one.
    assert minutes.add(rs.Sample(120_000, 0.9, 0.7)) == {
        "t": 120_000,
        "cpu": 0.4,
        "cpu_max": 0.6,
        "memory": 0.5,
    }
    # A gap of a minute with no samples yields no line for it: the next line is the minute that had samples.
    assert minutes.add(rs.Sample(300_000, 0.1, 0.7)) == {
        "t": 180_000,
        "cpu": 0.9,
        "cpu_max": 0.9,
        "memory": 0.7,
    }


def test_the_history_is_rewritten_to_the_last_day_once_it_holds_two(
    tmp_path: Path,
) -> None:
    path = tmp_path / "history.jsonl"
    now_ms = 3 * 24 * 3600 * 1000
    minute_ms = 60_000
    # Three days of minute lines, oldest first.
    for index in range(3 * 24 * 60):
        rs.append_history_line(
            path,
            {"t": index * minute_ms, "cpu": 0.1, "cpu_max": 0.1, "memory": 0.5},
            now_ms,
        )

    kept = [json.loads(line)["t"] for line in path.read_text().splitlines()]
    assert kept[0] >= now_ms - 24 * 3600 * 1000
    assert kept[-1] == (3 * 24 * 60 - 1) * minute_ms
    assert len(kept) <= 24 * 60 + 1


def _drive(
    tracker: rs.StretchTracker,
    start: float,
    seconds: float,
    usage: float,
    ticks: dict[int, rs.ProcessTicks],
) -> tuple[float, list[dict[str, object]]]:
    """Feed one sample every 5 s for ``seconds`` from ``start``; answer the end time and the events closed."""
    events: list[dict[str, object]] = []
    now = start
    while now < start + seconds:
        event = tracker.feed(now, usage, ticks, cores=4)
        if event is not None:
            events.append(event)
        now += 5.0
    return now, events


def test_a_stretch_opens_after_a_minute_above_the_threshold_and_not_before() -> None:
    tracker = rs.StretchTracker(lambda: {})
    ticks = _ticks({1: (0, 0, 0)})

    now, _ = _drive(tracker, 0.0, 55.0, 0.95, ticks)
    assert not tracker.is_open
    now, _ = _drive(tracker, now, 10.0, 0.95, ticks)
    assert tracker.is_open


def test_a_stretch_opening_at_time_zero_is_measured_from_its_first_busy_sample() -> None:
    tracker = rs.StretchTracker(lambda: {})
    ticks = _ticks({1: (0, 0, 0)})

    now, events = _drive(tracker, 0.0, 300.0, 0.95, ticks)
    assert events == []
    now, events = _drive(tracker, now, 70.0, 0.2, ticks)

    assert [event["duration_seconds"] for event in events] == [300]
    assert events[0]["started_at"] == "1970-01-01T00:00:00.000000000Z"


def test_a_stretch_s_peak_and_average_cover_its_own_samples_and_not_its_closing_minute() -> (
    None
):
    tracker = rs.StretchTracker(lambda: {})
    ticks = _ticks({1: (0, 0, 0)})

    # The opening minute runs flat out, the next four at 0.9, and the closing minute at 0.2.
    now, _ = _drive(tracker, 0.0, 60.0, 1.0, ticks)
    now, _ = _drive(tracker, now, 240.0, 0.9, ticks)
    now, events = _drive(tracker, now, 70.0, 0.2, ticks)

    assert len(events) == 1
    assert events[0]["peak"] == 1.0
    assert events[0]["average"] == 0.92
    assert events[0]["duration_seconds"] == 300


def test_a_short_stretch_is_dropped_and_a_long_one_is_credited_to_its_roots() -> None:
    chat_root, program_root = 100, 200
    roots = {
        chat_root: {"kind": "chat", "agent_id": "agent-a"},
        program_root: {"kind": "program", "program": "recipe-box"},
    }
    tracker = rs.StretchTracker(lambda: roots)
    # Process 101 is a test run the chat started; 201 belongs to the program; 300 belongs to no root.
    counters = {
        1: [0, 0],
        chat_root: [0, 0],
        101: [0, 0],
        program_root: [0, 0],
        201: [0, 0],
        300: [0, 0],
    }
    parents = {
        1: 0,
        chat_root: 1,
        101: chat_root,
        program_root: 1,
        201: program_root,
        300: 1,
    }

    def ticks() -> dict[int, rs.ProcessTicks]:
        return {
            pid: rs.ProcessTicks(own + children, parents[pid])
            for pid, (own, children) in counters.items()
        }

    def busy(
        seconds: float, start: float, usage: float, per_second: dict[int, int]
    ) -> tuple[float, list[dict[str, object]]]:
        events: list[dict[str, object]] = []
        now = start
        while now < start + seconds:
            for pid, rate in per_second.items():
                counters[pid][0] += rate * 5
            event = tracker.feed(now, usage, ticks(), cores=4)
            if event is not None:
                events.append(event)
            now += 5.0
        return now, events

    # A stretch that lasts only 90 s before usage drops: dropped.
    now, events = busy(90.0, 0.0, 0.95, {101: 300})
    now, events = busy(70.0, now, 0.2, {})
    assert events == [] and not tracker.is_open

    # A long one: the chat's test run does most of it, the program's child some, an orphan the rest.
    now, events = busy(400.0, now, 0.98, {101: 300, 201: 60, 300: 40})
    # The test run exits mid-stretch and the chat reaps it: its lifetime moves into the chat's children.
    counters[chat_root][1] += counters[101][0]
    del counters[101]
    del parents[101]
    now, events = busy(200.0, now, 0.95, {201: 60, 300: 40})
    now, events = busy(70.0, now, 0.3, {})

    assert len(events) == 1
    event: Any = events[0]
    assert event["type"] == "cpu_stretch" and event["source"] == "cpu_stretches"
    assert 590 <= event["duration_seconds"] <= 610
    assert event["peak"] == 0.98
    # The stretch opened a minute into the busy phase, so the first minute's ticks are not credited.
    top = event["top"]
    assert [cause["kind"] for cause in top] == ["chat", "program"]
    assert top[0]["agent_id"] == "agent-a" and 0.6 <= top[0]["share"] <= 0.72
    assert top[1]["program"] == "recipe-box" and 0.15 <= top[1]["share"] <= 0.25
    assert 0.1 <= event["other_share"] <= 0.2


def test_the_sampler_writes_the_recent_file_from_its_second_sample_and_continues_its_ring_on_restart(
    tmp_path: Path,
) -> None:
    proc = tmp_path / "proc"
    _write_proc(proc, {1: (0, 100, 0), 2: (1, 50, 0)})
    state = tmp_path / "state"
    clock = [1000.0]
    sampler = rs.Sampler(
        state_dir=state,
        proc=proc,
        cgroup_dir=tmp_path / "no-cgroup",
        host_meminfo=tmp_path / "no-host-meminfo",
        roots_reader=lambda: {},
        clock=lambda: clock[0],
        clk_tck=_CLK_TCK,
        cpu_count=2,
    )

    assert sampler.sample_once() is None
    assert not (state / "recent.json").exists()
    clock[0] += 5.0
    _write_proc(proc, {1: (0, 100, 0), 2: (1, 250, 0)})
    assert sampler.sample_once() is None

    data = json.loads((state / "recent.json").read_text())
    assert data["cores"] == 2
    assert data["cpu"] == {"usage": 0.2, "by_pid": {"2": 0.2}}
    assert data["memory"] == {
        "total": 8000 * 1024,
        "available": 3000 * 1024,
        "used": 5000 * 1024,
    }
    assert data["samples"] == [[1_005_000, 0.2, 0.625]]

    clock[0] += 5.0
    restarted = rs.Sampler(
        state_dir=state,
        proc=proc,
        cgroup_dir=tmp_path / "no-cgroup",
        host_meminfo=tmp_path / "no-host-meminfo",
        roots_reader=lambda: {},
        clock=lambda: clock[0],
        clk_tck=_CLK_TCK,
        cpu_count=2,
    )
    assert list(restarted.samples) == [rs.Sample(1_005_000, 0.2, 0.625)]
