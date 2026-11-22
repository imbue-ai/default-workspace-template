import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from imbue.chat.suggestions import ChatSuggestions
from imbue.chat.suggestions import SUGGESTIONS_FILENAME
from imbue.chat.suggestions import Suggestion
from imbue.chat.suggestions import SuggestionAction
from imbue.chat.suggestions import SuggestionPlacement
from imbue.chat.suggestions import SuggestionSource
from imbue.chat.suggestions import read_chat_suggestions
from imbue.chat.suggestions import remove_chat_suggestions
from imbue.chat.suggestions import write_chat_suggestions


def _fixed_set() -> ChatSuggestions:
    return ChatSuggestions(
        after_event_id="agent-1:seed:3",
        source=SuggestionSource.FIXED,
        suggestions=(
            Suggestion(suggestion_id="start-describe", label="Describe what you want to make"),
            Suggestion(suggestion_id="start-features", label="See what Studio can do", is_recommended=True),
        ),
    )


def test_a_suggestion_is_not_recommended_unless_it_says_so() -> None:
    assert Suggestion(suggestion_id="a", label="Yes").is_recommended is False


def test_a_suggestion_is_a_reply_unless_it_names_an_action_and_only_a_known_action_is_one() -> None:
    assert Suggestion(suggestion_id="a", label="Yes").action is None
    coach = Suggestion.model_validate(
        {"suggestion_id": "build_app", "label": "Build an app", "action": "build_app_coach"}
    )
    assert coach.action is SuggestionAction.BUILD_APP_COACH
    assert coach.model_dump(mode="json") == {
        "suggestion_id": "build_app",
        "label": "Build an app",
        "action": "build_app_coach",
        "is_recommended": False,
    }
    with pytest.raises(ValidationError):
        Suggestion.model_validate({"suggestion_id": "a", "label": "Yes", "action": "open_the_pod_bay_doors"})


def test_the_placements_and_the_actions_are_the_wire_values_the_pages_read() -> None:
    assert [placement.value for placement in SuggestionPlacement] == ["next_step", "activity", "new_chat"]
    assert [action.value for action in SuggestionAction] == [
        "new_chat_coach",
        "phone_access",
        "build_app_coach",
        "connect_data_coach",
    ]


def test_a_suggestion_needs_an_id_and_a_label() -> None:
    with pytest.raises(ValidationError):
        Suggestion(suggestion_id="", label="Yes")
    with pytest.raises(ValidationError):
        Suggestion(suggestion_id="a", label="")


def test_the_suggestions_file_holds_the_set_in_the_shape_the_pages_read(tmp_path: Path) -> None:
    write_chat_suggestions(tmp_path / "agent-1", _fixed_set())

    assert json.loads((tmp_path / "agent-1" / SUGGESTIONS_FILENAME).read_text()) == {
        "version": 1,
        "after_event_id": "agent-1:seed:3",
        "source": "fixed",
        "suggestions": [
            {
                "suggestion_id": "start-describe",
                "label": "Describe what you want to make",
                "action": None,
                "is_recommended": False,
            },
            {
                "suggestion_id": "start-features",
                "label": "See what Studio can do",
                "action": None,
                "is_recommended": True,
            },
        ],
    }


def test_a_written_set_is_what_the_next_read_returns_and_a_later_write_replaces_it_whole(tmp_path: Path) -> None:
    write_chat_suggestions(tmp_path, _fixed_set())
    assert read_chat_suggestions(tmp_path) == _fixed_set()

    later = ChatSuggestions(
        after_event_id="evt-9",
        source=SuggestionSource.GENERATED,
        suggestions=(Suggestion(suggestion_id="s1", label="Go with your recommendations"),),
    )
    write_chat_suggestions(tmp_path, later)

    assert read_chat_suggestions(tmp_path) == later
    assert [path.name for path in tmp_path.iterdir()] == [SUGGESTIONS_FILENAME]


def test_a_file_written_without_the_optional_fields_reads_with_their_defaults(tmp_path: Path) -> None:
    (tmp_path / SUGGESTIONS_FILENAME).write_text(
        json.dumps(
            {
                "version": 1,
                "after_event_id": "evt-1",
                "source": "generated",
                "suggestions": [{"suggestion_id": "s1", "label": "Yes"}],
            }
        )
    )

    stored = read_chat_suggestions(tmp_path)

    assert stored == ChatSuggestions(
        after_event_id="evt-1",
        source=SuggestionSource.GENERATED,
        suggestions=(Suggestion(suggestion_id="s1", label="Yes", action=None, is_recommended=False),),
    )


def test_a_removed_set_reads_as_none_and_removing_none_says_so(tmp_path: Path) -> None:
    chat_dir = tmp_path / "agent-suggested"
    write_chat_suggestions(chat_dir, _fixed_set())

    assert remove_chat_suggestions(chat_dir) is True
    assert read_chat_suggestions(chat_dir) is None
    assert remove_chat_suggestions(chat_dir) is False
    assert remove_chat_suggestions(tmp_path / "agent-never") is False


def test_a_chat_with_no_suggestions_file_reads_as_none(tmp_path: Path) -> None:
    assert read_chat_suggestions(tmp_path / "nowhere") is None


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        "[]",
        json.dumps({"version": 1, "after_event_id": "evt-1", "source": "guessed", "suggestions": []}),
        json.dumps({"version": 1, "source": "fixed", "suggestions": []}),
        json.dumps({"version": 2, "after_event_id": "evt-1", "source": "fixed", "suggestions": []}),
    ],
)
def test_an_unreadable_file_or_one_from_a_newer_version_reads_as_none(
    tmp_path: Path, content: str, loguru_records: list[str]
) -> None:
    """A set this build cannot read is no set: the page shows no strip rather than a wrong one."""
    (tmp_path / SUGGESTIONS_FILENAME).write_text(content)

    assert read_chat_suggestions(tmp_path) is None
    assert any("unreadable suggestions file" in record for record in loguru_records)
