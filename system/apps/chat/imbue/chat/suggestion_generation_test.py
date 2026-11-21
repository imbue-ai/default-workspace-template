import re
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

import pytest

from imbue.chat.activity_state import ActivityState
from imbue.chat.chat_seed import SeedRole
from imbue.chat.chat_seed import SeedTurn
from imbue.chat.chat_settings import ChatSettings
from imbue.chat.harnesses.harness_type import HarnessType
from imbue.chat.harnesses.mock_one_shot_test import ScriptedOneShotCompletion
from imbue.chat.harnesses.one_shot import OneShotCompletion
from imbue.chat.harnesses.one_shot import OneShotCompletionError
from imbue.chat.harnesses.one_shot import OneShotModelTier
from imbue.chat.models import ChatAccountBinding
from imbue.chat.models import ChatSnapshot
from imbue.chat.models import HandoffPhase
from imbue.chat.models import HandoffState
from imbue.chat.models import QueuedMessageState
from imbue.chat.models import TransitionKind
from imbue.chat.primitives import ChatId
from imbue.chat.primitives import ChatKind
from imbue.chat.primitives import ChatStatus
from imbue.chat.suggestion_generation import ConversationEnd
from imbue.chat.suggestion_generation import SUGGESTION_GENERATION_SYSTEM_PROMPT
from imbue.chat.suggestion_generation import SuggestionGenerator
from imbue.chat.suggestion_generation import conversation_turns
from imbue.chat.suggestion_generation import parse_generated_suggestions
from imbue.chat.suggestion_generation import read_conversation_end
from imbue.chat.suggestion_generation import reason_chat_gets_no_suggestions
from imbue.chat.suggestion_generation import suggestion_generation_prompt
from imbue.chat.suggestions import ChatSuggestions
from imbue.chat.suggestions import Suggestion
from imbue.chat.suggestions import SuggestionSource
from imbue.chat.suggestions import read_chat_suggestions
from imbue.chat.suggestions import write_chat_suggestions
from imbue.chat.testing import InlineExecutor
from imbue.chat.testing import make_chat_snapshot
from imbue.imbue_common.model_update import to_update

_CHAT_ID = ChatId("agent-7c1e5b2a9d4f4e6b8a3c0d9e1f2a3b4c")
_STARTED_AT = datetime(2026, 11, 21, 10, 0, tzinfo=timezone.utc)


def _user(event_id: str, content: str, **extra: Any) -> dict[str, Any]:
    return {"type": "user_message", "event_id": event_id, "content": content, **extra}


def _assistant(event_id: str, text: str, **extra: Any) -> dict[str, Any]:
    return {"type": "assistant_message", "event_id": event_id, "text": text, **extra}


def _tool_result(event_id: str) -> dict[str, Any]:
    return {"type": "tool_result", "event_id": event_id, "tool_call_id": "call-1"}


def _labels(answer: str) -> list[str]:
    return [suggestion.label for suggestion in parse_generated_suggestions(answer)]


def test_parse_generated_suggestions_reads_each_element_in_order_with_the_first_recommended() -> None:
    answer = '<suggestion recommended="true">Yes, add reminders</suggestion>\n<suggestion>Show me the app</suggestion>'

    assert parse_generated_suggestions(answer) == (
        Suggestion(suggestion_id="generated-1", label="Yes, add reminders", is_recommended=True),
        Suggestion(suggestion_id="generated-2", label="Show me the app", is_recommended=False),
    )


