from datetime import datetime, timezone
from decimal import Decimal

from workspace_usage.events import build_model_call, qualify_model

_AT = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)


def test_qualify_model_prefixes_the_provider_once() -> None:
    assert (
        qualify_model("anthropic", "claude-haiku-4-5") == "anthropic/claude-haiku-4-5"
    )
    assert (
        qualify_model("anthropic", "anthropic/claude-haiku-4-5")
        == "anthropic/claude-haiku-4-5"
    )
    assert qualify_model("openai", " ") is None


def test_build_model_call_carries_context_and_drops_unknowns() -> None:
    event = build_model_call(
        event_id="call-1",
        started_at=_AT,
        ended_at=_AT,
        provider="openai",
        model="gpt-5",
        context={"origin_kind": "APP", "origin_id": "files", "run_id": ""},
        workspace_id="ws-1",
        tokens={"input": 10, "output": 2, "reasoning": 5},
        cost_usd=Decimal("0.01"),
        harness="litellm",
    )

    assert event["model"] == "openai/gpt-5"
    assert event["tokens"] == {"input": 10, "output": 2}
    assert event["cost_usd"] == "0.01"
    assert event["cost_provenance"] == "REPORTED"
    assert event["attribution"] == {
        "workspace_id": "ws-1",
        "origin_kind": "APP",
        "origin_id": "files",
        "harness": "litellm",
    }


def test_build_model_call_without_cost_leaves_it_for_the_collector() -> None:
    event = build_model_call(
        event_id="call-2",
        started_at=_AT,
        ended_at=_AT,
        provider="anthropic",
        model=None,
        context={},
        workspace_id="ws",
    )

    assert (
        "cost_usd" not in event
        and "cost_provenance" not in event
        and "model" not in event
    )
    assert event["attribution"] == {"workspace_id": "ws", "origin_kind": "UNKNOWN"}


def test_build_model_call_carries_a_turn_id_when_given_one() -> None:
    event = build_model_call(
        event_id="call-3",
        started_at=_AT,
        ended_at=_AT,
        provider="anthropic",
        model=None,
        context={},
        workspace_id="ws",
        turn_id="request-7",
    )

    assert event["attribution"] == {
        "workspace_id": "ws",
        "origin_kind": "UNKNOWN",
        "turn_id": "request-7",
    }
