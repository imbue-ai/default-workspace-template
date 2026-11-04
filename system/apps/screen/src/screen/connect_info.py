"""``GET /api/connect-info`` (docs/system/specs/screen/spec.md section 5.2): the brokers asked in parallel, each
bounded (two seconds for candidates, longer for relay servers, whose first mint goes through the connector to
Cloudflare), and the union of what they answered in time, held in memory only. A candidate a broker names by a
hostname is answered as the IPv4 addresses the name resolves to here, or as the name when it does not."""

import ipaddress
import socket
import time
from collections.abc import Callable
from concurrent.futures import Future
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import Final
from typing import TypeVar

from loguru import logger
from pydantic import Field

from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from screen.data_types import ConnectInfo
from screen.data_types import IceServersAnswer
from screen.data_types import MediaCandidate
from screen.errors import ConnectivityBrokerError
from screen.interfaces import ConnectivityBrokerInterface
from screen.primitives import ICE_MUX_PORT

BROKER_DEADLINE_SECONDS: Final[float] = 2.0
# Relay servers are asked for only after the direct path has already failed, so the viewer is waiting anyway; the
# desktop's first answer mints a credential through the connector and Cloudflare, which has taken more than two seconds.
ICE_SERVERS_DEADLINE_SECONDS: Final[float] = 8.0

T = TypeVar("T")


def resolve_ipv4_addresses(host: str) -> tuple[str, ...]:
    """The IPv4 addresses ``host`` resolves to, in the resolver's order without repeats; raises OSError when the name
    does not resolve, and ValueError when it is not a name at all (an empty label, or one over 63 characters)."""
    entries = socket.getaddrinfo(host, None, family=socket.AF_INET, type=socket.SOCK_STREAM)
    return tuple(dict.fromkeys(str(entry[4][0]) for entry in entries))


@pure
def is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def resolve_candidate_hosts(
    candidates: tuple[MediaCandidate, ...], resolve: Callable[[str], tuple[str, ...]]
) -> tuple[MediaCandidate, ...]:
    """Each candidate named by a hostname replaced by one candidate per address the name resolves to, since Firefox
    ignores a remote ICE candidate whose address is not an IP. A name that does not resolve here is kept as it is,
    since it may name a host only the viewer's machine resolves (a remote docker daemon's); a malformed one is
    dropped."""
    resolved: list[MediaCandidate] = []
    for candidate in candidates:
        if is_ip_literal(candidate.host):
            resolved.append(candidate)
            continue
        try:
            addresses = resolve(candidate.host)
        except ValueError as e:
            logger.warning(
                "Dropped the candidate {}:{} ({}): the name is malformed: {}",
                candidate.host,
                candidate.port,
                candidate.protocol.value.lower(),
                e,
            )
            continue
        except OSError as e:
            logger.info(
                "Kept the candidate {}:{} as a name: it does not resolve here: {}", candidate.host, candidate.port, e
            )
            resolved.append(candidate)
            continue
        resolved.extend(
            candidate.model_copy_update(to_update(candidate.field_ref().host, address)) for address in addresses
        )
    return tuple(resolved)


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
        default=BROKER_DEADLINE_SECONDS, frozen=True, description="How long each broker's candidates are waited for"
    )
    ice_servers_deadline_seconds: float = Field(
        default=ICE_SERVERS_DEADLINE_SECONDS,
        frozen=True,
        description="How long each broker's relay servers are waited for",
    )
    resolve_host: Callable[[str], tuple[str, ...]] = Field(
        default=resolve_ipv4_addresses,
        frozen=True,
        description="A candidate hostname's IPv4 addresses, raising OSError when it does not resolve",
    )

    def fetch(self, is_relay: bool) -> ConnectInfo:
        # Each broker's names are resolved within its own bound, so a slow resolver costs that broker's answer alone.
        candidate_calls: list[tuple[str, Callable[[], tuple[MediaCandidate, ...]]]] = [
            (f"{broker.name} candidates", self._resolved_candidates_call(broker)) for broker in self.brokers
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
        started = time.monotonic()
        candidates_deadline = started + self.deadline_seconds
        ice_servers_deadline = started + self.ice_servers_deadline_seconds
        try:
            candidate_futures = [(name, executor.submit(call)) for name, call in candidate_calls]
            ice_server_futures = [(name, executor.submit(call)) for name, call in ice_server_calls]
            candidate_answers = tuple(
                answer
                for answer in (
                    self._await(name, future, candidates_deadline, self.deadline_seconds)
                    for name, future in candidate_futures
                )
                if answer
            )
            ice_server_answers = tuple(
                answer
                for answer in (
                    self._await(name, future, ice_servers_deadline, self.ice_servers_deadline_seconds)
                    for name, future in ice_server_futures
                )
                if answer
            )
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
        return merge_connect_info(candidate_answers, ice_server_answers, ICE_MUX_PORT)

    def _resolved_candidates_call(
        self, broker: ConnectivityBrokerInterface
    ) -> Callable[[], tuple[MediaCandidate, ...]]:
        return lambda: resolve_candidate_hosts(broker.fetch_candidates(), self.resolve_host)

    def _await(self, name: str, future: Future[T], deadline: float, bound_seconds: float) -> T | None:
        try:
            return future.result(timeout=max(0.0, deadline - time.monotonic()))
        except FutureTimeoutError:
            logger.debug("The {} did not answer within {}s; answering without it", name, bound_seconds)
        except ConnectivityBrokerError as e:
            logger.debug("The {} are unavailable: {}", name, e)
        return None
