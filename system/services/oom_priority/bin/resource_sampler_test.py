"""The sampler program end to end: a few rounds under a bare ``python3`` against a fake ``/proc``, since that is
how supervisord runs it. The policy itself is tested with ``oom_priority.resource_sampler``.
"""

import json
import subprocess
import sys
from pathlib import Path

_SCRIPT = Path(__file__).parent / "resource_sampler.py"


def _stat_line(pid: int, ppid: int, own: int) -> str:
    return f"{pid} (python3) S {ppid} 1 1 0 -1 4194560 10 20 0 0 {own} 0 0 0 20 0 1 0 5 0 0\n"


def test_a_few_rounds_write_the_recent_file_and_exit_cleanly(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    for pid, ppid, own in ((1, 0, 100), (2, 1, 300)):
        (proc / str(pid)).mkdir(parents=True)
        (proc / str(pid) / "stat").write_text(_stat_line(pid, ppid, own))
    (proc / "meminfo").write_text("MemTotal: 8000 kB\nMemAvailable: 3000 kB\n")
    state = tmp_path / "state"

    result = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--proc",
            str(proc),
            "--state-dir",
            str(state),
            "--cgroup-dir",
            str(tmp_path / "no-cgroup"),
            "--host-meminfo",
            str(tmp_path / "no-host-meminfo"),
            "--supervisor-socket",
            str(tmp_path / "no.sock"),
            "--interval-seconds",
            "0.05",
            "--rounds",
            "3",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    data = json.loads((state / "recent.json").read_text())
    assert data["version"] == 1
    assert data["memory"]["total"] == 8000 * 1024
    assert len(data["samples"]) == 2
    assert "recording CPU and memory readings" in result.stderr
