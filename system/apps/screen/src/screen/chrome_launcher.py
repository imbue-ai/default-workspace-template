"""Spawning one Fortress (Chromium) on the Screen's display and waiting for its debug port.

Deliberately not Playwright's ``launch_persistent_context``, which applies Playwright's own default switches
(``--enable-automation``, ``--disable-extensions``; see ``chrome_args``). Owning the process handle gives the
fleet an unambiguous death signal and lets it notice a Chromium orphaned by a previous service life before
launching a second one onto the same profile.
"""

import os
import shutil
import signal
import subprocess
import threading
import time
from abc import ABC
from abc import abstractmethod
from collections.abc import Callable
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.mutable_model import MutableModel
from screen.chrome_args import launch_args
from screen.errors import ChromiumStartError

# Chromium writes this into the profile as soon as the debug port is bound. A stale one names the previous run's
# port, so it is cleared with the singleton locks before a relaunch.
DEVTOOLS_PORT_FILE: Final[str] = "DevToolsActivePort"
SINGLETON_NAMES: Final[tuple[str, ...]] = ("SingletonLock", "SingletonSocket", "SingletonCookie", DEVTOOLS_PORT_FILE)

# Fortress's launcher geolocates the egress (two bounded lookups) before exec'ing Chromium, so a cold launch can
# take a while before the port appears.
PORT_WAIT_SECONDS: Final[float] = 45.0
_PORT_POLL_SECONDS: Final[float] = 0.1
_ORPHAN_REAP_TIMEOUT_SECONDS: Final[float] = 10.0
_ORPHAN_POLL_SECONDS: Final[float] = 0.2
_KILL_WAIT_SECONDS: Final[float] = 5.0


class ChromiumLaunchRequest(FrozenModel):
    """What one launch needs."""

    executable: Path = Field(description="The Fortress launcher")
    profile_dir: Path = Field(description="The persistent user data directory")
    start_url: str = Field(description="The page Chromium opens on")
    extensions: tuple[str, ...] = Field(default=(), description="Unpacked extensions to load")
    environment: dict[str, str] = Field(description="The child's environment: the Screen's display and audio")
    is_sandbox_disabled: bool = Field(description="Whether to pass --no-sandbox (as root Chromium refuses otherwise)")


class ChromiumHandleInterface(MutableModel, ABC):
    """A launched Chromium: its CDP endpoint, whether it still runs, and how to end it."""

    http_endpoint: str = Field(frozen=True, description="Chromium's debug HTTP origin")
    pid: int = Field(frozen=True, description="The browser process's pid, the root of Chromium's process tree")

    @abstractmethod
    def is_alive(self) -> bool:
        """Whether the browser process still runs."""

    @abstractmethod
    def kill(self) -> None:
        """End the browser process; a no-op once it has exited."""


class ChromiumLauncherInterface(MutableModel, ABC):
    """How the fleet starts a Chromium."""

    @abstractmethod
    def launch(self, request: ChromiumLaunchRequest) -> ChromiumHandleInterface:
        """Spawn Chromium and wait until it publishes its debug port; raises ``ChromiumStartError`` otherwise."""


def clear_stale_singleton(profile_dir: Path) -> None:
    """Remove the lock files a hard kill left so a relaunch is not refused."""
    for name in SINGLETON_NAMES:
        try:
            (profile_dir / name).unlink(missing_ok=True)
        except OSError as e:
            logger.debug("Could not clear {} in {} ({})", name, profile_dir, e)


def _has_exact_arg(pid: int, argument: str) -> bool:
    """Whether ``pid``'s argv carries exactly ``argument`` as one token: ``pgrep -f`` matches a substring, so
    ``browser-1`` would otherwise match a healthy ``browser-10``."""
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False
    return argument in raw.decode(errors="replace").split("\0")


def profile_holder_pid(profile_dir: Path) -> int | None:
    """The pid of a live Chromium already using this profile, if any (an unexpected service restart orphans one,
    and launching a second onto the same profile would make two writers of it)."""
    marker = f"--user-data-dir={profile_dir}"
    try:
        # ``--`` terminates pgrep's option parsing: the pattern itself starts with ``--``.
        output = subprocess.run(
            ["pgrep", "-f", "--", marker], capture_output=True, text=True, timeout=5, check=False
        ).stdout
    except (OSError, subprocess.SubprocessError) as e:
        logger.debug("The profile-holder probe failed ({})", e)
        return None
    for line in output.split():
        if not line.isdigit():
            continue
        pid = int(line)
        if pid != os.getpid() and _has_exact_arg(pid, marker):
            return pid
    return None


def reap_orphan(profile_dir: Path, pause: threading.Event) -> bool:
    """Kill a Chromium orphaned onto this profile; answers whether one was reaped."""
    pid = profile_holder_pid(profile_dir)
    if pid is None:
        return False
    logger.warning("Reaping an orphaned Chromium (pid {}) on {}", pid, profile_dir)
    for signal_number in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.kill(pid, signal_number)
        except ProcessLookupError:
            return True
        except OSError as e:
            logger.debug("Could not signal the orphan {} ({})", pid, e)
            return False
        deadline = time.monotonic() + _ORPHAN_REAP_TIMEOUT_SECONDS / 2
        while time.monotonic() < deadline:
            if profile_holder_pid(profile_dir) is None:
                return True
            pause.wait(_ORPHAN_POLL_SECONDS)
    return profile_holder_pid(profile_dir) is None


