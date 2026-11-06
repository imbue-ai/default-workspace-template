import threading

from pydantic import Field
from pydantic import PrivateAttr

from screen.data_types import IceServersAnswer
from screen.data_types import MediaCandidate
from screen.errors import ConnectivityBrokerError
from screen.interfaces import ConnectivityBrokerInterface


class FakeBroker(ConnectivityBrokerInterface):
    """A connectivity broker in memory: answers what it is given, refuses on request, or holds every answer until
    released (a slow broker)."""

    model_config = {"arbitrary_types_allowed": True}

    candidates: tuple[MediaCandidate, ...] = Field(default=(), description="What the candidates call answers")
    ice_servers: IceServersAnswer = Field(
        default=IceServersAnswer(ice_servers=(), expires_at=None), description="What the ICE servers call answers"
    )
    is_refusing: bool = Field(default=False, description="Whether every call raises")
    release: threading.Event = Field(
        default_factory=lambda: _set_event(), description="Every call waits on this first; clear it for a slow broker"
    )
    calls: list[str] = Field(default_factory=list, description="Every call, in order")

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    def _enter(self, call: str) -> None:
        with self._lock:
            self.calls.append(call)
        self.release.wait()
        if self.is_refusing:
            raise ConnectivityBrokerError(f"{self.name} refused")

    def fetch_candidates(self) -> tuple[MediaCandidate, ...]:
        self._enter("candidates")
        return self.candidates

    def fetch_ice_servers(self) -> IceServersAnswer:
        self._enter("ice_servers")
        return self.ice_servers


def _set_event() -> threading.Event:
    event = threading.Event()
    event.set()
    return event
