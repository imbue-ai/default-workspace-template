from pathlib import Path

import pytest

from onboarding_activity import markers
from onboarding_activity.items import (
    OnboardingItem,
    done_marker,
    is_item_done,
    item_label,
)
from onboarding_activity.markers import Marker


def test_the_items_are_the_four_in_order_with_their_words() -> None:
    assert [(item.value, item_label(item)) for item in OnboardingItem] == [
        ("new_chat", "Work in parallel by starting a new chat"),
        ("phone_access", "Access Studio from your phone"),
        ("build_app", "Build an app"),
        ("connect_data", "Connect some data"),
    ]


def test_each_item_but_building_an_app_is_done_by_its_own_marker() -> None:
    assert {item: done_marker(item) for item in OnboardingItem} == {
        OnboardingItem.NEW_CHAT: Marker.SECOND_CHAT_STARTED,
        OnboardingItem.PHONE_ACCESS: Marker.PHONE_LAYOUT_USED,
        OnboardingItem.BUILD_APP: None,
        OnboardingItem.CONNECT_DATA: Marker.PERMISSION_REQUEST_FILED,
    }


@pytest.mark.parametrize(
    ("item", "marker"),
    [
        (OnboardingItem.NEW_CHAT, Marker.SECOND_CHAT_STARTED),
        (OnboardingItem.PHONE_ACCESS, Marker.PHONE_LAYOUT_USED),
        (OnboardingItem.CONNECT_DATA, Marker.PERMISSION_REQUEST_FILED),
    ],
)
def test_an_item_is_done_once_its_marker_is_set_whatever_the_registry_holds(
    item: OnboardingItem, marker: Marker, tmp_path: Path
) -> None:
    assert not is_item_done(item, True, tmp_path)

    markers.mark(marker, tmp_path)

    assert is_item_done(item, False, tmp_path)
    assert [
        other for other in OnboardingItem if is_item_done(other, False, tmp_path)
    ] == [item]


def test_building_an_app_is_done_when_the_registry_holds_an_app_the_user_built(
    tmp_path: Path,
) -> None:
    for marker in Marker:
        markers.mark(marker, tmp_path)

    assert not is_item_done(OnboardingItem.BUILD_APP, False, tmp_path)
    assert is_item_done(OnboardingItem.BUILD_APP, True, tmp_path)


def test_an_item_whose_marker_cannot_be_read_is_told_apart_from_one_not_done(
    tmp_path: Path,
) -> None:
    # A file where the directory should be: nothing under it can be looked up.
    not_a_directory = tmp_path / "onboarding_activity"
    not_a_directory.write_text("")

    for item in (
        OnboardingItem.NEW_CHAT,
        OnboardingItem.PHONE_ACCESS,
        OnboardingItem.CONNECT_DATA,
    ):
        with pytest.raises(markers.MarkerUnreadableError):
            is_item_done(item, False, not_a_directory)
    assert is_item_done(OnboardingItem.BUILD_APP, True, not_a_directory)