def test_parse_generated_suggestions_ignores_everything_that_is_not_a_suggestion_element() -> None:
    answer = (
        "Draft list:\n1. Yes\n2. No\n\nCritique: too terse.\n\n"
        "<suggestion>Yes, go ahead</suggestion>\nSome closing remark.\n<SUGGESTION>Not yet</SUGGESTION>"
    )

    assert _labels(answer) == ["Yes, go ahead", "Not yet"]


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("", []),
        ("I have no suggestions for this one.", []),
        ("<suggestion>   </suggestion><suggestion></suggestion>", []),
        ("<suggestion>  Trim me  </suggestion>", ["Trim me"]),
        ("<suggestion>Two\nlines</suggestion><suggestion>One line</suggestion>", ["One line"]),
        (f"<suggestion>{'x' * 81}</suggestion><suggestion>{'y' * 80}</suggestion>", ["y" * 80]),
        (
            "<suggestion>Go ahead</suggestion><suggestion>go AHEAD</suggestion><suggestion>Stop</suggestion>",
            ["Go ahead", "Stop"],
        ),
    ],
)
def test_parse_generated_suggestions_keeps_only_distinct_one_line_labels_of_a_usable_length(
    answer: str, expected: list[str]
) -> None:
    assert _labels(answer) == expected


def test_parse_generated_suggestions_keeps_at_most_ten() -> None:
    answer = "".join(f"<suggestion>Option {number}</suggestion>" for number in range(1, 14))

    assert _labels(answer) == [f"Option {number}" for number in range(1, 11)]


def test_parse_generated_suggestions_honours_recommended_on_the_first_kept_suggestion_only() -> None:
    marked_second = '<suggestion>First</suggestion><suggestion recommended="true">Second</suggestion>'
    first_dropped = f'<suggestion recommended="true">{"x" * 81}</suggestion><suggestion>Kept</suggestion>'
    marked_false = '<suggestion recommended="false">First</suggestion>'

    assert [suggestion.is_recommended for suggestion in parse_generated_suggestions(marked_second)] == [False, False]
    assert [suggestion.is_recommended for suggestion in parse_generated_suggestions(first_dropped)] == [False]
    assert [suggestion.is_recommended for suggestion in parse_generated_suggestions(marked_false)] == [False]


def test_the_conversation_ends_on_its_last_reply_past_what_the_page_draws_nothing_for() -> None:
    events = [
        _user("u1", "Build me a plant tracker"),
        _assistant("a1", "Working on it."),
        _tool_result("t1"),
        _assistant("a2", "  It is built. Want reminders?  "),
        _tool_result("t2"),
        _user("u2", "<system-reminder>internal</system-reminder>", display="hidden"),
        _user("u3", "Context was compacted", display="status"),
    ]

    end = read_conversation_end(events)

    assert end == ConversationEnd(
        reply_event_id="a2",
        reply_text="It is built. Want reminders?",
        is_reply_a_failure=False,
        earlier_turns=(
            SeedTurn(role=SeedRole.USER, text="Build me a plant tracker"),
            SeedTurn(role=SeedRole.ASSISTANT, text="Working on it."),
        ),
    )


@pytest.mark.parametrize(
    "later_message",
    [
        _user("u2", "And make it green"),
        _user("u2", "ls", display="person_chip"),
        _user("u2", "Stop hook feedback:\nkeep going", display="chip", display_label="Stop hook feedback"),
        _user("u2", "Background task completed", display="notice"),
    ],
)
def test_a_reply_somebody_has_spoken_after_is_not_the_conversations_end(later_message: dict[str, Any]) -> None:
    events = [_user("u1", "Build it"), _assistant("a1", "Built."), later_message]

    assert read_conversation_end(events) is None


def test_a_transcript_with_no_reply_has_no_end_to_suggest_under() -> None:
    assert read_conversation_end([]) is None
    assert read_conversation_end([_tool_result("t1")]) is None


def test_the_end_says_when_the_last_reply_is_a_failure_or_says_nothing() -> None:
    failed = read_conversation_end([_assistant("a1", "API Error: overloaded", is_api_error=True)])
    signed_out = read_conversation_end([_assistant("a1", "Please run /login", is_auth_error=True)])
    silent = read_conversation_end([_assistant("a1", "", tool_calls=[{"tool_call_id": "call-1"}])])

    assert failed is not None and failed.is_reply_a_failure
    assert signed_out is not None and signed_out.is_reply_a_failure
    assert silent is not None and silent.reply_text == "" and not silent.is_reply_a_failure


