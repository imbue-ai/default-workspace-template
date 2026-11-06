from datetime import timedelta

from routines.data_types import WakeOutcome
from routines.mock_chat_app_test import ScriptedChatApp
from routines.mock_clock_test import FakeClock
from routines.primitives import ChatStatusWord
from routines.primitives import RoutineId
from routines.primitives import RunOutcome
from routines.testing import TEST_NOW
from routines.wake import NEVER_LISTED_SUMMARY
from routines.wake import NEVER_STARTED_SUMMARY
from routines.wake import NO_ANSWER_SUMMARY
from routines.wake import summary_of_message
from routines.wake import wake_skill

DIGEST = RoutineId("digest")


def _wake(
    chat_app: ScriptedChatApp, clock: FakeClock, known_chat_id: str | None, timeout: timedelta = timedelta(hours=1)
) -> tuple[WakeOutcome, list[bool], list[str]]:
    blocked: list[bool] = []
    known: list[str] = []
    outcome = wake_skill(
        routine_id=DIGEST,
        title="Digest",
        skill="digest",
        known_chat_id=known_chat_id,
        timeout=timeout,
        chat_app=chat_app,
        clock=clock,
        on_blocked=lambda: blocked.append(True),
        on_chat_known=known.append,
    )
    return outcome, blocked, known


def test_the_first_run_creates_the_chat_with_the_skill_as_its_first_message(
    chat_app: ScriptedChatApp, clock: FakeClock
) -> None:
    chat_app.script("agent-created", (ChatStatusWord.WORKING, ChatStatusWord.IDLE), "All set.")

    outcome, _, known = _wake(chat_app, clock, None)

    assert outcome.outcome is RunOutcome.SUCCEEDED
    assert outcome.summary == "All set."
    assert chat_app.created == [("Digest", DIGEST, "/digest")]
    assert chat_app.sent == [], "a fresh chat needs no clear and no second send"
    assert known == ["agent-created"]


def test_a_fresh_chat_the_app_has_not_listed_yet_is_waited_for(chat_app: ScriptedChatApp, clock: FakeClock) -> None:
    """The chat app lists a just-created agent a moment after mngr made it; that moment is not a stopped agent."""
    chat_app.script("agent-created", (ChatStatusWord.WORKING, ChatStatusWord.IDLE), "Done.")
    chat_app.unlisted_polls_by_chat["agent-created"] = 2

    outcome, _, _ = _wake(chat_app, clock, None)

    assert outcome.outcome is RunOutcome.SUCCEEDED
    assert outcome.summary == "Done."


def test_a_fresh_chat_still_unlisted_after_the_start_grace_is_a_failed_run(
    chat_app: ScriptedChatApp, clock: FakeClock
) -> None:
    outcome, _, _ = _wake(chat_app, clock, None)
    assert outcome.outcome is RunOutcome.FAILED
    assert outcome.summary == NEVER_LISTED_SUMMARY
    assert sum(clock.pauses) > 60


def test_a_chat_the_app_no_longer_knows_is_made_again(chat_app: ScriptedChatApp, clock: FakeClock) -> None:
    chat_app.script("agent-created", (ChatStatusWord.IDLE,), "ok")
    outcome, _, known = _wake(chat_app, clock, "agent-gone")
    assert outcome.outcome is RunOutcome.SUCCEEDED
    assert known == ["agent-created"]


def test_a_chat_whose_agent_still_carries_the_routine_is_taken_up_instead_of_made_again(
    chat_app: ScriptedChatApp, clock: FakeClock
) -> None:
    """A migrated automation's agent, or the chat of a routine removed and added again, is named after the
    routine: mngr would refuse a second create under that name, so the wake adopts it."""
    chat_app.script("agent-old", (ChatStatusWord.IDLE, ChatStatusWord.WORKING, ChatStatusWord.IDLE), "Done.")
    chat_app.findable[DIGEST] = "agent-old"

    outcome, _, known = _wake(chat_app, clock, None)

    assert outcome.outcome is RunOutcome.SUCCEEDED
    assert outcome.chat_id == "agent-old"
    assert known == ["agent-old"]
    assert chat_app.created == []
    assert chat_app.sent == [("agent-old", "/clear"), ("agent-old", "/digest")]


def test_a_found_agent_the_chat_app_does_not_list_is_not_taken_up(chat_app: ScriptedChatApp, clock: FakeClock) -> None:
    chat_app.script("agent-created", (ChatStatusWord.IDLE,), "ok")
    chat_app.findable[DIGEST] = "agent-unlisted"
    outcome, _, known = _wake(chat_app, clock, "agent-gone")
    assert outcome.outcome is RunOutcome.SUCCEEDED
    assert known == ["agent-created"]


