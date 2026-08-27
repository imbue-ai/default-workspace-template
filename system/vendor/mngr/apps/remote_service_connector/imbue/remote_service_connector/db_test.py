"""Tests for the pooled Neon connection allocator."""

import psycopg2

from imbue.remote_service_connector.db import PooledConnectionAllocator


class _StubConnection:
    """Minimal stand-in for a psycopg2 connection (closed flag + rollback)."""

    def __init__(self, is_rollback_failing: bool) -> None:
        self.closed = False
        self.rollback_count = 0
        self.is_rollback_failing = is_rollback_failing

    def rollback(self) -> None:
        if self.is_rollback_failing:
            raise psycopg2.OperationalError("simulated dead connection 84213")
        self.rollback_count += 1

    def close(self) -> None:
        self.closed = True


def _make_allocator(created: list[_StubConnection], max_idle_connections: int) -> PooledConnectionAllocator:
    def _factory() -> _StubConnection:
        connection = _StubConnection(is_rollback_failing=False)
        created.append(connection)
        return connection

    return PooledConnectionAllocator(
        connection_factory=_factory,
        max_idle_connections=max_idle_connections,
        is_poolable_connection=lambda connection: isinstance(connection, _StubConnection),
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