def test_the_conversation_is_what_each_side_said_as_the_page_shows_it() -> None:
    events = [
        _user(
            "u1",
            "<chat-seed-context>the welcome</chat-seed-context>\nGet ideas",
            display="prompt_with_context",
            display_body="Get ideas",
        ),
        _user("u2", "Base directory for this skill: /x", display="skill_expansion"),
        _assistant("a1", "Let me look."),
        _tool_result("t1"),
        _assistant("a2", ""),
        _assistant("a3", "API Error: overloaded", is_api_error=True),
        _assistant("a4", "Here are three ideas."),
        _user("u3", "[from: Sam] The second one", display="prompt_with_context", display_body="The second one"),
        _user("u4", "Make it blue\n\n<dictation-note>\nParts were dictated.\n</dictation-note>"),
        _user("u5", "!ls", display="person_chip"),
    ]

    assert conversation_turns(events) == (
        SeedTurn(role=SeedRole.USER, text="Get ideas"),
        SeedTurn(role=SeedRole.ASSISTANT, text="Let me look.\n\nHere are three ideas."),
        SeedTurn(role=SeedRole.USER, text="The second one\n\nMake it blue"),
    )


def _end(reply_text: str, earlier_turns: tuple[SeedTurn, ...]) -> ConversationEnd:
    return ConversationEnd(
        reply_event_id="a-last", reply_text=reply_text, is_reply_a_failure=False, earlier_turns=earlier_turns
    )


def test_the_prompt_is_the_reply_under_the_turns_before_it_as_tagged_turns() -> None:
    end = _end(
        "Built. Want reminders?",
        (SeedTurn(role=SeedRole.USER, text="Build me a plant tracker"),),
    )

    assert suggestion_generation_prompt(end) == (
        '<turn speaker="user">\nBuild me a plant tracker\n</turn>\n'
        '<turn speaker="assistant">\nBuilt. Want reminders?\n</turn>'
    )


def test_the_prompt_carries_the_last_six_turns_the_reply_among_them() -> None:
    earlier = tuple(
        SeedTurn(role=SeedRole.USER if number % 2 else SeedRole.ASSISTANT, text=f"turn {number}")
        for number in range(1, 9)
    )

    prompt = suggestion_generation_prompt(_end("the reply", earlier))

    assert prompt.count("<turn ") == 6
    assert "turn 3\n" not in prompt
    assert prompt.index("turn 4") < prompt.index("turn 8") < prompt.index("the reply")


def test_a_long_conversation_is_cut_from_its_oldest_end_and_the_reply_is_kept_whole() -> None:
    earlier = (
        SeedTurn(role=SeedRole.USER, text="oldest " + "a" * 5000),
        SeedTurn(role=SeedRole.ASSISTANT, text="middle-start " + "b" * 5000 + " middle-end"),
        SeedTurn(role=SeedRole.USER, text="newest " + "c" * 3000),
    )
    reply = "reply-start " + "d" * 6000 + " reply-end"

    prompt = suggestion_generation_prompt(_end(reply, earlier))

    assert reply in prompt
    assert "newest " in prompt
    assert "oldest" not in prompt
    assert "middle-start" not in prompt and "middle-end" in prompt
    turn_texts = re.findall(r'<turn speaker="[a-z]+">\n(.*?)\n</turn>', prompt, re.DOTALL)
    assert turn_texts[0].startswith("[...]\n")
    assert sum(len(text) for text in turn_texts) - len("[...]\n") == 12_000


def test_a_reply_longer_than_the_limit_goes_alone_and_whole() -> None:
    reply = "r" * 13_000

    prompt = suggestion_generation_prompt(_end(reply, (SeedTurn(role=SeedRole.USER, text="Explain it all"),)))

    assert prompt == f'<turn speaker="assistant">\n{reply}\n</turn>'


def _idle_snapshot() -> ChatSnapshot:
    return make_chat_snapshot(str(_CHAT_ID))


def _with_status(status: ChatStatus) -> ChatSnapshot:
    snapshot = _idle_snapshot()
    return snapshot.model_copy_update(to_update(snapshot.field_ref().status, status))


