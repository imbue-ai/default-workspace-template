"""Try an install file out before it is handed to the store: the one way a chat test-runs an app from GitHub.

``python -m app_store.try_install <install-file.json>`` (run from the workspace root) checks the file by the store's
rules, installs it into a scratch folder with exactly the commands the store would run, writes its settings files and
runs its one-time setup, starts it on a free port, and checks it the way a workspace window will show it: a page
must answer, it must not refuse to be framed (by its headers, and in a framed screenshot), and its disk and memory are
measured. Then it is stopped and the scratch folder deleted, whatever happened.

A memory guard watches every command: when the memory the workspace has left falls below ``--memory-floor-mb``, the
command's whole process group is stopped and the trial ends with ``out_of_memory`` instead of the workspace running
out. The workspace cannot cap a process's memory, so this is watch-and-stop, checked every half second.

What it found is printed as one JSON object (``TrialResult``) and kept, with the full log, the screenshot and the
install file as given, under ``data/.tasks/repo-trials/<trial>/``. It exits 0 only when the app works.
"""

import argparse
import json
import os
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from datetime import datetime
from enum import auto
from pathlib import Path
from typing import Final

import click
import httpx
from pydantic import Field
from pydantic import ValidationError

from app_manifest.manifest import describe_validation_error
from app_store.catalog import GithubBuildRecipe
from app_store.catalog import render_argv
from app_store.catalog import render_setup_content
from app_store.command_finder import framing_refusal
from app_store.command_finder import take_screenshot
from app_store.engine import INIT_TIMEOUT_SECONDS
from app_store.errors import InstallError
from app_store.process_memory import descendants_of
from app_store.process_memory import parse_parent_pid
from app_store.process_memory import tree_memory_mb
from app_store.records import now_iso
from app_store.repo_install_file import RepoInstallFile
from app_store.room import measure_directory_mb
from app_store.system import INSTALL_TIMEOUT_SECONDS
from app_store.system import install_steps
from app_store.system import keep_built_site
from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from oom_priority.meminfo import read_meminfo

BYTES_PER_MB: Final[int] = 1024 * 1024
# The trial stops a command once less than this share of the workspace's memory is left: above the workspace's own
# memory guard (earlyoom, which starts stopping processes at 10%), so the trial stops first and can say why.
MEMORY_FLOOR_SHARE: Final[float] = 0.15
MINIMUM_MEMORY_FLOOR_MB: Final[int] = 300
# Where the workspace's memory guard records each process it stopped (one JSON object per line).
SHED_LEDGER: Final[Path] = Path("data/.state/oom_priority/events/shed.jsonl")
DEFAULT_START_TIMEOUT_SECONDS: Final[float] = 90.0
# How long a started app is watched after it answers, for its memory to settle.
SETTLE_SECONDS: Final[float] = 8.0
SAMPLE_INTERVAL_SECONDS: Final[float] = 0.5
STOP_GRACE_SECONDS: Final[float] = 5.0
OUTPUT_TAIL_CHARACTERS: Final[int] = 4000
TRIALS_RECORD_DIRECTORY: Final[Path] = Path("data/.tasks/repo-trials")
# The install itself can be large (a checkout and its tools), so it goes outside the backed-up home tree.
TRIALS_SCRATCH_DIRECTORY: Final[Path] = Path("/var/tmp/app-store-trials")


class TrialOutcome(UpperCaseStrEnum):
    """How a trial went."""

    WORKS = auto()
    INVALID_FILE = auto()
    INSTALL_FAILED = auto()
    START_FAILED = auto()
    REFUSES_FRAMING = auto()
    OUT_OF_MEMORY = auto()


class Figures(FrozenModel):
    """What a trial measured so far."""

    install_seconds: float | None = Field(default=None, description="How long the install took")
    disk_mb: int | None = Field(default=None, description="Disk the installed app takes")
    install_peak_memory_mb: int | None = Field(default=None, description="The most the install held at once")
    run_memory_mb: int | None = Field(default=None, description="The most the running app held")
    lowest_memory_left_mb: int | None = Field(default=None, description="The least memory the workspace had left")
    screenshot_path: str = Field(default="", description="The screenshot; empty when none")
    output_tail: str = Field(default="", description="The end of the last command's output")

    def with_lowest(self, left: int) -> "Figures":
        lowest = left if self.lowest_memory_left_mb is None else min(self.lowest_memory_left_mb, left)
        return self.model_copy(update={"lowest_memory_left_mb": lowest})


