"""Tests for the pooled Neon connection allocator."""

import psycopg2

from imbue.remote_service_connector.db import PooledConnectionAllocator


class _StubCursor:
    """Records the probe statement; raises when the owning connection is marked dead."""

    def __init__(self, connection: "_StubConnection") -> None:
        self._connection = connection

    def execute(self, query: str) -> None:
        self._connection.executed_queries.append(query)
        if self._connection.is_probe_failing:
            raise psycopg2.OperationalError("simulated dead connection 51927")

    def fetchone(self) -> tuple[int]:
        return (1,)

    def __enter__(self) -> "_StubCursor":
        return self

    def __exit__(self, *args: object) -> None:
        pass


class _StubConnection:
    """Minimal stand-in for a psycopg2 connection (closed flag, rollback, probe cursor)."""

    def __init__(self, is_rollback_failing: bool, is_probe_failing: bool = False) -> None:
        self.closed = False
        self.rollback_count = 0
        self.is_rollback_failing = is_rollback_failing
        self.is_probe_failing = is_probe_failing
        self.executed_queries: list[str] = []

    def rollback(self) -> None:
        if self.is_rollback_failing:
            raise psycopg2.OperationalError("simulated dead connection 84213")
        self.rollback_count += 1

    def cursor(self) -> _StubCursor:
        return _StubCursor(self)

    def close(self) -> None:
        self.closed = True


class _FakeClock:
    """A settable monotonic clock for aging idle connections."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _make_allocator(
    created: list[_StubConnection],
    max_idle_connections: int,
    clock: _FakeClock | None = None,
    idle_probe_threshold_seconds: float = 60.0,
) -> PooledConnectionAllocator:
    def _factory() -> _StubConnection:
        connection = _StubConnection(is_rollback_failing=False)
        created.append(connection)
        return connection

    return PooledConnectionAllocator(
        connection_factory=_factory,
        max_idle_connections=max_idle_connections,
        is_poolable_connection=lambda connection: isinstance(connection, _StubConnection),
        idle_probe_threshold_seconds=idle_probe_threshold_seconds,
        monotonic=clock if clock is not None else _FakeClock(),
    )


def test_checkout_reuses_a_checked_in_connection() -> None:
    created: list[_StubConnection] = []
    allocator = _make_allocator(created, max_idle_connections=2)

    first = allocator.checkout()
    allocator.check_in(first)
    second = allocator.checkout()

    assert second is first
    assert len(created) == 1


def test_check_in_rolls_back_before_retaining() -> None:
    created: list[_StubConnection] = []
    allocator = _make_allocator(created, max_idle_connections=2)
    connection = allocator.checkout()

    allocator.check_in(connection)

    assert connection.rollback_count == 1
    assert not connection.closed


def test_check_in_discards_a_connection_whose_rollback_fails() -> None:
    created: list[_StubConnection] = []
    allocator = _make_allocator(created, max_idle_connections=2)
    broken = _StubConnection(is_rollback_failing=True)

    allocator.check_in(broken)
    fresh = allocator.checkout()

    assert broken.closed
    assert fresh is not broken
    assert len(created) == 1


def test_checkout_skips_connections_that_were_closed_while_idle() -> None:
    created: list[_StubConnection] = []
    allocator = _make_allocator(created, max_idle_connections=2)
    connection = allocator.checkout()
    allocator.check_in(connection)
    connection.close()

    fresh = allocator.checkout()

    assert fresh is not connection
    assert len(created) == 2


def test_check_in_closes_non_poolable_connections_instead_of_retaining() -> None:
    class _ForeignConnection:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    created: list[_StubConnection] = []
    allocator = _make_allocator(created, max_idle_connections=2)
    foreign = _ForeignConnection()

    allocator.check_in(foreign)
    fresh = allocator.checkout()

    assert foreign.closed
    assert fresh is not foreign


def test_check_in_closes_connections_beyond_the_idle_capacity() -> None:
    created: list[_StubConnection] = []
    allocator = _make_allocator(created, max_idle_connections=1)
    first = allocator.checkout()
    second = allocator.checkout()

    allocator.check_in(first)
    allocator.check_in(second)

    assert not first.closed
    assert second.closed


def test_check_in_of_an_already_closed_connection_is_a_no_op() -> None:
    created: list[_StubConnection] = []
    allocator = _make_allocator(created, max_idle_connections=2)
    connection = allocator.checkout()
    connection.close()

    allocator.check_in(connection)
    fresh = allocator.checkout()

    assert fresh is not connection
    assert connection.rollback_count == 0


def test_checkout_hands_out_a_recently_used_connection_without_probing() -> None:
    created: list[_StubConnection] = []
    clock = _FakeClock()
    allocator = _make_allocator(created, max_idle_connections=2, clock=clock)
    connection = allocator.checkout()
    allocator.check_in(connection)
    clock.now += 5.0

    reused = allocator.checkout()

    assert reused is connection
    assert connection.executed_queries == []


def test_checkout_probes_a_connection_idle_past_the_threshold() -> None:
    created: list[_StubConnection] = []
    clock = _FakeClock()
    allocator = _make_allocator(created, max_idle_connections=2, clock=clock, idle_probe_threshold_seconds=60.0)
    connection = allocator.checkout()
    allocator.check_in(connection)
    clock.now += 61.0

    reused = allocator.checkout()

    assert reused is connection
    assert connection.executed_queries == ["SELECT 1"]


def test_checkout_discards_a_stale_connection_whose_probe_fails_and_opens_a_fresh_one() -> None:
    created: list[_StubConnection] = []
    clock = _FakeClock()
    allocator = _make_allocator(created, max_idle_connections=2, clock=clock)
    connection = allocator.checkout()
    allocator.check_in(connection)
    connection.is_probe_failing = True
    clock.now += 3600.0

    fresh = allocator.checkout()

    assert connection.closed
    assert fresh is not connection
    assert len(created) == 2
