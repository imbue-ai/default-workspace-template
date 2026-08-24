from pathlib import Path
from typing import Any

from imbue.system_interface.app_instances import allocate_app_instance
from imbue.system_interface.app_instances import instance_ref
from imbue.system_interface.app_instances import list_app_instances
from imbue.system_interface.app_instances import parse_instance_name
from imbue.system_interface.app_instances import parse_instance_ref
from imbue.system_interface.app_instances import release_app_instance
from imbue.system_interface.projects import DEFAULT_PROJECT_ID
from imbue.system_interface.projects import EVERYTHING_VIEW_ID
from imbue.system_interface.projects import add_member
from imbue.system_interface.projects import remove_member
from imbue.system_interface.projects import write_project_content


def _content_with_instance_panel(service_name: str, instance_name: str) -> dict[str, Any]:
    panel_id = f"app-instance-{instance_name}"
    return {
        "dockview": {"panels": {panel_id: {"title": service_name}}},
        "panelParams": {
            panel_id: {
                "panelType": "iframe",
                "agentId": "primary",
                "url": "http://files.host-00000000000000000000000000000000.localhost:8421/",
                "serviceName": service_name,
                "serviceInstanceId": instance_name,
            }
        },
    }


def test_instance_ref_round_trips_through_the_parser() -> None:
    ref = instance_ref("files", "files-2")
    assert ref == "service:files?instance=files-2"
    assert parse_instance_ref(ref) == ("files", "files-2")


def test_bare_service_and_browser_session_refs_are_not_instances() -> None:
    assert parse_instance_ref("service:files") is None
    assert parse_instance_ref("service:browser?session=browser-2") is None
    assert parse_instance_ref("chat:agent-1") is None


def test_parse_instance_name_recovers_the_service_even_when_it_ends_in_digits() -> None:
    assert parse_instance_name("files-2") == ("files", 2)
    assert parse_instance_name("app-2-3") == ("app-2", 3)
    assert parse_instance_name("files") is None
    assert parse_instance_name("files-0") is None


def test_a_fresh_workspace_has_no_instances(tmp_path: Path) -> None:
    assert list_app_instances(tmp_path) == {}


def test_instances_are_derived_from_member_lists(tmp_path: Path) -> None:
    add_member(tmp_path, DEFAULT_PROJECT_ID, instance_ref("files", "files-1"))
    add_member(tmp_path, DEFAULT_PROJECT_ID, instance_ref("docs", "docs-1"))
    # A bare service ref is the app's pin, not an instance.
    add_member(tmp_path, DEFAULT_PROJECT_ID, "service:files")

    assert list_app_instances(tmp_path) == {"files": ["files-1"], "docs": ["docs-1"]}


def test_instances_are_derived_from_saved_layouts_everything_included(tmp_path: Path) -> None:
    write_project_content(tmp_path, EVERYTHING_VIEW_ID, _content_with_instance_panel("files", "files-3"))
    write_project_content(tmp_path, DEFAULT_PROJECT_ID, _content_with_instance_panel("files", "files-1"), "mobile")

    assert list_app_instances(tmp_path) == {"files": ["files-1", "files-3"]}


def test_the_same_instance_referenced_twice_lists_once_ordered_by_number(tmp_path: Path) -> None:
    add_member(tmp_path, DEFAULT_PROJECT_ID, instance_ref("files", "files-2"))
    write_project_content(tmp_path, DEFAULT_PROJECT_ID, _content_with_instance_panel("files", "files-2"))
    add_member(tmp_path, DEFAULT_PROJECT_ID, instance_ref("files", "files-1"))

    assert list_app_instances(tmp_path) == {"files": ["files-1", "files-2"]}


def test_allocation_takes_the_lowest_free_number_machine_wide(tmp_path: Path) -> None:
    add_member(tmp_path, DEFAULT_PROJECT_ID, instance_ref("files", "files-1"))
    write_project_content(tmp_path, EVERYTHING_VIEW_ID, _content_with_instance_panel("files", "files-3"))

    assert allocate_app_instance(tmp_path, "files") == "files-2"


def test_allocation_reserves_names_until_they_are_referenced(tmp_path: Path) -> None:
    # Two rapid mints, neither filed yet: the reservation set is what keeps
    # them apart, since both would otherwise see the same free number.
    first = allocate_app_instance(tmp_path, "files")
    second = allocate_app_instance(tmp_path, "files")
    assert first != second

    # Filing the first releases nothing it should not: a third mint still
    # avoids both the referenced name and the outstanding reservation.
    add_member(tmp_path, DEFAULT_PROJECT_ID, instance_ref("files", first))
    third = allocate_app_instance(tmp_path, "files")
    assert third not in {first, second}


def test_a_deleted_instances_number_is_reused_once_its_reservation_is_released(tmp_path: Path) -> None:
    minted = allocate_app_instance(tmp_path, "files")
    ref = instance_ref("files", minted)
    add_member(tmp_path, DEFAULT_PROJECT_ID, ref)
    # Removing the last reference IS deletion; the delete path also releases
    # the allocator reservation, and the number frees up.
    remove_member(tmp_path, DEFAULT_PROJECT_ID, ref)
    release_app_instance(tmp_path, minted)
    assert allocate_app_instance(tmp_path, "files") == "files-1"
