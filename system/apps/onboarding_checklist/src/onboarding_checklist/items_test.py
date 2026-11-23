from pathlib import Path

import pytest

from app_manifest.errors import RegistryReadError
from onboarding_activity.markers import Marker
from onboarding_activity.markers import mark
from onboarding_checklist.items import read_checklist_items


def _refuse_to_read_the_registry() -> bool:
    raise RegistryReadError("the registry cannot be read")


def _done_by_id(has_user_built_app: bool, marker_directory: Path) -> dict[str, bool]:
    return {item.id: item.is_done for item in read_checklist_items(lambda: has_user_built_app, marker_directory)}


def test_the_checklist_lists_the_four_items_in_order_with_their_words(tmp_path: Path) -> None:
    items = read_checklist_items(lambda: False, tmp_path / "markers")

    assert [(item.id, item.label, item.is_done) for item in items] == [
        ("new_chat", "Work in parallel by starting a new chat", False),
        ("phone_access", "Access Studio from your phone", False),
        ("build_app", "Build an app", False),
        ("connect_data", "Connect some data", False),
    ]


@pytest.mark.parametrize(
    ("marker", "item_id"),
    [
        (Marker.SECOND_CHAT_STARTED, "new_chat"),
        (Marker.PHONE_LAYOUT_USED, "phone_access"),
        (Marker.PERMISSION_REQUEST_FILED, "connect_data"),
    ],
)
def test_a_marker_checks_its_own_item_and_no_other(tmp_path: Path, marker: Marker, item_id: str) -> None:
    markers = tmp_path / "markers"
    mark(marker, markers)

    done = _done_by_id(False, markers)

    assert [checked for checked, is_done in done.items() if is_done] == [item_id]


def test_an_app_the_user_built_checks_building_an_app_and_no_marker_does(tmp_path: Path) -> None:
    markers = tmp_path / "markers"
    mark(Marker.APP_BUILD_STARTED, markers)
    mark(Marker.TOUR_FINISHED, markers)

    assert not any(_done_by_id(False, markers).values())
    assert [checked for checked, is_done in _done_by_id(True, markers).items() if is_done] == ["build_app"]


def test_every_item_done_is_every_item_checked(tmp_path: Path) -> None:
    markers = tmp_path / "markers"
    for marker in (Marker.SECOND_CHAT_STARTED, Marker.PHONE_LAYOUT_USED, Marker.PERMISSION_REQUEST_FILED):
        mark(marker, markers)

    assert all(_done_by_id(True, markers).values())


def test_markers_that_cannot_be_read_leave_their_items_unchecked_and_the_list_whole(tmp_path: Path) -> None:
    not_a_directory = tmp_path / "markers"
    not_a_directory.write_text("")

    assert _done_by_id(True, not_a_directory) == {
        "new_chat": False,
        "phone_access": False,
        "build_app": True,
        "connect_data": False,
    }


def test_a_registry_that_cannot_be_read_leaves_building_an_app_unchecked_and_the_rest_as_read(
    tmp_path: Path,
) -> None:
    markers = tmp_path / "markers"
    mark(Marker.SECOND_CHAT_STARTED, markers)

    items = read_checklist_items(_refuse_to_read_the_registry, markers)

    assert {item.id: item.is_done for item in items} == {
        "new_chat": True,
        "phone_access": False,
        "build_app": False,
        "connect_data": False,
    }
