from collections.abc import Mapping
from datetime import datetime
from datetime import timedelta
from datetime import timezone

from imbue.chat.harnesses.harness_type import HarnessType
from imbue.chat.usage_turns import UnreportedTurnRecorder

_START = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)


def test_an_antigravity_turn_is_recorded_once_it_ends_with_unknown_usage() -> None:
    sent: list[Mapping[str, object]] = []
    recorder = UnreportedTurnRecorder(emit=sent.append, workspace_id="ws-1")

    recorder.turn_started("agent-ag", HarnessType.ANTIGRAVITY, _START)
    recorder.turn_ended("agent-ag", HarnessType.ANTIGRAVITY, "chat-1", "gemini-3-pro", _START + timedelta(seconds=9))

    [event] = sent
    assert event["event_id"] == f"antigravity-agent-ag-{int(_START.timestamp() * 1000)}"
    assert event["provider"] == "google"
    assert event["model"] == "google/gemini-3-pro"
    assert event["tokens"] == {}
    assert "cost_usd" not in event
    assert event["started_at"] == _START.isoformat()
    assert event["attribution"] == {
        "workspace_id": "ws-1",
        "origin_kind": "CHAT",
        "origin_id": "chat-1",
        "harness": "antigravity",
        "purpose": "TURN",
        "turn_id": f"antigravity-agent-ag-{int(_START.timestamp() * 1000)}",
        "agent_id": "agent-ag",
        "call_source": "MAIN",
    }


def test_harnesses_that_report_their_own_usage_are_left_to_their_writers() -> None:
    sent: list[Mapping[str, object]] = []
    recorder = UnreportedTurnRecorder(emit=sent.append, workspace_id="ws-1")

    recorder.turn_started("agent-cl", HarnessType.CLAUDE, _START)
    recorder.turn_ended("agent-cl", HarnessType.CLAUDE, "chat-2", None, _START)

    assert sent == []


def test_a_turn_whose_start_was_missed_starts_when_it_ends() -> None:
    sent: list[Mapping[str, object]] = []
    recorder = UnreportedTurnRecorder(emit=sent.append, workspace_id="ws-1")

    recorder.turn_ended("agent-ag", HarnessType.ANTIGRAVITY, None, None, _START)

    assert sent[0]["started_at"] == sent[0]["ended_at"]
    attribution = sent[0]["attribution"]
    assert isinstance(attribution, dict) and attribution["origin_kind"] == "UNKNOWN"
