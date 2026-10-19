"""The Screen service's state and verbs (docs/system/specs/screen/spec.md section 10), composed from the stack, the
lease, neko and the display tools; the routes are a thin layer over it."""

import io
import json
import threading
import time
from collections.abc import Callable
from collections.abc import Sequence
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import Final
from typing import IO

from PIL import Image
from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr
from pydantic import SecretStr

from imbue.imbue_common.event_envelope import parse_iso_timestamp
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from screen.band_sweep import BandSweeper
from screen.broadcast import ViewerBroadcaster
from screen.browser_fleet import Browser
from screen.browser_fleet import BrowserFleet
from screen.connect_info import ConnectInfoAggregator
from screen.control import ScreenControl
from screen.data_types import AcquireStatus
from screen.data_types import AddedMode
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
from screen.desktop_files import desktop_directory
from screen.desktop_files import save_dropped_file
from screen.errors import DisplayModeError
from screen.errors import NekoError
from screen.errors import ScreenError
from screen.errors import ScreenNotRunningError
from screen.errors import WindowToolError
from screen.identity import display_name_for_identity
from screen.interfaces import AgentMessengerInterface
from screen.interfaces import DisplayModesInterface
from screen.interfaces import NekoInterface
from screen.interfaces import ProcessLauncherInterface
from screen.interfaces import WindowToolsInterface
from screen.neko_client import NekoEventSubscriber
from screen.primitives import AgentId
from screen.primitives import BROWSER_TITLE
from screen.primitives import BrowserName
from screen.primitives import CHROMIUM_WINDOW_CLASS_MARKERS
from screen.primitives import DEFAULT_TITLE
from screen.primitives import DroppedFileName
from screen.primitives import FILE_MANAGER_COMMAND
from screen.primitives import NekoSessionId
from screen.primitives import VIDEO_ID_WATCH
from screen.primitives import XWindowId
from screen.profiles import ProfileInputs
from screen.profiles import choose_video_id
from screen.sizing import MODES_RECORD_FILENAME
from screen.sizing import ModesLedger
from screen.sizing import decide_mode_addition
from screen.sizing import quantize_screen_size
from screen.stack import ScreenStack
from screen.xclipboard import ClipboardContents
from screen.xclipboard import ClipboardMonitor
from screen.xclipboard import XClipboard

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
def is_chromium_window_class(window_class: str) -> bool:
    """Whether a WM_CLASS is a Fortress (Chromium) window's."""
    lowered = window_class.lower()
    return any(marker in lowered for marker in CHROMIUM_WINDOW_CLASS_MARKERS)


