"""``GET /api/connect-info`` (docs/system/specs/screen/spec.md section 5.2): the brokers asked in parallel with a
two-second bound each, and the union of what they answered in time, held in memory only."""

import time
from collections.abc import Callable
from concurrent.futures import Future
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import Final
from typing import TypeVar

from loguru import logger
from pydantic import Field

from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from screen.data_types import ConnectInfo
from screen.data_types import IceServersAnswer
from screen.data_types import MediaCandidate
from screen.errors import ConnectivityBrokerError
from screen.interfaces import ConnectivityBrokerInterface
from screen.primitives import ICE_MUX_PORT

BROKER_DEADLINE_SECONDS: Final[float] = 2.0

T = TypeVar("T")


@pure
def merge_connect_info(
    candidate_answers: tuple[tuple[MediaCandidate, ...], ...],
    ice_server_answers: tuple[IceServersAnswer, ...],
    guest_port: int,
) -> ConnectInfo:
    """The union: candidates filtered to the ICE mux's guest port and de-duplicated in answer order, every ICE server,
    and the earliest expiry."""
    seen: set[tuple[str, int, str]] = set()
    candidates: list[MediaCandidate] = []
    for answer in candidate_answers:
        for candidate in answer:
            if candidate.guest_port is not None and candidate.guest_port != guest_port:
                continue
            key = (candidate.host, candidate.port, candidate.protocol.value)
            if key in seen:
                continue
            seen.add(key)
            candidates.append(candidate)
    ice_servers = tuple(server for answer in ice_server_answers for server in answer.ice_servers)
    expiries = sorted(answer.expires_at for answer in ice_server_answers if answer.expires_at is not None)
    return ConnectInfo(
        ice_servers=ice_servers, candidates=tuple(candidates), expires_at=expiries[0] if expiries else None
    )


class ConnectInfoAggregator(MutableModel):
    """Asks every broker for its candidates (and, with ``relay``, its ICE servers) at once and answers what came back
    within the deadline; a broker that is slow, unreachable or refusing is absent from the answer."""

    brokers: tuple[ConnectivityBrokerInterface, ...] = Field(frozen=True, description="The brokers to ask")
    deadline_seconds: float = Field(
        default=BROKER_DEADLINE_SECONDS, frozen=True, description="How long each broker's answer is waited for"
    )

    def fetch(self, is_relay: bool) -> ConnectInfo:
        candidate_calls: list[tuple[str, Callable[[], tuple[MediaCandidate, ...]]]] = [
            (f"{broker.name} candidates", broker.fetch_candidates) for broker in self.brokers
        ]
        ice_server_calls: list[tuple[str, Callable[[], IceServersAnswer]]] = (
            [(f"{broker.name} ICE servers", broker.fetch_ice_servers) for broker in self.brokers] if is_relay else []
        )
        call_count = len(candidate_calls) + len(ice_server_calls)
        if call_count == 0:
            return merge_connect_info((), (), ICE_MUX_PORT)
        # The pool is abandoned rather than joined once the deadline passes, so a broker that hangs costs the viewer
        # nothing beyond the bound; its request times out on its own soon after.
        executor = ThreadPoolExecutor(max_workers=call_count, thread_name_prefix="screen-connect-info")
        deadline = time.monotonic() + self.deadline_seconds
        try:
            candidate_futures = [(name, executor.submit(call)) for name, call in candidate_calls]
            ice_server_futures = [(name, executor.submit(call)) for name, call in ice_server_calls]
            candidate_answers = tuple(
                answer
                for answer in (self._await(name, future, deadline) for name, future in candidate_futures)
                if answer
            )
            ice_server_answers = tuple(
                answer
                for answer in (self._await(name, future, deadline) for name, future in ice_server_futures)
                if answer
            )
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
        return merge_connect_info(candidate_answers, ice_server_answers, ICE_MUX_PORT)

    def _await(self, name: str, future: Future[T], deadline: float) -> T | None:
        try:
            return future.result(timeout=max(0.0, deadline - time.monotonic()))
        except FutureTimeoutError:
            logger.debug("The {} did not answer within {}s; answering without it", name, self.deadline_seconds)
        except ConnectivityBrokerError as e:
            logger.debug("The {} are unavailable: {}", name, e)
        return None