def _room() -> ChatSnapshot:
    snapshot = _idle_snapshot()
    return snapshot.model_copy_update(to_update(snapshot.field_ref().kind, ChatKind.ROOM))


def _started_by_an_agent() -> ChatSnapshot:
    snapshot = _idle_snapshot()
    return snapshot.model_copy_update(to_update(snapshot.field_ref().labels, {"agent_created": "true"}))


def _switching_agents() -> ChatSnapshot:
    snapshot = _idle_snapshot()
    handoff = HandoffState(
        kind=TransitionKind.HANDOFF,
        phase=HandoffPhase.SUMMARIZING,
        started_at=_STARTED_AT,
        target_lane="codex",
        target_account_id="acct-2",
        target_harness=HarnessType.CODEX,
        target_label="Codex",
    )
    return snapshot.model_copy_update(to_update(snapshot.field_ref().handoff, handoff))


def _with_a_message_queued() -> ChatSnapshot:
    snapshot = _idle_snapshot()
    agent = snapshot.active_agent
    queued = (QueuedMessageState(queued_id="q-1", content="and another thing", timestamp=_STARTED_AT.isoformat()),)
    return snapshot.model_copy_update(
        to_update(
            snapshot.field_ref().active_agent,
            agent.model_copy_update(to_update(agent.field_ref().queued_messages, queued)),
        )
    )


def _connecting() -> ChatSnapshot:
    snapshot = _idle_snapshot()
    agent = snapshot.active_agent
    return snapshot.model_copy_update(
        to_update(
            snapshot.field_ref().active_agent,
            agent.model_copy_update(to_update(agent.field_ref().is_connecting, True)),
        )
    )


def _resending_after_sign_in() -> ChatSnapshot:
    snapshot = _idle_snapshot()
    agent = snapshot.active_agent
    return snapshot.model_copy_update(
        to_update(
            snapshot.field_ref().active_agent,
            agent.model_copy_update(to_update(agent.field_ref().is_resending_after_sign_in, True)),
        )
    )


def test_an_idle_agent_chat_with_the_switch_on_gets_suggestions() -> None:
    assert reason_chat_gets_no_suggestions(_idle_snapshot(), ChatSettings()) is None


@pytest.mark.parametrize(
    ("snapshot", "reason_words"),
    [
        (None, "not a chat the app lists"),
        (_room(), "a room"),
        (_started_by_an_agent(), "an agent started it"),
        (_with_status(ChatStatus.WORKING), "working"),
        (_with_status(ChatStatus.BACKGROUND), "background"),
        (_with_status(ChatStatus.ATTENTION), "attention"),
        (_with_status(ChatStatus.STOPPED), "stopped"),
        (_with_status(ChatStatus.ERROR), "error"),
        (_switching_agents(), "not waiting on its user"),
        (_with_a_message_queued(), "on its way"),
        (_connecting(), "on its way"),
        (_resending_after_sign_in(), "on its way"),
    ],
)
def test_a_chat_not_waiting_on_a_person_gets_no_suggestions(snapshot: ChatSnapshot | None, reason_words: str) -> None:
    reason = reason_chat_gets_no_suggestions(snapshot, ChatSettings())

    assert reason is not None and reason_words in reason


def test_no_chat_gets_suggestions_while_the_switch_is_off() -> None:
    reason = reason_chat_gets_no_suggestions(_idle_snapshot(), ChatSettings(is_next_step_suggestions_enabled=False))

    assert reason is not None and "switched off" in reason


_TWO_SUGGESTIONS = (
    '<suggestion recommended="true">Yes, add reminders</suggestion>\n<suggestion>Show me the app</suggestion>'
)


