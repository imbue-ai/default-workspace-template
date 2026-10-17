import threading

from pydantic import Field
from pydantic import PrivateAttr

from screen.cdp_client import CdpClientInterface
from screen.cdp_client import CdpConnectorInterface
from screen.cdp_client import CdpTarget
from screen.chrome_launcher import ChromiumHandleInterface
from screen.chrome_launcher import ChromiumLaunchRequest
from screen.chrome_launcher import ChromiumLauncherInterface
from screen.errors import CdpError
from screen.errors import ChromiumStartError


class FakeChromiumHandle(ChromiumHandleInterface):
    """A Chromium that is alive until killed (or until a test kills it behind the fleet's back)."""

    alive: bool = Field(default=True, description="Whether the process still runs")
    kills: int = Field(default=0, description="How many times it was killed")

    def is_alive(self) -> bool:
        return self.alive

    def kill(self) -> None:
        self.kills += 1
        self.alive = False


class FakeChromiumLauncher(ChromiumLauncherInterface):
    """Launches Chromiums in memory: every request is recorded, and a launch can be made to fail or to block."""

    model_config = {"arbitrary_types_allowed": True}

    requests: list[ChromiumLaunchRequest] = Field(default_factory=list, description="Every launch request, in order")
    handles: list[FakeChromiumHandle] = Field(default_factory=list, description="Every handle launched, in order")
    failure: str | None = Field(default=None, description="When set, every launch raises with this message")
    release: threading.Event = Field(
        default_factory=lambda: _set_event(), description="Every launch waits on this first; clear it to hold a launch"
    )
    entered: threading.Event = Field(default_factory=threading.Event, description="Set once a launch has begun")

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    def launch(self, request: ChromiumLaunchRequest) -> ChromiumHandleInterface:
        self.entered.set()
        self.release.wait()
        with self._lock:
            self.requests.append(request)
            if self.failure is not None:
                raise ChromiumStartError(self.failure)
            handle = FakeChromiumHandle(
                http_endpoint=f"http://127.0.0.1:{9000 + len(self.handles)}", pid=4000 + len(self.handles)
            )
            self.handles.append(handle)
            return handle


class FakeCdpClient(CdpClientInterface):
    """Chromium's tabs in memory: targets are created, closed and activated, the shown tab is the one activated last
    (or the one the test names), and any method can be made to refuse."""

    targets: list[CdpTarget] = Field(default_factory=list, description="The open tabs, in creation order")
    shown_target_id: str | None = Field(default=None, description="The tab in front, as the pages would answer")
    failing_methods: set[str] = Field(default_factory=set, description="Methods that raise CdpError")
    created: list[str] = Field(default_factory=list, description="Every URL opened as a new tab, in order")
    closed: list[str] = Field(default_factory=list, description="Every target closed, in order")
    activated: list[str] = Field(default_factory=list, description="Every target activated, in order")
    alive: bool = Field(default=True, description="Whether the connection is up")
    ping_answer: bool = Field(default=True, description="What a ping answers")
    is_closed: bool = Field(default=False, description="Whether the fleet closed the connection")

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    def _check(self, method: str) -> None:
        if method in self.failing_methods:
            raise CdpError(f"{method}: refused")

    def is_alive(self) -> bool:
        return self.alive and not self.is_closed

    def ping(self) -> bool:
        return self.ping_answer

    def page_targets(self) -> list[CdpTarget]:
        self._check("Target.getTargets")
        with self._lock:
            return list(self.targets)

    def activate(self, target_id: str) -> None:
        self._check("Target.activateTarget")
        with self._lock:
            self.activated.append(target_id)
            self.shown_target_id = target_id

    def create_target(self, url: str) -> str:
        self._check("Target.createTarget")
        with self._lock:
            self.created.append(url)
            target_id = f"new-{len(self.created)}"
            self.targets.append(CdpTarget(target_id=target_id, url=url, title=""))
            return target_id

    def close_target(self, target_id: str) -> None:
        self._check("Target.closeTarget")
        with self._lock:
            self.closed.append(target_id)
            self.targets = [target for target in self.targets if target.target_id != target_id]

    def is_shown(self, target_id: str) -> bool:
        self._check("Runtime.evaluate")
        return target_id == self.shown_target_id

    def close(self) -> None:
        self.is_closed = True


class FakeCdpConnector(CdpConnectorInterface):
    """Hands out the clients the test prepared, one per connection, in order; a connection can be made to fail."""

    clients: list[FakeCdpClient] = Field(default_factory=list, description="The clients to hand out, in order")
    connected: list[str] = Field(default_factory=list, description="Every endpoint connected to, in order")
    failure: str | None = Field(default=None, description="When set, every connection raises with this message")

    def connect(self, http_endpoint: str) -> CdpClientInterface:
        self.connected.append(http_endpoint)
        if self.failure is not None:
            raise CdpError(self.failure)
        if len(self.connected) <= len(self.clients):
            return self.clients[len(self.connected) - 1]
        client = FakeCdpClient()
        self.clients.append(client)
        return client


def _set_event() -> threading.Event:
    event = threading.Event()
    event.set()
    return event
