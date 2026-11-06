"""Trying an install file out: the memory guard, guarded commands, starting the app and checking it the way a window
shows it, and the command line."""

import json
import os
import sys
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import pytest

from app_store.process_memory import tree_memory_mb
from app_store.repo_install_file import RepoInstallFile
from app_store.try_install import Figures
from app_store.try_install import MemoryGuard
from app_store.try_install import Trial
from app_store.try_install import TrialOutcome
from app_store.try_install import default_memory_floor_mb
from app_store.try_install import main
from app_store.try_install import run_guarded
from app_store.try_install import shed_pids_since
from app_store.try_install import tree_pids

_REPOSITORY = "https://github.com/someone/tiny"
_COMMIT = "a" * 40

# A tiny web server standing in for an app: serves on the port it is given, with any extra header asked for.
_SERVER = """
import http.server, sys
port = int(sys.argv[1])
header = next((argument for argument in sys.argv[2:] if ":" in argument), "")
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        if header:
            name, value = header.split(":", 1)
            self.send_header(name, value.strip())
        self.end_headers()
        self.wfile.write(b"<p>tiny</p>")
    def log_message(self, *args):
        pass
http.server.HTTPServer(("127.0.0.1", port), Handler).serve_forever()
"""


def _guard(available_mb: int = 4000, floor_mb: int = 300) -> MemoryGuard:
    return MemoryGuard(floor_mb=floor_mb, read_available_mb=lambda: available_mb, read_tree_mb=tree_memory_mb)


def _install_file(launch: list[str]) -> RepoInstallFile:
    return RepoInstallFile.model_validate(
        {
            "format": 1,
            "title": "Tiny",
            "description": "A tiny server.",
            "repository": _REPOSITORY,
            "commit": _COMMIT,
            "recipe": {"kind": "python_git", "repository": _REPOSITORY, "commit": _COMMIT},
            "launch": {"argv": launch},
            "resources": {"disk_mb": 1},
        }
    )


def _trial(tmp_path: Path, launch: list[str], guard: MemoryGuard | None = None) -> Trial:
    record_dir = tmp_path / "record"
    record_dir.mkdir(parents=True)
    trial = Trial(
        install_file=_install_file(launch),
        repo_root=tmp_path / "workspace",
        record_dir=record_dir,
        scratch_dir=tmp_path / "scratch",
        guard=guard if guard is not None else _guard(),
        start_timeout_seconds=6.0,
        settle_seconds=1.0,
    )
    bin_dir = trial.env_dir / "bin"
    bin_dir.mkdir(parents=True)
    script = bin_dir / "tiny"
    script.write_text(f"#!{sys.executable}\n{_SERVER}")
    script.chmod(0o755)
    trial.data_dir.mkdir(parents=True)
    return trial


def test_a_guarded_command_logs_what_it_ran_and_how_it_ended(tmp_path: Path) -> None:
    log = tmp_path / "trial.log"
    run = run_guarded([sys.executable, "-c", "print('hello')"], None, None, log, 30, _guard())
    assert run.exit_status == 0
    assert not run.is_stopped_for_memory
    assert "hello" in run.output_tail
    assert "hello" in log.read_text()
    assert "[trial] exit status 0" in log.read_text()


def test_the_guard_stops_a_command_when_memory_runs_low(tmp_path: Path) -> None:
    started = time.monotonic()
    run = run_guarded(
        [sys.executable, "-c", "import threading; threading.Event().wait(60)"],
        None,
        None,
        tmp_path / "log",
        60,
        _guard(100, 300),
    )
    assert run.is_stopped_for_memory
    assert run.exit_status is None
    assert run.lowest_memory_left_mb == 100
    assert time.monotonic() - started < 20


def test_a_command_that_runs_too_long_is_stopped(tmp_path: Path) -> None:
    run = run_guarded(
        [sys.executable, "-c", "import threading; threading.Event().wait(60)"],
        None,
        None,
        tmp_path / "log",
        1,
        _guard(),
    )
    assert run.is_timed_out
    assert run.exit_status is None