def test_a_later_run_clears_the_chat_and_sends_the_skill(chat_app: ScriptedChatApp, clock: FakeClock) -> None:
    chat_app.script(
        "agent-1", (ChatStatusWord.IDLE, ChatStatusWord.WORKING, ChatStatusWord.IDLE), "Summarized 3 messages."
    )
    chat_app.event_ids["agent-1"] = "evt-9"

    outcome, _, _ = _wake(chat_app, clock, "agent-1")

    assert outcome.outcome is RunOutcome.SUCCEEDED
    assert chat_app.sent == [("agent-1", "/clear"), ("agent-1", "/digest")]
    assert chat_app.verbs == []


def test_a_create_refusal_is_a_failed_run_with_the_refusal_as_its_summary(
    chat_app: ScriptedChatApp, clock: FakeClock
) -> None:
    chat_app.is_create_refused = True
    outcome, _, _ = _wake(chat_app, clock, None)
    assert outcome.outcome is RunOutcome.FAILED
    assert "sign in" in outcome.summary
    assert outcome.chat_id is None


def test_a_chat_list_that_cannot_be_read_fails_the_run_but_keeps_the_known_chat(
    chat_app: ScriptedChatApp, clock: FakeClock
) -> None:
    chat_app.is_list_refused = True
    outcome, _, known = _wake(chat_app, clock, "agent-1")
    assert outcome.outcome is RunOutcome.FAILED
    assert "chat list" in outcome.summary
    assert outcome.chat_id == "agent-1"
    assert known == []


def test_a_chat_app_unreachable_for_a_while_mid_turn_does_not_end_the_run(
    chat_app: ScriptedChatApp, clock: FakeClock
) -> None:
    """A bounce of the chat app is not the agent's turn ending: the poll waits for it and the run still succeeds."""
    chat_app.script("agent-1", (ChatStatusWord.IDLE, ChatStatusWord.WORKING, ChatStatusWord.IDLE), "Done.")
    # The first poll finds the chat; the next three, mid-turn, cannot be answered.
    chat_app.refused_poll_attempts = {1, 2, 3}
    outcome, _, _ = _wake(chat_app, clock, "agent-1")
    assert outcome.outcome is RunOutcome.SUCCEEDED
    assert outcome.summary == "Done."


def test_a_chat_app_unreachable_past_the_grace_is_a_failed_run(chat_app: ScriptedChatApp, clock: FakeClock) -> None:
    chat_app.script("agent-1", (ChatStatusWord.IDLE, ChatStatusWord.WORKING), None)
    chat_app.refused_poll_attempts = set(range(1, 10_000))
    outcome, _, _ = _wake(chat_app, clock, "agent-1")
    assert outcome.outcome is RunOutcome.FAILED
    assert "chat list" in outcome.summary
    assert clock.now() - TEST_NOW < timedelta(minutes=3)


def test_a_stopped_agent_is_a_failed_run(chat_app: ScriptedChatApp, clock: FakeClock) -> None:
    chat_app.script("agent-1", (ChatStatusWord.IDLE, ChatStatusWord.WORKING, ChatStatusWord.STOPPED), None)
    outcome, _, _ = _wake(chat_app, clock, "agent-1")
    assert outcome.outcome is RunOutcome.FAILED
    assert outcome.summary == "The agent stopped before it finished."


def test_an_agent_waiting_on_the_user_is_reported_once_and_the_run_can_still_succeed(
    chat_app: ScriptedChatApp, clock: FakeClock
) -> None:
    chat_app.script(
        "agent-1",
        (
            ChatStatusWord.IDLE,
            ChatStatusWord.ATTENTION,
            ChatStatusWord.ATTENTION,
            ChatStatusWord.WORKING,
            ChatStatusWord.IDLE,
        ),
        "Thanks, done.",
    )
    outcome, blocked, _ = _wake(chat_app, clock, "agent-1")
    assert outcome.outcome is RunOutcome.SUCCEEDED
    assert blocked == [True]


def test_a_restarted_chat_that_comes_back_stopped_is_a_failed_run_not_a_timeout(
    chat_app: ScriptedChatApp, clock: FakeClock
) -> None:
    chat_app.script("agent-1", (ChatStatusWord.WORKING, ChatStatusWord.STOPPED), None)
    outcome, _, _ = _wake(chat_app, clock, "agent-1", timeout=timedelta(hours=2))
    assert outcome.outcome is RunOutcome.FAILED
    assert outcome.summary == "The agent stopped after the restart."
    assert chat_app.verbs == [("agent-1", "stop"), ("agent-1", "start")]
    assert 60 <= sum(clock.pauses) < 600, "the start grace, not the whole timeout"