class TrialResult(FrozenModel):
    """What one trial found, for the chat and for the person."""

    outcome: TrialOutcome = Field(description="How it went")
    what_happened: str = Field(description="One plain sentence; empty when it works")
    install_seconds: float | None = Field(description="How long the install took; null when it did not run")
    disk_mb: int | None = Field(description="Disk the installed app takes; null when it did not install")
    install_peak_memory_mb: int | None = Field(description="The most the install's commands held at once, in MB")
    run_memory_mb: int | None = Field(description="The most the running app held while it was watched, in MB")
    lowest_memory_left_mb: int | None = Field(description="The least memory the workspace had left during the trial")
    screenshot_path: str = Field(description="A screenshot of the app inside a frame; empty when none was taken")
    log_path: str = Field(description="The trial's full log: every command, its output, and what was measured")
    record_directory: str = Field(description="Where everything about this trial is kept")
    output_tail: str = Field(description="The end of the last command's own output, for working out a failure")


class GuardedRun(FrozenModel):
    """How one guarded command went."""

    exit_status: int | None = Field(description="Its exit status; null when it was stopped")
    is_stopped_for_memory: bool = Field(description="Whether the memory guard stopped it")
    is_timed_out: bool = Field(description="Whether it ran out of time")
    peak_memory_mb: int = Field(description="The most its process tree held at once")
    lowest_memory_left_mb: int = Field(description="The least memory the workspace had left while it ran")
    output_tail: str = Field(default="", description="The end of its own output")


def available_memory_mb() -> int:
    return read_meminfo()["available"] // BYTES_PER_MB


