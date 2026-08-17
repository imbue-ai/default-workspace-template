import threading
import time
from collections.abc import Callable
from enum import Enum
from pathlib import Path
from typing import Final

from loguru import logger as _loguru_logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.concurrency_group.subprocess_utils import FinishedProcess
from imbue.concurrency_group.subprocess_utils import ProcessSetupError
from imbue.concurrency_group.subprocess_utils import run_local_command_modern_version
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel

logger = _loguru_logger

# The frontend sources the served bundle is compiled from. The app is installed
# as an editable uv tool, so this resolves back into the workspace checkout; a
# non-editable install ships no frontend/ and therefore cannot self-repair.
FRONTEND_DIRECTORY: Final[Path] = Path(__file__).parents[2] / "frontend"

# Generous ceilings: a cold npm cache on a loaded host is slow but recoverable,
# and cutting a working install off mid-flight is the failure we are trying to
# avoid. The lower thresholds only warn, so degradation is visible before it
# turns into a timeout.
_NPM_INSTALL_TIMEOUT_SECONDS: Final[float] = 900.0
_NPM_INSTALL_SLOW_SECONDS: Final[float] = 180.0
_NPM_BUILD_TIMEOUT_SECONDS: Final[float] = 600.0
_NPM_BUILD_SLOW_SECONDS: Final[float] = 120.0

# How much of a failed command's output travels back to the browser. Enough to
# name the cause, bounded so a runaway build log cannot bloat the response.
_ERROR_OUTPUT_CHARACTER_LIMIT: Final[int] = 2000

# The injection seam for the build commands, mirroring ``claude_auth``: tests
# pass a deterministic fake, production uses the module default. Spelled out
# rather than ``Callable[..., Any]`` so both the arguments and the
# ``FinishedProcess`` result are checked -- a fake that returns some other shape
# is then a type error here rather than an AttributeError inside a build thread.
# Named for the build so it does not read as a second definition of
# ``claude_auth.CommandRunner``, which is a different (looser) alias.
BuildCommandRunner = Callable[[list[str], Path, float], FinishedProcess]


def _default_command_runner(command: list[str], cwd: Path, timeout: float) -> FinishedProcess:
    return run_local_command_modern_version(command=command, is_checked=False, timeout=timeout, cwd=cwd)


class FrontendBuildError(RuntimeError):
    """Raised when the frontend bundle cannot be rebuilt."""


class FrontendBuildPhase(str, Enum):
    """Lifecycle of a self-repair rebuild of the frontend bundle."""

    INSTALLING = "installing"
    BUILDING = "building"
    DONE = "done"
    FAILED = "failed"


class FrontendBuildStatus(FrozenModel):
    """Snapshot of the served bundle's state and of any rebuild of it."""

    is_built: bool = Field(description="Whether the served bundle's index.html is present")
    is_repairable: bool = Field(description="Whether the frontend sources are present, so a rebuild can run at all")
    phase: FrontendBuildPhase | None = Field(
        default=None, description="Phase of the in-flight or most recent rebuild; absent if none has run"
    )
    detail: str | None = Field(default=None, description="Human-readable detail for the current phase")
    error: str | None = Field(default=None, description="Command output explaining a FAILED phase")


def _describe_command_failure(command: list[str], result: FinishedProcess) -> str:
    """Render a failed command's exit status and tail of output for the user."""
    output = result.stderr.strip() or result.stdout.strip()
    truncated = output[-_ERROR_OUTPUT_CHARACTER_LIMIT:] if output else "(no output)"
    return f"`{' '.join(command)}` failed (exit {result.returncode}):\n{truncated}"


