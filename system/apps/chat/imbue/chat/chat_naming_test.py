from pathlib import Path

import pytest

from imbue.chat.agent_discovery import AgentInfo
from imbue.chat.chat_naming import CHAT_NAMING_FILENAME
from imbue.chat.chat_naming import ChatNamer
from imbue.chat.chat_naming import ChatNamingState
from imbue.chat.chat_naming import MAX_NAMING_ATTEMPTS
from imbue.chat.chat_naming import parse_generated_chat_name
from imbue.chat.chat_naming import read_chat_naming_state
from imbue.chat.harnesses.harness_type import HarnessType
from imbue.chat.harnesses.mock_one_shot_test import ScriptedOneShotCompletion
from imbue.chat.harnesses.one_shot import OneShotCompletion
from imbue.chat.harnesses.one_shot import OneShotCompletionError
from imbue.chat.models import AgentNameConflictError
from imbue.chat.models import ChatAccountBinding
from imbue.chat.primitives import ChatId
from imbue.chat.testing import InlineExecutor

_CHAT_ID = ChatId("agent-4f1c2a9e7b6d4c3a8e5f0b1d2c3e4f5a")


def _agent_info(display_name: str, tmp_path: Path) -> AgentInfo:
    return AgentInfo(
        id=str(_CHAT_ID),
        name=display_name.replace(" ", "-"),
        state="RUNNING",
        agent_state_dir=tmp_path / "state",
        claude_config_dir=tmp_path / "account",
        labels={"display_name": display_name},
        harness=HarnessType.CLAUDE,
    )


class _NamingHarness:
    """The namer's collaborators, as a test drives them: one chat, a scripted model, and the renames it asked for."""

    def __init__(self, tmp_path: Path, display_name: str, answers: list[str | OneShotCompletionError]) -> None:
        self.agent_info: AgentInfo | None = _agent_info(display_name, tmp_path)
        self.binding: ChatAccountBinding | None = ChatAccountBinding(
            harness=HarnessType.CLAUDE, account_dir=tmp_path / "account"
        )
        self.completion = ScriptedOneShotCompletion(answers)
        self.renames: list[str] = []
        # The name the chat list shows over the chat's own, as the manager would hold it
        self.shown_title: str | None = None
        self.shown_titles: list[str] = []
        self.rename_error: Exception | None = None
        # Whether the chat still wears a name nobody chose, as the manager would answer.
        self.is_placeholder_named = True
        # Whether the app shuts down while the namer waits for the chat to be listed.
        self.is_shut_down_while_waiting = False
        self.chat_files_root = tmp_path / "chats"
        self.namer = ChatNamer(
            chat_files_root=self.chat_files_root,
            get_active_agent_info=self._active_agent_info,
            resolve_chat_account_binding=lambda _chat_id: self.binding,
            show_automatic_title=self._show_title,
            clear_automatic_title=self._clear_title,
            has_placeholder_name=lambda _chat_id: self.is_placeholder_named,
            rename_placeholder_named_chat=self._rename,
            build_one_shot_completion=self._build_completion,
            agent_wait_seconds=0.0,
            executor=InlineExecutor(),
        )

    def _active_agent_info(self, _chat_id: ChatId) -> AgentInfo | None:
        if self.is_shut_down_while_waiting:
            self.namer.stop()
        return self.agent_info

    def _show_title(self, _chat_id: ChatId, title: str) -> None:
        self.shown_title = title
        self.shown_titles.append(title)

    def _clear_title(self, _chat_id: ChatId) -> None:
        self.shown_title = None

    def _rename(self, _chat_id: ChatId, name: str) -> bool:
        if self.rename_error is not None:
            raise self.rename_error
        # The name is on screen before the rename that makes it the chat's own is asked for
        assert self.shown_title == name
        self.renames.append(name)
        return True

    def _build_completion(self, harness: HarnessType) -> OneShotCompletion | None:
        return self.completion if harness is HarnessType.CLAUDE else None

    def naming_state(self) -> ChatNamingState:
        return read_chat_naming_state(self.chat_files_root / _CHAT_ID)


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("Rome trip: plan five days in May", "Rome trip: plan five days in May"),
        ('"Nginx 502: fix websockets behind proxy."', "Nginx 502: fix websockets behind proxy"),
        ("  Stripe MRR: spreadsheet by customer\n", "Stripe MRR: spreadsheet by customer"),
    ],
)
def test_parse_generated_chat_name_keeps_a_short_name_and_description(answer: str, expected: str) -> None:
    assert parse_generated_chat_name(answer) == expected


@pytest.mark.parametrize(
    "answer",
    [
        "NONE",
        "Rome trip",
        ": plan a trip",
        "Rome trip: ",
        "Rome trip: plan\nSecond line: more",
        "Rome trip: " + "a very long description " * 4,
        "!!!: ???",
    ],
)
def test_parse_generated_chat_name_refuses_an_answer_that_is_not_a_usable_name(answer: str) -> None:
    assert parse_generated_chat_name(answer) is None


def test_a_minted_chat_is_named_from_its_first_message_and_never_again(tmp_path: Path) -> None:
    harness = _NamingHarness(tmp_path, "Chat 3", ["Rome trip: plan five days in May", "Pasta: find a recipe"])

    harness.namer.consider_message(_CHAT_ID, "Help me plan 5 days in Rome in May")
    harness.namer.consider_message(_CHAT_ID, "Now find me a pasta recipe")

    assert harness.renames == ["Rome trip: plan five days in May"]
    assert harness.completion.prompts == ["Help me plan 5 days in Rome in May"]
    assert harness.naming_state().is_settled


