"""Per-service spend tracking with a rolling-window ceiling.

A service records the estimated/actual USD cost of each paid call. Before making
a call the service checks the ceiling; once cumulative spend in the rolling
window meets or exceeds it, ``check_ceiling`` escalates to the user (via an
injected callback) and raises ``SpendCeilingExceededError`` rather than letting
volume silently run past the budget.

State persists as JSON under ``runtime/<service>/`` so the window survives
service restarts.
"""

import json
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from imbue.imbue_common.mutable_model import MutableModel
from loguru import logger
from pydantic import Field

from ai_integration.errors import SpendCeilingExceededError

# Default rolling window: 24 hours.
DEFAULT_WINDOW_SECONDS = 86_400.0

_Record = tuple[float, float]  # (timestamp, cost_usd)


def _default_escalate(message: str) -> None:
    logger.warning("ai_integration spend ceiling: {}", message)


class SpendTracker(MutableModel):
    """Tracks per-service spend against a rolling-window ceiling.

    A stateful Implementation: it owns the on-disk spend ledger under
    ``state_root/<service_name>/ai_spend.json``. ``clock`` and ``escalate`` are
    injected (tests pass deterministic fakes); ``escalate`` is called once when
    the ceiling is hit (default: log a warning -- a service should pass a
    callback that routes through ``send-user-message``).
    """

    model_config = {"arbitrary_types_allowed": True, "extra": "forbid", "frozen": False}

    service_name: str = Field(description="The service whose spend this tracks")
    ceiling_usd: float = Field(
        description="Rolling-window spend ceiling; paused once cumulative spend meets it"
    )
    state_root: Path = Field(
        default=Path("runtime"),
        description="Root under which the per-service spend ledger is persisted",
    )
    window_seconds: float = Field(
        default=DEFAULT_WINDOW_SECONDS,
        description="Length of the rolling spend window in seconds",
    )
    clock: Callable[[], float] = Field(
        default=time.time, description="Wall-clock source (injected for tests)"
    )
    escalate: Callable[[str], None] = Field(
        default=_default_escalate,
        description="Called once when the ceiling is hit (default: log a warning)",
    )

    @property
    def _state_path(self) -> Path:
        return self.state_root / self.service_name / "ai_spend.json"

    def _load(self) -> list[_Record]:
        if not self._state_path.is_file():
            return []
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        if not isinstance(raw, list):
            return []
        records: list[_Record] = []
        for entry in raw:
            if (
                isinstance(entry, Sequence)
                and not isinstance(entry, str)
                and len(entry) == 2
            ):
                ts, cost = entry
                if isinstance(ts, (int, float)) and isinstance(cost, (int, float)):
                    records.append((float(ts), float(cost)))
        return records

    def _save(self, records: Sequence[_Record]) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        self._state_path.write_text(
            json.dumps([list(r) for r in records]), encoding="utf-8"
        )

    def _within_window(self, records: Sequence[_Record]) -> list[_Record]:
        cutoff = self.clock() - self.window_seconds
        return [(ts, cost) for ts, cost in records if ts >= cutoff]

    def spent_in_window(self) -> float:
        """Cumulative spend within the current rolling window."""
        return sum(cost for _ts, cost in self._within_window(self._load()))

    def record(self, cost_usd: float) -> None:
        """Append a paid call's cost and prune entries outside the window."""
        records = self._within_window(self._load())
        records.append((self.clock(), cost_usd))
        self._save(records)

    def check_ceiling(self) -> None:
        """Escalate and raise if cumulative spend has met/exceeded the ceiling.

        Call this *before* a paid call so the call is never made once the budget
        is exhausted.
        """
        spent = self.spent_in_window()
        if spent >= self.ceiling_usd:
            message = (
                f"service '{self.service_name}' has spent ~${spent:.2f} in the last "
                f"{self.window_seconds / 3600:.0f}h, at or over its ${self.ceiling_usd:.2f} "
                f"ceiling; pausing paid AI calls"
            )
            self.escalate(message)
            raise SpendCeilingExceededError(message)
