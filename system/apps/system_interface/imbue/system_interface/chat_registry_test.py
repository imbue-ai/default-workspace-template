from pathlib import Path

import pytest

from imbue.system_interface.chat_registry import ChatRecord
from imbue.system_interface.chat_registry import ChatRegistry
from imbue.system_interface.chat_registry import ChatSegment
from imbue.system_interface.chat_registry import chats_dir_for_layout_dir
from imbue.system_interface.harnesses.harness_type import HarnessType
from imbue.system_interface.models import ChatId


def _make_registry(tmp_path: Path) -> ChatRegistry:
    return ChatRegistry(chats_dir=chats_dir_for_layout_dir(tmp_path))


def test_ensure_chat_creates_a_single_active_segment_record(tmp_path: Path) -> None:
    registry = _make_registry(tmp_path)
    chat_id = ChatId("agent-" + "a" * 32)

    registry.ensure_chat(chat_id, agent_id=str(chat_id), harness=HarnessType.CODEX, account_id="account-1")

    record = registry.get(chat_id)
    assert record is not None
    assert record.active_agent_id == str(chat_id)
    assert len(record.segments) == 1
    assert record.segments[0].harness == HarnessType.CODEX
    assert record.segments[0].account_id == "account-1"
    assert record.segments[0].ended_at is None


def test_records_survive_a_reload_from_disk(tmp_path: Path) -> None:
    chat_id = ChatId("agent-" + "b" * 32)
    _make_registry(tmp_path).ensure_chat(chat_id, agent_id=str(chat_id), harness=HarnessType.CLAUDE, account_id=None)

    reloaded = _make_registry(tmp_path)

    record = reloaded.get(chat_id)
    assert record is not None
    assert record.active_agent_id == str(chat_id)


def test_ensure_chat_is_idempotent_and_never_rewrites_history(tmp_path: Path) -> None:
    registry = _make_registry(tmp_path)
    chat_id = ChatId("agent-" + "c" * 32)
    registry.ensure_chat(chat_id, agent_id=str(chat_id), harness=HarnessType.CLAUDE, account_id="account-1")

    # A later discovery pass must not overwrite the record, even with different details.
    registry.ensure_chat(chat_id, agent_id="agent-" + "d" * 32, harness=HarnessType.CODEX, account_id="account-2")

    record = registry.get(chat_id)
    assert record is not None
    assert record.active_agent_id == str(chat_id)
    assert record.segments[0].harness == HarnessType.CLAUDE


def test_resolution_falls_back_to_identity_for_an_unrecorded_chat(tmp_path: Path) -> None:
    registry = _make_registry(tmp_path)
    unknown = ChatId("agent-" + "e" * 32)

    assert registry.resolve_active_agent_id(unknown) == str(unknown)


def test_resolution_uses_the_recorded_active_agent(tmp_path: Path) -> None:
    registry = _make_registry(tmp_path)
    chat_id = ChatId("agent-" + "f" * 32)
    registry.ensure_chat(chat_id, agent_id=str(chat_id), harness=HarnessType.CLAUDE, account_id=None)

    assert registry.resolve_active_agent_id(chat_id) == str(chat_id)


def test_remove_drops_the_record_and_its_file(tmp_path: Path) -> None:
    registry = _make_registry(tmp_path)
    chat_id = ChatId("agent-" + "1" * 32)
    registry.ensure_chat(chat_id, agent_id=str(chat_id), harness=HarnessType.CLAUDE, account_id=None)
    record_path = chats_dir_for_layout_dir(tmp_path) / f"{chat_id}.json"
    assert record_path.exists()

    registry.remove(chat_id)

    assert registry.get(chat_id) is None
    assert not record_path.exists()
    # Removing again (or removing an id that was never a chat) is a no-op.
    registry.remove(chat_id)


def test_a_none_chats_dir_keeps_the_registry_in_memory(tmp_path: Path) -> None:
    registry = ChatRegistry(chats_dir=None)
    chat_id = ChatId("agent-" + "2" * 32)

    registry.ensure_chat(chat_id, agent_id=str(chat_id), harness=HarnessType.CLAUDE, account_id=None)

    assert registry.resolve_active_agent_id(chat_id) == str(chat_id)
    assert list(tmp_path.iterdir()) == []


def test_an_unreadable_record_is_skipped_on_load(tmp_path: Path) -> None:
    chats_dir = chats_dir_for_layout_dir(tmp_path)
    good_id = ChatId("agent-" + "3" * 32)
    _make_registry(tmp_path).ensure_chat(good_id, agent_id=str(good_id), harness=HarnessType.CLAUDE, account_id=None)
    (chats_dir / "agent-garbage.json").write_text("{not json")

    reloaded = _make_registry(tmp_path)

    assert reloaded.get(good_id) is not None


def test_chat_record_rejects_a_mismatched_active_segment() -> None:
    segment = ChatSegment(agent_id="agent-x", harness=HarnessType.CLAUDE, started_at="2026-01-01T00:00:00+00:00")
    with pytest.raises(ValueError, match="active"):
        ChatRecord(chat_id="agent-x", active_agent_id="agent-y", segments=(segment,))


def test_chat_record_rejects_an_already_ended_final_segment() -> None:
    ended = ChatSegment(
        agent_id="agent-x",
        harness=HarnessType.CLAUDE,
        started_at="2026-01-01T00:00:00+00:00",
        ended_at="2026-01-02T00:00:00+00:00",
    )
    with pytest.raises(ValueError, match="active"):
        ChatRecord(chat_id="agent-x", active_agent_id="agent-x", segments=(ended,))


def test_chat_record_rejects_a_non_final_active_segment() -> None:
    first = ChatSegment(agent_id="agent-x", harness=HarnessType.CLAUDE, started_at="2026-01-01T00:00:00+00:00")
    second = ChatSegment(
        agent_id="agent-y",
        harness=HarnessType.CODEX,
        started_at="2026-01-02T00:00:00+00:00",
        ended_at=None,
    )
    with pytest.raises(ValueError, match="non-final"):
        ChatRecord(chat_id="agent-x", active_agent_id="agent-y", segments=(first, second))
