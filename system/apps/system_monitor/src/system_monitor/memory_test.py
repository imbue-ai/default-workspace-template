"""Tests for the memory totals, the disk, and the memory graph's history."""

from pathlib import Path

from system_monitor import memory


def test_meminfo_reports_used_as_total_minus_available(tmp_path: Path) -> None:
    (tmp_path / "meminfo").write_text(
        "MemTotal:  4000 kB\nMemFree:  500 kB\nMemAvailable:  1000 kB\nHugePages_Total: 0\n"
    )

    assert memory.read_meminfo(tmp_path, host_meminfo=tmp_path / "no-host-meminfo") == {
        "total": 4000 * 1024,
        "available": 1000 * 1024,
        "used": 3000 * 1024,
    }


def test_meminfo_follows_the_memory_guard_to_a_fresh_host_meminfo(
    tmp_path: Path,
) -> None:
    (tmp_path / "meminfo").write_text("MemTotal: 16000 kB\nMemAvailable: 12000 kB\n")
    host = tmp_path / ".host-meminfo"
    host.write_text("MemTotal: 4000 kB\nMemAvailable: 600 kB\n")
    written = host.stat().st_mtime

    # Fresh: the workspace's own limit and headroom, as earlyoom reads them.
    assert memory.read_meminfo(tmp_path, host, clock=lambda: written + 4.0) == {
        "total": 4000 * 1024,
        "available": 600 * 1024,
        "used": 3400 * 1024,
    }
    # Older than 5 s: earlyoom ignores it, and so does the app.
    assert (
        memory.read_meminfo(tmp_path, host, clock=lambda: written + 6.0)["total"]
        == 16000 * 1024
    )
    # Not a full answer: /proc/meminfo.
    host.write_text("MemTotal: 4000 kB\n")
    assert (
        memory.read_meminfo(tmp_path, host, clock=lambda: written)["total"]
        == 16000 * 1024
    )


def test_memory_history_keeps_only_its_window_of_readings() -> None:
    now = [1000.0]
    used = [500]
    history = memory.MemoryHistory(
        read=lambda: {"total": 1000, "available": 1000 - used[0], "used": used[0]},
        clock=lambda: now[0],
        window=600.0,
    )

    history.record()
    now[0] += 300
    used[0] = 750
    history.record()
    assert history.samples() == [
        {"t": 1_000_000, "v": 0.5},
        {"t": 1_300_000, "v": 0.75},
    ]

    now[0] += 400  # the first reading is now 700 s old, past the 600 s window
    history.record()
    assert [sample["t"] for sample in history.samples()] == [1_300_000, 1_700_000]


def test_the_sampler_records_a_reading_at_once_and_stops_when_asked() -> None:
    history = memory.MemoryHistory(
        read=lambda: {"total": 4, "available": 1, "used": 3}, clock=lambda: 1.0
    )

    stop = memory.start_sampler(history, every=60.0)
    try:
        assert history.samples() == [{"t": 1000, "v": 0.75}]
    finally:
        stop.set()
