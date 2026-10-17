"""The Screen service's state and verbs (docs/system/specs/screen/spec.md section 10), composed from the stack, the
lease, neko and the display tools; the routes are a thin layer over it."""

import io
import json
import threading
import time
from collections.abc import Sequence
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import Final

from PIL import Image
from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr
from pydantic import SecretStr

from imbue.imbue_common.event_envelope import parse_iso_timestamp
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from screen.broadcast import ViewerBroadcaster
from screen.control import ScreenControl
from screen.data_types import AcquireStatus
from screen.data_types import ChromiumView
from screen.data_types import ControlView
from screen.data_types import Controller
from screen.data_types import Lifecycle
from screen.data_types import NekoSecrets
from screen.data_types import NekoSession
from screen.data_types import RequestIdentity
from screen.data_types import ScreenSize
from screen.data_types import ScreenState
from screen.data_types import XWindow
from screen.errors import NekoError
from screen.errors import ScreenNotRunningError
from screen.errors import WindowToolError
from screen.identity import display_name_for_identity
from screen.interfaces import AgentMessengerInterface
from screen.interfaces import NekoInterface
from screen.interfaces import ProcessLauncherInterface
from screen.interfaces import WindowToolsInterface
from screen.neko_client import NekoEventSubscriber
from screen.primitives import AgentId
from screen.primitives import DEFAULT_TITLE
from screen.primitives import NekoSessionId
from screen.primitives import VIDEO_ID_WATCH
from screen.primitives import XWindowId
from screen.stack import ScreenStack

# How long a disconnected human session is kept before the reaper deletes it (spec section 6.3).
SESSION_REAP_AFTER: Final[timedelta] = timedelta(minutes=10)
# The sweeper's cadence for the lease clocks and the title poll; the reaper's.
_TICK_SECONDS: Final[float] = 1.0
_REAP_INTERVAL_SECONDS: Final[float] = 60.0
_POLL_SECONDS: Final[float] = 0.2

DEFAULT_SCREENSHOT_QUALITY: Final[int] = 90

# neko's event names the service acts on (spec section 2.3).
EVENT_SYSTEM_INIT: Final[str] = "system/init"
EVENT_CONTROL_HOST: Final[str] = "control/host"
EVENT_SESSION_DELETED: Final[str] = "session/deleted"
EVENT_SCREEN_UPDATED: Final[str] = "screen/updated"


@pure
def control_document(view: ControlView) -> dict[str, Any]:
    return {
        "controller": view.controller.value.lower(),
        "human_pinned": view.human_pinned,
        "human_session_id": view.human_session_id,
        "human_name": view.human_name,
        "agent_id": view.agent_id,
        "agent_name": view.agent_name,
        "waiting": list(view.waiting),
        "idle_seconds": view.idle_seconds,
        "idle_release_seconds": view.idle_release_seconds,
    }


@pure
def state_document(state: ScreenState) -> dict[str, Any]:
    """The ``GET /api/state`` body: the state with its enums spelled lower-case on the wire."""
    return {
        "lifecycle": state.lifecycle.value.lower(),
        "install_reason": state.install_reason,
        "stop_reason": state.stop_reason,
        "title": state.title,
        "video_id": state.video_id,
        "control": control_document(state.control),
        "screen_size": state.screen_size.model_dump() if state.screen_size is not None else None,
        "chromium": state.chromium.model_dump(),
    }


@pure
def is_session_reapable(not_connected_since: str | None, is_connected: bool, now: datetime) -> bool:
    """Whether a session has been disconnected long enough to be deleted."""
    if is_connected or not_connected_since is None:
        return False
    since = parse_iso_timestamp(not_connected_since)
    if since is None:
        return False
    return now - since >= SESSION_REAP_AFTER


@pure
def jpeg_to_png(jpeg_bytes: bytes) -> bytes:
    with Image.open(io.BytesIO(jpeg_bytes)) as image:
        output = io.BytesIO()
        image.save(output, format="PNG")
        return output.getvalue()