def default_memory_floor_mb() -> int:
    return max(MINIMUM_MEMORY_FLOOR_MB, int(read_meminfo()["total"] * MEMORY_FLOOR_SHARE) // BYTES_PER_MB)


def tree_pids(root: int, proc: Path = Path("/proc")) -> set[int]:
    """``root`` and every process under it, now."""
    parent_by_pid: dict[int, int] = {}
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            parent_by_pid[int(entry.name)] = parse_parent_pid((entry / "stat").read_text())
        except (OSError, ValueError, IndexError):
            continue
    return descendants_of(root, parent_by_pid)


def shed_pids_since(ledger: Path, since_epoch: float) -> set[int]:
    """The processes the workspace's memory guard recorded stopping at or after ``since_epoch``."""
    pids: set[int] = set()
    try:
        lines = ledger.read_text(encoding="utf-8").splitlines()
    except OSError:
        return pids
    for line in lines:
        try:
            event = json.loads(line)
            stamp = datetime.fromisoformat(str(event["timestamp"]).replace("Z", "+00:00")).timestamp()
            if stamp >= since_epoch:
                pids.add(int(event["pid"]))
        except (ValueError, KeyError, TypeError):
            continue
    return pids


class MemoryGuard(FrozenModel):
    """Stops a process group when the workspace's memory runs low."""

    floor_mb: int = Field(description="Stop when less than this is left, in MB")
    read_available_mb: Callable[[], int] = Field(description="The memory the workspace has left, in MB")
    read_tree_mb: Callable[[int], int] = Field(description="What a process tree holds, in MB")
    read_tree_pids: Callable[[int], set[int]] = Field(
        default=lambda root: {root}, description="The processes in a tree"
    )
    read_shed_pids: Callable[[], set[int]] = Field(
        default=set, description="The processes the workspace's own memory guard has stopped"
    )


def _stop_group(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(STOP_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        process.wait()


def _append(log_path: Path, text: str) -> None:
    with log_path.open("a", encoding="utf-8") as log:
        log.write(text.rstrip("\n") + "\n")


def _tail(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-OUTPUT_TAIL_CHARACTERS:]
    except OSError:
        return ""


def watch(
    process: subprocess.Popen[bytes],
    guard: MemoryGuard,
    deadline: float,
    is_done: Callable[[], bool] = lambda: False,
) -> GuardedRun:
    """Watch ``process`` until it exits, ``is_done`` says so, the time runs out, or memory runs low (then stop it).
    A process of its tree that the workspace's own memory guard stopped counts as running out of memory too."""
    peak = 0
    lowest: int | None = None
    seen: set[int] = {process.pid}
    pause = threading.Event()

    def ended(exit_status: int | None, is_stopped_for_memory: bool, is_timed_out: bool) -> GuardedRun:
        return GuardedRun(
            exit_status=exit_status,
            is_stopped_for_memory=is_stopped_for_memory,
            is_timed_out=is_timed_out,
            peak_memory_mb=peak,
            lowest_memory_left_mb=lowest if lowest is not None else guard.read_available_mb(),
        )

    def check() -> GuardedRun | None:
        nonlocal peak, lowest, seen
        left = guard.read_available_mb()
        lowest = left if lowest is None else min(lowest, left)
        peak = max(peak, guard.read_tree_mb(process.pid))
        seen |= guard.read_tree_pids(process.pid)
        status = process.poll()
        if status is not None:
            is_shed = status != 0 and bool(seen & guard.read_shed_pids())
            return ended(status, is_shed, False)
        if left < guard.floor_mb:
            _stop_group(process)
            return ended(None, True, False)
        if is_done():
            return ended(None, False, False)
        if time.monotonic() >= deadline:
            _stop_group(process)
            return ended(None, False, True)
        return None

    result = check()
    while result is None:
        pause.wait(SAMPLE_INTERVAL_SECONDS)
        result = check()
    return result


def run_guarded(
    argv: list[str],
    cwd: Path | None,
    env: dict[str, str] | None,
    log_path: Path,
    timeout_seconds: float,
    guard: MemoryGuard,
) -> GuardedRun:
    """Run one command to completion under the memory guard, its output appended to the log."""
    _append(log_path, f"$ {shlex.join(argv)}")
    output_path = log_path.with_suffix(".out")
    with output_path.open("w", encoding="utf-8") as output:
        try:
            process = subprocess.Popen(
                argv,
                cwd=cwd,
                env={**os.environ, **env} if env else None,
                stdout=output,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as e:
            _append(log_path, f"[trial] could not run {argv[0]}: {e}")
            return GuardedRun(
                exit_status=-1,
                is_stopped_for_memory=False,
                is_timed_out=False,
                peak_memory_mb=0,
                lowest_memory_left_mb=guard.read_available_mb(),
            )
        try:
            result = watch(process, guard, time.monotonic() + timeout_seconds)
        finally:
            _stop_group(process)
    _append(log_path, output_path.read_text(encoding="utf-8", errors="replace"))
    _append(
        log_path,
        f"[trial] exit status {result.exit_status}; peak {result.peak_memory_mb} MB; "
        f"lowest left {result.lowest_memory_left_mb} MB",
    )
    return result.model_copy(update={"output_tail": _tail(output_path)})


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def is_answering(port: int) -> httpx.Headers | None:
    """The headers of the page on ``port`` (after redirects) when it answers below 500; None otherwise."""
    try:
        response = httpx.get(f"http://127.0.0.1:{port}/", timeout=2.0, follow_redirects=True)
    except httpx.HTTPError:
        return None
    return response.headers if response.status_code < 500 else None


class Trial(FrozenModel):
    """One trial of one install file."""

    install_file: RepoInstallFile = Field(description="What is tried")
    repo_root: Path = Field(description="The workspace root")
    record_dir: Path = Field(description="Where the log, screenshot and result are kept")
    scratch_dir: Path = Field(description="Where it is installed; deleted at the end")
    guard: MemoryGuard = Field(description="The memory guard")
    start_timeout_seconds: float = Field(description="How long it may take to answer once started")
    settle_seconds: float = Field(
        default=SETTLE_SECONDS, description="How long it is watched once it answers, for its memory to settle"
    )

    @property
    def log_path(self) -> Path:
        return self.record_dir / "trial.log"

    @property
    def env_dir(self) -> Path:
        return self.scratch_dir / "env"

    @property
    def data_dir(self) -> Path:
        return self.scratch_dir / "data"

    def run(self) -> TrialResult:
        try:
            return self._run()
        finally:
            shutil.rmtree(self.scratch_dir, ignore_errors=True)
            self.log_path.with_suffix(".out").unlink(missing_ok=True)

    def _result(self, outcome: TrialOutcome, what_happened: str, figures: Figures) -> TrialResult:
        result = TrialResult(
            outcome=outcome,
            what_happened=what_happened,
            install_seconds=figures.install_seconds,
            disk_mb=figures.disk_mb,
            install_peak_memory_mb=figures.install_peak_memory_mb,
            run_memory_mb=figures.run_memory_mb,
            lowest_memory_left_mb=figures.lowest_memory_left_mb,
            screenshot_path=figures.screenshot_path,
            log_path=str(self.log_path),
            record_directory=str(self.record_dir),
            output_tail=figures.output_tail,
        )
        (self.record_dir / "result.json").write_text(result.model_dump_json(indent=2), encoding="utf-8")
        _append(self.log_path, f"[trial] {outcome.value}: {what_happened or 'it works'}")
        return result

    def _run(self) -> TrialResult:
        recipe = self.install_file.recipe
        self.env_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        left = self.guard.read_available_mb()
        figures = Figures().with_lowest(left)
        if left < self.guard.floor_mb:
            return self._result(
                TrialOutcome.OUT_OF_MEMORY,
                f"Only {left} MB of memory is free, below the {self.guard.floor_mb} MB the trial keeps in reserve; "
                "close some apps or chats and try again.",
                figures,
            )
        _append(
            self.log_path, f"[trial] {self.install_file.owner_and_name} at {self.install_file.commit}, {now_iso()}"
        )
        started = time.monotonic()
        install_peak = 0
        for step in install_steps(recipe, self.env_dir):
            run = run_guarded(
                list(step.argv), step.cwd, step.env or None, self.log_path, INSTALL_TIMEOUT_SECONDS, self.guard
            )
            install_peak = max(install_peak, run.peak_memory_mb)
            figures = figures.with_lowest(run.lowest_memory_left_mb).model_copy(
                update={"install_peak_memory_mb": install_peak, "output_tail": run.output_tail}
            )
            if run.is_stopped_for_memory:
                return self._result(
                    TrialOutcome.OUT_OF_MEMORY,
                    f"Installing needed more memory than this workspace has: it was stopped at {install_peak} MB, "
                    f"with {run.lowest_memory_left_mb} MB left.",
                    figures,
                )
            if run.exit_status != 0:
                why = "ran out of time" if run.is_timed_out else f"failed (exit status {run.exit_status})"
                return self._result(
                    TrialOutcome.INSTALL_FAILED,
                    f"The install command `{shlex.join(step.argv[:3])} ...` {why}.",
                    figures,
                )
        if isinstance(recipe, GithubBuildRecipe):
            try:
                keep_built_site(recipe, self.env_dir)
            except InstallError as e:
                return self._result(TrialOutcome.INSTALL_FAILED, str(e), figures)
        figures = figures.model_copy(
            update={
                "install_seconds": round(time.monotonic() - started, 1),
                "disk_mb": measure_directory_mb(self.env_dir),
                "output_tail": "",
            }
        )
        _append(self.log_path, f"[trial] installed in {figures.install_seconds}s; it takes {figures.disk_mb} MB")
        return self._start(figures)

    def _start(self, figures: Figures) -> TrialResult:
        launch = self.install_file.launch
        port = free_port()
        for setup_file in launch.setup_files:
            target = self.data_dir / setup_file.path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(render_setup_content(setup_file.content, port, self.data_dir), encoding="utf-8")
            _append(self.log_path, f"[trial] wrote {setup_file.path}")
        if launch.init_argv:
            init = run_guarded(
                render_argv(launch.init_argv, port, self.data_dir, self.env_dir),
                self.data_dir,
                None,
                self.log_path,
                INIT_TIMEOUT_SECONDS,
                self.guard,
            )
            figures = figures.with_lowest(init.lowest_memory_left_mb)
            if init.exit_status != 0:
                return self._result(
                    TrialOutcome.START_FAILED,
                    "Its one-time setup command failed.",
                    figures.model_copy(update={"output_tail": init.output_tail}),
                )
        argv = render_argv(launch.argv, port, self.data_dir, self.env_dir)
        values = render_argv(tuple(launch.env.values()), port, self.data_dir, self.env_dir)
        env = dict(zip(launch.env.keys(), values, strict=True))
        _append(self.log_path, f"$ {shlex.join(argv)}")
        output_path = self.record_dir / "app-output.log"
        with output_path.open("w", encoding="utf-8") as output:
            try:
                process = subprocess.Popen(
                    argv,
                    cwd=self.data_dir,
                    env={**os.environ, **env},
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    start_new_session=True,
                )
            except OSError as e:
                return self._result(TrialOutcome.START_FAILED, f"It couldn't be started ({e}).", figures)
            try:
                return self._check_running(process, port, output_path, figures)
            finally:
                _stop_group(process)

    def _check_running(
        self, process: subprocess.Popen[bytes], port: int, output_path: Path, figures: Figures
    ) -> TrialResult:
        answered: list[httpx.Headers] = []

        def has_answered() -> bool:
            headers = is_answering(port)
            if headers is not None:
                answered.append(headers)
            return headers is not None

        start = watch(process, self.guard, time.monotonic() + self.start_timeout_seconds, has_answered)
        figures = figures.with_lowest(start.lowest_memory_left_mb).model_copy(
            update={"run_memory_mb": start.peak_memory_mb, "output_tail": _tail(output_path)}
        )
        if start.is_stopped_for_memory:
            return self._result(
                TrialOutcome.OUT_OF_MEMORY,
                f"Once started it needed more memory than this workspace has: it was stopped at "
                f"{start.peak_memory_mb} MB.",
                figures,
            )
        if not answered:
            why = (
                f"It stopped right away (exit status {start.exit_status})."
                if start.exit_status is not None
                else f"It kept running but never showed a page within {self.start_timeout_seconds:.0f} seconds."
            )
            return self._result(TrialOutcome.START_FAILED, why, figures)
        refusal = framing_refusal(answered[0])
        screenshot = take_screenshot(self.repo_root, port)
        if screenshot.image is not None:
            target = self.record_dir / "screenshot.png"
            target.write_bytes(screenshot.image)
            figures = figures.model_copy(update={"screenshot_path": str(target)})
        if refusal is None and screenshot.blocked_reason is not None:
            refusal = screenshot.blocked_reason
        settle = watch(process, self.guard, time.monotonic() + self.settle_seconds)
        run_memory = max(start.peak_memory_mb, settle.peak_memory_mb)
        figures = figures.with_lowest(settle.lowest_memory_left_mb).model_copy(
            update={"run_memory_mb": run_memory, "output_tail": _tail(output_path)}
        )
        _append(self.log_path, f"[trial] answered on {port}; it held up to {run_memory} MB")
        if refusal is not None:
            return self._result(
                TrialOutcome.REFUSES_FRAMING,
                f"It starts, but it refuses to be shown inside a window ({refusal}), so its window would be blank; "
                "find its option that allows any site to frame it.",
                figures,
            )
        if settle.is_stopped_for_memory:
            return self._result(
                TrialOutcome.OUT_OF_MEMORY,
                f"It started, then needed more memory than this workspace has (stopped at {run_memory} MB).",
                figures,
            )
        if settle.exit_status is not None:
            return self._result(
                TrialOutcome.START_FAILED,
                f"It answered once, then stopped (exit status {settle.exit_status}).",
                figures,
            )
        return self._result(TrialOutcome.WORKS, "", figures)


def trial_name(install_file: RepoInstallFile) -> str:
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    return f"{install_file.owner_and_name.replace('/', '-')}-{install_file.short_commit}-{stamp}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("install_file", type=Path, help="The install file, as JSON")
    parser.add_argument(
        "--memory-floor-mb", type=int, default=None, help="Stop when less is left (default: 15%% of the memory)"
    )
    parser.add_argument("--start-timeout", type=float, default=DEFAULT_START_TIMEOUT_SECONDS)
    arguments = parser.parse_args(argv)
    try:
        install_file = RepoInstallFile.model_validate(json.loads(arguments.install_file.read_text(encoding="utf-8")))
    except (OSError, ValueError) as e:
        reason = describe_validation_error(e) if isinstance(e, ValidationError) else str(e)
        click.echo(json.dumps({"outcome": TrialOutcome.INVALID_FILE.value, "what_happened": reason}, indent=2))
        return 2
    repo_root = Path.cwd()
    started_epoch = time.time()
    name = trial_name(install_file)
    record_dir = repo_root / TRIALS_RECORD_DIRECTORY / name
    record_dir.mkdir(parents=True, exist_ok=True)
    (record_dir / "install-file.json").write_text(install_file.model_dump_json(indent=2), encoding="utf-8")
    trial = Trial(
        install_file=install_file,
        repo_root=repo_root,
        record_dir=record_dir,
        scratch_dir=TRIALS_SCRATCH_DIRECTORY / name,
        guard=MemoryGuard(
            floor_mb=arguments.memory_floor_mb if arguments.memory_floor_mb is not None else default_memory_floor_mb(),
            read_available_mb=available_memory_mb,
            read_tree_mb=tree_memory_mb,
            read_tree_pids=tree_pids,
            read_shed_pids=lambda: shed_pids_since(repo_root / SHED_LEDGER, started_epoch),
        ),
        start_timeout_seconds=arguments.start_timeout,
    )
    result = trial.run()
    click.echo(result.model_dump_json(indent=2))
    return 0 if result.outcome is TrialOutcome.WORKS else 1


if __name__ == "__main__":
    sys.exit(main())