def read_devtools_port(profile_dir: Path) -> int | None:
    """The port in ``DevToolsActivePort`` (``<port>\\n<ws path>``), once Chromium has written it."""
    try:
        first_line = (profile_dir / DEVTOOLS_PORT_FILE).read_text().splitlines()[0].strip()
        return int(first_line)
    except (OSError, IndexError, ValueError):
        return None


def kill_and_reap(process: subprocess.Popen[bytes], wait_seconds: float) -> None:
    """SIGKILL a Chromium and wait for it, so its exit is collected rather than left as a zombie in the service's
    process table (nothing polls the handle once the fleet drops it)."""
    try:
        process.kill()
    except OSError as e:
        logger.debug("Killing Chromium (pid {}) was ignored ({})", process.pid, e)
        return
    try:
        process.wait(timeout=wait_seconds)
    except subprocess.TimeoutExpired:
        logger.warning("Chromium (pid {}) did not exit within {:.1f}s of SIGKILL", process.pid, wait_seconds)


class ChromiumProcess(ChromiumHandleInterface):
    """A launched Fortress process."""

    model_config = {"arbitrary_types_allowed": True}

    process: subprocess.Popen[bytes] = Field(frozen=True, description="The browser process")
    profile_dir: Path = Field(frozen=True, description="The profile it runs on")
    kill_wait_seconds: float = Field(
        default=_KILL_WAIT_SECONDS, frozen=True, description="How long a kill waits on SIGTERM, then on SIGKILL"
    )

    def is_alive(self) -> bool:
        return self.process.poll() is None

    def kill(self) -> None:
        if self.process.poll() is not None:
            return
        try:
            self.process.terminate()
        except OSError as e:
            logger.debug("Ending Chromium was ignored ({})", e)
            return
        try:
            self.process.wait(timeout=self.kill_wait_seconds)
            return
        except subprocess.TimeoutExpired:
            logger.debug(
                "Chromium (pid {}) ignored SIGTERM for {:.1f}s; killing it", self.process.pid, self.kill_wait_seconds
            )
        kill_and_reap(self.process, self.kill_wait_seconds)


class SubprocessChromiumLauncher(ChromiumLauncherInterface):
    """Launches Fortress as a subprocess, retrying once with the sandbox off when a sandboxed attempt fails (a
    runtime without unprivileged user namespaces)."""

    model_config = {"arbitrary_types_allowed": True}

    pause: threading.Event = Field(default_factory=threading.Event, description="What the polls wait on")

    def launch(self, request: ChromiumLaunchRequest) -> ChromiumHandleInterface:
        try:
            return self._launch_once(request)
        except ChromiumStartError:
            if request.is_sandbox_disabled:
                raise
            logger.warning("A sandboxed Chromium launch failed; retrying with --no-sandbox")
            return self._launch_once(
                request.model_copy_update(to_update(request.field_ref().is_sandbox_disabled, True))
            )

    def _launch_once(self, request: ChromiumLaunchRequest) -> ChromiumHandleInterface:
        executable = str(request.executable)
        if not shutil.which(executable) and not os.access(executable, os.X_OK):
            raise ChromiumStartError(f"Chromium executable not found or not executable: {executable}")
        request.profile_dir.mkdir(parents=True, exist_ok=True)
        if reap_orphan(request.profile_dir, self.pause):
            logger.info("Reaped an orphaned Chromium before launching on {}", request.profile_dir)
        clear_stale_singleton(request.profile_dir)
        argv = [
            executable,
            *launch_args(
                user_data_dir=str(request.profile_dir),
                remote_debugging_port=0,
                extensions=request.extensions,
                is_sandbox_disabled=request.is_sandbox_disabled,
            ),
            request.start_url,
        ]
        try:
            process = subprocess.Popen(
                argv,
                env=request.environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as e:
            raise ChromiumStartError(f"could not start Chromium: {e}") from e
        deadline = time.monotonic() + PORT_WAIT_SECONDS
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise ChromiumStartError(f"Chromium exited at once (code {process.returncode})")
            port = read_devtools_port(request.profile_dir)
            if port:
                logger.info("Chromium is up on debug port {} (profile {})", port, request.profile_dir.name)
                return ChromiumProcess(
                    http_endpoint=f"http://127.0.0.1:{port}",
                    pid=process.pid,
                    process=process,
                    profile_dir=request.profile_dir,
                )
            self.pause.wait(_PORT_POLL_SECONDS)
        kill_and_reap(process, _KILL_WAIT_SECONDS)
        raise ChromiumStartError(f"Chromium did not publish a debug port within {PORT_WAIT_SECONDS:.0f}s")


def extension_paths(bundled_dir: Path, installed_dir: Path) -> tuple[str, ...]:
    """The unpacked extensions to load: the Screen's own (shipped in this package), then the pinned ones the env.d
    unit installed beside Fortress; each subdirectory holding a manifest.json counts."""
    return tuple(
        str(child)
        for root in (bundled_dir, installed_dir)
        if root.is_dir()
        for child in sorted(root.iterdir())
        if (child / "manifest.json").is_file()
    )


def should_disable_sandbox(is_forced: bool, geteuid: Callable[[], int] = os.geteuid) -> bool:
    """Chromium's sandbox cannot run as root, and every workspace runs the service as root inside an outer boundary
    (gVisor, a VM), so it is off there; a non-root runtime keeps it unless forced off."""
    return is_forced or geteuid() == 0