def test_a_message_with_no_subject_leaves_the_minted_name_and_the_next_message_names_it(tmp_path: Path) -> None:
    harness = _NamingHarness(tmp_path, "Chat 3", ["NONE", "Rome trip: plan five days in May"])

    harness.namer.consider_message(_CHAT_ID, "hi")
    assert harness.renames == []
    assert harness.naming_state() == ChatNamingState(attempt_count=1, is_settled=False)

    harness.namer.consider_message(_CHAT_ID, "Help me plan 5 days in Rome in May")
    assert harness.renames == ["Rome trip: plan five days in May"]


def test_naming_stops_trying_after_the_attempt_limit(tmp_path: Path) -> None:
    answers: list[str | OneShotCompletionError] = [
        "NONE",
        OneShotCompletionError("claude -p exited 1"),
        "no colon here",
    ]
    harness = _NamingHarness(tmp_path, "Chat 3", answers)

    for message in ["hi", "hello?", "test", "Help me plan 5 days in Rome"]:
        harness.namer.consider_message(_CHAT_ID, message)

    assert len(harness.completion.prompts) == MAX_NAMING_ATTEMPTS
    assert harness.renames == []


def test_a_chat_named_any_other_way_is_left_alone_without_asking_the_model(tmp_path: Path) -> None:
    harness = _NamingHarness(tmp_path, "Planning notes", ["Greeting: say hello"])
    harness.is_placeholder_named = False

    harness.namer.consider_message(_CHAT_ID, "Hi, what can you do?")

    assert harness.completion.prompts == []
    assert harness.renames == []
    assert harness.naming_state().is_settled


def test_a_chat_named_while_its_name_is_generated_keeps_that_name_and_shows_no_other(tmp_path: Path) -> None:
    harness = _NamingHarness(tmp_path, "Chat 3", ["Rome trip: plan five days in May"])

    def rename_by_hand() -> None:
        harness.is_placeholder_named = False

    harness.completion.on_call = rename_by_hand

    harness.namer.consider_message(_CHAT_ID, "Help me plan 5 days in Rome in May")

    assert harness.completion.prompts == ["Help me plan 5 days in Rome in May"]
    assert harness.shown_titles == []
    assert harness.renames == []
    assert harness.naming_state().is_settled


def test_a_harness_with_no_one_shot_completion_keeps_its_minted_name(tmp_path: Path) -> None:
    harness = _NamingHarness(tmp_path, "Chat 3", [])
    harness.binding = ChatAccountBinding(harness=HarnessType.CODEX, account_dir=tmp_path / "account")

    harness.namer.consider_message(_CHAT_ID, "Help me plan 5 days in Rome")

    assert harness.renames == []
    assert harness.shown_titles == []
    assert not (harness.chat_files_root / _CHAT_ID / CHAT_NAMING_FILENAME).exists()


def test_a_chat_with_no_account_to_ask_on_is_not_named(tmp_path: Path) -> None:
    harness = _NamingHarness(tmp_path, "Chat 3", ["Rome trip: plan five days in May"])
    harness.binding = None

    harness.namer.consider_message(_CHAT_ID, "Help me plan 5 days in Rome")

    assert harness.completion.prompts == []
    assert harness.naming_state() == ChatNamingState()


def test_the_name_is_asked_for_before_the_chat_comes_up_and_shown_at_once(tmp_path: Path) -> None:
    harness = _NamingHarness(tmp_path, "Chat 3", ["Rome trip: plan five days in May"])
    harness.agent_info = None

    harness.namer.consider_message(_CHAT_ID, "Help me plan 5 days in Rome")

    assert harness.completion.prompts == ["Help me plan 5 days in Rome"]
    assert harness.shown_titles == ["Rome trip: plan five days in May"]
    # A chat that never came up keeps no name it was never given, and the attempt is spent
    assert harness.shown_title is None
    assert harness.renames == []
    assert harness.naming_state() == ChatNamingState(attempt_count=1, is_settled=False)


def test_a_shutdown_while_waiting_for_the_chat_spends_no_attempt(tmp_path: Path) -> None:
    harness = _NamingHarness(tmp_path, "Chat 3", ["Rome trip: plan five days in May"])
    harness.agent_info = None
    harness.is_shut_down_while_waiting = True

    harness.namer.consider_message(_CHAT_ID, "Help me plan 5 days in Rome")

    assert harness.shown_title is None
    assert harness.renames == []
    assert harness.naming_state() == ChatNamingState()


def test_a_refused_rename_costs_an_attempt(tmp_path: Path) -> None:
    harness = _NamingHarness(tmp_path, "Chat 3", ["Rome trip: plan five days in May"])
    harness.rename_error = AgentNameConflictError("A chat named 'Rome trip: plan five days in May' already exists")

    harness.namer.consider_message(_CHAT_ID, "Help me plan 5 days in Rome")

    assert harness.shown_title is None
    assert harness.naming_state() == ChatNamingState(attempt_count=1, is_settled=False)


def test_a_long_message_is_cut_before_it_is_sent_to_the_model(tmp_path: Path) -> None:
    harness = _NamingHarness(tmp_path, "Chat 3", ["Log dump: find the failing request"])

    harness.namer.consider_message(_CHAT_ID, "x" * 50_000)

    assert len(harness.completion.prompts[0]) == 3000


def test_a_stopped_namer_takes_no_more_messages(tmp_path: Path) -> None:
    harness = _NamingHarness(tmp_path, "Chat 3", ["Rome trip: plan five days in May"])

    harness.namer.stop()
    harness.namer.consider_message(_CHAT_ID, "Help me plan 5 days in Rome")

    assert harness.completion.prompts == []
