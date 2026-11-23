import os
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path

import pytest
from onboarding_activity.items import OnboardingItem
from onboarding_activity.markers import MARKER_DIR_ENV
from onboarding_activity.markers import Marker
from onboarding_activity.markers import mark

from imbue.chat.first_run_tree import FirstRunTreeStep
from imbue.chat.new_chat_suggestions import APP_BUILD_QUIET_PERIOD
from imbue.chat.new_chat_suggestions import MarkerReading
from imbue.chat.new_chat_suggestions import NewChatFacts
from imbue.chat.new_chat_suggestions import UNREADABLE_MARKER
from imbue.chat.new_chat_suggestions import UNSET_MARKER
from imbue.chat.new_chat_suggestions import is_new_chat_suggestion_offered
from imbue.chat.new_chat_suggestions import new_chat_suggestion
from imbue.chat.new_chat_suggestions import offered_new_chat_suggestions
from imbue.chat.new_chat_suggestions import read_marker
from imbue.chat.new_chat_suggestions import read_new_chat_facts
from imbue.chat.new_chat_suggestions import reason_no_new_chat_suggestions
from imbue.chat.suggestions import SuggestionAction
from imbue.chat.testing import block_onboarding_markers
from imbue.chat.testing import write_plain_app_registry
from imbue.chat.testing import write_workspace_with_a_user_built_app
from imbue.system_interface.shell.testing import write_registry

_NOW = datetime(2026, 11, 21, 12, 0, 0, tzinfo=timezone.utc)
_SET_MARKER = MarkerReading(is_readable=True, marked_at=_NOW - timedelta(days=3))

# A workspace whose owner has done none of it, with everything readable: every item is offered.
_NOTHING_DONE = NewChatFacts(items_not_done=tuple(OnboardingItem), app_build_started=UNSET_MARKER)


def _all_but(*done_or_unreadable: OnboardingItem, app_build_started: MarkerReading = UNSET_MARKER) -> NewChatFacts:
    return NewChatFacts(
        items_not_done=tuple(item for item in OnboardingItem if item not in done_or_unreadable),
        app_build_started=app_build_started,
    )


def _is_offered(item: OnboardingItem, facts: NewChatFacts) -> bool:
    return is_new_chat_suggestion_offered(item, facts, _NOW)


def _offered_ids(facts: NewChatFacts) -> list[str]:
    return [suggestion.suggestion_id for suggestion in offered_new_chat_suggestions(facts, _NOW)]


def test_every_suggestion_is_offered_in_the_fixed_order_with_its_words_and_action() -> None:
    offered = offered_new_chat_suggestions(_NOTHING_DONE, _NOW)

    assert [suggestion.model_dump(mode="json") for suggestion in offered] == [
        {
            "suggestion_id": "new_chat",
            "label": "Work in parallel by starting a new chat",
            "action": "new_chat_coach",
            "is_recommended": False,
        },
        {
            "suggestion_id": "phone_access",
            "label": "Access Studio from your phone",
            "action": "phone_access",
            "is_recommended": False,
        },
        {"suggestion_id": "build_app", "label": "Build an app", "action": "build_app_coach", "is_recommended": False},
        {
            "suggestion_id": "connect_data",
            "label": "Connect some data",
            "action": "connect_data_coach",
            "is_recommended": False,
        },
    ]
    assert [new_chat_suggestion(item).action for item in OnboardingItem] == [
        SuggestionAction.NEW_CHAT_COACH,
        SuggestionAction.PHONE_ACCESS,
        SuggestionAction.BUILD_APP_COACH,
        SuggestionAction.CONNECT_DATA_COACH,
    ]


