"""New-chat suggestions: the onboarding items this workspace's owner has not done yet, offered on a chat nobody
has spoken in.

Each is an action suggestion: a click shows the owner where the thing is rather than sending a message. The
items, their order, their words and what makes one done are the workspace's (``onboarding_activity.items``),
shared with the onboarding checklist. An item is offered while it is not done, and building an app is also held
back while a build was started within the past hour.

A fact that could not be read counts against its suggestion: with the marker directory unreadable nothing is
offered, rather than everything. The whole set is withheld behind one gate (``reason_no_new_chat_suggestions``).

The functions that decide are pure over the facts they are handed; ``read_marker`` and ``read_new_chat_facts``
are the only ones that look at the workspace.
"""

from datetime import datetime
from datetime import timedelta
from pathlib import Path
from typing import Final
from typing import assert_never

from app_manifest.errors import AppManifestError
from app_manifest.user_apps import has_user_built_app
from loguru import logger as _loguru_logger
from onboarding_activity.items import OnboardingItem
from onboarding_activity.items import done_marker
from onboarding_activity.items import is_item_done
from onboarding_activity.items import item_label
from onboarding_activity.markers import Marker
from onboarding_activity.markers import MarkerUnreadableError
from onboarding_activity.markers import marked_at
from pydantic import Field

from imbue.chat.first_run_tree import FirstRunTreeStep
from imbue.chat.suggestions import Suggestion
from imbue.chat.suggestions import SuggestionAction
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure

logger = _loguru_logger

# How long after the build-app skill's scaffolder last ran an app build is taken to be under way, so building an
# app is not suggested to someone in the middle of building one.
APP_BUILD_QUIET_PERIOD: Final[timedelta] = timedelta(hours=1)


class MarkerReading(FrozenModel):
    """One onboarding marker as it was read."""

    is_readable: bool = Field(description="Whether the marker could be looked at at all")
    marked_at: datetime | None = Field(
        description="When the marker was last set; None when it is not set, and when it could not be read"
    )


UNSET_MARKER: Final[MarkerReading] = MarkerReading(is_readable=True, marked_at=None)
UNREADABLE_MARKER: Final[MarkerReading] = MarkerReading(is_readable=False, marked_at=None)


class NewChatFacts(FrozenModel):
    """What the suggestions read about what the workspace's owner has already done."""

    items_not_done: tuple[OnboardingItem, ...] = Field(
        description="The items the owner is known not to have done; one whose state could not be read is left out"
    )
    app_build_started: MarkerReading = Field(description="When the build-app skill's scaffolder last ran")


@pure
def _item_action(item: OnboardingItem) -> SuggestionAction:
    match item:
        case OnboardingItem.NEW_CHAT:
            return SuggestionAction.NEW_CHAT_COACH
        case OnboardingItem.PHONE_ACCESS:
            return SuggestionAction.PHONE_ACCESS
        case OnboardingItem.BUILD_APP:
            return SuggestionAction.BUILD_APP_COACH
        case OnboardingItem.CONNECT_DATA:
            return SuggestionAction.CONNECT_DATA_COACH
        case _ as unreachable:
            assert_never(unreachable)


@pure
def new_chat_suggestion(item: OnboardingItem) -> Suggestion:
    """The item as the pages draw it: its words, and what a click on it does."""
    return Suggestion(suggestion_id=item.value, label=item_label(item), action=_item_action(item))


@pure
def _is_no_app_build_under_way(app_build_started: MarkerReading, now: datetime) -> bool:
    """Whether no app build was started within the quiet period before ``now``, as far as the marker says."""
    if not app_build_started.is_readable:
        return False
    if app_build_started.marked_at is None:
        return True
    return now - app_build_started.marked_at > APP_BUILD_QUIET_PERIOD


@pure
def is_new_chat_suggestion_offered(item: OnboardingItem, facts: NewChatFacts, now: datetime) -> bool:
    """Whether the item is offered: the owner has not done it yet, as far as could be read, and for building an
    app, none is being built."""
    if item not in facts.items_not_done:
        return False
    return item is not OnboardingItem.BUILD_APP or _is_no_app_build_under_way(facts.app_build_started, now)


@pure
def offered_new_chat_suggestions(facts: NewChatFacts, now: datetime) -> tuple[Suggestion, ...]:
    """Every suggestion that is offered, in the items' order."""
    return tuple(
        new_chat_suggestion(item) for item in OnboardingItem if is_new_chat_suggestion_offered(item, facts, now)
    )


@pure
def reason_no_new_chat_suggestions(
    is_owner: bool,
    tour_finished: MarkerReading,
    tree_step: FirstRunTreeStep | None,
    is_switched_on: bool,
) -> str | None:
    """Why nobody is offered a new-chat suggestion here, whatever the items say; None when the gate is open.

    ``tree_step`` is where the first conversation of the chat being shown stands: None for any chat but the
    first-run chat, and for a page showing no chat the app knows."""
    if not is_owner:
        return "the viewer is not the workspace's owner"
    if not is_switched_on:
        return "the placement is switched off"
    if tree_step is not None and tree_step is not FirstRunTreeStep.DONE:
        return "the chat's first conversation is still following its tree"
    if not tour_finished.is_readable:
        return "whether the tour has finished could not be read"
    if tour_finished.marked_at is None:
        return "the tour has not finished"
    return None


def read_marker(marker: Marker) -> MarkerReading:
    """The marker as it stands; one that cannot be read (logged) reads as unreadable rather than as not set."""
    try:
        return MarkerReading(is_readable=True, marked_at=marked_at(marker))
    except MarkerUnreadableError as error:
        logger.warning("Could not read the onboarding marker {}: {}", marker.value, error)
        return UNREADABLE_MARKER


def _is_item_known_not_done(item: OnboardingItem, is_app_built: bool | None) -> bool:
    """Whether the owner has not done the item; False too when that could not be read (logged for a marker).

    ``is_app_built`` is whether the registry holds an app the user built, None when it could not be read."""
    if is_app_built is None and done_marker(item) is None:
        # The registry alone says whether this item is done, and it could not be read.
        return False
    try:
        return not is_item_done(item, is_app_built is True)
    except MarkerUnreadableError as error:
        logger.warning("Could not read whether the onboarding item {} is done: {}", item.value, error)
        return False


def read_new_chat_facts(repo_root: Path, registry: Path) -> NewChatFacts:
    """What the suggestions read, from the workspace's markers and the app registry at ``registry``, for the
    workspace checked out at ``repo_root``."""
    try:
        is_app_built: bool | None = has_user_built_app(repo_root, registry)
    except AppManifestError as error:
        logger.warning("Could not read whether the workspace holds an app its user built: {}", error)
        is_app_built = None
    return NewChatFacts(
        items_not_done=tuple(item for item in OnboardingItem if _is_item_known_not_done(item, is_app_built)),
        app_build_started=read_marker(Marker.APP_BUILD_STARTED),
    )
