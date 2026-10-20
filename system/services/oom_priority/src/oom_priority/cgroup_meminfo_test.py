import re
from pathlib import Path

from oom_priority import cgroup_meminfo
from oom_priority.testing import write_fake_cgroup

_GIB = 1024**3
_REPO_ROOT = Path(__file__).resolve().parents[5]
_EARLYOOM_CONF = _REPO_ROOT / "system" / "supervisord.conf.d" / "earlyoom.conf"


def _publish(
    cgroup_dir: Path, target: Path, kernel_release: str = "7.0.0-38-generic"
) -> cgroup_meminfo.PublishOutcome:
    return cgroup_meminfo.publish_once(cgroup_dir, target, kernel_release, 1760000000)


def test_headroom_adds_reclaimable_page_cache_and_slab_to_the_free_part(
    tmp_path: Path,
) -> None:
    write_fake_cgroup(
        tmp_path,
        str(8 * _GIB),
        str(6 * _GIB),
        {
            "anon": 5 * _GIB,
            "active_file": _GIB // 2,
            "inactive_file": _GIB // 4,
            "slab_reclaimable": _GIB // 4,
            "slab_unreclaimable": _GIB,
        },
    )
    assert cgroup_meminfo.read_cgroup_memory_headroom(tmp_path) == (8 * _GIB, 3 * _GIB)


def test_headroom_is_clamped_to_the_limit_and_to_zero(tmp_path: Path) -> None:
    # Reclaimable cache can exceed what is in use at the start of a container's life.
    write_fake_cgroup(tmp_path, str(_GIB), "1024", {"inactive_file": 2 * _GIB})
    assert cgroup_meminfo.read_cgroup_memory_headroom(tmp_path) == (_GIB, _GIB)
    # Usage momentarily past the limit (the kernel is mid-reclaim) reads as no headroom.
    write_fake_cgroup(tmp_path, str(_GIB), str(_GIB + 4096), {})
    assert cgroup_meminfo.read_cgroup_memory_headroom(tmp_path) == (_GIB, 0)


def test_the_three_ways_a_cgroup_gives_no_headroom_are_told_apart(
    tmp_path: Path,
) -> None:
    write_fake_cgroup(tmp_path, "max", "4096", {})
    assert (
        cgroup_meminfo.read_cgroup_memory_headroom(tmp_path)
        == cgroup_meminfo.PublishOutcome.UNCAPPED
    )
    # A lima VM's root cgroup, a cgroup v1 host, or gVisor's own tree: no memory.max at all.
    assert (
        cgroup_meminfo.read_cgroup_memory_headroom(tmp_path / "missing")
        == cgroup_meminfo.PublishOutcome.NO_CONTROLLER
    )
    # A counter that is momentarily unreadable, or not a number.
    (tmp_path / "memory.max").write_text(f"{_GIB}\n")
    (tmp_path / "memory.current").unlink()
    assert (
        cgroup_meminfo.read_cgroup_memory_headroom(tmp_path)
        == cgroup_meminfo.PublishOutcome.UNREADABLE
    )
    (tmp_path / "memory.current").write_text("garbage\n")
    assert (
        cgroup_meminfo.read_cgroup_memory_headroom(tmp_path)
        == cgroup_meminfo.PublishOutcome.UNREADABLE
    )
    # memory.stat too: the gen-2 publisher skips the round rather than counting
    # the reclaimable share as zero, and the two must stay in step.
    (tmp_path / "memory.current").write_text("4096\n")
    (tmp_path / "memory.stat").unlink()
    assert (
        cgroup_meminfo.read_cgroup_memory_headroom(tmp_path)
        == cgroup_meminfo.PublishOutcome.UNREADABLE
    )


def test_render_writes_the_meminfo_subset_earlyoom_reads_in_kib() -> None:
    assert cgroup_meminfo.render_host_meminfo(8 * _GIB, 3 * _GIB, 1760000000) == (
        "MemTotal: 8388608 kB\nMemAvailable: 3145728 kB\nTimestamp: 1760000000\n"
    )


def test_gvisor_is_named_by_the_kernel_release() -> None:
    assert cgroup_meminfo.is_gvisor_kernel("4.19.0-gvisor")
    assert not cgroup_meminfo.is_gvisor_kernel("7.0.0-38-generic")


def test_publish_writes_the_file_by_rename_into_a_directory_it_creates(
    tmp_path: Path,
) -> None:
    write_fake_cgroup(
        tmp_path / "cgroup", str(8 * _GIB), str(6 * _GIB), {"inactive_file": _GIB}
    )
    target = tmp_path / "mngr-vol" / ".host-meminfo"
    assert (
        _publish(tmp_path / "cgroup", target) == cgroup_meminfo.PublishOutcome.PUBLISHED
    )
    assert (
        target.read_text()
        == "MemTotal: 8388608 kB\nMemAvailable: 3145728 kB\nTimestamp: 1760000000\n"
    )
    # No temp file is left beside the published one.
    assert sorted(path.name for path in target.parent.iterdir()) == [".host-meminfo"]


def test_publish_removes_the_file_once_the_container_is_uncapped(
    tmp_path: Path,
) -> None:
    write_fake_cgroup(tmp_path / "cgroup", "max", "4096", {})
    target = tmp_path / "mngr-vol" / ".host-meminfo"
    target.parent.mkdir()
    target.write_text("MemTotal: 1 kB\nMemAvailable: 1 kB\nTimestamp: 1\n")
    assert (
        _publish(tmp_path / "cgroup", target) == cgroup_meminfo.PublishOutcome.UNCAPPED
    )
    assert not target.exists()
    # Removing an already-absent file is fine too.
    assert (
        _publish(tmp_path / "cgroup", target) == cgroup_meminfo.PublishOutcome.UNCAPPED
    )


def test_publish_leaves_someone_elses_file_alone_when_it_has_no_answer(
    tmp_path: Path,
) -> None:
    # Under gVisor, and wherever there is no v2 controller or a counter cannot be
    # read, the file may be the gen-2 VM's: it is never removed or rewritten.
    target = tmp_path / "mngr-vol" / ".host-meminfo"
    target.parent.mkdir()
    published_by_the_vm = (
        "MemTotal: 4194304 kB\nMemAvailable: 1048576 kB\nTimestamp: 5\n"
    )
    target.write_text(published_by_the_vm)
    write_fake_cgroup(tmp_path / "cgroup", str(8 * _GIB), str(6 * _GIB), {})
    assert (
        _publish(tmp_path / "cgroup", target, "4.19.0-gvisor")
        == cgroup_meminfo.PublishOutcome.GVISOR
    )
    assert (
        _publish(tmp_path / "no-controller", target)
        == cgroup_meminfo.PublishOutcome.NO_CONTROLLER
    )
    (tmp_path / "cgroup" / "memory.current").unlink()
    assert (
        _publish(tmp_path / "cgroup", target)
        == cgroup_meminfo.PublishOutcome.UNREADABLE
    )
    assert target.read_text() == published_by_the_vm


def test_the_published_path_is_the_one_earlyoom_reads() -> None:
    # earlyoom.conf names the file on its command line; the publisher's constant
    # must be that file, or the two silently talk past each other.
    command_line = next(
        line
        for line in _EARLYOOM_CONF.read_text().splitlines()
        if line.startswith("command=")
    )
    match = re.search(r"--host-meminfo\s+(\S+)", command_line)
    assert match is not None, command_line
    assert Path(match.group(1)) == cgroup_meminfo.HOST_MEMINFO_PATH
