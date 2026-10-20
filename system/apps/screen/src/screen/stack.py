"""The stack's lifecycle (docs/system/specs/screen/spec.md section 9): rendering the configuration, starting the
group's programs in order, waiting for neko, and recording why the group ended."""

import os
import secrets as secrets_module
import shutil
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Final

import yaml
from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr
from pydantic import SecretStr

from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from screen.data_types import Lifecycle
from screen.data_types import NekoSecrets
from screen.data_types import NekoSession
from screen.errors import NekoError
from screen.errors import StackStartError
from screen.errors import SupervisorError
from screen.interfaces import NekoInterface
from screen.interfaces import ProgramState
from screen.interfaces import SupervisorInterface
from screen.neko_client import wait_until_healthy
from screen.neko_config import render_neko_yaml
from screen.neko_config import render_pulse_script
from screen.primitives import DISPLAY_SOCKET_PATH
from screen.primitives import FORTRESS_EXECUTABLE
from screen.primitives import NEKO_PROGRAM
from screen.primitives import PULSE_PROGRAM
from screen.primitives import RUNTIME_DIR
from screen.primitives import SESSION_PROGRAM
from screen.primitives import STACK_PROGRAMS
from screen.primitives import XORG_PROGRAM
from screen.user_dirs import write_user_dirs
from screen.xfce_helpers import write_browser_helper_files
from screen.xfdesktop_config import write_xfdesktop_channel
from screen.xorg_config import INPUT_SOCKET_PATH
from screen.xorg_config import render_xorg_conf

XORG_CONF_FILENAME: Final[str] = "xorg.conf"
NEKO_YAML_FILENAME: Final[str] = "neko.yaml"
PULSE_SCRIPT_FILENAME: Final[str] = "pulse.pa"
# Why the stack last ended, kept for the next service life (a service restart ends the stack, and the viewer
# would otherwise say the Screen was stopped with no reason).
STOP_REASON_FILENAME: Final[str] = "stop_reason"
NEKO_BINARY_PATH: Final[Path] = Path("/opt/neko/bin/neko")
# neko's Xorg input driver, in the server's input module directory, where the env.d unit installs it beside the
# binary (Xorg loads it as the ``neko`` driver of the touchscreen xorg.conf declares).
NEKO_INPUT_MODULE_PATH: Final[Path] = Path("/usr/lib/xorg/modules/input/neko_drv.so")

# The service's own neko member, logged in with the admin password for the event session.
SERVICE_SESSION_NAME: Final[str] = "Screen service"

# Xorg takes well under a second to open the display and bind the input driver's socket; neko a few seconds to bind
# and start its pipelines.
_XORG_SOCKETS_TIMEOUT_SECONDS: Final[float] = 20.0
_NEKO_HEALTH_TIMEOUT_SECONDS: Final[float] = 60.0
_POLL_SECONDS: Final[float] = 0.2
_INSTALL_POLL_SECONDS: Final[float] = 5.0
_SECRET_BYTES: Final[int] = 24


@pure
def generate_secrets() -> NekoSecrets:
    return NekoSecrets(
        user_password=SecretStr(secrets_module.token_urlsafe(_SECRET_BYTES)),
        admin_password=SecretStr(secrets_module.token_urlsafe(_SECRET_BYTES)),
        api_token=SecretStr(secrets_module.token_urlsafe(_SECRET_BYTES)),
    )


@pure
def secrets_from_neko_yaml(text: str) -> NekoSecrets:
    """The secrets a rendered ``neko.yaml`` carries, so a restarted service can speak to a stack that kept running."""
    document = yaml.safe_load(text)
    try:
        multiuser = document["member"]["multiuser"]
        return NekoSecrets(
            user_password=SecretStr(multiuser["user_password"]),
            admin_password=SecretStr(multiuser["admin_password"]),
            api_token=SecretStr(document["session"]["api_token"]),
        )
    except (KeyError, TypeError) as e:
        raise StackStartError("the rendered neko.yaml carries no secrets") from e


@pure
def describe_missing_install() -> str | None:
    """Why the stack cannot start yet, or None once the env.d unit has installed everything the programs exec and
    load."""
    missing = [
        name
        for name, is_present in (
            ("Xorg", shutil.which("Xorg") is not None),
            ("startxfce4", shutil.which("startxfce4") is not None),
            ("pulseaudio", shutil.which("pulseaudio") is not None),
            ("neko", NEKO_BINARY_PATH.is_file() and os.access(NEKO_BINARY_PATH, os.X_OK)),
            ("neko's input driver", NEKO_INPUT_MODULE_PATH.is_file()),
        )
        if not is_present
    ]
    if not missing:
        return None
    return "waiting for env-converge to install " + ", ".join(missing)


