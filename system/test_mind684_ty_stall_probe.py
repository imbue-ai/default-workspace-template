"""MIND-684 probe -- NOT FOR MERGE. Times how the type ratchet's ``ty`` gets started, in CI's sandbox.

Writes its findings straight to the batch log (``capsys.disabled``) and under
``.test_output/mind684/``. Each run records its wall time, exit code, diagnostics line count,
the timestamped uv/ty verbose log, the largest gap between consecutive log lines, and
process snapshots taken while a run is slow.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_OUT = _ROOT / ".test_output" / "mind684"
_PROJECTS = sorted(p.parent for p in _ROOT.glob("system/*/*/test_*_ratchets.py"))
_TY = ["ty", "check", "--output-format", "concise", "--color", "never"]
_MODES: dict[str, list[str]] = {
    "plain": ["uv", "run", "-vv", *_TY],
    "frozen": ["uv", "run", "-vv", "--frozen", *_TY],
    "no_sync": ["uv", "run", "-vv", "--no-sync", *_TY],
    "frozen_no_sync": ["uv", "run", "-vv", "--frozen", "--no-sync", *_TY],
    "venv_ty": [str(_ROOT / ".venv" / "bin" / "ty"), *_TY[1:], "-v"],
}
_SLOW_SECONDS = 2.5


def _ps() -> str:
    return subprocess.run(
        ["ps", "-eo", "pid,ppid,pgid,stat,etimes,pcpu,rss,wchan:24,args", "--sort=-pcpu"],
        capture_output=True,
        text=True,
    ).stdout


def _loadavg() -> str:
    p = Path("/proc/loadavg")
    return p.read_text().strip() if p.exists() else "n/a"


def _pressure() -> str:
    parts = []
    for name in ("cpu", "io", "memory"):
        p = Path("/proc/pressure") / name
        if p.exists():
            parts.append(f"{name}: {p.read_text().strip()}")
    return " | ".join(parts)


def _timed_run(mode: str, cwd: Path) -> dict[str, object]:
    start = time.monotonic()
    proc = subprocess.Popen(
        _MODES[mode], cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    stderr_lines: list[str] = []
    stdout_lines: list[str] = []

    def drain(stream, sink: list[str]) -> None:
        for line in stream:
            sink.append(f"{time.monotonic() - start:7.3f} {line.rstrip()}")

    readers = [
        threading.Thread(target=drain, args=(proc.stderr, stderr_lines)),
        threading.Thread(target=drain, args=(proc.stdout, stdout_lines)),
    ]
    for r in readers:
        r.start()
    snapshots: list[str] = []
    while proc.poll() is None:
        time.sleep(0.25)
        elapsed = time.monotonic() - start
        if elapsed > _SLOW_SECONDS and len(snapshots) < 6 and int(elapsed * 4) % 4 == 0:
            snapshots.append(f"--- t={elapsed:.2f} load={_loadavg()} {_pressure()}\n{_ps()}")
    for r in readers:
        r.join()
    total = time.monotonic() - start
    times = [0.0] + [float(line.split(None, 1)[0]) for line in stderr_lines] + [total]
    gap_at = max(range(1, len(times)), key=lambda i: times[i] - times[i - 1])
    before = stderr_lines[gap_at - 2] if gap_at >= 2 else "<start>"
    after = stderr_lines[gap_at - 1] if gap_at - 1 < len(stderr_lines) else "<exit>"
    return {
        "mode": mode,
        "project": str(cwd.relative_to(_ROOT)),
        "seconds": round(total, 3),
        "returncode": proc.returncode,
        "diagnostic_lines": sum(1 for line in stdout_lines if ": " in line),
        "largest_gap": round(times[gap_at] - times[gap_at - 1], 3),
        "gap_before": before[:300],
        "gap_after": after[:300],
        "load_after": _loadavg(),
        "stderr": stderr_lines if total > _SLOW_SECONDS else stderr_lines[-3:],
        "stdout_tail": stdout_lines[-5:],
        "snapshots": snapshots,
    }


def _emit(capsys: pytest.CaptureFixture[str], name: str, header: dict[str, object], runs: list[dict[str, object]]) -> None:
    _OUT.mkdir(parents=True, exist_ok=True)
    out = _OUT / f"{socket.gethostname()}-{name}.jsonl"
    with out.open("a") as f:
        f.write(json.dumps({"header": header}) + "\n")
        for run in runs:
            f.write(json.dumps(run) + "\n")
    with capsys.disabled():
        print(f"\nMIND684 {name} {json.dumps(header)}")
        for run in runs:
            slim = {k: v for k, v in run.items() if k not in ("stderr", "snapshots")}
            print(f"MIND684 RUN {json.dumps(slim)}")
            if float(run["seconds"]) > _SLOW_SECONDS:  # type: ignore[arg-type]
                print("MIND684 SLOW-STDERR\n" + "\n".join(run["stderr"]))  # type: ignore[arg-type]
                print("MIND684 SLOW-SNAPSHOTS\n" + "\n".join(run["snapshots"]))  # type: ignore[arg-type]


def _header() -> dict[str, object]:
    return {
        "host": socket.gethostname(),
        "uptime": Path("/proc/uptime").read_text().split()[0] if Path("/proc/uptime").exists() else "n/a",
        "nproc": os.cpu_count(),
        "load": _loadavg(),
        "pressure": _pressure(),
        "uv": subprocess.run(["uv", "--version"], capture_output=True, text=True).stdout.strip(),
        "uv_env": {k: v for k, v in os.environ.items() if k.startswith(("UV_", "VIRTUAL_ENV", "RAYON", "TY_"))},
        "venv": os.path.realpath(_ROOT / ".venv"),
    }


@pytest.mark.timeout(500)
def test_mind684_first_uv_run_after_the_installed_mngr_test(capsys: pytest.CaptureFixture[str]) -> None:
    """The sequence both stalls followed: the installed-mngr collector test, then a type ratchet."""
    header = _header()
    target = _ROOT / "system" / "libs" / "pytest_workspace_env"
    runs: list[dict[str, object]] = []
    for mode in ("plain", "plain", "venv_ty", "no_sync", "plain", "frozen_no_sync", "plain"):
        before = time.monotonic()
        mngr_test = subprocess.run(
            [
                str(_ROOT / ".venv" / "bin" / "python"),
                "-m",
                "pytest",
                "-p",
                "no:cacheprovider",
                "-q",
                "system/scripts/test_collect_bug_report_diagnostics_installed_mngr.py",
            ],
            cwd=_ROOT,
            capture_output=True,
            text=True,
        )
        mngr_seconds = time.monotonic() - before
        leftover = _ps()
        run = _timed_run(mode, target)
        run["after_mngr_test_seconds"] = round(mngr_seconds, 3)
        run["after_mngr_test_rc"] = mngr_test.returncode
        run["ps_right_after_mngr_test"] = leftover
        runs.append(run)
        runs.append({**_timed_run(mode, target), "control": "second run, no mngr test in between"})
    _emit(capsys, "after_mngr", header, runs)
    with capsys.disabled():
        print("MIND684 PS-AFTER-FIRST-MNGR-TEST\n" + str(runs[0]["ps_right_after_mngr_test"]))


@pytest.mark.timeout(500)
def test_mind684_each_mode_serially_then_all_projects_at_once(capsys: pytest.CaptureFixture[str]) -> None:
    header = _header()
    runs: list[dict[str, object]] = []
    for mode in ("plain", "no_sync", "venv_ty"):
        for project in _PROJECTS:
            runs.append({**_timed_run(mode, project), "phase": "serial"})
    for mode in ("plain", "frozen", "no_sync", "venv_ty"):
        burst_start = time.monotonic()
        with ThreadPoolExecutor(max_workers=8) as pool:
            burst = list(pool.map(lambda p, m=mode: _timed_run(m, p), _PROJECTS))
        wall = round(time.monotonic() - burst_start, 3)
        runs.extend({**r, "phase": "concurrent", "burst_wall": wall} for r in burst)
    _emit(capsys, "modes", header, runs)
