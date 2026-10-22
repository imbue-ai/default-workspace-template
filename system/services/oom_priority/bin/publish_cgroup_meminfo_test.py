"""The publisher script end to end: one round under a bare ``python3`` against a
fake cgroup directory, since that is how supervisord runs it. The publishing
policy itself is tested with ``oom_priority.cgroup_meminfo``.
"""

import subprocess
import sys
from pathlib import Path

from oom_priority.testing import write_fake_cgroup

_SCRIPT = Path(__file__).parent / "publish_cgroup_meminfo.py"
# A plain Linux kernel: the tests pass the release explicitly because the CI
# sandboxes themselves run under gVisor, where the script has nothing to do.
_LINUX_RELEASE = "7.0.0-38-generic"
_GVISOR_RELEASE = "4.19.0-gvisor"


def _run_script(
    cgroup_dir: Path,
    target: Path,
    *,
    once: bool,
    kernel_release: str = _LINUX_RELEASE,
) -> subprocess.CompletedProcess[str]:
    argv = [
        sys.executable,
        str(_SCRIPT),
        "--cgroup-dir",
        str(cgroup_dir),
        "--target",
        str(target),
        "--kernel-release",
        kernel_release,
    ]
    if once:
        argv.append("--once")
    return subprocess.run(argv, capture_output=True, text=True, timeout=30)


def test_one_round_publishes_a_capped_cgroup_and_says_so_once(tmp_path: Path) -> None:
    cgroup_dir = tmp_path / "cgroup"
    write_fake_cgroup(
        cgroup_dir, "8589934592", "1073741824", {"anon": 1000, "inactive_file": 1024}
    )
    target = tmp_path / "mngr-vol" / ".host-meminfo"
    result = _run_script(cgroup_dir, target, once=True)
    assert result.returncode == 0, result.stderr
    lines = target.read_text().splitlines()
    assert lines[0] == "MemTotal: 8388608 kB"
    assert lines[1] == "MemAvailable: 7340033 kB"
    assert lines[2].startswith("Timestamp: ")
    assert result.stderr.count("publishing the cgroup's limit and headroom") == 1


def test_one_round_on_an_uncapped_cgroup_publishes_nothing(tmp_path: Path) -> None:
    cgroup_dir = tmp_path / "cgroup"
    write_fake_cgroup(cgroup_dir, "max", "4096", {})
    target = tmp_path / "mngr-vol" / ".host-meminfo"
    result = _run_script(cgroup_dir, target, once=True)
    assert result.returncode == 0, result.stderr
    assert not target.exists()
    assert "no memory cap" in result.stderr


def test_under_gvisor_the_script_exits_cleanly_without_touching_the_file(
    tmp_path: Path,
) -> None:
    cgroup_dir = tmp_path / "cgroup"
    write_fake_cgroup(cgroup_dir, "8589934592", "1073741824", {})
    target = tmp_path / "mngr-vol" / ".host-meminfo"
    target.parent.mkdir()
    published_by_the_vm = (
        "MemTotal: 4194304 kB\nMemAvailable: 1048576 kB\nTimestamp: 5\n"
    )
    target.write_text(published_by_the_vm)
    # Not --once: under gVisor the loop ends on its own after the first round.
    result = _run_script(cgroup_dir, target, once=False, kernel_release=_GVISOR_RELEASE)
    assert result.returncode == 0, result.stderr
    assert "gVisor" in result.stderr
    assert target.read_text() == published_by_the_vm
