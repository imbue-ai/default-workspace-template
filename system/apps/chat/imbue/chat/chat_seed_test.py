from datetime import datetime
from datetime import timezone
from pathlib import Path

from imbue.chat.chat_seed import SEED_FILENAME
from imbue.chat.chat_seed import SEED_SOURCE
from imbue.chat.chat_seed import SeedRole
from imbue.chat.chat_seed import SeedTurn
from imbue.chat.chat_seed import read_seed_events
from imbue.chat.chat_seed import seed_agent_info
from imbue.chat.chat_seed import seed_event_id
from imbue.chat.chat_seed import seed_events
from imbue.chat.chat_seed import write_seed_file
from imbue.chat.harnesses.harness_type import HarnessType
from imbue.chat.primitives import ChatId

_CHAT_ID = ChatId("agent-seeded")
_CREATED_AT = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)


def _turns() -> tuple[SeedTurn, ...]:
    return (
        SeedTurn(role=SeedRole.USER, text="Wait.. what is honest software?"),
        SeedTurn(role=SeedRole.ASSISTANT, text="Software that works **for you**."),
    )


def test_seed_events_take_the_wire_shapes_the_pages_render_with_derived_ids() -> None:
    """A user turn is a ``user_message`` and an assistant turn an ``assistant_message``, both
    marked as the seed's and stamped at the seeding time; the ids come from the position, so
    a re-read of the same file dedups."""
    user, assistant = seed_events(_CHAT_ID, _turns(), _CREATED_AT)

    assert user["type"] == "user_message" and user["content"] == "Wait.. what is honest software?"
    assert assistant["type"] == "assistant_message" and assistant["text"] == "Software that works **for you**."
    assert {user["source"], assistant["source"]} == {SEED_SOURCE}
    assert (user["event_id"], assistant["event_id"]) == (seed_event_id(_CHAT_ID, 0), seed_event_id(_CHAT_ID, 1))
    assert {user["agent_id"], assistant["agent_id"]} == {"agent-seeded"}
    assert user["timestamp"] == "2026-09-16T12:00:00+00:00"
    assert assistant["tool_calls"] == [] and assistant["is_api_error"] is False


def test_the_seed_file_reads_back_the_events_it_was_written_with(tmp_path: Path) -> None:
    events = seed_events(_CHAT_ID, _turns(), _CREATED_AT)

    path = write_seed_file(tmp_path / "chats" / _CHAT_ID, events)

    assert path == tmp_path / "chats" / _CHAT_ID / SEED_FILENAME
    assert read_seed_events(tmp_path / "chats" / _CHAT_ID) == events
    assert not any(candidate.suffix == ".tmp" for candidate in path.parent.iterdir())


def test_a_chat_folder_without_a_seed_file_reads_as_no_events(tmp_path: Path) -> None:
    assert read_seed_events(tmp_path / "nowhere") == []


def test_the_seed_pseudo_agent_is_the_chat_itself_on_the_seed_harness(tmp_path: Path) -> None:
    info = seed_agent_info(_CHAT_ID, tmp_path)

    assert info.id == "agent-seeded"
    assert info.harness is HarnessType.SEED
    assert info.agent_state_dir == tmp_path and info.claude_config_dir == tmp_path
    assert info.state == "STOPPED"