class ScreenService(MutableModel):
    """Everything the routes act on: the stack, the lease, the sessions, and what the viewers are told."""

    model_config = {"arbitrary_types_allowed": True}

    repo_root: Path = Field(frozen=True, description="The workspace root every program runs from")
    state_dir: Path = Field(frozen=True, description="data/.state/screen")
    neko: NekoInterface = Field(frozen=True, description="neko's REST client")
    window_tools: WindowToolsInterface = Field(frozen=True, description="wmctrl and xdotool on the display")
    launcher: ProcessLauncherInterface = Field(frozen=True, description="How ``screen run`` starts a command")
    messenger: AgentMessengerInterface = Field(frozen=True, description="Wakes queued agents")
    broadcaster: ViewerBroadcaster = Field(default_factory=ViewerBroadcaster, description="The viewers' event fan-out")
    is_event_subscription_enabled: bool = Field(
        default=True, frozen=True, description="Whether to follow neko's WebSocket (off under a fake neko)"
    )

    title: str = Field(default=DEFAULT_TITLE, description="The frontmost window's title")
    video_id: str = Field(default=VIDEO_ID_WATCH, description="The profile every viewer selects")
    screen_size: ScreenSize | None = Field(default=None, description="The display's mode, while running")

    _stack: ScreenStack | None = PrivateAttr(default=None)
    _control: ScreenControl | None = PrivateAttr(default=None)
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _sessions: dict[NekoSessionId, NekoSession] = PrivateAttr(default_factory=dict)
    _service_session: NekoSession | None = PrivateAttr(default=None)
    _subscriber: NekoEventSubscriber | None = PrivateAttr(default=None)
    _secrets: NekoSecrets | None = PrivateAttr(default=None)
    _last_host_id: str | None = PrivateAttr(default=None)
    _projected_host_id: str | None = PrivateAttr(default=None)
    _projection_lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _pending_projection: ControlView | None = PrivateAttr(default=None)
    _projection_wake: threading.Event = PrivateAttr(default_factory=threading.Event)
    _stop: threading.Event = PrivateAttr(default_factory=threading.Event)
    _threads: list[threading.Thread] = PrivateAttr(default_factory=list)

    # wiring

    def model_post_init(self, _context: Any) -> None:
        self._control = ScreenControl(
            messenger=self.messenger,
            on_changed=self._on_control_changed,
            on_handoff_request=self._on_handoff_request,
            record_path=self.state_dir / "control.json",
        )

    def attach_stack(self, stack: ScreenStack) -> None:
        self._stack = stack

    @property
    def stack(self) -> ScreenStack:
        assert self._stack is not None, "the stack is attached before the service runs"
        return self._stack

    @property
    def control(self) -> ScreenControl:
        assert self._control is not None, "the lease is built with the service"
        return self._control

    def start(self) -> None:
        """Restore the lease, take over a stack a previous service life left running, and start the clocks."""
        self.control.restore()
        if not self.stack.adopt_running_stack():
            self.control.abandon("the Screen service restarted while the stack was down")
        self._threads = [
            threading.Thread(target=self._tick_loop, name="screen-tick", daemon=True),
            threading.Thread(target=self._reap_loop, name="screen-reap", daemon=True),
            threading.Thread(target=self._projection_loop, name="screen-projection", daemon=True),
        ]
        for thread in self._threads:
            thread.start()

    def shutdown(self, reason: str) -> None:
        """Stop the group (the service's own shutdown ends it, spec section 9.3) and the service's threads."""
        self._stop.set()
        self._projection_wake.set()
        if self._subscriber is not None:
            self._subscriber.stop()
        if self.stack.lifecycle != Lifecycle.STOPPED:
            self.stack.stop(reason)
        self.broadcaster.shutdown()

    # the state

    def state(self) -> ScreenState:
        stack = self.stack
        return ScreenState(
            lifecycle=stack.lifecycle,
            install_reason=stack.install_reason,
            stop_reason=stack.stop_reason,
            title=self.title,
            video_id=self.video_id,
            control=self.control.view(),
            screen_size=self.screen_size if stack.lifecycle == Lifecycle.RUNNING else None,
            chromium=ChromiumView(state="absent"),
        )

    def state_document(self) -> dict[str, Any]:
        return state_document(self.state())

    def _broadcast_state(self) -> None:
        self.broadcaster.broadcast({"type": "state", **self.state_document()})

    def broadcaster_state_text(self) -> str:
        """The ``state`` event a viewer is sent on connect."""
        return json.dumps({"type": "state", **self.state_document()}, default=str)

    # lifecycle hooks (the stack calls these)

    def on_lifecycle_changed(self) -> None:
        if self.stack.lifecycle == Lifecycle.STOPPED:
            self._end_of_stack()
        self._broadcast_state()

    def _end_of_stack(self) -> None:
        if self._subscriber is not None:
            self._subscriber.stop()
            self._subscriber = None
        with self._lock:
            self._sessions.clear()
            self._service_session = None
            self._secrets = None
            self._last_host_id = None
            self._projected_host_id = None
            self.screen_size = None
            self.title = DEFAULT_TITLE
        reason = self.stack.stop_reason or "the stack stopped"
        if self.control.view().controller != Controller.NOBODY or self.control.view().waiting:
            self.control.abandon(reason)

    def on_running(self, secrets: NekoSecrets, service_session: NekoSession) -> None:
        with self._lock:
            self._secrets = secrets
            self._service_session = service_session
        if self.is_event_subscription_enabled:
            subscriber = NekoEventSubscriber(
                token=SecretStr(str(service_session.token)), handler=self.handle_neko_event
            )
            self._subscriber = subscriber
            subscriber.start()
        self._request_projection(self.control.view())

    def set_api_token(self, token: SecretStr | None) -> None:
        self.neko.set_api_token(token)

    def ensure_started(self) -> Lifecycle:
        return self.stack.ensure_started()

    def notify_stopped(self, member: str, reason: str) -> bool:
        """The watcher says a member left and the rest of the group was stopped; believed only when supervisord
        agrees that the group is down, so a stray notification cannot declare a running stack stopped."""
        if self.stack.is_any_member_running():
            logger.warning("Ignored a stop notification for {} while the group still runs", member)
            return False
        if self.stack.lifecycle == Lifecycle.STOPPED:
            # The first member to go names the reason; the others leaving afterwards is the group ending as designed.
            return True
        logger.warning("The Screen stopped: {} ({})", reason, member)
        self.stack.mark_stopped(reason)
        self.broadcaster.broadcast({"type": "stopped", "reason": reason, "member": member})
        return True

    # sessions (spec section 6.3)

    def create_session(self, identity: RequestIdentity) -> NekoSession:
        """Log the requester in to neko with the user password and a name from the identity."""
        with self._lock:
            secrets = self._secrets
        if self.stack.lifecycle != Lifecycle.RUNNING or secrets is None:
            raise ScreenNotRunningError("the Screen is not running yet")
        name = display_name_for_identity(identity)
        session = self.neko.login(str(name), secrets.user_password.get_secret_value(), identity.owner)
        with self._lock:
            self._sessions[session.session_id] = session
        return session

    def known_session(self, session_id: NekoSessionId) -> NekoSession | None:
        with self._lock:
            return self._sessions.get(session_id)

    def _session_name(self, session_id: str) -> str:
        with self._lock:
            session = self._sessions.get(NekoSessionId(session_id)) if session_id else None
        return str(session.name) if session is not None else "Someone"

    def _reap_loop(self) -> None:
        while not self._stop.wait(_REAP_INTERVAL_SECONDS):
            if self.stack.lifecycle == Lifecycle.RUNNING:
                self.reap_disconnected_sessions(datetime.now(timezone.utc))

    def reap_disconnected_sessions(self, now: datetime) -> int:
        """Delete every human session disconnected for ten minutes; returns how many went."""
        try:
            records = self.neko.list_sessions()
        except NekoError as e:
            logger.debug("Could not list neko's sessions for reaping: {}", e)
            return 0
        reaped = 0
        for record in records:
            if record.is_admin or not is_session_reapable(record.not_connected_since, record.is_connected, now):
                continue
            try:
                self.neko.delete_session(record.session_id)
            except NekoError as e:
                logger.debug("Could not delete the stale neko session {}: {}", record.session_id, e)
                continue
            with self._lock:
                self._sessions.pop(record.session_id, None)
            self.control.human_session_gone(record.session_id)
            reaped += 1
        return reaped

    # the lease and its projection (spec sections 6.1 and 6.2)

    def _on_control_changed(self, view: ControlView) -> None:
        self._request_projection(view)
        self._broadcast_state()

    def _on_handoff_request(self, agent_name: str, reason: str) -> None:
        self.broadcaster.broadcast({"type": "handoff_request", "agent_name": agent_name, "reason": reason})

    def _request_projection(self, view: ControlView) -> None:
        with self._projection_lock:
            self._pending_projection = view
        self._projection_wake.set()

    def _projection_loop(self) -> None:
        while not self._stop.is_set():
            self._projection_wake.wait()
            self._projection_wake.clear()
            view = self._take_pending_projection()
            while view is not None and not self._stop.is_set():
                self.project_control(view)
                view = self._take_pending_projection()

    def _take_pending_projection(self) -> ControlView | None:
        with self._projection_lock:
            view, self._pending_projection = self._pending_projection, None
        return view

    def project_control(self, view: ControlView) -> None:
        """Put neko in step with the lease: locked while an agent holds it, the human made host, else no host."""
        if self.stack.lifecycle != Lifecycle.RUNNING:
            return
        try:
            match view.controller:
                case Controller.AGENT:
                    # Locking clears a non-admin host on neko's side.
                    self.neko.set_locked_controls(True)
                    self._projected_host_id = None
                case Controller.HUMAN:
                    self.neko.set_locked_controls(False)
                    if view.human_session_id is not None and self._projected_host_id != str(view.human_session_id):
                        self.neko.give_control(view.human_session_id)
                        self._projected_host_id = str(view.human_session_id)
                case Controller.NOBODY:
                    self.neko.set_locked_controls(False)
                    if self._projected_host_id is not None or self._last_host_id is not None:
                        self.neko.reset_control()
                        self._projected_host_id = None
        except NekoError as e:
            logger.warning("Could not project the lease onto neko ({}); the next change retries", e)

    def take_control(self, session_id: NekoSessionId) -> bool:
        session = self.known_session(session_id)
        if session is None:
            return False
        return self.control.take_control(session_id, str(session.name))

    def return_control(self, session_id: NekoSessionId) -> bool:
        return self.control.return_to_agents(session_id)

    def acquire(self, agent_id: AgentId, agent_name: str | None, is_reclaim: bool) -> dict[str, Any]:
        """An agent's ``acquire``: starts the stack when it is down, and answers ``starting`` until it runs."""
        lifecycle = self.stack.lifecycle
        if lifecycle != Lifecycle.RUNNING:
            self.ensure_started()
            return {"ok": False, "status": "starting", "enqueued": False, **control_document(self.control.view())}
        status = self.control.acquire(
            agent_id, agent_name, is_reclaim=is_reclaim, is_wait=False, max_wait_seconds=None, is_enqueue_on_busy=True
        )
        return {
            "ok": status == AcquireStatus.ACQUIRED,
            "status": status.value.lower(),
            "enqueued": status in (AcquireStatus.BUSY_HUMAN, AcquireStatus.BUSY_AGENT),
            **control_document(self.control.view()),
        }

    def release(self, agent_id: AgentId) -> bool:
        return self.control.release(agent_id)

    def handoff(self, agent_id: AgentId, agent_name: str | None, reason: str) -> dict[str, Any]:
        handed = self.control.handoff(agent_id, agent_name, reason)
        return {
            "ok": handed,
            "status": "handed_off" if handed else "not_owner",
            **control_document(self.control.view()),
        }

    def touch(self, agent_id: AgentId) -> None:
        self.control.touch(agent_id)

    # what neko tells the service

    def handle_neko_event(self, event: str, payload: dict[str, Any]) -> None:
        if event == EVENT_SYSTEM_INIT:
            self._handle_system_init(payload)
        elif event == EVENT_CONTROL_HOST:
            self._handle_control_host(payload)
        elif event == EVENT_SESSION_DELETED:
            session_id = payload.get("id")
            if isinstance(session_id, str) and session_id:
                with self._lock:
                    self._sessions.pop(NekoSessionId(session_id), None)
                self.control.human_session_gone(NekoSessionId(session_id))
        elif event == EVENT_SCREEN_UPDATED:
            self._handle_screen_size(payload)
        else:
            pass

    def _handle_system_init(self, payload: dict[str, Any]) -> None:
        screen_size = payload.get("screen_size")
        if isinstance(screen_size, dict):
            self._handle_screen_size(screen_size)
        control_host = payload.get("control_host")
        if isinstance(control_host, dict):
            self._handle_control_host(control_host)

    def _handle_screen_size(self, payload: dict[str, Any]) -> None:
        try:
            size = ScreenSize.model_validate({key: payload.get(key) for key in ("width", "height", "rate")})
        except ValueError:
            return
        with self._lock:
            self.screen_size = size
        self._broadcast_state()

    def _handle_control_host(self, payload: dict[str, Any]) -> None:
        """A host change the service did not make is a human taking an uncontrolled Screen; a host going away is
        the controlling human's session leaving."""
        has_host = payload.get("has_host") is True
        host_id = payload.get("host_id") if has_host else None
        with self._lock:
            previous_host_id = self._last_host_id
            self._last_host_id = str(host_id) if isinstance(host_id, str) and host_id else None
            service_session = self._service_session
        if isinstance(host_id, str) and host_id:
            if service_session is not None and host_id == str(service_session.session_id):
                return
            self.control.human_became_host(NekoSessionId(host_id), self._session_name(host_id))
        elif previous_host_id is not None:
            self.control.human_session_gone(NekoSessionId(previous_host_id))
        else:
            pass

    # the display

    def _tick_loop(self) -> None:
        while not self._stop.wait(_TICK_SECONDS):
            self.control.sweep()
            if self.stack.lifecycle == Lifecycle.RUNNING:
                self.poll_title()

    def poll_title(self) -> None:
        try:
            title = self.window_tools.active_window_title()
        except WindowToolError as e:
            logger.debug("Could not read the active window's title: {}", e)
            return
        new_title = title if title else DEFAULT_TITLE
        with self._lock:
            is_changed = new_title != self.title
            self.title = new_title
        if is_changed:
            self._broadcast_state()

    def _require_running(self) -> None:
        if self.stack.lifecycle != Lifecycle.RUNNING:
            raise ScreenNotRunningError("the Screen is not running")

    def screenshot(self, quality: int, is_png: bool) -> bytes:
        self._require_running()
        jpeg = self.neko.screenshot_jpeg(quality)
        return jpeg_to_png(jpeg) if is_png else jpeg

    def list_windows(self) -> list[XWindow]:
        self._require_running()
        return self.window_tools.list_windows()

    def activate_window(self, window_id: XWindowId) -> None:
        self._require_running()
        self.window_tools.activate_window(window_id)

    def run_command(self, command: Sequence[str]) -> int:
        """Start a command on the Screen, which must be running: the ``/api/run`` route starts the stack and waits
        for it first (spec section 9.2)."""
        if self.stack.lifecycle != Lifecycle.RUNNING:
            raise ScreenNotRunningError("the Screen is not running yet")
        return self.launcher.launch(command)

    def wait_until_running(self, timeout_seconds: float) -> bool:
        """Block until the stack runs or the deadline passes (the command's ``run`` waits a little for a start)."""
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if self.stack.lifecycle == Lifecycle.RUNNING:
                return True
            if self.stack.lifecycle == Lifecycle.STOPPED:
                return False
            self._stop.wait(_POLL_SECONDS)
        return self.stack.lifecycle == Lifecycle.RUNNING