@pure
def describe_missing_browser_install() -> str | None:
    """Why Chromium cannot launch yet, or None once the env.d unit has installed Fortress."""
    if os.access(FORTRESS_EXECUTABLE, os.X_OK):
        return None
    return "Chromium is still installing in this workspace; try again in a minute."


class ScreenStack(MutableModel):
    """Starts, probes and stops the group, keeping the lifecycle and the current start's secrets."""

    model_config = {"arbitrary_types_allowed": True}

    supervisor: SupervisorInterface = Field(frozen=True, description="supervisord, for the group's programs")
    neko: NekoInterface = Field(frozen=True, description="neko's REST client; its token follows each start")
    state_dir: Path = Field(frozen=True, description="data/.state/screen")
    home: Path = Field(
        default_factory=Path.home,
        frozen=True,
        description="The session's home, where its browser entries and desktop settings are rendered",
    )
    desktop_dir: Path = Field(
        frozen=True, description="The Screen's desktop and download folder, named in the session's user-dirs file"
    )
    runtime_dir: Path = Field(default=Path(RUNTIME_DIR), frozen=True, description="The members' XDG runtime dir")
    display_socket_path: Path = Field(default=Path(DISPLAY_SOCKET_PATH), frozen=True, description="Xorg's socket")
    input_socket_path: Path = Field(
        default=Path(INPUT_SOCKET_PATH), frozen=True, description="The input driver's socket, bound by Xorg"
    )
    install_check: Callable[[], str | None] = Field(
        default=describe_missing_install, frozen=True, description="Why the stack cannot start yet, or None"
    )
    xorg_sockets_timeout_seconds: float = Field(
        default=_XORG_SOCKETS_TIMEOUT_SECONDS, frozen=True, description="How long a start waits for Xorg's sockets"
    )
    neko_health_timeout_seconds: float = Field(
        default=_NEKO_HEALTH_TIMEOUT_SECONDS, frozen=True, description="How long a start waits for neko's health route"
    )
    install_poll_seconds: float = Field(
        default=_INSTALL_POLL_SECONDS, frozen=True, description="How often an install wait re-checks the unit"
    )
    on_lifecycle_changed: Callable[[], None] = Field(frozen=True, description="Called after every lifecycle change")
    on_running: Callable[[NekoSecrets, NekoSession], None] = Field(
        frozen=True, description="Called once neko answers, with the secrets and the service's admin session"
    )
    set_api_token: Callable[[SecretStr | None], None] = Field(
        frozen=True, description="Hands each start's API token to the neko client"
    )
    on_stopping: Callable[[], None] = Field(
        default=lambda: None,
        frozen=True,
        description="Called before a stop the service asked for ends the members (the browser's tab checkpoint)",
    )

    lifecycle: Lifecycle = Field(default=Lifecycle.STOPPED, description="Where the stack is")
    install_reason: str | None = Field(default=None, description="What is still installing")
    stop_reason: str | None = Field(default=None, description="Why the stack ended")
    secrets: NekoSecrets | None = Field(default=None, description="The current start's secrets")

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _starter: threading.Thread | None = PrivateAttr(default=None)
    # The current start's abort flag and its number: a stop sets the flag, and a later start gets a fresh one, so a
    # stop never outlives the start it interrupted.
    _abort_start: threading.Event = PrivateAttr(default_factory=threading.Event)
    _start_generation: int = PrivateAttr(default=0)

    def _set_lifecycle(self, lifecycle: Lifecycle, install_reason: str | None, stop_reason: str | None) -> None:
        with self._lock:
            self.lifecycle = lifecycle
            self.install_reason = install_reason
            self.stop_reason = stop_reason
        self.on_lifecycle_changed()

    def _set_lifecycle_unless_aborted(
        self, abort: threading.Event, lifecycle: Lifecycle, install_reason: str | None
    ) -> bool:
        """A start's own lifecycle step, skipped (False) once a stop has aborted it, so the STOPPED the stop
        recorded is never overwritten by the start it interrupted."""
        with self._lock:
            if abort.is_set():
                return False
            self.lifecycle = lifecycle
            self.install_reason = install_reason
            self.stop_reason = None
        self.on_lifecycle_changed()
        return True

    def ensure_started(self) -> Lifecycle:
        """Start the group unless it is running or coming up; answers the lifecycle as it stands."""
        with self._lock:
            if self.lifecycle == Lifecycle.RUNNING:
                return self.lifecycle
            # A starter that was told to abort is winding down and touches nothing more; one that was not is a
            # start in flight.
            if self._starter is not None and self._starter.is_alive() and not self._abort_start.is_set():
                return self.lifecycle
            if self.lifecycle != Lifecycle.STOPPED:
                # The starter died of an error it did not expect (its traceback went to stderr); the start is over.
                logger.warning("The Screen's starter ended while the stack was {}; starting afresh", self.lifecycle)
            # Callers see the start at once, before the thread has run.
            self.lifecycle = Lifecycle.STARTING
            self.stop_reason = None
            self._start_generation += 1
            self._abort_start = threading.Event()
            self._starter = threading.Thread(
                target=self._start,
                args=(self._abort_start, self._start_generation),
                name="screen-stack-start",
                daemon=True,
            )
            self._starter.start()
        self.on_lifecycle_changed()
        return Lifecycle.STARTING

    def _start(self, abort: threading.Event, generation: int) -> None:
        # The first boot of an existing workspace installs the stack through env-converge; the viewer shows
        # "Installing the screen" until the unit is satisfied.
        missing = self.install_check()
        is_installing = False
        while missing is not None and self._set_lifecycle_unless_aborted(abort, Lifecycle.INSTALLING, missing):
            is_installing = True
            abort.wait(self.install_poll_seconds)
            missing = self.install_check()
        if is_installing:
            self._set_lifecycle_unless_aborted(abort, Lifecycle.STARTING, None)
        try:
            self._bring_up(abort)
        except (StackStartError, SupervisorError, NekoError, OSError) as e:
            if abort.is_set():
                self._finish_abandoned_start(generation, e)
                return
            logger.warning("The Screen's stack did not come up: {}", e)
            self._stop_members()
            self.set_api_token(None)
            reason = f"the stack did not start: {e}"
            self._remember_stop_reason(reason)
            self._set_lifecycle(Lifecycle.STOPPED, None, reason)

    def _finish_abandoned_start(self, generation: int, cause: Exception) -> None:
        """A start that a stop interrupted: the stop recorded the lifecycle and the reason, so this only cleans up
        a member the start may have brought up after the stop, unless a newer start owns the members by now."""
        logger.debug("Abandoned the Screen's start: {}", cause)
        with self._lock:
            is_latest = generation == self._start_generation
        if is_latest:
            self._stop_members()
            self.set_api_token(None)

    @staticmethod
    def _check_not_abandoned(abort: threading.Event) -> None:
        if abort.is_set():
            raise StackStartError("the start was abandoned by a stop")

    def _bring_up(self, abort: threading.Event) -> None:
        new_secrets = generate_secrets()
        # Under the lock so that a stop and the render serialize: a render never follows the stop that aborted it.
        with self._lock:
            self._check_not_abandoned(abort)
            self._render_configuration(new_secrets)
        self.set_api_token(new_secrets.api_token)
        # A socket a killed Xorg left behind would pass the wait below before the new server has bound its own, and
        # neko dials it once; the driver unlinks a stale one itself, but only after this wait could have seen it.
        self.input_socket_path.unlink(missing_ok=True)
        for program in STACK_PROGRAMS:
            self._check_not_abandoned(abort)
            self.supervisor.start_program(program)
            if program == XORG_PROGRAM:
                self._wait_for_xorg(abort)
        if not wait_until_healthy(self.neko, self.neko_health_timeout_seconds, _POLL_SECONDS, abort):
            self._check_not_abandoned(abort)
            raise StackStartError("neko never answered its health route")
        self._check_not_abandoned(abort)
        service_session = self.neko.login(
            SERVICE_SESSION_NAME, new_secrets.admin_password.get_secret_value(), is_owner=True
        )
        # The hand-over to the service and the step to RUNNING happen under the lock, so a stop either precedes
        # them (and the check raises) or follows them (and finds a running stack to end).
        with self._lock:
            self._check_not_abandoned(abort)
            self.secrets = new_secrets
            self.lifecycle = Lifecycle.RUNNING
            self.install_reason = None
            self.stop_reason = None
            self.on_running(new_secrets, service_session)
        self._forget_stop_reason()
        self.on_lifecycle_changed()

    def _render_configuration(self, new_secrets: NekoSecrets) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.runtime_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        (self.state_dir / XORG_CONF_FILENAME).write_text(render_xorg_conf())
        (self.state_dir / PULSE_SCRIPT_FILENAME).write_text(render_pulse_script())
        neko_yaml = self.state_dir / NEKO_YAML_FILENAME
        neko_yaml.touch(mode=0o600, exist_ok=True)
        neko_yaml.chmod(0o600)
        neko_yaml.write_text(render_neko_yaml(new_secrets))
        write_browser_helper_files(self.home)
        write_xfdesktop_channel(self.home)
        write_user_dirs(self.home, self.desktop_dir)

    def _wait_for_xorg(self, abort: threading.Event) -> None:
        """Until Xorg has opened the display and bound the input driver's socket, which neko dials once at its
        start and exits without."""
        deadline = time.monotonic() + self.xorg_sockets_timeout_seconds
        while time.monotonic() < deadline:
            self._check_not_abandoned(abort)
            if self.display_socket_path.exists() and self.input_socket_path.exists():
                return
            if self.supervisor.program_state(XORG_PROGRAM) in (ProgramState.EXITED, ProgramState.FATAL):
                raise StackStartError(f"{XORG_PROGRAM} exited before opening the display")
            abort.wait(_POLL_SECONDS)
        missing = self.display_socket_path if not self.display_socket_path.exists() else self.input_socket_path
        raise StackStartError(f"{XORG_PROGRAM} did not open {missing}")

    def _stop_members(self) -> None:
        # neko first (its viewers see the stop), then the session, PulseAudio and the X server last.
        for program in (NEKO_PROGRAM, SESSION_PROGRAM, PULSE_PROGRAM, XORG_PROGRAM):
            try:
                self.supervisor.stop_program(program)
            except SupervisorError as e:
                logger.warning("Could not stop {}: {}", program, e)

    def stop(self, reason: str) -> None:
        """End the group (Quit, the service's own shutdown) and record why; a start in flight gives up."""
        with self._lock:
            self._abort_start.set()
            is_running = self.lifecycle == Lifecycle.RUNNING
        if is_running:
            # Chromium can still be asked for its tabs (spec section 9.3).
            self.on_stopping()
        self._stop_members()
        self.set_api_token(None)
        with self._lock:
            self.secrets = None
        self._remember_stop_reason(reason)
        self._set_lifecycle(Lifecycle.STOPPED, None, reason)

    def is_any_member_running(self) -> bool:
        return any(
            self.supervisor.program_state(program) in (ProgramState.RUNNING, ProgramState.STARTING)
            for program in STACK_PROGRAMS
        )

    def mark_stopped(self, reason: str) -> None:
        """The watcher says the group ended on its own; a start in flight gives up."""
        with self._lock:
            self._abort_start.set()
        self.set_api_token(None)
        with self._lock:
            self.secrets = None
        self._remember_stop_reason(reason)
        self._set_lifecycle(Lifecycle.STOPPED, None, reason)

    def _remember_stop_reason(self, reason: str) -> None:
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            (self.state_dir / STOP_REASON_FILENAME).write_text(reason)
        except OSError as e:
            logger.warning("Could not record why the stack stopped under {}: {}", self.state_dir, e)

    def _forget_stop_reason(self) -> None:
        try:
            (self.state_dir / STOP_REASON_FILENAME).unlink(missing_ok=True)
        except OSError as e:
            logger.warning("Could not clear the recorded stop reason under {}: {}", self.state_dir, e)

    def _recall_stop_reason(self) -> str | None:
        """Why the stack ended in a previous service life, when that life recorded it."""
        try:
            return (self.state_dir / STOP_REASON_FILENAME).read_text().strip() or None
        except FileNotFoundError:
            return None
        except OSError as e:
            logger.warning("Could not read the recorded stop reason under {}: {}", self.state_dir, e)
            return None

    def adopt_running_stack(self) -> bool:
        """At service start: when every member is RUNNING and neko answers, take the stack over from the previous
        service life with the secrets its neko.yaml carries. Otherwise leave the lifecycle stopped, with the reason
        the previous life recorded when it has one."""
        neko_yaml = self.state_dir / NEKO_YAML_FILENAME
        states = {program: self.supervisor.program_state(program) for program in STACK_PROGRAMS}
        if any(state != ProgramState.RUNNING for state in states.values()) or not neko_yaml.is_file():
            with self._lock:
                self.stop_reason = self._recall_stop_reason()
            return False
        try:
            found_secrets = secrets_from_neko_yaml(neko_yaml.read_text())
        except (OSError, StackStartError, yaml.YAMLError) as e:
            logger.warning("The running stack's neko.yaml is unreadable ({}); leaving it alone", e)
            return False
        self.set_api_token(found_secrets.api_token)
        if not self.neko.is_healthy():
            self.set_api_token(None)
            return False
        try:
            service_session = self.neko.login(
                SERVICE_SESSION_NAME, found_secrets.admin_password.get_secret_value(), is_owner=True
            )
        except NekoError as e:
            logger.warning("Could not log the service in to the running neko ({}); leaving it alone", e)
            self.set_api_token(None)
            return False
        with self._lock:
            self.secrets = found_secrets
        self.on_running(found_secrets, service_session)
        self._set_lifecycle(Lifecycle.RUNNING, None, None)
        return True
