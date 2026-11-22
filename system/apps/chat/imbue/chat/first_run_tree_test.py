from pathlib import Path

import pytest

from imbue.chat.first_run_tree import FIRST_RUN_TREE_FILENAME
from imbue.chat.first_run_tree import FirstRunTreeStep
from imbue.chat.first_run_tree import fixed_suggestions
from imbue.chat.first_run_tree import idea_names
from imbue.chat.first_run_tree import next_first_run_tree_step
from imbue.chat.first_run_tree import read_first_run_tree_step
from imbue.chat.first_run_tree import write_first_run_tree_step
from imbue.chat.suggestions import Suggestion

_IDEAS_REPLY = (
    "Here are some ideas for what you might want to build for **Sales and Money** with your Gmail and Stripe:\n"
    "\n"
    "- **Sales CRM with follow-up reminders**: a deal pipeline with a Follow up today view (Gmail).\n"
    "- **Subscription tracker**: every recurring charge in one place, with a warning before each renewal (Stripe).\n"
    "- **Outreach tracker**: who you wrote to, who answered, and who to nudge (Gmail).\n"
)


@pytest.mark.parametrize(
    ("step", "message", "expected"),
    [
        (FirstRunTreeStep.START, "Get ideas for what to make", FirstRunTreeStep.CATEGORIES),
        (FirstRunTreeStep.START, "  Get ideas for what to make\n", FirstRunTreeStep.CATEGORIES),
        (FirstRunTreeStep.START, "Describe what you want to make", FirstRunTreeStep.DONE),
        (FirstRunTreeStep.START, "See what Studio can do", FirstRunTreeStep.DONE),
        (FirstRunTreeStep.START, "Build me a plant tracker", FirstRunTreeStep.DONE),
        (FirstRunTreeStep.START, "A plant tracker\nGet ideas for what to make", FirstRunTreeStep.DONE),
        (FirstRunTreeStep.CATEGORIES, "Sales, Money, ", FirstRunTreeStep.CONNECTORS),
        (FirstRunTreeStep.CATEGORIES, "what is this?", FirstRunTreeStep.CONNECTORS),
        (FirstRunTreeStep.CONNECTORS, "Gmail, Stripe, ", FirstRunTreeStep.IDEAS),
        (FirstRunTreeStep.CONNECTORS, "Show me some ideas", FirstRunTreeStep.IDEAS),
        (FirstRunTreeStep.IDEAS, "More ideas", FirstRunTreeStep.IDEAS),
        (FirstRunTreeStep.IDEAS, " More ideas ", FirstRunTreeStep.IDEAS),
        (FirstRunTreeStep.IDEAS, "Subscription tracker", FirstRunTreeStep.DONE),
        (FirstRunTreeStep.IDEAS, "Something else", FirstRunTreeStep.DONE),
        (FirstRunTreeStep.IDEAS, "more ideas please", FirstRunTreeStep.DONE),
        (FirstRunTreeStep.DONE, "Get ideas for what to make", FirstRunTreeStep.DONE),
        (FirstRunTreeStep.DONE, "More ideas", FirstRunTreeStep.DONE),
    ],
)
def test_a_message_moves_the_tree_by_the_step_it_is_said_at(
    step: FirstRunTreeStep, message: str, expected: FirstRunTreeStep
) -> None:
    assert next_first_run_tree_step(step, message) is expected


@pytest.mark.parametrize("step", list(FirstRunTreeStep))
def test_a_written_step_is_read_back_whatever_the_chat_awaits(tmp_path: Path, step: FirstRunTreeStep) -> None:
    write_first_run_tree_step(tmp_path, step)

    assert read_first_run_tree_step(tmp_path, is_awaiting_first_agent=True) is step
    assert read_first_run_tree_step(tmp_path, is_awaiting_first_agent=False) is step
    assert (tmp_path / FIRST_RUN_TREE_FILENAME).read_text() == f'{{\n  "step": "{step}",\n  "version": 1\n}}\n'


