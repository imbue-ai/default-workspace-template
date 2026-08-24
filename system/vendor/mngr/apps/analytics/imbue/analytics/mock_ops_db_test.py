"""Concrete fake for the psycopg2 ops-DB connection, shared by the unit tests."""

from typing import Any

import psycopg2


class RecordingCursor:
    """Captures the SQL and parameters executed against the fake connection.

    Reads are served from ``rows_to_return``: tests preload the rows a SELECT
    should yield, and ``fetchall``/``fetchone`` return them.
    """

    executed: list[tuple[str, tuple[Any, ...]]]
    rows_to_return: list[tuple[Any, ...]]

    def __init__(self) -> None:
        self.executed = []
        self.rows_to_return = []

    def execute(self, statement: str, parameters: tuple[Any, ...] = ()) -> None:
        self.executed.append((statement, parameters))

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self.rows_to_return)

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.rows_to_return[0] if self.rows_to_return else None

    def __enter__(self) -> "RecordingCursor":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        return None


class FakeOpsConnection:
    """Minimal psycopg2-connection stand-in: transaction and cursor context managers."""

    def __init__(self) -> None:
        self.recording_cursor = RecordingCursor()
        self.is_closed = False

    def cursor(self) -> RecordingCursor:
        return self.recording_cursor

    def close(self) -> None:
        self.is_closed = True

    def __enter__(self) -> "FakeOpsConnection":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        return None


class RoutingCursor:
    """A cursor whose reads are routed by statement substring.

    Multi-query flows (the collection poll) issue several different SELECTs on
    one connection; tests preload ``rows_by_statement_substring`` and each
    read returns the rows of the first matching substring (empty otherwise).
    """

    executed: list[tuple[str, tuple[Any, ...]]]
    rows_by_statement_substring: dict[str, list[tuple[Any, ...]]]
    _last_statement: str

    def __init__(self, rows_by_statement_substring: dict[str, list[tuple[Any, ...]]]) -> None:
        self.executed = []
        self.rows_by_statement_substring = rows_by_statement_substring
        self._last_statement = ""

    def execute(self, statement: str, parameters: tuple[Any, ...] = ()) -> None:
        self.executed.append((statement, parameters))
        self._last_statement = statement

    def _rows_for_last_statement(self) -> list[tuple[Any, ...]]:
        for substring, rows in self.rows_by_statement_substring.items():
            if substring in self._last_statement:
                return rows
        return []

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows_for_last_statement()

    def fetchone(self) -> tuple[Any, ...] | None:
        rows = self._rows_for_last_statement()
        return rows[0] if rows else None

    def __enter__(self) -> "RoutingCursor":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        return None


class RoutingFakeConnection:
    """A psycopg2-connection stand-in whose reads route by statement substring."""

    def __init__(self, rows_by_statement_substring: dict[str, list[tuple[Any, ...]]]) -> None:
        self.routing_cursor = RoutingCursor(rows_by_statement_substring)
        self.is_closed = False

    def cursor(self) -> RoutingCursor:
        return self.routing_cursor

    def close(self) -> None:
        self.is_closed = True

    def __enter__(self) -> "RoutingFakeConnection":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        return None


class FailingCursor:
    """A cursor whose every execute raises psycopg2.OperationalError."""

    def execute(self, statement: str, parameters: tuple[Any, ...] = ()) -> None:
        raise psycopg2.OperationalError("the ops database is unreachable")

    def __enter__(self) -> "FailingCursor":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        return None


class FailingOpsConnection:
    """A psycopg2-connection stand-in whose cursors always fail (a dead ops DB)."""

    def __init__(self) -> None:
        self.is_closed = False

    def cursor(self) -> FailingCursor:
        return FailingCursor()

    def close(self) -> None:
        self.is_closed = True

    def __enter__(self) -> "FailingOpsConnection":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        return None