def test_an_item_that_is_done_or_unreadable_drops_its_suggestion_and_keeps_the_others_in_order() -> None:
    assert _offered_ids(_all_but(OnboardingItem.PHONE_ACCESS)) == ["new_chat", "build_app", "connect_data"]
    assert _offered_ids(_all_but(OnboardingItem.NEW_CHAT, OnboardingItem.BUILD_APP)) == [
        "phone_access",
        "connect_data",
    ]
    assert offered_new_chat_suggestions(_all_but(*OnboardingItem), _NOW) == ()
    # The order is the items', whatever order the facts name them in.
    reversed_facts = NewChatFacts(items_not_done=tuple(reversed(OnboardingItem)), app_build_started=UNSET_MARKER)
    assert _offered_ids(reversed_facts) == ["new_chat", "phone_access", "build_app", "connect_data"]


def test_building_an_app_is_suggested_while_the_user_has_built_none_and_no_build_is_under_way() -> None:
    assert _is_offered(OnboardingItem.BUILD_APP, _NOTHING_DONE)
    assert not _is_offered(OnboardingItem.BUILD_APP, _all_but(OnboardingItem.BUILD_APP))
    # A build started long ago that left no app is no reason to hold the suggestion back, but an app is.
    long_ago = MarkerReading(is_readable=True, marked_at=_NOW - timedelta(days=2))
    assert _is_offered(OnboardingItem.BUILD_APP, _all_but(app_build_started=long_ago))
    assert not _is_offered(OnboardingItem.BUILD_APP, _all_but(OnboardingItem.BUILD_APP, app_build_started=long_ago))
    # Whether a build could be under way is unknown, which counts against this suggestion alone.
    assert _offered_ids(_all_but(app_build_started=UNREADABLE_MARKER)) == ["new_chat", "phone_access", "connect_data"]


def test_an_app_build_started_within_the_hour_holds_the_suggestion_back_until_the_hour_is_over() -> None:
    def started(ago: timedelta) -> NewChatFacts:
        return _all_but(app_build_started=MarkerReading(is_readable=True, marked_at=_NOW - ago))

    assert APP_BUILD_QUIET_PERIOD == timedelta(hours=1)
    assert not _is_offered(OnboardingItem.BUILD_APP, started(timedelta(seconds=0)))
    assert not _is_offered(OnboardingItem.BUILD_APP, started(timedelta(minutes=59, seconds=59)))
    # Older than an hour means more than one, so the hour itself still holds it back.
    assert not _is_offered(OnboardingItem.BUILD_APP, started(timedelta(hours=1)))
    assert _is_offered(OnboardingItem.BUILD_APP, started(timedelta(hours=1, seconds=1)))
    # A marker set after "now" (two clocks that disagree) is a build under way, not an old one.
    assert not _is_offered(OnboardingItem.BUILD_APP, started(timedelta(minutes=-5)))
    # A build under way holds back that one suggestion and no other.
    assert _offered_ids(started(timedelta(minutes=5))) == ["new_chat", "phone_access", "connect_data"]


def test_the_gate_is_open_for_the_owner_once_the_tour_has_finished_outside_a_tree_with_the_switch_on() -> None:
    for tree_step in (None, FirstRunTreeStep.DONE):
        assert (
            reason_no_new_chat_suggestions(
                is_owner=True, tour_finished=_SET_MARKER, tree_step=tree_step, is_switched_on=True
            )
            is None
        )


def test_each_part_of_the_gate_closes_it_by_itself() -> None:
    def reason(
        is_owner: bool = True,
        tour_finished: MarkerReading = _SET_MARKER,
        tree_step: FirstRunTreeStep | None = None,
        is_switched_on: bool = True,
    ) -> str | None:
        return reason_no_new_chat_suggestions(
            is_owner=is_owner,
            tour_finished=tour_finished,
            tree_step=tree_step,
            is_switched_on=is_switched_on,
        )

    assert reason() is None
    assert reason(is_owner=False) == "the viewer is not the workspace's owner"
    assert reason(is_switched_on=False) == "the placement is switched off"
    assert reason(tour_finished=UNSET_MARKER) == "the tour has not finished"
    assert reason(tour_finished=UNREADABLE_MARKER) == "whether the tour has finished could not be read"
    for in_progress in (
        FirstRunTreeStep.START,
        FirstRunTreeStep.CATEGORIES,
        FirstRunTreeStep.CONNECTORS,
        FirstRunTreeStep.IDEAS,
    ):
        assert reason(tree_step=in_progress) == "the chat's first conversation is still following its tree"