def test_a_chat_with_no_step_recorded_is_at_the_start_only_while_nobody_has_answered_it(tmp_path: Path) -> None:
    """A first conversation from before the step was recorded is never put back into a tree."""
    assert read_first_run_tree_step(tmp_path, is_awaiting_first_agent=True) is FirstRunTreeStep.START
    assert read_first_run_tree_step(tmp_path, is_awaiting_first_agent=False) is FirstRunTreeStep.DONE


@pytest.mark.parametrize(
    "content", ["not json", '{"version": 1, "step": "somewhere"}', '{"version": 2, "step": "ideas"}']
)
def test_a_step_file_that_cannot_be_read_counts_as_none_recorded(tmp_path: Path, content: str) -> None:
    (tmp_path / FIRST_RUN_TREE_FILENAME).write_text(content)

    assert read_first_run_tree_step(tmp_path, is_awaiting_first_agent=False) is FirstRunTreeStep.DONE


def test_the_ideas_of_a_reply_are_the_bold_openings_of_its_list_items_in_order() -> None:
    assert idea_names(_IDEAS_REPLY) == (
        "Sales CRM with follow-up reminders",
        "Subscription tracker",
        "Outreach tracker",
    )


def test_bold_text_that_opens_no_list_item_is_no_idea() -> None:
    reply = (
        "**Which apps do you use most?**\n\nWe'll suggest things to build around them.\n\n- A plain item\n- Another"
    )

    assert idea_names(reply) == ()


def test_an_idea_is_read_from_any_list_marker_and_loses_a_colon_inside_its_bold() -> None:
    reply = "A few more:\n\n* **Trip planner:** one page per trip.\n1. **Gift tracker** - who gets what.\n  + **My week**: the week ahead."

    assert idea_names(reply) == ("Trip planner", "Gift tracker", "My week")


def test_an_idea_named_twice_or_too_long_for_a_pill_or_like_a_fixed_reply_is_left_out() -> None:
    reply = (
        "- **Habit tracker**: one.\n"
        "- **habit tracker**: the same again.\n"
        f"- **{'A very long name ' * 6}**: too long.\n"
        "- **More ideas**: would be taken for the fixed reply.\n"
        "- **Weekly planner**: two.\n"
    )

    assert idea_names(reply) == ("Habit tracker", "Weekly planner")


def test_at_most_ten_ideas_are_read_from_a_reply() -> None:
    reply = "\n".join(f"- **Idea {index}**: something." for index in range(1, 14))

    assert idea_names(reply) == tuple(f"Idea {index}" for index in range(1, 11))


def test_the_question_about_what_the_workspace_is_for_offers_no_suggestions() -> None:
    assert fixed_suggestions(FirstRunTreeStep.CATEGORIES, "**What do you want to use this Studio for?**") == ()


def test_the_question_about_apps_offers_a_way_to_skip_it() -> None:
    assert fixed_suggestions(FirstRunTreeStep.CONNECTORS, "**Which apps do you use most?**") == (
        Suggestion(suggestion_id="show-ideas", label="Show me some ideas"),
    )


def test_a_list_of_ideas_offers_each_idea_then_more_ideas_then_something_else() -> None:
    assert fixed_suggestions(FirstRunTreeStep.IDEAS, _IDEAS_REPLY) == (
        Suggestion(suggestion_id="idea-1", label="Sales CRM with follow-up reminders"),
        Suggestion(suggestion_id="idea-2", label="Subscription tracker"),
        Suggestion(suggestion_id="idea-3", label="Outreach tracker"),
        Suggestion(suggestion_id="more-ideas", label="More ideas"),
        Suggestion(suggestion_id="something-else", label="Something else"),
    )


def test_a_reply_at_the_ideas_step_that_names_no_idea_is_left_to_the_general_prompt() -> None:
    assert fixed_suggestions(FirstRunTreeStep.IDEAS, "Here are some ideas: a habit tracker, or a planner.") is None


@pytest.mark.parametrize("step", [FirstRunTreeStep.START, FirstRunTreeStep.DONE])
def test_the_tree_has_no_say_outside_its_scripted_steps(step: FirstRunTreeStep) -> None:
    assert fixed_suggestions(step, _IDEAS_REPLY) is None
