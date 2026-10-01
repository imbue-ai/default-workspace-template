from pathlib import Path

import pytest

from imbue.system_interface.chat_registry import ChatRecord
from imbue.system_interface.chat_registry import ChatRecordError
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


def test_begin_segment_repoints_the_chat_and_closes_the_outgoing_segment(tmp_path: Path) -> None:
    registry = _make_registry(tmp_path)
    chat_id = ChatId("agent-" + "f" * 32)
    registry.ensure_chat(chat_id, agent_id=str(chat_id), harness=HarnessType.CLAUDE, account_id="account-1")
    successor_id = "agent-" + "0" * 32

    record = registry.begin_segment(chat_id, agent_id=successor_id, harness=HarnessType.CODEX, account_id="account-2")

    # The chat's identity is untouched; only which agent answers for it has moved.
    assert record.chat_id == str(chat_id)
    assert record.active_agent_id == successor_id
    assert registry.resolve_active_agent_id(chat_id) == successor_id
    assert [segment.agent_id for segment in record.segments] == [str(chat_id), successor_id]
    assert record.segments[0].ended_at is not None
    assert record.segments[0].harness == HarnessType.CLAUDE
    assert record.segments[1].ended_at is None
    assert record.segments[1].harness == HarnessType.CODEX
    assert record.segments[1].account_id == "account-2"


def test_begin_segment_hands_off_without_a_gap_in_the_history(tmp_path: Path) -> None:
    registry = _make_registry(tmp_path)
    chat_id = ChatId("agent-" + "1" * 32)
    registry.ensure_chat(chat_id, agent_id=str(chat_id), harness=HarnessType.CLAUDE, account_id=None)

    record = registry.begin_segment(chat_id, agent_id="agent-" + "2" * 32, harness=HarnessType.CODEX, account_id=None)

    # The outgoing segment ends exactly when the incoming one starts, so no instant of the
    # chat's history is attributable to no agent (or to two).
    assert record.segments[0].ended_at == record.segments[1].started_at


def test_begin_segment_survives_a_reload_from_disk(tmp_path: Path) -> None:
    chat_id = ChatId("agent-" + "3" * 32)
    successor_id = "agent-" + "4" * 32
    registry = _make_registry(tmp_path)
    registry.ensure_chat(chat_id, agent_id=str(chat_id), harness=HarnessType.CLAUDE, account_id=None)
    registry.begin_segment(chat_id, agent_id=successor_id, harness=HarnessType.CODEX, account_id=None)

    reloaded = _make_registry(tmp_path)

    # The flip is the handoff's commit point, so a restart right after it must find the
    # chat pointing at the replacement rather than at the agent about to be destroyed.
    assert reloaded.resolve_active_agent_id(chat_id) == successor_id
    record = reloaded.get(chat_id)
    assert record is not None
    assert len(record.segments) == 2


def test_repeated_begin_segment_accumulates_segments(tmp_path: Path) -> None:
    registry = _make_registry(tmp_path)
    chat_id = ChatId("agent-" + "5" * 32)
    registry.ensure_chat(chat_id, agent_id=str(chat_id), harness=HarnessType.CLAUDE, account_id=None)
    registry.begin_segment(chat_id, agent_id="agent-" + "6" * 32, harness=HarnessType.CODEX, account_id=None)

    record = registry.begin_segment(chat_id, agent_id="agent-" + "7" * 32, harness=HarnessType.CLAUDE, account_id=None)

    assert len(record.segments) == 3
    assert [segment.ended_at is None for segment in record.segments] == [False, False, True]


def test_begin_segment_rejects_an_unrecorded_chat(tmp_path: Path) -> None:
    registry = _make_registry(tmp_path)

    # An unrecorded chat resolves by identity, so re-pointing it would silently do nothing.
    with pytest.raises(ChatRecordError, match="no record"):
        registry.begin_segment(
            ChatId("agent-" + "8" * 32),
            agent_id="agent-" + "9" * 32,
            harness=HarnessType.CODEX,
            account_id=None,
        )


def test_reverse_lookups_follow_the_active_agent_across_a_switch(tmp_path: Path) -> None:
    """The inverse mapping names the CURRENT backing agent only.

    The agents projection stamps a chat id on every row from this, so a retired agent
    still matching would put two rows on one chat -- and on a first switch the retired
    agent's id IS the chat id, which is the case most likely to go unnoticed.
    """
    registry = _make_registry(tmp_path)
    chat_id = ChatId("agent-" + "b" * 32)
    successor_id = "agent-" + "c" * 32
    registry.ensure_chat(chat_id, agent_id=str(chat_id), harness=HarnessType.CLAUDE, account_id=None)

    assert registry.chat_id_for_active_agent(str(chat_id)) == chat_id
    assert registry.chat_id_by_active_agent() == {str(chat_id): chat_id}

    registry.begin_segment(chat_id, agent_id=successor_id, harness=HarnessType.CODEX, account_id=None)

    assert registry.chat_id_for_active_agent(successor_id) == chat_id
    assert registry.chat_id_for_active_agent(str(chat_id)) is None
    assert registry.chat_id_by_active_agent() == {successor_id: chat_id}


def test_reverse_lookup_of_an_unrecorded_agent_is_none(tmp_path: Path) -> None:
    """None, not the id itself: an unrecorded chat resolves forward by identity, so the
    projection must be able to tell "backs no recorded chat" from "backs this one"."""
    registry = _make_registry(tmp_path)

    assert registry.chat_id_for_active_agent("agent-" + "d" * 32) is None


