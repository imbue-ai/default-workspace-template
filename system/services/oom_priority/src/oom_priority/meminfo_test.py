"""Tests for the memory reader: /proc/meminfo, and the memory guard's headroom file while it is fresh."""

from pathlib import Path

from oom_priority import meminfo


def test_meminfo_reports_used_as_total_minus_available(tmp_path: Path) -> None:
    (tmp_path / "meminfo").write_text(
        "MemTotal:  4000 kB\nMemFree:  500 kB\nMemAvailable:  1000 kB\nHugePages_Total: 0\n"
    )

    assert meminfo.read_meminfo(
        tmp_path, host_meminfo=tmp_path / "no-host-meminfo"
    ) == {
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
    assert meminfo.read_meminfo(tmp_path, host, clock=lambda: written + 4.0) == {
        "total": 4000 * 1024,
        "available": 600 * 1024,
        "used": 3400 * 1024,
    }
    # Older than 5 s: earlyoom ignores it, and so does the app.
    assert (
        meminfo.read_meminfo(tmp_path, host, clock=lambda: written + 6.0)["total"]
        == 16000 * 1024
    )
    # Not a full answer: /proc/meminfo.
    host.write_text("MemTotal: 4000 kB\n")
    assert (
        meminfo.read_meminfo(tmp_path, host, clock=lambda: written)["total"]
        == 16000 * 1024
    )
