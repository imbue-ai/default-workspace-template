from pathlib import Path

from activity.memory_reading import MemoryCloser
from activity.memory_reading import MemoryReading
from activity.memory_reading import MemorySource
from activity.memory_reading import MemorySources
from activity.memory_reading import closing_point
from activity.memory_reading import parse_kib_fields
from activity.memory_reading import parse_min_available_percent
from activity.memory_reading import parse_min_free_swap_percent
from activity.memory_reading import read_earlyoom_meminfo
from activity.memory_reading import read_memory
from activity.memory_reading import reading_from_cgroup
from activity.memory_reading import reading_from_meminfo

_MIB = 1024 * 1024


def _sources(tmp_path: Path) -> MemorySources:
    return MemorySources(
        host_meminfo_path=tmp_path / "host-meminfo",
        cgroup_dir=tmp_path / "cgroup",
        proc_meminfo_path=tmp_path / "meminfo",
    )


def test_meminfo_fields_are_read_in_kib_and_lines_without_numbers_are_skipped() -> None:
    text = "MemTotal:        8124516 kB\nMemAvailable:    6400172 kB\nHugePages_Surp:\nBogus line\n"
    assert parse_kib_fields(text) == {"MemTotal": 8124516, "MemAvailable": 6400172}


def test_a_meminfo_reading_is_total_minus_available_and_needs_both_fields(tmp_path: Path) -> None:
    reading = reading_from_meminfo(
        "MemTotal: 6815744 kB\nMemAvailable: 2097152 kB\n", MemorySource.HOST_MEMINFO, tmp_path
    )
    assert reading is not None
    assert (reading.limit_bytes, reading.used_bytes) == (6656 * _MIB, 4608 * _MIB)
    assert reading_from_meminfo("MemTotal: 6815744 kB\n", MemorySource.HOST_MEMINFO, tmp_path) is None


def test_a_cgroup_reading_leaves_out_reclaimable_page_cache_and_an_unlimited_cgroup_is_not_a_limit(
    tmp_path: Path,
) -> None:
    reading = reading_from_cgroup(
        str(8192 * _MIB), str(3000 * _MIB), f"anon 1\ninactive_file {500 * _MIB}\n", tmp_path
    )
    assert reading is not None
    assert (reading.limit_bytes, reading.used_bytes, reading.source) == (8192 * _MIB, 2500 * _MIB, MemorySource.CGROUP)
    assert reading_from_cgroup("max\n", "123", "", tmp_path) is None


def test_the_host_meminfo_wins_then_the_cgroup_then_proc_meminfo(tmp_path: Path) -> None:
    sources = _sources(tmp_path)
    assert read_memory(sources) is None

    sources.proc_meminfo_path.write_text("MemTotal: 16777216 kB\nMemAvailable: 8388608 kB\n")
    proc_reading = read_memory(sources)
    assert proc_reading is not None and proc_reading.source is MemorySource.PROC_MEMINFO

    sources.cgroup_dir.mkdir()
    (sources.cgroup_dir / "memory.max").write_text(f"{8192 * _MIB}\n")
    (sources.cgroup_dir / "memory.current").write_text(f"{1024 * _MIB}\n")
    cgroup_reading = read_memory(sources)
    assert cgroup_reading is not None and cgroup_reading.source is MemorySource.CGROUP
    assert cgroup_reading.used_bytes == 1024 * _MIB

    sources.host_meminfo_path.write_text("MemTotal: 6815744 kB\nMemAvailable: 6815744 kB\n")
    host_reading = read_memory(sources)
    assert host_reading is not None and host_reading.source is MemorySource.HOST_MEMINFO
    assert host_reading.used_bytes == 0


_GIB = 1024 * _MIB
_EARLYOOM_ARGV = ("/usr/local/bin/earlyoom", "-m", "10,5", "-s", "10,5", "--avoid", "^(sshd|supervisord)$")


def _reading(source: MemorySource, limit_gib: int) -> MemoryReading:
    return MemoryReading(limit_bytes=limit_gib * _GIB, used_bytes=_GIB, source=source, source_detail="")


def test_earlyoom_threshold_is_read_from_its_own_argv() -> None:
    assert parse_min_available_percent(_EARLYOOM_ARGV) == 10
    assert parse_min_available_percent(("earlyoom", "-m15")) == 15
    assert parse_min_available_percent(("earlyoom", "-m", "lots")) is None
    assert parse_min_available_percent(("earlyoom", "-m")) is None


def test_earlyoom_without_a_threshold_uses_its_default_of_ten_percent() -> None:
    assert parse_min_available_percent(("earlyoom", "-r", "0")) == 10
    assert parse_min_free_swap_percent(("earlyoom",)) == 10
    assert parse_min_free_swap_percent(("earlyoom", "-s", "5,2")) == 5


def test_on_a_cloud_workspace_earlyoom_closes_at_its_threshold_of_the_limit_it_reads() -> None:
    meminfo = f"MemTotal: {6656 * 1024} kB\nMemAvailable: 0 kB\nSwapTotal: 0 kB\n"
    point = closing_point(_reading(MemorySource.HOST_MEMINFO, 6656 // 1024), meminfo, _EARLYOOM_ARGV)
    assert point.closer is MemoryCloser.MEMORY_GUARD
    assert point.used_bytes == (6656 // 1024) * _GIB * 90 // 100
    assert (point.min_available_percent, point.badness_total_kib) == (10, 6656 * 1024)
    assert "assumes nothing outside" not in point.detail
    assert "swap" not in point.detail


def test_under_runc_a_cap_below_earlyooms_point_means_the_kernel_closes_first_at_the_cap() -> None:
    machine = f"MemTotal: {16 * 1024 * 1024} kB\nSwapTotal: 0 kB\n"
    point = closing_point(_reading(MemorySource.CGROUP, 8), machine, _EARLYOOM_ARGV)
    assert (point.closer, point.used_bytes, point.badness_total_kib) == (
        MemoryCloser.SYSTEM_LIMIT,
        8 * _GIB,
        8 * 1024 * 1024,
    )


def test_under_runc_a_cap_above_earlyooms_point_leaves_earlyoom_closing_first() -> None:
    machine = f"MemTotal: {8 * 1024 * 1024} kB\nSwapTotal: {1024 * 1024} kB\n"
    point = closing_point(_reading(MemorySource.CGROUP, 8), machine, _EARLYOOM_ARGV)
    assert point.closer is MemoryCloser.MEMORY_GUARD
    assert point.used_bytes == 8 * _GIB * 90 // 100
    assert point.badness_total_kib == 9 * 1024 * 1024
    assert "assumes nothing outside the workspace is using it" in point.detail
    # The machine has swap, and earlyoom waits for free swap to fall below its -s threshold too.
    assert "waits until less than 10% of swap is free" in point.detail


def test_with_no_earlyoom_running_only_the_kernel_closes_things() -> None:
    point = closing_point(_reading(MemorySource.CGROUP, 8), "MemTotal: 1 kB\n", None)
    assert point.closer is MemoryCloser.SYSTEM_LIMIT


def test_earlyoom_reads_the_host_meminfo_when_there_is_one(tmp_path: Path) -> None:
    sources = _sources(tmp_path)
    assert read_earlyoom_meminfo(sources) is None
    sources.proc_meminfo_path.write_text("proc")
    assert read_earlyoom_meminfo(sources) == "proc"
    sources.host_meminfo_path.write_text("host")
    assert read_earlyoom_meminfo(sources) == "host"
