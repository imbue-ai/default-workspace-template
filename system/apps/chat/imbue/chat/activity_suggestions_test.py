import os
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path

import pytest
from app_manifest.testing import write_app_manifest
from onboarding_activity.markers import MARKER_DIR_ENV
from onboarding_activity.markers import Marker
from onboarding_activity.markers import mark

from imbue.chat.activity_suggestions import APP_BUILD_QUIET_PERIOD
from imbue.chat.activity_suggestions import ActivityFacts
from imbue.chat.activity_suggestions import ActivitySuggestionId
from imbue.chat.activity_suggestions import MarkerReading
from imbue.chat.activity_suggestions import UNREADABLE_MARKER
from imbue.chat.activity_suggestions import UNSET_MARKER
from imbue.chat.activity_suggestions import activity_placement_named
from imbue.chat.activity_suggestions import activity_suggestion
from imbue.chat.activity_suggestions import is_activity_suggestion_offered
from imbue.chat.activity_suggestions import is_placement_enabled
from imbue.chat.activity_suggestions import marker_set_by_click
from imbue.chat.activity_suggestions import offered_activity_suggestions
from imbue.chat.activity_suggestions import read_activity_facts
from imbue.chat.activity_suggestions import read_marker
from imbue.chat.activity_suggestions import reason_no_activity_suggestions
from imbue.chat.chat_settings import ChatSettings
from imbue.chat.first_run_tree import FirstRunTreeStep
from imbue.chat.suggestions import SuggestionAction
from imbue.chat.suggestions import SuggestionPlacement
from imbue.imbue_common.model_update import to_update

_NOW = datetime(2026, 11, 21, 12, 0, 0, tzinfo=timezone.utc)
_SET_MARKER = MarkerReading(is_readable=True, marked_at=_NOW - timedelta(days=3))

# A workspace whose owner has done none of it, with everything readable: every condition holds.
_NOTHING_DONE = ActivityFacts(
    second_chat_started=UNSET_MARKER,
    phone_layout_used=UNSET_MARKER,
    app_build_started=UNSET_MARKER,
    permission_request_filed=UNSET_MARKER,
    connect_data_coach_shown=UNSET_MARKER,
    has_user_built_app=False,
)


def _with(**changes: MarkerReading | bool | None) -> ActivityFacts:
    return ActivityFacts.model_validate({**_NOTHING_DONE.model_dump(), **changes})


def _is_offered(suggestion_id: ActivitySuggestionId, facts: ActivityFacts) -> bool:
    return is_activity_suggestion_offered(suggestion_id, facts, _NOW)


def _offered_ids(facts: ActivityFacts) -> list[str]:
    return [suggestion.suggestion_id for suggestion in offered_activity_suggestions(facts, _NOW)]


def test_every_suggestion_is_offered_in_the_fixed_order_with_its_words_and_action() -> None:
    offered = offered_activity_suggestions(_NOTHING_DONE, _NOW)

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
    assert [activity_suggestion(suggestion_id).action for suggestion_id in ActivitySuggestionId] == [
        SuggestionAction.NEW_CHAT_COACH,
        SuggestionAction.PHONE_ACCESS,
        SuggestionAction.BUILD_APP_COACH,
        SuggestionAction.CONNECT_DATA_COACH,
    ]


def test_a_condition_that_no_longer_holds_drops_its_suggestion_and_keeps_the_others_in_order() -> None:
    assert _offered_ids(_with(phone_layout_used=_SET_MARKER)) == ["new_chat", "build_app", "connect_data"]
    assert _offered_ids(_with(second_chat_started=_SET_MARKER, has_user_built_app=True)) == [
        "phone_access",
        "connect_data",
    ]
    everything_done = _with(
        second_chat_started=_SET_MARKER,
        phone_layout_used=_SET_MARKER,
        has_user_built_app=True,
        permission_request_filed=_SET_MARKER,
    )
    assert offered_activity_suggestions(everything_done, _NOW) == ()


def test_a_new_chat_is_suggested_until_a_person_has_started_a_second_one() -> None:
    assert _is_offered(ActivitySuggestionId.NEW_CHAT, _NOTHING_DONE)
    assert not _is_offered(ActivitySuggestionId.NEW_CHAT, _with(second_chat_started=_SET_MARKER))


def test_phone_access_is_suggested_until_the_phone_layout_has_been_used() -> None:
    assert _is_offered(ActivitySuggestionId.PHONE_ACCESS, _NOTHING_DONE)
    assert not _is_offered(ActivitySuggestionId.PHONE_ACCESS, _with(phone_layout_used=_SET_MARKER))


def test_building_an_app_is_suggested_while_the_user_has_built_none_and_no_build_is_under_way() -> None:
    assert _is_offered(ActivitySuggestionId.BUILD_APP, _NOTHING_DONE)
    assert not _is_offered(ActivitySuggestionId.BUILD_APP, _with(has_user_built_app=True))
    # A build started long ago that left no app is no reason to hold the suggestion back, but an app is.
    long_ago = MarkerReading(is_readable=True, marked_at=_NOW - timedelta(days=2))
    assert _is_offered(ActivitySuggestionId.BUILD_APP, _with(app_build_started=long_ago))
    assert not _is_offered(ActivitySuggestionId.BUILD_APP, _with(app_build_started=long_ago, has_user_built_app=True))


