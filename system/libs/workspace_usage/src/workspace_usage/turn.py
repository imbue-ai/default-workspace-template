"""The task or turn an app's model calls belong to, which only the app knows.

An app that groups its calls (one per request, per job, per conversation turn)
sets a turn id around the work; every call recorded inside it carries that id
as ``attribution.turn_id``. An app that sets none records its calls without one.
"""

from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Final

# The key a LiteLLM call's ``metadata`` names its turn under, for an app that passes it per call.
TURN_ID_METADATA_KEY: Final[str] = "imbue_turn_id"


class UsageTurnError(ValueError):
    """Raised for a turn id that cannot be recorded."""


_current_turn_id: ContextVar[str | None] = ContextVar(
    "imbue_usage_turn_id", default=None
)


@contextmanager
def usage_turn(turn_id: str) -> Generator[str, None, None]:
    """Attribute every model call recorded inside the block (this thread or task) to ``turn_id``."""
    if not turn_id.strip():
        raise UsageTurnError("A usage turn id must not be empty")
    token = _current_turn_id.set(turn_id)
    try:
        yield turn_id
    finally:
        _current_turn_id.reset(token)


def current_turn_id() -> str | None:
    """The turn id of the innermost ``usage_turn`` around this code, or None outside one."""
    return _current_turn_id.get()