class FrontendBuildService(MutableModel):
    """Owns the workspace's ability to rebuild its own frontend bundle.

    One instance per app, held on the ``SystemInterfaceState`` so that a rebuild
    started by one request stays observable to the status polls that follow it.
    """

    model_config = {"arbitrary_types_allowed": True, "extra": "forbid", "frozen": False}

    static_directory: Path = Field(frozen=True, description="Directory the backend serves the built bundle from")
    frontend_directory: Path = Field(
        default=FRONTEND_DIRECTORY, frozen=True, description="Directory the bundle is compiled from"
    )
    command_runner: BuildCommandRunner = _default_command_runner

    _state_lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _build_thread: threading.Thread | None = PrivateAttr(default=None)
    _phase: FrontendBuildPhase | None = PrivateAttr(default=None)
    _detail: str | None = PrivateAttr(default=None)
    _error: str | None = PrivateAttr(default=None)

    @property
    def is_built(self) -> bool:
        return (self.static_directory / "index.html").exists()

    @property
    def is_repairable(self) -> bool:
        """Whether the sources needed to run a rebuild are present."""
        return (self.frontend_directory / "package.json").exists()

    def current_status(self) -> FrontendBuildStatus:
        with self._state_lock:
            phase, detail, error = self._phase, self._detail, self._error
            # A running phase with no live thread means the build thread died
            # without recording an outcome -- ``_run_build_in_background``
            # handles the failures it knows about, but nothing else is watching
            # that thread. Reported as failed rather than left as-is: this is a
            # recovery surface, and a phase that can never advance would leave
            # the page polling a disabled button forever, which is the dead end
            # the whole page exists to prevent.
            if phase in (FrontendBuildPhase.INSTALLING, FrontendBuildPhase.BUILDING) and not (
                self._build_thread is not None and self._build_thread.is_alive()
            ):
                phase = FrontendBuildPhase.FAILED
                detail = None
                error = error or "The rebuild stopped unexpectedly before it finished."
            return FrontendBuildStatus(
                is_built=self.is_built,
                is_repairable=self.is_repairable,
                phase=phase,
                detail=detail,
                error=error,
            )

    def start_background_build(self) -> None:
        """Rebuild the bundle on a background thread.

        The endpoint calls this and returns immediately; the placeholder page
        follows the rebuild through ``current_status``. Raises
        ``FrontendBuildError`` when the sources are absent or a rebuild from a
        previous request is still running.
        """
        if not self.is_repairable:
            raise FrontendBuildError(
                f"No frontend sources at {self.frontend_directory}, so the interface cannot rebuild itself here."
            )
        with self._state_lock:
            if self._build_thread is not None and self._build_thread.is_alive():
                raise FrontendBuildError("A rebuild is already running; wait for it to finish.")
            self._phase = FrontendBuildPhase.INSTALLING
            self._detail = "Preparing to rebuild the interface"
            self._error = None
            thread = threading.Thread(target=self._run_build_in_background, name="frontend-build", daemon=True)
            self._build_thread = thread
            thread.start()

    def _set_progress(self, phase: FrontendBuildPhase, detail: str | None, error: str | None) -> None:
        with self._state_lock:
            self._phase = phase
            self._detail = detail
            self._error = error

    def _run_command(self, command: list[str], timeout: float, slow_threshold: float) -> None:
        """Run one build command, raising ``FrontendBuildError`` if it does not succeed."""
        started_at = time.monotonic()
        try:
            result = self.command_runner(command, self.frontend_directory, timeout)
        except ProcessSetupError as e:
            raise FrontendBuildError(f"`{' '.join(command)}` could not be started: {e}") from e
        elapsed = time.monotonic() - started_at
        if elapsed > slow_threshold:
            logger.warning("Rebuilt the interface slowly: {} took {:.0f}s", " ".join(command), elapsed)
        # Checked before the exit status, which does not carry this: the runner
        # stops an overrunning command with a signal, so a timeout otherwise
        # reads to the user as "exit -15" -- and if the command happened to
        # finish while it was being stopped, as a success.
        if result.is_timed_out:
            raise FrontendBuildError(f"`{' '.join(command)}` did not finish within {timeout:.0f}s and was stopped.")
        if result.returncode != 0:
            raise FrontendBuildError(_describe_command_failure(command, result))

    def _run_build_in_background(self) -> None:
        # Thread entry point, so this is the top-level handler for the rebuild:
        # anything escaping must surface as the FAILED phase on the placeholder
        # page rather than dying silently in a daemon thread.
        try:
            # npm ci deletes node_modules before installing, so it only runs
            # when there is nothing to lose. With deps already present, going
            # straight to the build keeps a working install working even if the
            # build itself fails.
            if not (self.frontend_directory / "node_modules").is_dir():
                self._set_progress(FrontendBuildPhase.INSTALLING, "Installing the interface's dependencies", None)
                self._run_command(["npm", "ci"], _NPM_INSTALL_TIMEOUT_SECONDS, _NPM_INSTALL_SLOW_SECONDS)
            self._set_progress(FrontendBuildPhase.BUILDING, "Rebuilding the interface", None)
            self._run_command(["npm", "run", "build"], _NPM_BUILD_TIMEOUT_SECONDS, _NPM_BUILD_SLOW_SECONDS)
            # The build tool empties the output directory before writing, so a
            # zero exit that produced nothing is still a failed rebuild -- and
            # reporting it as success would send the user to a blank page.
            if not self.is_built:
                raise FrontendBuildError(f"The build reported success but wrote no bundle to {self.static_directory}.")
            self._set_progress(FrontendBuildPhase.DONE, None, None)
        except FrontendBuildError as e:
            logger.error("Failed to rebuild the interface: {}", e)
            self._set_progress(FrontendBuildPhase.FAILED, None, str(e))
