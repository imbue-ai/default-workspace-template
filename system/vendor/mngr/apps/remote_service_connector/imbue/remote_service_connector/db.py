"""Neon pool-database connection helpers.

``pooled_db_connection`` is the way every store talks to the DB: it checks a
connection out of a per-container pool and returns it on exit, so the steady
request load reuses warm connections instead of paying a fresh TLS handshake
to Neon (~hundreds of ms from an unpinned Modal container) per call.

The raw factory ``get_pool_db_connection`` stays the single test seam: it is
resolved through the module attribute at checkout time, so tests that install
a fake factory on this module feed fakes through ``pooled_db_connection``
transparently (fakes are never retained in the pool -- see
``PooledConnectionAllocator.check_in``).
"""

import os
import threading
import time
from collections.abc import Callable
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from typing import Final

import psycopg2
import psycopg2.extensions
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import PositiveInt
from pydantic import PrivateAttr

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.modal_app_kit.metrics import emit_metric
from imbue.remote_service_connector.deploy_constants import API_MAX_CONCURRENT_INPUTS

# Upper bound on idle connections retained per container. Matches the web
# function's concurrency cap: more than one connection per concurrently-served
# request can never be warm-useful, and each retained connection holds a slot
# on the Neon side.
_MAX_IDLE_CONNECTIONS: Final[int] = API_MAX_CONCURRENT_INPUTS

# A connection idle longer than this is probed (``SELECT 1``) before it is
# handed out. The failures the probe catches -- Neon suspending the compute,
# the pooler cutting an idle client -- only ever follow idleness, so gating
# the probe on idle age keeps a busy container's hot path (the frps Ping
# heartbeats, which recycle every connection within seconds) free of the
# extra round trip while a container waking from a quiet spell never hands a
# dead connection to a request.
_IDLE_PROBE_THRESHOLD_SECONDS: Final[float] = 60.0

# A connection idle longer than this is closed instead of reused. The pool is
# a LIFO stack, so its deepest entries can sit unused for hours, and probing
# one that went half-open in that time can still stall the request for up to
# the whole dead-peer detection window (POOL_CONNECTION_SOCKET_BOUNDS) before
# it fails.
_MAX_IDLE_AGE_SECONDS: Final[float] = 300.0


class PoolConnectionSocketBounds(FrozenModel):
    """libpq socket bounds applied to every pool connection.

    psycopg2 has no read timeout, so without these a peer that vanished without
    a FIN (a dropped NAT or tunnel flow) blocks the calling request until the
    kernel's own retransmit limit, well past Modal's 300 s function timeout.
    """

    # Positive by type: libpq reads 0 as "disabled" or "system default" for
    # every one of these, which would silently remove the bound.
    connect_timeout_seconds: PositiveInt = Field(description="libpq connect_timeout")
    keepalive_idle_seconds: PositiveInt = Field(description="Idle time before the first TCP keepalive probe")
    keepalive_interval_seconds: PositiveInt = Field(description="Gap between unanswered keepalive probes")
    keepalive_probe_count: PositiveInt = Field(description="Unanswered probes before the connection is declared dead")
    unacknowledged_send_timeout_seconds: PositiveInt = Field(
        description="tcp_user_timeout: how long sent data may stay unacknowledged before the connection is dropped"
    )

    def worst_case_dead_peer_detection_seconds(self) -> int:
        """The longest any single stall can last before libpq raises: connect, idle probing, or an unacknowledged send."""
        idle_detection_seconds = (
            self.keepalive_idle_seconds + self.keepalive_interval_seconds * self.keepalive_probe_count
        )
        return max(self.connect_timeout_seconds, idle_detection_seconds, self.unacknowledged_send_timeout_seconds)


# Sized so every stall surfaces well under the heartbeat timeout of the
# workspaces' frpc: a stalled ping must fail (open) before the tunnel drops.
# db_test pins that bound.
POOL_CONNECTION_SOCKET_BOUNDS: Final[PoolConnectionSocketBounds] = PoolConnectionSocketBounds(
    connect_timeout_seconds=10,
    keepalive_idle_seconds=10,
    keepalive_interval_seconds=5,
    keepalive_probe_count=2,
    unacknowledged_send_timeout_seconds=15,
)


def get_pool_db_connection() -> Any:
    """Open a new psycopg2 connection to the Neon pool database."""
    database_url = os.environ["DATABASE_URL"]
    bounds = POOL_CONNECTION_SOCKET_BOUNDS
    return psycopg2.connect(
        database_url,
        connect_timeout=bounds.connect_timeout_seconds,
        keepalives=1,
        keepalives_idle=bounds.keepalive_idle_seconds,
        keepalives_interval=bounds.keepalive_interval_seconds,
        keepalives_count=bounds.keepalive_probe_count,
        tcp_user_timeout=bounds.unacknowledged_send_timeout_seconds * 1000,
    )