class _GenerationHarness:
    """The generator's collaborators, as a test drives them: one chat, its transcript, and a scripted model."""

    def __init__(self, tmp_path: Path, answers: list[str | OneShotCompletionError]) -> None:
        self.chat_files_root = tmp_path / "chats"
        self.settings = ChatSettings()
        self.snapshot: ChatSnapshot | None = make_chat_snapshot(str(_CHAT_ID))
        # None stands for a chat with no transcript to read.
        self.events: list[dict[str, Any]] | None = [
            _user("u1", "Build me a plant tracker"),
            _assistant("a1", "Built. Want reminders?"),
        ]
        self.binding: ChatAccountBinding | None = ChatAccountBinding(
            harness=HarnessType.CLAUDE, account_dir=tmp_path / "account"
        )
        self.completion = ScriptedOneShotCompletion(answers)
        self.is_harness_answering_questions = True
        self.pushed_chat_ids: list[ChatId] = []
        # The reply the generator held as pending at each push, which is what a page's read on that push is answered.
        self.pending_reply_event_id_at_each_push: list[str | None] = []
        self.generator = SuggestionGenerator(
            chat_files_root=self.chat_files_root,
            read_chat_settings=lambda: self.settings,
            get_chat_snapshot=lambda _chat_id: self.snapshot,
            read_tail_events=lambda _chat_id, _limit: self.events,
            resolve_chat_account_binding=lambda _chat_id: self.binding,
            build_one_shot_completion=self._build_completion,
            broadcast_suggestions_updated=self._push,
            executor=InlineExecutor(),
        )

    def _push(self, chat_id: ChatId) -> None:
        self.pushed_chat_ids.append(chat_id)
        self.pending_reply_event_id_at_each_push.append(self.generator.get_pending_reply_event_id(chat_id))

    def _build_completion(self, _harness: HarnessType) -> OneShotCompletion | None:
        return self.completion if self.is_harness_answering_questions else None

    @property
    def chat_dir(self) -> Path:
        return self.chat_files_root / _CHAT_ID

    def stored(self) -> ChatSuggestions | None:
        return read_chat_suggestions(self.chat_dir)

    def store_a_set_after(self, event_id: str) -> ChatSuggestions:
        earlier = ChatSuggestions(
            after_event_id=event_id,
            source=SuggestionSource.FIXED,
            suggestions=(Suggestion(suggestion_id="start-describe", label="Describe what you want to make"),),
        )
        write_chat_suggestions(self.chat_dir, earlier)
        return earlier

    def end_turn(self) -> None:
        self.generator.consider_turn_end(_CHAT_ID)


def test_a_turns_end_stores_the_replies_a_mid_sized_model_suggests_and_tells_the_pages(tmp_path: Path) -> None:
    harness = _GenerationHarness(tmp_path, [_TWO_SUGGESTIONS])

    harness.end_turn()

    assert harness.stored() == ChatSuggestions(
        after_event_id="a1",
        source=SuggestionSource.GENERATED,
        suggestions=(
            Suggestion(suggestion_id="generated-1", label="Yes, add reminders", is_recommended=True),
            Suggestion(suggestion_id="generated-2", label="Show me the app"),
        ),
    )
    assert harness.pushed_chat_ids == [_CHAT_ID]
    assert harness.completion.system_prompts == [SUGGESTION_GENERATION_SYSTEM_PROMPT]
    assert harness.completion.prompts == [
        '<turn speaker="user">\nBuild me a plant tracker\n</turn>\n'
        '<turn speaker="assistant">\nBuilt. Want reminders?\n</turn>'
    ]
    assert harness.completion.model_tiers == [OneShotModelTier.MEDIUM]
    assert harness.completion.thinking_token_allowances == [4000]


def test_a_set_replaces_the_one_an_earlier_reply_had(tmp_path: Path) -> None:
    harness = _GenerationHarness(tmp_path, [_TWO_SUGGESTIONS])
    harness.store_a_set_after("a0")

    harness.end_turn()

    stored = harness.stored()
    assert stored is not None and stored.after_event_id == "a1" and stored.source is SuggestionSource.GENERATED


def test_an_answer_with_no_suggestions_removes_the_earlier_replys_set(tmp_path: Path) -> None:
    harness = _GenerationHarness(tmp_path, ["Nothing sensible to suggest here."])
    harness.store_a_set_after("a0")

    harness.end_turn()

    assert harness.stored() is None
    assert harness.pushed_chat_ids == [_CHAT_ID]