def test_a_chat_that_never_starts_is_a_failed_run_after_a_minute(chat_app: ScriptedChatApp, clock: FakeClock) -> None:
    chat_app.script("agent-1", (ChatStatusWord.IDLE,), None)
    outcome, _, _ = _wake(chat_app, clock, "agent-1")
    assert outcome.outcome is RunOutcome.FAILED
    assert outcome.summary == NEVER_STARTED_SUMMARY
    assert sum(clock.pauses) > 60


def test_a_chat_that_worked_and_went_idle_without_answering_is_a_failed_run_not_a_timeout(
    chat_app: ScriptedChatApp, clock: FakeClock
) -> None:
    chat_app.script("agent-1", (ChatStatusWord.IDLE, ChatStatusWord.WORKING, ChatStatusWord.IDLE), None)
    outcome, _, _ = _wake(chat_app, clock, "agent-1")
    assert outcome.outcome is RunOutcome.FAILED
    assert outcome.summary == NO_ANSWER_SUMMARY
    assert 60 <= sum(clock.pauses) < 600, "the grace after the chat went idle, not the whole timeout"
    assert chat_app.verbs == []


def test_a_turn_past_the_timeout_is_interrupted(chat_app: ScriptedChatApp, clock: FakeClock) -> None:
    chat_app.script("agent-1", (ChatStatusWord.IDLE, ChatStatusWord.WORKING), None)
    outcome, _, _ = _wake(chat_app, clock, "agent-1", timeout=timedelta(minutes=2))
    assert outcome.outcome is RunOutcome.TIMED_OUT
    assert chat_app.verbs == [("agent-1", "interrupt")]


def test_an_interrupt_the_chat_app_refuses_leaves_the_run_timed_out(
    chat_app: ScriptedChatApp, clock: FakeClock
) -> None:
    """The turn may end between the last poll and the interrupt; the run still took longer than its time."""
    chat_app.script("agent-1", (ChatStatusWord.IDLE, ChatStatusWord.WORKING), None)
    chat_app.is_interrupt_refused = True
    outcome, _, _ = _wake(chat_app, clock, "agent-1", timeout=timedelta(minutes=2))
    assert outcome.outcome is RunOutcome.TIMED_OUT
    assert chat_app.verbs == [("agent-1", "interrupt")]


def test_the_summary_is_the_first_real_line_of_the_message_without_its_heading_marks() -> None:
    assert summary_of_message("\n## Done\nThe rest.") == "Done"
    assert summary_of_message("x" * 300) == "x" * 200
    assert summary_of_message(None) == ""


def test_a_clear_the_chat_app_held_for_a_handoff_is_waited_out_before_the_cursor_is_read(
    chat_app: ScriptedChatApp, clock: FakeClock
) -> None:
    """The cursor must sit after the clear, so a held clear is given time to reach the new agent and run."""
    chat_app.script(
        "agent-1",
        (
            ChatStatusWord.IDLE,
            ChatStatusWord.WORKING,
            ChatStatusWord.IDLE,
            ChatStatusWord.WORKING,
            ChatStatusWord.IDLE,
        ),
        "Summarized 3 messages.",
    )
    chat_app.held_sends_left = 1

    outcome, _, _ = _wake(chat_app, clock, "agent-1")

    assert outcome.outcome is RunOutcome.SUCCEEDED
    assert chat_app.sent == [("agent-1", "/clear"), ("agent-1", "/digest")]
    # The first poll listed the chat; the next two watched the held clear run and settle before the command went.
    assert chat_app.polls_by_chat["agent-1"] == 5


def test_a_held_command_gets_a_longer_start_grace(chat_app: ScriptedChatApp, clock: FakeClock) -> None:
    """The handoff delivers the command once the chat has its new agent, which takes longer than a send."""
    chat_app.script("agent-1", (ChatStatusWord.IDLE,) * 100 + (ChatStatusWord.WORKING, ChatStatusWord.IDLE), "Done.")
    chat_app.held_sends_left = 2

    outcome, _, _ = _wake(chat_app, clock, "agent-1")

    assert outcome.outcome is RunOutcome.SUCCEEDED


def test_a_held_clear_whose_chat_never_comes_back_is_a_failed_run(chat_app: ScriptedChatApp, clock: FakeClock) -> None:
    chat_app.script("agent-1", (ChatStatusWord.IDLE, ChatStatusWord.STOPPED), None)
    chat_app.held_sends_left = 1

    outcome, _, _ = _wake(chat_app, clock, "agent-1")

    assert outcome.outcome is RunOutcome.FAILED
    assert chat_app.sent == [("agent-1", "/clear")]