def test_an_app_that_answers_in_a_frame_works_and_is_measured(tmp_path: Path) -> None:
    trial = _trial(tmp_path, ["{env_dir}/bin/tiny", "{port}", "127.0.0.1"])
    result = trial._start(Figures(disk_mb=1))
    assert result.outcome is TrialOutcome.WORKS
    assert result.what_happened == ""
    assert result.run_memory_mb is not None and result.run_memory_mb > 0
    assert result.disk_mb == 1
    saved = json.loads((trial.record_dir / "result.json").read_text())
    assert saved["outcome"] == "WORKS"
    assert "answered on" in trial.log_path.read_text()


def test_an_app_that_refuses_framing_is_reported(tmp_path: Path) -> None:
    trial = _trial(tmp_path, ["{env_dir}/bin/tiny", "{port}", "X-Frame-Options: DENY", "127.0.0.1"])
    result = trial._start(Figures())
    assert result.outcome is TrialOutcome.REFUSES_FRAMING
    assert "X-Frame-Options: DENY" in result.what_happened


def test_an_app_that_exits_or_never_answers_failed_to_start(tmp_path: Path) -> None:
    exits = _trial(tmp_path / "a", ["{env_dir}/bin/tiny", "not-a-port", "{port}", "127.0.0.1"])
    result = exits._start(Figures())
    assert result.outcome is TrialOutcome.START_FAILED
    assert "stopped right away" in result.what_happened
    assert "ValueError" in result.output_tail


def test_a_trial_with_too_little_memory_does_not_start_and_leaves_nothing(tmp_path: Path) -> None:
    trial = _trial(tmp_path, ["{env_dir}/bin/tiny", "{port}", "127.0.0.1"], guard=_guard(100, 300))
    result = trial.run()
    assert result.outcome is TrialOutcome.OUT_OF_MEMORY
    assert "Only 100 MB" in result.what_happened
    assert not trial.scratch_dir.exists()


def test_an_invalid_file_is_refused_with_the_reason(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "file.json"
    path.write_text(json.dumps({"format": 1, "title": "x"}))
    assert main([str(path)]) == 2
    output = json.loads(capsys.readouterr().out)
    assert output["outcome"] == "INVALID_FILE"
    assert "description" in output["what_happened"]


def _record_as_shed(shed: set[int]) -> Callable[[int], set[int]]:
    """A tree reader that also records each process as stopped by the workspace's guard."""

    def read(root: int) -> set[int]:
        shed.add(root)
        return {root}

    return read


def test_a_command_the_workspace_guard_stopped_counts_as_out_of_memory(tmp_path: Path) -> None:
    shed: set[int] = set()

    def read_shed() -> set[int]:
        return shed

    guard = MemoryGuard(
        floor_mb=300,
        read_available_mb=lambda: 4000,
        read_tree_mb=tree_memory_mb,
        read_tree_pids=_record_as_shed(shed),
        read_shed_pids=read_shed,
    )
    run = run_guarded([sys.executable, "-c", "import sys; sys.exit(1)"], None, None, tmp_path / "log", 30, guard)
    assert run.exit_status == 1
    assert run.is_stopped_for_memory

    ordinary = run_guarded(
        [sys.executable, "-c", "import sys; sys.exit(1)"], None, None, tmp_path / "log", 30, _guard()
    )
    assert not ordinary.is_stopped_for_memory


def test_the_shed_ledger_is_read_from_a_moment_on(tmp_path: Path) -> None:
    ledger = tmp_path / "shed.jsonl"
    ledger.write_text(
        "\n".join(
            [
                json.dumps({"timestamp": "2026-11-05T22:00:00.0Z", "pid": 1}),
                json.dumps({"timestamp": "2026-11-05T22:10:00.0Z", "pid": 2}),
                "not json",
            ]
        )
    )
    since = datetime.fromisoformat("2026-11-05T22:05:00+00:00").timestamp()
    assert shed_pids_since(ledger, since) == {2}
    assert shed_pids_since(tmp_path / "absent.jsonl", since) == set()


def test_the_tree_holds_the_process_itself() -> None:
    assert os.getpid() in tree_pids(os.getpid())
    assert default_memory_floor_mb() >= 300