def test_an_answer_with_no_suggestions_tells_the_pages_nothing_when_no_set_was_stored(tmp_path: Path) -> None:
    harness = _GenerationHarness(tmp_path, [""])

    harness.end_turn()

    assert harness.stored() is None
    assert harness.pushed_chat_ids == []


def test_a_failed_call_leaves_no_set_and_is_not_tried_again_for_the_same_reply(tmp_path: Path) -> None:
    harness = _GenerationHarness(tmp_path, [OneShotCompletionError("claude -p exited 1"), _TWO_SUGGESTIONS])
    harness.store_a_set_after("a0")

    harness.end_turn()
    harness.end_turn()

    assert harness.stored() is None
    assert harness.pushed_chat_ids == [_CHAT_ID]
    assert len(harness.completion.prompts) == 1


def test_the_next_reply_is_asked_about_after_a_failed_call(tmp_path: Path) -> None:
    harness = _GenerationHarness(tmp_path, [OneShotCompletionError("timed out"), _TWO_SUGGESTIONS])
    harness.end_turn()
    assert harness.events is not None
    harness.events.extend([_user("u2", "Yes please"), _assistant("a2", "Reminders are on.")])

    harness.end_turn()

    stored = harness.stored()
    assert stored is not None and stored.after_event_id == "a2"


def test_a_reply_that_already_has_its_set_is_not_asked_about(tmp_path: Path) -> None:
    harness = _GenerationHarness(tmp_path, [_TWO_SUGGESTIONS])
    fixed = harness.store_a_set_after("a1")

    harness.end_turn()

    assert harness.stored() == fixed
    assert harness.completion.prompts == []
    assert harness.pushed_chat_ids == []


def _switch_off(harness: _GenerationHarness) -> None:
    harness.settings = ChatSettings(is_next_step_suggestions_enabled=False)


def _make_a_room(harness: _GenerationHarness) -> None:
    harness.snapshot = _room()


def _leave_it_waiting_on_a_background_task(harness: _GenerationHarness) -> None:
    harness.snapshot = _with_status(ChatStatus.BACKGROUND)


def _say_something_more(harness: _GenerationHarness) -> None:
    assert harness.events is not None
    harness.events.append(_user("u2", "Actually, make it a game"))


def _end_on_the_users_message(harness: _GenerationHarness) -> None:
    assert harness.events is not None
    harness.events.append(_user("u2", "/compact"))


def _end_on_a_provider_failure(harness: _GenerationHarness) -> None:
    assert harness.events is not None
    harness.events.append(_assistant("a2", "API Error: overloaded", is_api_error=True))


def _end_on_a_reply_that_says_nothing(harness: _GenerationHarness) -> None:
    assert harness.events is not None
    harness.events.append(_assistant("a2", ""))


def _run_on_a_harness_with_no_one_shot_call(harness: _GenerationHarness) -> None:
    harness.is_harness_answering_questions = False


def _lose_its_account(harness: _GenerationHarness) -> None:
    harness.binding = None


@pytest.mark.parametrize(
    "arrange",
    [
        _switch_off,
        _make_a_room,
        _leave_it_waiting_on_a_background_task,
        _end_on_the_users_message,
        _end_on_a_provider_failure,
        _end_on_a_reply_that_says_nothing,
        _run_on_a_harness_with_no_one_shot_call,
        _lose_its_account,
    ],
)
def test_a_turn_end_that_asks_no_model_still_removes_the_earlier_replys_set(tmp_path: Path, arrange: Any) -> None:
    harness = _GenerationHarness(tmp_path, [_TWO_SUGGESTIONS])
    harness.store_a_set_after("a0")
    arrange(harness)

    harness.end_turn()

    assert harness.completion.prompts == []
    assert harness.stored() is None
    assert harness.pushed_chat_ids == [_CHAT_ID]


