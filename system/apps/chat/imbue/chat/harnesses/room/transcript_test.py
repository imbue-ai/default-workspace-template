import io
import json
from pathlib import Path
from typing import Final

from workspace_layout.primitives import MAX_PARTICIPANT_NAME_LENGTH
from workspace_layout.primitives import ParticipantKey

from imbue.chat.harnesses.room.transcript import append_record
from imbue.chat.harnesses.room.transcript import header_record
from imbue.chat.harnesses.room.transcript import is_workspace_agent_turn
from imbue.chat.harnesses.room.transcript import person_step_record
from imbue.chat.harnesses.room.transcript import render_transcript
from imbue.chat.harnesses.room.transcript import step_as_page_event
from imbue.chat.harnesses.room.transcript import transcript_path
from imbue.chat.testing import load_room_agent_script
from imbue.mngr.agents.common_transcript_records import HeaderRecord
from imbue.mngr.agents.common_transcript_records import StepRecord

_AGENT_ID: Final[str] = "agent-00000000000000000000000000000501"


def test_a_persons_step_is_read_back_as_their_turn_with_the_composers_id() -> None:
    record = person_step_record("hello room", "m-17")

    StepRecord.model_validate(record)
    event = step_as_page_event(record)
    assert event is not None
    assert event["type"] == "user_message" and event["role"] == "user"
    assert event["content"] == "hello room" and event["message_id"] == "m-17"
    assert event["event_id"] == record["event_id"] and event["source"] == "room"
    # Who said it is the chat's annotations' to say: absent means the owner.
    assert "author" not in event


def test_the_readers_step_is_read_back_as_the_workspace_agents_turn() -> None:
    room_agent = load_room_agent_script()
    author, name, said = room_agent.author_of_typed_text("done, see the file")
    record = room_agent.step_record(said, author, name, "2026-10-23T12:00:00+00:00")

    StepRecord.model_validate(record)
    event = step_as_page_event(record)
    assert event is not None
    assert event["content"] == "done, see the file"
    assert event["author"] == "workspace-agent" and "author_name" not in event
    assert is_workspace_agent_turn(event)


def test_the_readers_prefixed_step_names_its_author_by_the_prefix() -> None:
    room_agent = load_room_agent_script()
    author, name, said = room_agent.author_of_typed_text("[from: Bob] morning")
    record = room_agent.step_record(said, author, name, "2026-10-23T12:00:00+00:00")

    event = step_as_page_event(record)
    assert event is not None
    assert (event["content"], event["author"], event["author_name"]) == ("morning", "name:Bob", "Bob")
    assert not is_workspace_agent_turn(event)


def test_the_readers_prefixed_step_cuts_the_name_to_what_a_participant_key_holds() -> None:
    room_agent = load_room_agent_script()
    assert room_agent.MAX_AUTHOR_NAME_LENGTH == MAX_PARTICIPANT_NAME_LENGTH

    author, name, said = room_agent.author_of_typed_text(f"[from: {'x' * 80} ] hi")

    assert (author, name, said) == (
        f"name:{'x' * MAX_PARTICIPANT_NAME_LENGTH}",
        "x" * MAX_PARTICIPANT_NAME_LENGTH,
        "hi",
    )
    assert ParticipantKey(author) == author


def test_the_header_is_not_a_turn_and_both_writers_mint_the_same_one() -> None:
    assert step_as_page_event(header_record(_AGENT_ID)) is None
    HeaderRecord.model_validate(header_record(_AGENT_ID))
    assert load_room_agent_script().header_record(_AGENT_ID) == header_record(_AGENT_ID)


def test_the_first_append_writes_the_header_first(tmp_path: Path) -> None:
    path = transcript_path(tmp_path / "agents" / _AGENT_ID)

    append_record(path, _AGENT_ID, person_step_record("first", "m-1"))
    append_record(path, _AGENT_ID, person_step_record("second", "m-2"))

    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert [line["type"] for line in lines] == ["header", "step", "step"]
    assert lines[0] == header_record(_AGENT_ID)


def test_the_readers_messages_arrive_one_per_line_or_one_per_paste() -> None:
    room_agent = load_room_agent_script()
    typed = b"one line\n\x1b[200~two\nlines\x1b[201~\nthree\n"

    assert list(room_agent.read_messages(io.BytesIO(typed))) == ["one line", "two\nlines", "three"]


def test_the_transcript_renders_one_line_per_message_naming_each_author() -> None:
    events = [
        {"type": "user_message", "timestamp": "t1", "content": "hi", "author": "user:bob", "author_name": "Bob"},
        {"type": "user_message", "timestamp": "t2", "content": "hello\nthere"},
        {"type": "user_message", "timestamp": "t3", "content": "done", "author": "workspace-agent"},
        {"type": "special", "timestamp": "t4", "content": "not a message"},
    ]

    assert render_transcript(events) == (
        "[t1] Bob: hi\n[t2] the owner: hello\n    there\n[t3] the workspace agent: done"
    )
