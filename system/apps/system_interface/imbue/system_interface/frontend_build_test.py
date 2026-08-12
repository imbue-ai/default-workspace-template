"""Tests for the self-repair rebuild of the frontend bundle.

The build commands are injected, so nothing here runs npm. What is exercised is
the decision-making around them: which commands run at all, and what counts as a
finished rebuild -- the part that decides whether a user staring at the
placeholder gets their interface back or a confident lie.
"""

import threading
from collections.abc import Sequence
from pathlib import Path

import pytest

from imbue.concurrency_group.subprocess_utils import FinishedProcess
from imbue.concurrency_group.subprocess_utils import ProcessSetupError
from imbue.system_interface.frontend_build import FrontendBuildError
from imbue.system_interface.frontend_build import FrontendBuildPhase
from imbue.system_interface.frontend_build import FrontendBuildService

_BUILD_ARGV = ["npm", "run", "build"]
_INSTALL_ARGV = ["npm", "ci"]


def _finished(
    returncode: int = 0, stdout: str = "", stderr: str = "", is_timed_out: bool = False
) -> FinishedProcess:
    """A real command result, so the fakes match what the runner actually returns."""
    return FinishedProcess(
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        command=("npm",),
        is_timed_out=is_timed_out,
        is_output_already_logged=False,
    )


class _RecordingRunner:
    """Records the commands a rebuild runs and optionally writes the bundle.

    ``bundle_to_write`` models a build that actually produces output; leaving it
    None models one that exits 0 having written nothing, which is what a build
    tool killed after emptying its output directory looks like.
    """

    def __init__(
        self,
        *,
        result: FinishedProcess | None = None,
        bundle_to_write: Path | None = None,
        raises: BaseException | None = None,
    ) -> None:
        self.calls: list[list[str]] = []
        self._result = result if result is not None else _finished()
        self._bundle_to_write = bundle_to_write
        self._raises = raises

    def __call__(self, command: Sequence[str], cwd: Path, timeout: float) -> FinishedProcess:
        self.calls.append(list(command))
        if self._raises is not None:
            raise self._raises
        if list(command) == _BUILD_ARGV and self._bundle_to_write is not None and self._result.returncode == 0:
            self._bundle_to_write.mkdir(parents=True, exist_ok=True)
            (self._bundle_to_write / "index.html").write_text("<!doctype html>")
        return self._result


def _build_service(
    tmp_path: Path, runner: _RecordingRunner, *, has_node_modules: bool = False
) -> FrontendBuildService:
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "package.json").write_text("{}")
    if has_node_modules:
        (frontend / "node_modules").mkdir()
    return FrontendBuildService(
        static_directory=tmp_path / "static",
        frontend_directory=frontend,
        command_runner=runner,
    )


def test_successful_rebuild_installs_builds_and_reports_done(tmp_path: Path) -> None:
    runner = _RecordingRunner(bundle_to_write=tmp_path / "static")
    service = _build_service(tmp_path, runner)

    service._run_build_in_background()

    assert runner.calls == [_INSTALL_ARGV, _BUILD_ARGV]
    status = service.current_status()
    assert status.phase == FrontendBuildPhase.DONE
    assert status.is_built and status.error is None


def test_rebuild_skips_the_install_when_dependencies_are_present(tmp_path: Path) -> None:
    # npm ci deletes node_modules before installing, so running it when there is
    # a working install to lose is exactly the destructive step this whole flow
    # exists to avoid.
    runner = _RecordingRunner(bundle_to_write=tmp_path / "static")
    service = _build_service(tmp_path, runner, has_node_modules=True)

    service._run_build_in_background()

    assert runner.calls == [_BUILD_ARGV]
    assert service.current_status().phase == FrontendBuildPhase.DONE


def test_failed_build_reports_the_command_output(tmp_path: Path) -> None:
    runner = _RecordingRunner(result=_finished(returncode=1, stderr="ENOTFOUND registry.npmjs.org"))
    service = _build_service(tmp_path, runner, has_node_modules=True)

    service._run_build_in_background()

    status = service.current_status()
    assert status.phase == FrontendBuildPhase.FAILED
    assert not status.is_built
    # The user (or the agent they forward this to) needs the actual cause, not
    # just "it failed".
    assert status.error is not None and "ENOTFOUND registry.npmjs.org" in status.error


