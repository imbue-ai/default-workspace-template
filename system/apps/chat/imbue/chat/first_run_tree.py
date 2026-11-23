"""The tree: the fixed branches of a new workspace's first conversation (specs/chat-suggestions in mngr).

The first-run chat opens on a welcome that asks how the user wants to start, and each answer picks a
branch. The replies are the agent's, which follows the ``first-run-onboarding`` skill. This module holds
the rest: where the conversation stands (one file in the chat's folder), the one move a message makes,
and the suggestions offered under each scripted reply, which follow from the step and the reply's own
text with no model asked.

The step moves when a message is taken in the chat, so by the time a reply's turn ends the step names
that reply: ``categories`` is the question about what the workspace is for, ``connectors`` the one about
the apps the user has, ``ideas`` the list of ideas.
"""

import re
from enum import auto
from pathlib import Path
from typing import Final
from typing import assert_never

from pydantic import Field

from imbue.chat.chat_setting_files import read_chat_setting_file
from imbue.chat.chat_setting_files import write_chat_setting_file
from imbue.chat.suggestions import MAX_SUGGESTION_COUNT
from imbue.chat.suggestions import MAX_SUGGESTION_LABEL_LENGTH
from imbue.chat.suggestions import Suggestion
from imbue.imbue_common.enums import LowerCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure

FIRST_RUN_TREE_FILENAME: Final[str] = "first_run_tree.json"
FIRST_RUN_TREE_FILE_VERSION: Final[int] = 1

# The replies offered under the welcome and under the tree's own replies. The skill names the same words, so a
# click and its reply stay matched only while the two are edited together.
START_DESCRIBE_LABEL: Final[str] = "Describe what you want to make"
START_IDEAS_LABEL: Final[str] = "Get ideas for what to make"
START_FEATURES_LABEL: Final[str] = "See what Studio can do"
SHOW_IDEAS_LABEL: Final[str] = "Show me some ideas"
MORE_IDEAS_LABEL: Final[str] = "More ideas"
SOMETHING_ELSE_LABEL: Final[str] = "Something else"

# An idea as the skill has the agent list it: a list item that opens on the idea's name in bold.
_IDEA_ITEM_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[ \t]*(?:[-*+]|\d+[.)])[ \t]+\*\*(?P<name>[^*\n]+)\*\*", re.MULTILINE
)


class FirstRunTreeStep(LowerCaseStrEnum):
    """Where a first-run chat's first conversation stands (a value kept on disk)."""

    # The welcome is up, and nothing was answered.
    START = auto()
    # The user asked for ideas, and is asked what the workspace is for.
    CATEGORIES = auto()
    # The user is asked which apps they use.
    CONNECTORS = auto()
    # The ideas are listed.
    IDEAS = auto()
    DONE = auto()


class FirstRunTree(FrozenModel):
    """What a first-run chat's tree file holds."""

    version: int = Field(
        default=FIRST_RUN_TREE_FILE_VERSION,
        ge=1,
        le=FIRST_RUN_TREE_FILE_VERSION,
        description="The on-disk shape the file was written with",
    )
    step: FirstRunTreeStep = Field(description="Where the conversation stands")


@pure
def next_first_run_tree_step(step: FirstRunTreeStep, message: str) -> FirstRunTreeStep:
    """Where the tree stands once the user has said ``message`` at ``step``.

    A suggestion's words count whether they were clicked or typed. A step that offers picks ends the tree on any
    other message; the two questions take whatever is answered, since their answer is typed.
    """
    said = message.strip()
    match step:
        case FirstRunTreeStep.START:
            return FirstRunTreeStep.CATEGORIES if said == START_IDEAS_LABEL else FirstRunTreeStep.DONE
        case FirstRunTreeStep.CATEGORIES:
            return FirstRunTreeStep.CONNECTORS
        case FirstRunTreeStep.CONNECTORS:
            return FirstRunTreeStep.IDEAS
        case FirstRunTreeStep.IDEAS:
            return FirstRunTreeStep.IDEAS if said == MORE_IDEAS_LABEL else FirstRunTreeStep.DONE
        case FirstRunTreeStep.DONE:
            return FirstRunTreeStep.DONE
        case _ as unreachable:
            assert_never(unreachable)


def read_first_run_tree_step(chat_dir: Path, is_awaiting_first_agent: bool) -> FirstRunTreeStep:
    """Where the first-run chat under ``chat_dir`` stands.

    A chat with no readable file had its welcome written before the tree was recorded: it is at the start while
    nobody has answered it, and done otherwise, so a conversation already under way is never put back into a tree.
    """
    tree = read_chat_setting_file(chat_dir, FIRST_RUN_TREE_FILENAME, FirstRunTree, "first-run tree")
    if tree is not None:
        return tree.step
    return FirstRunTreeStep.START if is_awaiting_first_agent else FirstRunTreeStep.DONE


def write_first_run_tree_step(chat_dir: Path, step: FirstRunTreeStep) -> None:
    write_chat_setting_file(chat_dir, FIRST_RUN_TREE_FILENAME, FirstRunTree(step=step))


@pure
def idea_names(reply_text: str) -> tuple[str, ...]:
    """The ideas a reply lists, by name and in order: the bold text that opens each list item, which is the form
    the skill gives an idea. A name is kept when it can be a suggestion's label and no earlier one says the same."""
    names: list[str] = []
    seen_names: set[str] = {MORE_IDEAS_LABEL.casefold(), SOMETHING_ELSE_LABEL.casefold()}
    for item in _IDEA_ITEM_PATTERN.finditer(reply_text):
        if len(names) == MAX_SUGGESTION_COUNT:
            break
        name = item.group("name").strip().rstrip(":").strip()
        if not name or len(name) > MAX_SUGGESTION_LABEL_LENGTH or name.casefold() in seen_names:
            continue
        seen_names.add(name.casefold())
        names.append(name)
    return tuple(names)


@pure
def fixed_suggestions(step: FirstRunTreeStep, reply_text: str) -> tuple[Suggestion, ...] | None:
    """The suggestions the tree offers under a reply that ended at ``step``; none is a set of its own.

    None when the tree has no say about the reply: outside its scripted steps, and for a reply at the ideas step
    that names no idea this can read.
    """
    match step:
        case FirstRunTreeStep.CATEGORIES:
            return ()
        case FirstRunTreeStep.CONNECTORS:
            return (Suggestion(suggestion_id="show-ideas", label=SHOW_IDEAS_LABEL),)
        case FirstRunTreeStep.IDEAS:
            names = idea_names(reply_text)
            if not names:
                return None
            return (
                *(Suggestion(suggestion_id=f"idea-{index}", label=name) for index, name in enumerate(names, start=1)),
                Suggestion(suggestion_id="more-ideas", label=MORE_IDEAS_LABEL),
                Suggestion(suggestion_id="something-else", label=SOMETHING_ELSE_LABEL),
            )
        case FirstRunTreeStep.START | FirstRunTreeStep.DONE:
            return None
        case _ as unreachable:
            assert_never(unreachable)