def test_an_app_build_started_within_the_hour_holds_the_suggestion_back_until_the_hour_is_over() -> None:
    def started(ago: timedelta) -> ActivityFacts:
        return _with(app_build_started=MarkerReading(is_readable=True, marked_at=_NOW - ago))

    assert APP_BUILD_QUIET_PERIOD == timedelta(hours=1)
    assert not _is_offered(ActivitySuggestionId.BUILD_APP, started(timedelta(seconds=0)))
    assert not _is_offered(ActivitySuggestionId.BUILD_APP, started(timedelta(minutes=59, seconds=59)))
    # Older than an hour means more than one, so the hour itself still holds it back.
    assert not _is_offered(ActivitySuggestionId.BUILD_APP, started(timedelta(hours=1)))
    assert _is_offered(ActivitySuggestionId.BUILD_APP, started(timedelta(hours=1, seconds=1)))
    # A marker set after "now" (two clocks that disagree) is a build under way, not an old one.
    assert not _is_offered(ActivitySuggestionId.BUILD_APP, started(timedelta(minutes=-5)))


def test_connecting_data_is_suggested_until_a_request_is_filed_or_the_coach_was_shown() -> None:
    assert _is_offered(ActivitySuggestionId.CONNECT_DATA, _NOTHING_DONE)
    assert not _is_offered(ActivitySuggestionId.CONNECT_DATA, _with(permission_request_filed=_SET_MARKER))
    assert not _is_offered(ActivitySuggestionId.CONNECT_DATA, _with(connect_data_coach_shown=_SET_MARKER))
    assert not _is_offered(
        ActivitySuggestionId.CONNECT_DATA,
        _with(permission_request_filed=_SET_MARKER, connect_data_coach_shown=_SET_MARKER),
    )


def test_a_fact_that_could_not_be_read_counts_against_its_suggestion_alone() -> None:
    assert _offered_ids(_with(second_chat_started=UNREADABLE_MARKER)) == ["phone_access", "build_app", "connect_data"]
    assert _offered_ids(_with(phone_layout_used=UNREADABLE_MARKER)) == ["new_chat", "build_app", "connect_data"]
    assert _offered_ids(_with(app_build_started=UNREADABLE_MARKER)) == ["new_chat", "phone_access", "connect_data"]
    assert _offered_ids(_with(has_user_built_app=None)) == ["new_chat", "phone_access", "connect_data"]
    assert _offered_ids(_with(permission_request_filed=UNREADABLE_MARKER)) == ["new_chat", "phone_access", "build_app"]
    assert _offered_ids(_with(connect_data_coach_shown=UNREADABLE_MARKER)) == ["new_chat", "phone_access", "build_app"]
    nothing_readable = ActivityFacts(
        second_chat_started=UNREADABLE_MARKER,
        phone_layout_used=UNREADABLE_MARKER,
        app_build_started=UNREADABLE_MARKER,
        permission_request_filed=UNREADABLE_MARKER,
        connect_data_coach_shown=UNREADABLE_MARKER,
        has_user_built_app=None,
    )
    assert offered_activity_suggestions(nothing_readable, _NOW) == ()


def test_the_gate_is_open_for_the_owner_once_the_tour_has_finished_outside_a_tree_with_the_switch_on() -> None:
    for tree_step in (None, FirstRunTreeStep.DONE):
        assert (
            reason_no_activity_suggestions(
                is_owner=True, tour_finished=_SET_MARKER, tree_step=tree_step, is_placement_switched_on=True
            )
            is None
        )


def test_each_part_of_the_gate_closes_it_by_itself() -> None:
    def reason(
        is_owner: bool = True,
        tour_finished: MarkerReading = _SET_MARKER,
        tree_step: FirstRunTreeStep | None = None,
        is_placement_switched_on: bool = True,
    ) -> str | None:
        return reason_no_activity_suggestions(
            is_owner=is_owner,
            tour_finished=tour_finished,
            tree_step=tree_step,
            is_placement_switched_on=is_placement_switched_on,
        )

    assert reason() is None
    assert reason(is_owner=False) == "the viewer is not the workspace's owner"
    assert reason(is_placement_switched_on=False) == "the placement is switched off"
    assert reason(tour_finished=UNSET_MARKER) == "the tour has not finished"
    assert reason(tour_finished=UNREADABLE_MARKER) == "whether the tour has finished could not be read"
    for in_progress in (
        FirstRunTreeStep.START,
        FirstRunTreeStep.CATEGORIES,
        FirstRunTreeStep.CONNECTORS,
        FirstRunTreeStep.IDEAS,
    ):
        assert reason(tree_step=in_progress) == "the chat's first conversation is still following its tree"