def test_build_that_writes_no_bundle_is_reported_as_failed(tmp_path: Path) -> None:
    # A zero exit with no output is the state that produces a blank page, so
    # reporting it as done would send the user straight into a worse failure.
    runner = _RecordingRunner(bundle_to_write=None)
    service = _build_service(tmp_path, runner, has_node_modules=True)

    service._run_build_in_background()

    status = service.current_status()
    assert status.phase == FrontendBuildPhase.FAILED
    assert status.error is not None and "wrote no bundle" in status.error


def test_a_build_stopped_by_its_timeout_is_reported_as_a_timeout(tmp_path: Path) -> None:
    # The runner stops an overrunning command with a signal and reports it on
    # is_timed_out, not through the exit status -- which can even be 0 if the
    # command finished while it was being stopped. Judging on the exit status
    # alone would call that a successful rebuild, and in the ordinary case would
    # tell the user "exit -15" instead of naming the timeout.
    runner = _RecordingRunner(result=_finished(returncode=0, is_timed_out=True), bundle_to_write=tmp_path / "static")
    service = _build_service(tmp_path, runner, has_node_modules=True)

    service._run_build_in_background()

    status = service.current_status()
    assert status.phase == FrontendBuildPhase.FAILED
    assert status.error is not None and "did not finish within" in status.error


def test_a_command_that_cannot_start_is_reported_rather_than_escaping(tmp_path: Path) -> None:
    # npm missing entirely: the thread must surface it as a FAILED phase, since
    # nothing else is watching this thread.
    runner = _RecordingRunner(raises=ProcessSetupError(command=("npm", "ci"), stdout="", stderr="npm not found"))
    service = _build_service(tmp_path, runner)

    service._run_build_in_background()

    status = service.current_status()
    assert status.phase == FrontendBuildPhase.FAILED
    assert status.error is not None and "could not be started" in status.error


# The escaping exception is the subject of this test, so pytest reporting it as
# an unhandled thread exception is the setup working, not a problem to chase.
@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_a_build_thread_that_dies_is_reported_as_failed(tmp_path: Path) -> None:
    # The rebuild handles the failures it knows about, but nothing else is
    # watching that thread. An exception it does not anticipate would otherwise
    # leave the phase stuck at "building" forever, and the placeholder polling a
    # disabled button -- a dead end on the one surface meant to get the user out
    # of one. RuntimeError specifically, since FrontendBuildError subclasses it
    # and must not be what makes this pass.
    runner = _RecordingRunner(raises=RuntimeError("something nobody anticipated"))
    service = _build_service(tmp_path, runner, has_node_modules=True)

    service.start_background_build()
    thread = service._build_thread
    assert thread is not None
    thread.join(timeout=10.0)
    assert not thread.is_alive()

    status = service.current_status()
    assert status.phase == FrontendBuildPhase.FAILED
    assert not status.is_built
    assert status.error is not None


def test_rebuild_is_refused_without_frontend_sources(tmp_path: Path) -> None:
    service = FrontendBuildService(
        static_directory=tmp_path / "static",
        frontend_directory=tmp_path / "absent",
        command_runner=_RecordingRunner(),
    )

    assert not service.is_repairable
    with pytest.raises(FrontendBuildError):
        service.start_background_build()


def test_a_second_rebuild_is_refused_while_one_is_running(tmp_path: Path) -> None:
    # Two concurrent builds would race on the same output directory, and the
    # placeholder page is reloadable, so a double-click must not start a second.
    release = threading.Event()
    finished = threading.Event()

    def blocking_runner(command: Sequence[str], cwd: Path, timeout: float) -> FinishedProcess:
        release.wait(timeout=10.0)
        finished.set()
        return _finished(returncode=1)

    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "package.json").write_text("{}")
    (frontend / "node_modules").mkdir()
    service = FrontendBuildService(
        static_directory=tmp_path / "static",
        frontend_directory=frontend,
        command_runner=blocking_runner,
    )

    service.start_background_build()
    try:
        with pytest.raises(FrontendBuildError):
            service.start_background_build()
    finally:
        release.set()
    assert finished.wait(timeout=10.0)
