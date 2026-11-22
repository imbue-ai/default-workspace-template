"""Activity suggestions: things worth trying in Studio that this workspace's owner has not done yet.

They are offered while an agent works on a reply (the activity placement) and on a chat with no message
yet (the new-chat placement), and each is an action suggestion: a click shows the owner where the thing
is rather than sending a message. Each has a condition that holds until the owner has done the thing, and
most conditions read a one-way marker (``onboarding_activity``), so a fact stays true after what proved
it is gone: a deleted chat, a phone put away.

A fact that could not be read counts against its suggestion: with the marker directory unreadable nothing
is offered, rather than everything. The whole set is withheld behind one gate (``reason_no_activity_suggestions``).

The functions that decide are pure over the facts they are handed; ``read_marker`` and
``read_activity_facts`` are the only ones that look at the workspace.
"""

from datetime import datetime
from datetime import timedelta
from enum import auto
from pathlib import Path
from typing import Final
from typing import assert_never

from app_manifest.errors import AppManifestError
from app_manifest.user_apps import has_user_built_app
from loguru import logger as _loguru_logger
from onboarding_activity.markers import Marker
from onboarding_activity.markers import MarkerUnreadableError
from onboarding_activity.markers import marked_at
from pydantic import Field

from imbue.chat.chat_settings import ChatSettings
from imbue.chat.first_run_tree import FirstRunTreeStep
from imbue.chat.suggestions import Suggestion
from imbue.chat.suggestions import SuggestionAction
from imbue.chat.suggestions import SuggestionPlacement
from imbue.imbue_common.enums import LowerCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure

logger = _loguru_logger

# How long after the build-app skill's scaffolder last ran an app build is taken to be under way, so building an
# app is not suggested to someone in the middle of building one.
APP_BUILD_QUIET_PERIOD: Final[timedelta] = timedelta(hours=1)


class ActivitySuggestionId(LowerCaseStrEnum):
    """Every activity suggestion, in the order they are offered (a wire value: the suggestion's id)."""

    NEW_CHAT = auto()
    PHONE_ACCESS = auto()
    BUILD_APP = auto()
    CONNECT_DATA = auto()


# The placements that offer activity suggestions: every one but the replies under an agent's reply.
_ACTIVITY_PLACEMENTS: Final[tuple[SuggestionPlacement, ...]] = (
    SuggestionPlacement.ACTIVITY,
    SuggestionPlacement.NEW_CHAT,
)


class MarkerReading(FrozenModel):
    """One onboarding marker as it was read."""

    is_readable: bool = Field(description="Whether the marker could be looked at at all")
    marked_at: datetime | None = Field(
        description="When the marker was last set; None when it is not set, and when it could not be read"
    )


UNSET_MARKER: Final[MarkerReading] = MarkerReading(is_readable=True, marked_at=None)
UNREADABLE_MARKER: Final[MarkerReading] = MarkerReading(is_readable=False, marked_at=None)


class ActivityFacts(FrozenModel):
    """What the conditions read about what the workspace's owner has already done."""

    second_chat_started: MarkerReading = Field(description="Whether a person has started a second chat")
    phone_layout_used: MarkerReading = Field(description="Whether the owner has opened the phone layout")
    app_build_started: MarkerReading = Field(description="When the build-app skill's scaffolder last ran")
    permission_request_filed: MarkerReading = Field(description="Whether an agent has filed a permission request")
    connect_data_coach_shown: MarkerReading = Field(
        description="Whether the owner has been shown where the workspace's connections live"
    )
    has_user_built_app: bool | None = Field(
        description="Whether the app registry holds an app the user built; None when that could not be read"
    )


@pure
def activity_suggestion(suggestion_id: ActivitySuggestionId) -> Suggestion:
    """The suggestion as the pages draw it: its words, and what a click on it does."""
    match suggestion_id:
        case ActivitySuggestionId.NEW_CHAT:
            return Suggestion(
                suggestion_id=suggestion_id.value,
                label="Work in parallel by starting a new chat",
                action=SuggestionAction.NEW_CHAT_COACH,
            )
        case ActivitySuggestionId.PHONE_ACCESS:
            return Suggestion(
                suggestion_id=suggestion_id.value,
                label="Access Studio from your phone",
                action=SuggestionAction.PHONE_ACCESS,
            )
        case ActivitySuggestionId.BUILD_APP:
            return Suggestion(
                suggestion_id=suggestion_id.value,
                label="Build an app",
                action=SuggestionAction.BUILD_APP_COACH,
            )
        case ActivitySuggestionId.CONNECT_DATA:
            return Suggestion(
                suggestion_id=suggestion_id.value,
                label="Connect some data",
                action=SuggestionAction.CONNECT_DATA_COACH,
            )
        case _ as unreachable:
            assert_never(unreachable)