def test_a_marker_is_read_as_unset_set_with_its_time_or_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert read_marker(Marker.APP_BUILD_STARTED) == UNSET_MARKER

    mark(Marker.APP_BUILD_STARTED)
    set_at = _NOW - timedelta(minutes=30)
    os.utime(Path(os.environ[MARKER_DIR_ENV]) / "app_build_started", (set_at.timestamp(), set_at.timestamp()))

    assert read_marker(Marker.APP_BUILD_STARTED) == MarkerReading(is_readable=True, marked_at=set_at)

    block_onboarding_markers(tmp_path, monkeypatch)

    assert read_marker(Marker.APP_BUILD_STARTED) == UNREADABLE_MARKER


def test_the_facts_are_read_from_the_markers_and_the_registry(tmp_path: Path) -> None:
    write_workspace_with_a_user_built_app(tmp_path)
    registry = write_plain_app_registry(tmp_path / "data" / ".state" / "apps.toml", "chat")

    assert read_new_chat_facts(tmp_path, registry) == _NOTHING_DONE

    mark(Marker.SECOND_CHAT_STARTED)
    assert read_new_chat_facts(tmp_path, registry) == _all_but(OnboardingItem.NEW_CHAT)

    mark(Marker.PERMISSION_REQUEST_FILED)
    write_plain_app_registry(registry, "chat", "todo-list")
    facts = read_new_chat_facts(tmp_path, registry)

    assert facts == _all_but(OnboardingItem.NEW_CHAT, OnboardingItem.BUILD_APP, OnboardingItem.CONNECT_DATA)
    assert _offered_ids(facts) == ["phone_access"]

    mark(Marker.PHONE_LAYOUT_USED)
    mark(Marker.APP_BUILD_STARTED)
    everything_done = read_new_chat_facts(tmp_path, registry)

    assert everything_done.items_not_done == ()
    assert everything_done.app_build_started.marked_at is not None


def test_connecting_data_is_suggested_until_a_permission_request_is_filed(tmp_path: Path) -> None:
    write_workspace_with_a_user_built_app(tmp_path)
    registry = write_plain_app_registry(tmp_path / "data" / ".state" / "apps.toml", "chat")
    # Every marker but the one that says a request was filed leaves the suggestion standing.
    for marker in (Marker.TOUR_FINISHED, Marker.SECOND_CHAT_STARTED, Marker.PHONE_LAYOUT_USED):
        mark(marker)

    assert _is_offered(OnboardingItem.CONNECT_DATA, read_new_chat_facts(tmp_path, registry))

    mark(Marker.PERMISSION_REQUEST_FILED)

    assert not _is_offered(OnboardingItem.CONNECT_DATA, read_new_chat_facts(tmp_path, registry))


def test_a_registry_or_markers_that_cannot_be_read_offer_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_workspace_with_a_user_built_app(tmp_path)
    registry = write_registry(tmp_path / "data" / ".state" / "apps.toml", "[[apps]\nname = \n")

    unreadable_registry = read_new_chat_facts(tmp_path, registry)

    assert unreadable_registry == _all_but(OnboardingItem.BUILD_APP)
    assert _offered_ids(unreadable_registry) == ["new_chat", "phone_access", "connect_data"]

    block_onboarding_markers(tmp_path, monkeypatch)
    unreadable_markers = read_new_chat_facts(tmp_path, registry)

    assert unreadable_markers == NewChatFacts(items_not_done=(), app_build_started=UNREADABLE_MARKER)
    assert _offered_ids(unreadable_markers) == []