def test_a_chat_with_no_transcript_to_read_is_left_as_it_is(tmp_path: Path) -> None:
    harness = _GenerationHarness(tmp_path, [_TWO_SUGGESTIONS])
    earlier = harness.store_a_set_after("a0")
    harness.events = None

    harness.end_turn()

    assert harness.stored() == earlier
    assert harness.completion.prompts == []
    assert harness.pushed_chat_ids == []


def test_an_answer_is_dropped_when_somebody_spoke_while_the_model_answered(tmp_path: Path) -> None:
    harness = _GenerationHarness(tmp_path, [_TWO_SUGGESTIONS])
    earlier = harness.store_a_set_after("a0")
    harness.completion.on_call = lambda: _say_something_more(harness)

    harness.end_turn()

    assert harness.stored() == earlier
    assert harness.pushed_chat_ids == []


def test_an_answer_is_dropped_when_a_turn_started_while_the_model_answered(tmp_path: Path) -> None:
    harness = _GenerationHarness(tmp_path, [_TWO_SUGGESTIONS])

    def start_a_turn() -> None:
        working = _with_status(ChatStatus.WORKING)
        agent = working.active_agent
        harness.snapshot = working.model_copy_update(
            to_update(
                working.field_ref().active_agent,
                agent.model_copy_update(to_update(agent.field_ref().activity_state, ActivityState.THINKING)),
            )
        )

    harness.completion.on_call = start_a_turn

    harness.end_turn()

    assert harness.stored() is None
    assert harness.pushed_chat_ids == []


def test_an_answer_is_dropped_when_the_switch_went_off_while_the_model_answered(tmp_path: Path) -> None:
    harness = _GenerationHarness(tmp_path, [_TWO_SUGGESTIONS])
    harness.completion.on_call = lambda: _switch_off(harness)

    harness.end_turn()

    assert harness.stored() is None
    assert harness.pushed_chat_ids == []


def test_a_set_is_pending_for_its_reply_only_while_the_model_answers(tmp_path: Path) -> None:
    harness = _GenerationHarness(tmp_path, [_TWO_SUGGESTIONS])
    pending_during_call: list[str | None] = []
    harness.completion.on_call = lambda: pending_during_call.append(
        harness.generator.get_pending_reply_event_id(_CHAT_ID)
    )
    assert harness.generator.get_pending_reply_event_id(_CHAT_ID) is None

    harness.end_turn()

    assert pending_during_call == ["a1"]
    assert harness.pending_reply_event_id_at_each_push == [None]
    assert harness.generator.get_pending_reply_event_id(_CHAT_ID) is None


def test_a_turn_that_ends_while_the_last_one_is_settled_gets_its_own_set_afterwards(tmp_path: Path) -> None:
    harness = _GenerationHarness(tmp_path, [_TWO_SUGGESTIONS, "<suggestion>Thanks, that is all</suggestion>"])

    def end_the_next_turn() -> None:
        harness.completion.on_call = None
        assert harness.events is not None
        harness.events.extend([_user("u2", "Yes please"), _assistant("a2", "Reminders are on.")])
        harness.end_turn()

    harness.completion.on_call = end_the_next_turn

    harness.end_turn()

    stored = harness.stored()
    assert stored is not None and stored.after_event_id == "a2"
    assert [suggestion.label for suggestion in stored.suggestions] == ["Thanks, that is all"]
    assert len(harness.completion.prompts) == 2
    assert harness.pushed_chat_ids == [_CHAT_ID]


def test_a_stopped_generator_takes_no_more_turn_ends(tmp_path: Path) -> None:
    harness = _GenerationHarness(tmp_path, [_TWO_SUGGESTIONS])
    harness.generator.stop()

    harness.end_turn()

    assert harness.completion.prompts == []
    assert harness.stored() is None


def test_an_answer_arriving_after_the_generator_stopped_is_not_stored(tmp_path: Path) -> None:
    harness = _GenerationHarness(tmp_path, [_TWO_SUGGESTIONS])
    harness.completion.on_call = harness.generator.stop

    harness.end_turn()

    assert harness.stored() is None
    assert harness.pushed_chat_ids == []
