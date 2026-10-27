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


def test_process_memory_is_anonymous_memory_from_smaps_rollup_else_summed_smaps(tmp_path: Path) -> None:
    entry = tmp_path / "10"
    entry.mkdir()
    # Under gVisor there is no rollup: every mapping's Anonymous line is summed, and the binary's
    # file-backed Rss is not counted.
    (entry / "smaps").write_text(
        "00400000-00500000 r-xp 00000000 00:00 0\nRss:   900 kB\nAnonymous:   0 kB\n"
        "00600000-00700000 rw-p 00000000 00:00 0\nRss:   300 kB\nAnonymous:   300 kB\n"
        "7f000000-7f100000 rw-p 00000000 00:00 0\nRss:   50 kB\nAnonymous:   50 kB\n"
    )
    assert meminfo.read_process_anonymous_kib(10, tmp_path) == 350

    (entry / "smaps_rollup").write_text("Rss:   1250 kB\nAnonymous:   340 kB\n")
    assert meminfo.read_process_anonymous_kib(10, tmp_path) == 340

    assert meminfo.read_process_anonymous_kib(11, tmp_path) is None
