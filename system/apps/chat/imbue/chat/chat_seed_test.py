import json
from datetime import datetime
from datetime import timezone
from pathlib import Path

from loguru import logger

from imbue.chat.chat_seed import INLINE_SEED_MAX_BYTES
from imbue.chat.chat_seed import SEED_FILENAME
from imbue.chat.chat_seed import SEED_SOURCE
from imbue.chat.chat_seed import SeedRole
from imbue.chat.chat_seed import SeedTurn
from imbue.chat.chat_seed import read_seed_events
from imbue.chat.chat_seed import seed_agent_info
from imbue.chat.chat_seed import seed_context_message
from imbue.chat.chat_seed import seed_event_id
from imbue.chat.chat_seed import seed_events
from imbue.chat.chat_seed import write_seed_file
from imbue.chat.harnesses.events import DisplayKind
from imbue.chat.harnesses.harness_type import HarnessType
from imbue.chat.harnesses.message_display import classify_user_message
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


def test_a_damaged_seed_line_is_skipped_with_a_warning(tmp_path: Path) -> None:
    """One damaged line, whether it parses to something other than an object, does not parse at
    all, or is an object without the event id the loader indexes by, loses that line and nothing
    else."""
    events = seed_events(_CHAT_ID, _turns(), _CREATED_AT)
    path = write_seed_file(tmp_path, events)
    no_id = {key: value for key, value in events[1].items() if key != "event_id"}
    path.write_text(
        json.dumps(events[0]) + "\n[1, 2]\n{not json\n" + json.dumps(no_id) + "\n" + json.dumps(events[1]) + "\n"
    )
    warnings: list[str] = []
    handler_id = logger.add(lambda message: warnings.append(str(message)), level="WARNING")
    try:
        assert read_seed_events(tmp_path) == events
    finally:
        logger.remove(handler_id)
    assert len(warnings) == 3
    assert "line 2" in warnings[0] and "line 3" in warnings[1] and "not valid JSON" in warnings[1]
    assert "line 4" in warnings[2] and "no event_id" in warnings[2]


def test_a_chat_folder_without_a_seed_file_reads_as_no_events(tmp_path: Path) -> None:
    assert read_seed_events(tmp_path / "nowhere") == []


def _seeded_chat_dir(tmp_path: Path, turns: tuple[SeedTurn, ...]) -> Path:
    chat_dir = tmp_path / "chats" / _CHAT_ID
    write_seed_file(chat_dir, seed_events(_CHAT_ID, turns, _CREATED_AT))
    return chat_dir


def test_the_launch_message_carries_the_seeded_conversation_ahead_of_the_users_words(tmp_path: Path) -> None:
    """What the chat's first agent is started with: every seeded turn, in order and whole, then
    the message the user actually sent -- which on its own ("1") names nothing at all."""
    turns = (
        SeedTurn(role=SeedRole.USER, text="Wait.. what is honest software?"),
        SeedTurn(role=SeedRole.ASSISTANT, text="Software that works **for you**."),
        SeedTurn(role=SeedRole.ASSISTANT, text="### 1. Take a tour\n\n### 2. Bring a repository over"),
    )

    launch = seed_context_message(_seeded_chat_dir(tmp_path, turns), "1")

    for turn in turns:
        assert turn.text in launch
    assert launch.index(turns[0].text) < launch.index(turns[2].text)
    assert launch.endswith("\n1")


def test_the_launch_message_shows_the_user_only_what_they_typed(tmp_path: Path) -> None:
    """The two halves of the fix are one contract: what the agent reads carries the conversation,
    and what the page renders is the user's own turn. The detector table is the seam, so the
    message this module builds is classified here rather than taken on faith."""
    turns = (SeedTurn(role=SeedRole.ASSISTANT, text="### 1. Take a tour\n\n### 2. Bring a repository over"),)

    launch = seed_context_message(_seeded_chat_dir(tmp_path, turns), "1")

    decision = classify_user_message(launch)
    assert decision is not None
    assert decision.display is DisplayKind.PROMPT_WITH_CONTEXT
    assert decision.display_body == "1"


def test_a_chat_with_no_readable_seed_is_launched_with_the_users_message_alone(tmp_path: Path) -> None:
    """A seed file that is absent or damaged past reading leaves the agent where it stood before
    any of this: started on the user's words, with no half-built context block around them."""
    assert seed_context_message(tmp_path / "nowhere", "1") == "1"


def test_a_seeded_conversation_too_long_to_carry_points_at_the_file_instead(tmp_path: Path) -> None:
    """The message rides an argv, which is bounded, so past the inline limit the agent is sent to
    the seed file -- the same choice a handoff makes with an oversized summary."""
    turns = (SeedTurn(role=SeedRole.ASSISTANT, text="x" * (INLINE_SEED_MAX_BYTES + 1)),)

    launch = seed_context_message(_seeded_chat_dir(tmp_path, turns), "1")

    assert turns[0].text not in launch
    assert str(_seeded_chat_dir(tmp_path, turns) / SEED_FILENAME) in launch
    assert classify_user_message(launch) is not None


def test_the_seed_pseudo_agent_is_the_chat_itself_on_the_seed_harness(tmp_path: Path) -> None:
    info = seed_agent_info(_CHAT_ID, tmp_path)

    assert info.id == "agent-seeded"
    assert info.harness is HarnessType.SEED
    assert info.agent_state_dir == tmp_path and info.claude_config_dir == tmp_path
    assert info.state == "STOPPED"