def test_retired_agent_ids_are_the_archive_read_order(tmp_path: Path) -> None:
    """Oldest first, active excluded: the chat's whole history is this list then the
    active agent, and the archive is keyed by exactly these ids."""
    registry = _make_registry(tmp_path)
    chat_id = ChatId("agent-" + "e" * 32)
    second = "agent-" + "f" * 32
    third = "agent-" + "0" * 32
    registry.ensure_chat(chat_id, agent_id=str(chat_id), harness=HarnessType.CLAUDE, account_id=None)

    assert registry.retired_agent_ids(chat_id) == ()

    registry.begin_segment(chat_id, agent_id=second, harness=HarnessType.CODEX, account_id=None)
    registry.begin_segment(chat_id, agent_id=third, harness=HarnessType.CLAUDE, account_id=None)

    assert registry.retired_agent_ids(chat_id) == (str(chat_id), second)


def test_retired_agent_ids_of_an_unrecorded_chat_is_empty(tmp_path: Path) -> None:
    registry = _make_registry(tmp_path)

    assert registry.retired_agent_ids(ChatId("agent-" + "1" * 32)) == ()


def test_discovery_after_a_switch_does_not_give_the_successor_a_chat_of_its_own(tmp_path: Path) -> None:
    """A discovery pass over the post-switch agent must add nothing.

    The successor's id is not the chat's id, so the bootstrap sees an agent it has no
    record *under that key* for and would record a second chat -- which then competes
    with the real one for the same active agent and makes the chat's id appear to change
    at the commit point.
    """
    registry = _make_registry(tmp_path)
    chat_id = ChatId("agent-" + "2" * 32)
    successor_id = "agent-" + "3" * 32
    registry.ensure_chat(chat_id, agent_id=str(chat_id), harness=HarnessType.CLAUDE, account_id=None)
    registry.begin_segment(chat_id, agent_id=successor_id, harness=HarnessType.CODEX, account_id="account-2")

    registry.ensure_chat(
        ChatId(successor_id), agent_id=successor_id, harness=HarnessType.CODEX, account_id="account-2"
    )

    assert registry.get(ChatId(successor_id)) is None
    assert registry.chat_id_by_active_agent() == {successor_id: chat_id}
    assert sorted(p.name for p in chats_dir_for_layout_dir(tmp_path).glob("*.json")) == [f"{chat_id}.json"]


def test_a_retired_agent_never_gets_a_chat_of_its_own_again(tmp_path: Path) -> None:
    """The retired agent's id IS the chat id on a first switch, so the ``chat_id`` guard
    already covers it; a later segment's retired agent is only covered by knowing every
    agent that has ever backed the chat."""
    registry = _make_registry(tmp_path)
    chat_id = ChatId("agent-" + "4" * 32)
    second = "agent-" + "5" * 32
    third = "agent-" + "6" * 32
    registry.ensure_chat(chat_id, agent_id=str(chat_id), harness=HarnessType.CLAUDE, account_id=None)
    registry.begin_segment(chat_id, agent_id=second, harness=HarnessType.CODEX, account_id=None)
    registry.begin_segment(chat_id, agent_id=third, harness=HarnessType.CLAUDE, account_id=None)

    registry.ensure_chat(ChatId(second), agent_id=second, harness=HarnessType.CODEX, account_id=None)

    assert registry.get(ChatId(second)) is None
    assert registry.chat_id_by_active_agent() == {third: chat_id}


def test_the_agent_guard_survives_a_reload_from_disk(tmp_path: Path) -> None:
    """The index is rebuilt on load, so a restart mid-life of a switched chat does not
    let the next discovery pass duplicate it."""
    chat_id = ChatId("agent-" + "7" * 32)
    successor_id = "agent-" + "8" * 32
    registry = _make_registry(tmp_path)
    registry.ensure_chat(chat_id, agent_id=str(chat_id), harness=HarnessType.CLAUDE, account_id=None)
    registry.begin_segment(chat_id, agent_id=successor_id, harness=HarnessType.CODEX, account_id=None)

    reloaded = _make_registry(tmp_path)
    reloaded.ensure_chat(
        ChatId(successor_id), agent_id=successor_id, harness=HarnessType.CODEX, account_id=None
    )

    assert reloaded.get(ChatId(successor_id)) is None
    assert reloaded.chat_id_by_active_agent() == {successor_id: chat_id}


def test_removing_a_chat_frees_its_agents_from_the_guard(tmp_path: Path) -> None:
    """A deleted chat must leave nothing behind that would stop a future agent id from
    being recorded -- ids are unique in practice, but a stale guard entry would silently
    swallow a chat rather than fail loudly."""
    registry = _make_registry(tmp_path)
    chat_id = ChatId("agent-" + "9" * 32)
    successor_id = "agent-" + "a" * 32
    registry.ensure_chat(chat_id, agent_id=str(chat_id), harness=HarnessType.CLAUDE, account_id=None)
    registry.begin_segment(chat_id, agent_id=successor_id, harness=HarnessType.CODEX, account_id=None)

    registry.remove(chat_id)
    registry.ensure_chat(ChatId(successor_id), agent_id=successor_id, harness=HarnessType.CODEX, account_id=None)

    record = registry.get(ChatId(successor_id))
    assert record is not None
    assert record.active_agent_id == successor_id