@pure
def _is_known_unset(reading: MarkerReading) -> bool:
    return reading.is_readable and reading.marked_at is None


@pure
def _is_no_app_build_under_way(app_build_started: MarkerReading, now: datetime) -> bool:
    """Whether no app build was started within the quiet period before ``now``, as far as the marker says."""
    if not app_build_started.is_readable:
        return False
    if app_build_started.marked_at is None:
        return True
    return now - app_build_started.marked_at > APP_BUILD_QUIET_PERIOD


@pure
def is_activity_suggestion_offered(suggestion_id: ActivitySuggestionId, facts: ActivityFacts, now: datetime) -> bool:
    """Whether the suggestion's condition holds: the owner has not done the thing yet, as far as could be read."""
    match suggestion_id:
        case ActivitySuggestionId.NEW_CHAT:
            return _is_known_unset(facts.second_chat_started)
        case ActivitySuggestionId.PHONE_ACCESS:
            return _is_known_unset(facts.phone_layout_used)
        case ActivitySuggestionId.BUILD_APP:
            return facts.has_user_built_app is False and _is_no_app_build_under_way(facts.app_build_started, now)
        case ActivitySuggestionId.CONNECT_DATA:
            return _is_known_unset(facts.permission_request_filed) and _is_known_unset(facts.connect_data_coach_shown)
        case _ as unreachable:
            assert_never(unreachable)


@pure
def offered_activity_suggestions(facts: ActivityFacts, now: datetime) -> tuple[Suggestion, ...]:
    """Every suggestion whose condition holds, in the order they are offered."""
    return tuple(
        activity_suggestion(suggestion_id)
        for suggestion_id in ActivitySuggestionId
        if is_activity_suggestion_offered(suggestion_id, facts, now)
    )


@pure
def activity_placement_named(name: str | None) -> SuggestionPlacement | None:
    """The placement a wire value names, when it is one that offers activity suggestions; else None."""
    return next((placement for placement in _ACTIVITY_PLACEMENTS if placement.value == name), None)


@pure
def marker_set_by_click(suggestion_id: ActivitySuggestionId) -> Marker | None:
    """The marker a click on the suggestion sets, which retires it; None for a suggestion that is retired only by
    the owner doing the thing it points at."""
    match suggestion_id:
        case ActivitySuggestionId.CONNECT_DATA:
            return Marker.CONNECT_DATA_COACH_SHOWN
        case ActivitySuggestionId.NEW_CHAT | ActivitySuggestionId.PHONE_ACCESS | ActivitySuggestionId.BUILD_APP:
            return None
        case _ as unreachable:
            assert_never(unreachable)


@pure
def is_placement_enabled(placement: SuggestionPlacement, settings: ChatSettings) -> bool:
    """Whether the workspace's switch for the placement is on."""
    match placement:
        case SuggestionPlacement.NEXT_STEP:
            return settings.is_next_step_suggestions_enabled
        case SuggestionPlacement.ACTIVITY:
            return settings.is_activity_suggestions_enabled
        case SuggestionPlacement.NEW_CHAT:
            return settings.is_new_chat_suggestions_enabled
        case _ as unreachable:
            assert_never(unreachable)


@pure
def reason_no_activity_suggestions(
    is_owner: bool,
    tour_finished: MarkerReading,
    tree_step: FirstRunTreeStep | None,
    is_placement_switched_on: bool,
) -> str | None:
    """Why nobody is offered an activity suggestion here, whatever the conditions say; None when the gate is open.

    ``tree_step`` is where the first conversation of the chat being shown stands: None for any chat but the
    first-run chat, and for a page showing no chat the app knows."""
    if not is_owner:
        return "the viewer is not the workspace's owner"
    if not is_placement_switched_on:
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


def read_activity_facts(repo_root: Path, registry: Path) -> ActivityFacts:
    """What the conditions read, from the workspace's markers and the app registry at ``registry``, for the
    workspace checked out at ``repo_root``."""
    try:
        is_app_built: bool | None = has_user_built_app(repo_root, registry)
    except AppManifestError as error:
        logger.warning("Could not read whether the workspace holds an app its user built: {}", error)
        is_app_built = None
    return ActivityFacts(
        second_chat_started=read_marker(Marker.SECOND_CHAT_STARTED),
        phone_layout_used=read_marker(Marker.PHONE_LAYOUT_USED),
        app_build_started=read_marker(Marker.APP_BUILD_STARTED),
        permission_request_filed=read_marker(Marker.PERMISSION_REQUEST_FILED),
        connect_data_coach_shown=read_marker(Marker.CONNECT_DATA_COACH_SHOWN),
        has_user_built_app=is_app_built,
    )