def test_each_placement_reads_its_own_switch() -> None:
    settings = ChatSettings()
    activity_off = settings.model_copy_update(to_update(settings.field_ref().is_activity_suggestions_enabled, False))
    new_chat_off = settings.model_copy_update(to_update(settings.field_ref().is_new_chat_suggestions_enabled, False))
    next_step_off = settings.model_copy_update(to_update(settings.field_ref().is_next_step_suggestions_enabled, False))

    assert all(is_placement_enabled(placement, settings) for placement in SuggestionPlacement)
    assert [is_placement_enabled(placement, activity_off) for placement in SuggestionPlacement] == [True, False, True]
    assert [is_placement_enabled(placement, new_chat_off) for placement in SuggestionPlacement] == [True, True, False]
    assert [is_placement_enabled(placement, next_step_off) for placement in SuggestionPlacement] == [False, True, True]


def test_only_the_two_placements_that_offer_activity_suggestions_are_named() -> None:
    assert activity_placement_named("activity") is SuggestionPlacement.ACTIVITY
    assert activity_placement_named("new_chat") is SuggestionPlacement.NEW_CHAT
    assert activity_placement_named("next_step") is None
    assert activity_placement_named("") is None
    assert activity_placement_named(None) is None
    assert activity_placement_named("ACTIVITY") is None


def test_only_the_connect_data_suggestion_is_retired_by_its_click() -> None:
    assert {suggestion_id: marker_set_by_click(suggestion_id) for suggestion_id in ActivitySuggestionId} == {
        ActivitySuggestionId.NEW_CHAT: None,
        ActivitySuggestionId.PHONE_ACCESS: None,
        ActivitySuggestionId.BUILD_APP: None,
        ActivitySuggestionId.CONNECT_DATA: Marker.CONNECT_DATA_COACH_SHOWN,
    }


def test_a_marker_is_read_as_unset_set_with_its_time_or_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert read_marker(Marker.APP_BUILD_STARTED) == UNSET_MARKER

    mark(Marker.APP_BUILD_STARTED)
    set_at = _NOW - timedelta(minutes=30)
    os.utime(Path(os.environ[MARKER_DIR_ENV]) / "app_build_started", (set_at.timestamp(), set_at.timestamp()))

    assert read_marker(Marker.APP_BUILD_STARTED) == MarkerReading(is_readable=True, marked_at=set_at)

    # A file where the marker directory should be: nothing under it can be looked up.
    blocked = tmp_path / "blocked"
    blocked.write_text("")
    monkeypatch.setenv(MARKER_DIR_ENV, str(blocked))

    assert read_marker(Marker.APP_BUILD_STARTED) == UNREADABLE_MARKER


def _write_workspace(repo_root: Path) -> Path:
    """A workspace whose root project lists one app, with a second app its user scaffolded; answers the registry."""
    (repo_root / "pyproject.toml").write_text(
        '[project]\nname = "workspace"\nversion = "0.1.0"\n\n[tool.uv.workspace]\nmembers = ["system/apps/chat"]\n'
    )
    for package, name in (("chat", "chat"), ("todo_list", "todo-list")):
        manifest = f'name = "{name}"\ndisplay_name = "{name.title()}"\nicon = "icon.svg"\n'
        app_directory = write_app_manifest(repo_root, package, manifest, True).parent
        (app_directory / "pyproject.toml").write_text(f'[project]\nname = "{name}"\nversion = "0.1.0"\n')
    return repo_root / "data" / ".state" / "apps.toml"


def _row(name: str) -> str:
    return f'[[apps]]\nname = "{name}"\nurl = "http://localhost:5000"\n'


def test_the_facts_are_read_from_the_markers_and_the_registry(tmp_path: Path) -> None:
    registry = _write_workspace(tmp_path)
    registry.parent.mkdir(parents=True)
    registry.write_text(_row("chat"))

    assert read_activity_facts(tmp_path, registry) == _NOTHING_DONE
    assert _offered_ids(read_activity_facts(tmp_path, registry)) == [
        "new_chat",
        "phone_access",
        "build_app",
        "connect_data",
    ]

    mark(Marker.SECOND_CHAT_STARTED)
    mark(Marker.CONNECT_DATA_COACH_SHOWN)
    registry.write_text(_row("chat") + _row("todo-list"))
    facts = read_activity_facts(tmp_path, registry)

    assert facts.has_user_built_app is True
    assert facts.second_chat_started.marked_at is not None
    assert facts.connect_data_coach_shown.marked_at is not None
    assert (facts.phone_layout_used, facts.permission_request_filed, facts.app_build_started) == (
        UNSET_MARKER,
        UNSET_MARKER,
        UNSET_MARKER,
    )
    assert _offered_ids(facts) == ["phone_access"]


def test_a_registry_or_markers_that_cannot_be_read_offer_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = _write_workspace(tmp_path)
    registry.parent.mkdir(parents=True)
    registry.write_text("[[apps]\nname = \n")

    unreadable_registry = read_activity_facts(tmp_path, registry)

    assert unreadable_registry.has_user_built_app is None
    assert _offered_ids(unreadable_registry) == ["new_chat", "phone_access", "connect_data"]

    blocked = tmp_path / "blocked"
    blocked.write_text("")
    monkeypatch.setenv(MARKER_DIR_ENV, str(blocked))

    assert _offered_ids(read_activity_facts(tmp_path, registry)) == []
