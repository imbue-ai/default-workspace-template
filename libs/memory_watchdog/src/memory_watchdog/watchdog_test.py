import pytest

from memory_watchdog.data_types import (
    MemoryPressure,
    ShedRecord,
    Tier,
)
from memory_watchdog.watchdog import _build_status, _services_session_name


def _pressure(used_fraction: float) -> MemoryPressure:
    total = 1_000_000
    return MemoryPressure(
        total_kb=total, available_kb=int(total * (1.0 - used_fraction))
    )


def test_status_under_pressure_when_usage_high() -> None:
    status = _build_status(_pressure(0.95), (), (), "2026-06-12T10:00:00.000000000Z")
    assert status.is_under_pressure is True


def test_status_under_pressure_when_recent_sheds_even_if_usage_low() -> None:
    record = ShedRecord(
        timestamp="2026-06-12T10:00:00.000000000Z",
        tier=Tier.AGENT_CHILD,
        tier_rank=8,
        label="pytest",
        pid=1,
        resident_kb=1000,
        agent_name=None,
    )
    status = _build_status(
        _pressure(0.10), (record,), (), "2026-06-12T10:00:00.000000000Z"
    )
    assert status.is_under_pressure is True
    assert status.recently_shed[0].label == "pytest"


def test_status_not_under_pressure_when_calm() -> None:
    status = _build_status(_pressure(0.10), (), (), "2026-06-12T10:00:00.000000000Z")
    assert status.is_under_pressure is False


def test_services_session_name_from_agent_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # The services session is the services agent's own tmux session:
    # <mngr_prefix><MNGR_AGENT_NAME>.
    monkeypatch.setenv("MNGR_AGENT_NAME", "system-services")
    assert _services_session_name("mngr-") == "mngr-system-services"


def test_services_session_name_when_agent_name_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # With no agent name, the result is just the prefix -- it matches no real
    # session, so the services session is never mistaken for a sheddable one.
    monkeypatch.delenv("MNGR_AGENT_NAME", raising=False)
    assert _services_session_name("mngr-") == "mngr-"