@pure
def title_for_active_window(window: XWindow | None) -> str:
    """What the Screen's window is titled (spec section 4.1): "Browser" for a Chromium window, the window's title
    otherwise, the app's name for an empty desktop."""
    if window is None:
        return DEFAULT_TITLE
    if is_chromium_window_class(window.window_class):
        return BROWSER_TITLE
    return window.title or DEFAULT_TITLE


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
    display_modes: DisplayModesInterface = Field(frozen=True, description="xrandr on the display")
    clipboard: XClipboard = Field(default_factory=XClipboard, frozen=True, description="xclip on the display")
    desktop_dir: Path = Field(
        default_factory=lambda: desktop_directory(Path.home()),
        frozen=True,
        description="Where dropped files land: the XDG desktop folder, shown in a file manager window after a drop",
    )
    launcher: ProcessLauncherInterface = Field(frozen=True, description="How ``screen run`` starts a command")
    messenger: AgentMessengerInterface = Field(frozen=True, description="Wakes queued agents")
    connectivity: ConnectInfoAggregator = Field(
        frozen=True, description="Answers connect-info from the brokers (spec section 5.2)"
    )
    broadcaster: ViewerBroadcaster = Field(default_factory=ViewerBroadcaster, description="The viewers' event fan-out")
    is_event_subscription_enabled: bool = Field(
        default=True, frozen=True, description="Whether to follow neko's WebSocket (off under a fake neko)"
    )
    is_clipboard_monitor_enabled: bool = Field(
        default=True, frozen=True, description="Whether to watch the X clipboard (off where no display runs)"
    )
    clock: Callable[[], float] = Field(default=time.monotonic, frozen=True, description="The monotonic clock")

    title: str = Field(default=DEFAULT_TITLE, description="The frontmost window's title")
    video_id: str = Field(default=VIDEO_ID_WATCH, description="The profile every viewer selects")
    screen_size: ScreenSize | None = Field(default=None, description="The display's mode, while running")

    _stack: ScreenStack | None = PrivateAttr(default=None)
    _control: ScreenControl | None = PrivateAttr(default=None)
    _fleet: BrowserFleet | None = PrivateAttr(default=None)
    _band_sweeper: BandSweeper | None = PrivateAttr(default=None)
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _sessions: dict[NekoSessionId, NekoSession] = PrivateAttr(default_factory=dict)
    _service_session: NekoSession | None = PrivateAttr(default=None)
    _subscriber: NekoEventSubscriber | None = PrivateAttr(default=None)
    _clipboard_monitor: ClipboardMonitor | None = PrivateAttr(default=None)
    _secrets: NekoSecrets | None = PrivateAttr(default=None)
    _last_host_id: str | None = PrivateAttr(default=None)
    _last_input_at: float | None = PrivateAttr(default=None)
    _modes_ledger: ModesLedger | None = PrivateAttr(default=None)
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
            clock=self.clock,
        )
        self._modes_ledger = ModesLedger(path=self.state_dir / MODES_RECORD_FILENAME)

    def attach_stack(self, stack: ScreenStack) -> None:
        self._stack = stack

    def attach_fleet(self, fleet: BrowserFleet, band_sweeper: BandSweeper | None) -> None:
        self._fleet = fleet
        self._band_sweeper = band_sweeper

    @property
    def stack(self) -> ScreenStack:
        assert self._stack is not None, "the stack is attached before the service runs"
        return self._stack

    @property
    def fleet(self) -> BrowserFleet:
        assert self._fleet is not None, "the fleet is attached before the service runs"
        return self._fleet

    @property
    def control(self) -> ScreenControl:
        assert self._control is not None, "the lease is built with the service"
        return self._control

    @property
    def modes_ledger(self) -> ModesLedger:
        assert self._modes_ledger is not None, "the ledger is built with the service"
        return self._modes_ledger

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
        if self._band_sweeper is not None:
            self._band_sweeper.start()

    def shutdown(self, reason: str) -> None:
        """Stop the group (the service's own shutdown ends it, spec section 9.3) and the service's threads."""
        self._stop.set()
        self._projection_wake.set()
        if self._band_sweeper is not None:
            self._band_sweeper.stop()
        if self._subscriber is not None:
            self._subscriber.stop()
        self._stop_clipboard_monitor()
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
            chromium=self._fleet.chromium_view() if self._fleet is not None else ChromiumView(state="absent"),
        )

    def state_document(self) -> dict[str, Any]:
        return state_document(self.state())

    def broadcast_state(self) -> None:
        """Every viewer is sent the state document (what the fleet calls when the browser's state changes)."""
        self.broadcaster.broadcast({"type": "state", **self.state_document()})

    def broadcaster_state_text(self) -> str:
        """The ``state`` event a viewer is sent on connect."""
        return json.dumps({"type": "state", **self.state_document()}, default=str)

    # lifecycle hooks (the stack calls these)

    def on_lifecycle_changed(self) -> None:
        if self.stack.lifecycle == Lifecycle.STOPPED:
            self._end_of_stack()
        self.broadcast_state()

    def _end_of_stack(self) -> None:
        if self._subscriber is not None:
            self._subscriber.stop()
            self._subscriber = None
        self._stop_clipboard_monitor()
        with self._lock:
            self._sessions.clear()
            self._service_session = None
            self._secrets = None
            self._last_host_id = None
            self._projected_host_id = None
            self._last_input_at = None
            self.screen_size = None
            self.title = DEFAULT_TITLE
        # The X server is gone, and with it every mode added to it and every program that ran on it.
        self.modes_ledger.clear()
        if self._fleet is not None:
            self._fleet.on_stack_stopped()
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
        if self.is_clipboard_monitor_enabled and self._clipboard_monitor is None:
            monitor = ClipboardMonitor(clipboard=self.clipboard, on_change=self.on_clipboard_changed)
            self._clipboard_monitor = monitor
            monitor.start()
        # A start is activity: the first minute streams at ``watch`` rather than at the idle rate.
        with self._lock:
            self._last_input_at = self.clock()
        self._request_projection(self.control.view())
        self.refresh_profile()
        if self._fleet is not None:
            self._fleet.on_stack_running()

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
        self.refresh_profile()
        self.broadcast_state()
        if view.controller != Controller.AGENT and self._fleet is not None:
            # A page held while an agent drove opens now (spec section 4.2); a tab is slow CDP work, off this thread.
            threading.Thread(target=self._fleet.open_pending_url, name="screen-pending-url", daemon=True).start()

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
        self._note_input_now()

    # input and the profile (spec section 8.1)

    def is_controller_session(self, session_id: NekoSessionId) -> bool:
        view = self.control.view()
        return view.controller == Controller.HUMAN and view.human_session_id == session_id

    def note_input(self, session_id: NekoSessionId) -> bool:
        """A viewer's input relay reports activity; believed for a session the service minted."""
        if self.known_session(session_id) is None:
            return False
        self._note_input_now()
        return True

    def _note_input_now(self) -> None:
        with self._lock:
            self._last_input_at = self.clock()
        self.refresh_profile()

    def profile_inputs(self) -> ProfileInputs:
        with self._lock:
            last_input_at = self._last_input_at
        return ProfileInputs(
            controller=self.control.view().controller,
            seconds_since_input=None if last_input_at is None else max(0.0, self.clock() - last_input_at),
        )

    def refresh_profile(self) -> str:
        """Re-choose the video id from the aggregate state; a change is announced to every viewer. A stack that is
        not running has no encoder to choose for, so the id rests at ``watch`` until the next start."""
        chosen = (
            choose_video_id(self.profile_inputs()) if self.stack.lifecycle == Lifecycle.RUNNING else VIDEO_ID_WATCH
        )
        with self._lock:
            is_changed = chosen != self.video_id
            self.video_id = chosen
        if is_changed:
            self.broadcaster.broadcast({"type": "profile", "video_id": chosen})
            self.broadcast_state()
        return chosen

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
        except ValueError as e:
            logger.debug("Ignored a screen size neko reported in a shape this build does not read: {}", e)
            return
        with self._lock:
            self.screen_size = size
        self.broadcast_state()

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
                self.refresh_profile()
                if self._fleet is not None:
                    self._fleet.tick()

    def poll_title(self) -> None:
        try:
            active = self.window_tools.active_window()
        except WindowToolError as e:
            logger.debug("Could not read the active window: {}", e)
            return
        new_title = title_for_active_window(active)
        with self._lock:
            is_changed = new_title != self.title
            self.title = new_title
        if is_changed:
            self.broadcast_state()

    def _require_running(self) -> None:
        if self.stack.lifecycle != Lifecycle.RUNNING:
            raise ScreenNotRunningError("the Screen is not running")

    # the clipboard (spec section 13)

    def _stop_clipboard_monitor(self) -> None:
        if self._clipboard_monitor is not None:
            self._clipboard_monitor.stop()
            self._clipboard_monitor = None

    def on_clipboard_changed(self, contents: ClipboardContents) -> None:
        """The X clipboard has a new owner: when it offers an image, viewers are told so the controller's can fetch
        it through neko's clipboard image route (text and HTML reach the host through neko's own event)."""
        if contents.has_image:
            self.broadcaster.broadcast({"type": "clipboard", "kind": "image"})

    def set_clipboard_image(self, data: bytes, mime: str) -> None:
        """Place a viewer's image on the X clipboard (local to remote)."""
        self._require_running()
        if self._clipboard_monitor is not None:
            self._clipboard_monitor.note_written(data)
        self.clipboard.set_contents(data, mime)

    # files dropped on the viewer (spec section 4.3)

    def save_dropped_files(self, files: Sequence[tuple[DroppedFileName, IO[bytes]]]) -> list[Path]:
        """Write the controller's dropped files into the desktop folder, each under its own name (suffixed on a
        clash), and show the folder in a file manager window on the Screen (its icons are off); answers the paths
        they landed at."""
        self._require_running()
        saved = [save_dropped_file(self.desktop_dir, name, stream) for name, stream in files]
        # The window is a convenience on top of a drop that has landed: the paths are answered either way.
        try:
            self.launcher.launch((FILE_MANAGER_COMMAND, str(self.desktop_dir)))
        except ScreenError as e:
            logger.warning("Could not open the desktop folder {} on the Screen after a drop: {}", self.desktop_dir, e)
        return saved

    def set_screen_size(self, width: int, height: int) -> ScreenSize:
        """Resize the display (spec section 7.2): quantize and clamp, add the RANDR mode when the server lacks it,
        then have neko select it. Answers the size that stands afterwards."""
        self._require_running()
        requested = quantize_screen_size(width, height)
        with self._lock:
            current = self.screen_size
        if current is not None and (current.width, current.height) == (requested.width, requested.height):
            return current
        addition = decide_mode_addition(self.display_modes.query(), requested)
        if addition is not None:
            self.display_modes.add_mode(addition)
            self.modes_ledger.append(
                AddedMode(name=addition.modeline.name, width=requested.width, height=requested.height)
            )
        try:
            applied = self.neko.set_screen_size(requested)
        except NekoError as e:
            raise DisplayModeError(f"neko refused the size {requested.width}x{requested.height}: {e}") from e
        with self._lock:
            self.screen_size = applied
        self.broadcast_state()
        return applied

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

    # the browser (spec section 16)

    def launch_browser(self, start_url: str | None) -> Browser:
        """The ``browser`` launch path: start the stack when it is down, start Chromium when it is not running (or
        register it to start with the stack), open the start page as a new tab when given, and raise its window."""
        self.ensure_started()
        browser = self.fleet.ensure_browser(start_url)
        if browser.is_running:
            self.raise_browser_window()
        return browser

    def open_url_for_human(self, url: str) -> Browser:
        """The ``open:url`` handler: a tab in Chromium, raised, unless an agent drives (the page is then held)."""
        self.ensure_started()
        browser = self.fleet.open_url_for_human(url)
        if browser.is_running and browser.pending_url is None:
            self.raise_browser_window()
        return browser

    def raise_browser_window(self) -> bool:
        """Bring Chromium's window (the most recently created, when it has several) to the front; answers whether
        one was found."""
        if self.stack.lifecycle != Lifecycle.RUNNING:
            return False
        try:
            windows = self.window_tools.list_windows()
            chromium_windows = [window for window in windows if is_chromium_window_class(window.window_class)]
            if not chromium_windows:
                return False
            self.window_tools.activate_window(chromium_windows[-1].id)
        except WindowToolError as e:
            logger.debug("Could not raise the browser's window: {}", e)
            return False
        return True

    def on_browser_running(self, name: BrowserName) -> None:
        """A browser came up (the fleet's launch thread): its window comes to the front."""
        logger.debug("Browser {} is running; raising its window", name)
        self.raise_browser_window()

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