class PooledConnectionAllocator(BaseModel):
    """Thread-safe free-list of reusable psycopg2 connections.

    A connection idle past the probe threshold is round-tripped before reuse,
    one idle past the maximum age is closed, and one whose server side is gone
    surfaces as an ``OperationalError`` on use, is discarded on check-in, and
    leaves the caller's normal error handling to apply (the frps Ping path
    fails open, everything else surfaces a retryable 5xx). Checkout beyond
    the idle capacity just opens a fresh connection, so the allocator can
    never deadlock or refuse a request.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    connection_factory: Callable[[], Any]
    max_idle_connections: int
    # Whether a connection may be retained for reuse; anything else is closed
    # on check-in exactly as before the pool existed.
    is_poolable_connection: Callable[[Any], bool]
    # Idle age past which a checked-out connection is probed before reuse.
    idle_probe_threshold_seconds: float = _IDLE_PROBE_THRESHOLD_SECONDS
    # Idle age past which a checked-out connection is closed instead of reused.
    max_idle_age_seconds: float = _MAX_IDLE_AGE_SECONDS
    # Injected clock (monotonic seconds) so tests can age connections.
    monotonic: Callable[[], float] = time.monotonic

    _lock: Any = PrivateAttr(default_factory=threading.Lock)
    # Idle connections paired with the monotonic time they were checked in.
    _idle_connections: list[tuple[Any, float]] = PrivateAttr(default_factory=list)

    def checkout(self) -> Any:
        idle_entry = self._pop_idle_connection()
        while idle_entry is not None and not self._is_ready_for_reuse(idle_entry):
            idle_entry = self._pop_idle_connection()
        return idle_entry[0] if idle_entry is not None else self.connection_factory()

    def _pop_idle_connection(self) -> tuple[Any, float] | None:
        """The most recently checked-in idle connection (with its check-in time), or None when the pool is empty."""
        with self._lock:
            if not self._idle_connections:
                return None
            return self._idle_connections.pop()

    def _is_ready_for_reuse(self, idle_entry: tuple[Any, float]) -> bool:
        """Whether an idle connection can be handed out: open, not aged out, and fresh or probed alive."""
        connection, idle_since = idle_entry
        if connection.closed:
            return False
        idle_seconds = self.monotonic() - idle_since
        if idle_seconds >= self.max_idle_age_seconds:
            emit_metric("db_pooled_connection_discarded", 1, {"reason": "idle_age"})
            connection.close()
            return False
        if idle_seconds < self.idle_probe_threshold_seconds:
            return True
        return self._is_probe_passing(connection)

    def _is_probe_passing(self, connection: Any) -> bool:
        """Round-trip ``SELECT 1``; a failure closes the connection so a dead one is never handed out."""
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
        except psycopg2.Error:
            emit_metric("db_pooled_connection_discarded", 1, {"reason": "checkout_probe"})
            connection.close()
            return False
        return True

    def check_in(self, connection: Any) -> None:
        # Only genuine psycopg2 connections are retained: anything else (test
        # fakes injected through the factory seam) is closed like before the
        # pool existed, so no fake ever leaks across tests via module state.
        if not self.is_poolable_connection(connection):
            connection.close()
            return
        if connection.closed:
            return
        # Rolling back before reuse drops any implicit transaction a read-only
        # caller left open (psycopg2 starts one on the first execute) and
        # doubles as the health probe: a connection whose server side is gone
        # fails here and is discarded instead of being handed to the next
        # request.
        try:
            connection.rollback()
        except psycopg2.Error:
            emit_metric("db_pooled_connection_discarded", 1, {"reason": "check_in_rollback"})
            connection.close()
            return
        with self._lock:
            if len(self._idle_connections) < self.max_idle_connections:
                self._idle_connections.append((connection, self.monotonic()))
                return
        connection.close()


def _is_real_psycopg2_connection(connection: Any) -> bool:
    return isinstance(connection, psycopg2.extensions.connection)


# The lambda resolves ``get_pool_db_connection`` through the module attribute
# at call time, keeping the factory patchable by tests.
_allocator = PooledConnectionAllocator(
    connection_factory=lambda: get_pool_db_connection(),
    max_idle_connections=_MAX_IDLE_CONNECTIONS,
    is_poolable_connection=_is_real_psycopg2_connection,
)


@contextmanager
def pooled_db_connection() -> Iterator[Any]:
    """Check a DB connection out of the per-container pool for the duration of the block."""
    connection = _allocator.checkout()
    try:
        yield connection
    finally:
        _allocator.check_in(connection)
