import pytest
from app_instances.data_types import InstanceLifetime, InstanceRecord, InstanceStatus
from app_instances.errors import (
    InstanceConflictError,
    InvalidInstanceValueError,
    InvalidParamsError,
    NotReadyError,
    NotRenameableError,
    UnknownActionError,
    UnknownInstanceError,
)
from app_instances.primitives import (
    AbsoluteHttpUrl,
    InstanceKey,
    InstanceTitle,
    LocationPath,
)
from app_manifest.primitives import ActionId
from browser.data_types import BrowserController, BrowserLifecycle, BrowserSnapshot
from browser.instances import (
    NEW_ACTION_ID,
    FleetInstanceSource,
    instance_status_for_browser,
)
from browser.primitives import BrowserName
from mock_fleet_test import FakeFleet

_URL = AbsoluteHttpUrl("https://example.com/page")


def _snapshot(
    name: str,
    lifecycle: BrowserLifecycle = BrowserLifecycle.RUNNING,
    controller: BrowserController = BrowserController.HUMAN,
) -> BrowserSnapshot:
    return BrowserSnapshot(
        name=BrowserName(name), lifecycle=lifecycle, controller=controller
    )


def _source(*snapshots: BrowserSnapshot) -> tuple[FleetInstanceSource, FakeFleet]:
    fleet = FakeFleet(browsers=list(snapshots))
    return FleetInstanceSource(fleet=fleet), fleet


def test_list_maps_numbered_and_legacy_names_to_the_contracts_browser_row() -> None:
    source, _ = _source(_snapshot("browser-2"), _snapshot("alex-smith"))

    assert source.list_instances() == [
        InstanceRecord(
            key=InstanceKey("browser-2"),
            url="/?session=browser-2",
            title=InstanceTitle("Browser 2"),
            status=InstanceStatus.IDLE,
            lifetime=InstanceLifetime.EXPLICIT,
            last_active=None,
            renameable=False,
        ),
        InstanceRecord(
            key=InstanceKey("alex-smith"),
            url="/?session=alex-smith",
            title=InstanceTitle("alex-smith"),
            status=InstanceStatus.IDLE,
            lifetime=InstanceLifetime.EXPLICIT,
            last_active=None,
            renameable=False,
        ),
    ]


@pytest.mark.parametrize(
    ("lifecycle", "controller", "status"),
    [
        (BrowserLifecycle.RUNNING, BrowserController.HUMAN, InstanceStatus.IDLE),
        (BrowserLifecycle.RUNNING, BrowserController.AGENT, InstanceStatus.WORKING),
        (BrowserLifecycle.INIT, BrowserController.HUMAN, InstanceStatus.IDLE),
        (BrowserLifecycle.CRASHED, BrowserController.HUMAN, InstanceStatus.ERROR),
        (BrowserLifecycle.CRASHED, BrowserController.AGENT, InstanceStatus.ERROR),
    ],
)
def test_status_derives_from_ownership_and_lifecycle(
    lifecycle: BrowserLifecycle,
    controller: BrowserController,
    status: InstanceStatus,
) -> None:
    assert (
        instance_status_for_browser(_snapshot("browser-1", lifecycle, controller))
        == status
    )


def test_create_delegates_to_the_fleet_and_returns_the_new_browser() -> None:
    source, fleet = _source(_snapshot("browser-1"))

    record = source.create_instance(NEW_ACTION_ID, {})

    assert record.key == "browser-2"
    assert record.title == "Browser 2"
    assert record.status == InstanceStatus.IDLE
    assert [snapshot.name for snapshot in fleet.browsers] == ["browser-1", "browser-2"]


def test_create_refuses_other_actions_and_any_param() -> None:
    source, fleet = _source()

    with pytest.raises(UnknownActionError, match="unknown action 'open'"):
        source.create_instance(ActionId("open"), {})
    with pytest.raises(InvalidParamsError, match="takes none"):
        source.create_instance(NEW_ACTION_ID, {"url": "https://example.com"})
    assert fleet.browsers == []


def test_create_turns_a_fleet_refusal_into_a_conflict_with_its_detail() -> None:
    source, fleet = _source()
    fleet.create_refusal = "2/2 browsers open -- close one first."

    with pytest.raises(InstanceConflictError, match="close one first"):
        source.create_instance(NEW_ACTION_ID, {})


def test_create_is_not_gated_on_the_init_gate() -> None:
    source, fleet = _source()
    fleet.is_fleet_ready = False

    assert source.create_instance(NEW_ACTION_ID, {}).key == "browser-1"


def test_delete_closes_the_browser_and_ignores_keys_no_browser_can_have() -> None:
    source, fleet = _source(_snapshot("browser-1"))

    source.delete_instance(InstanceKey("browser-1"))
    source.delete_instance(InstanceKey("browser-9"))
    source.delete_instance(InstanceKey("Not.A.Browser"))

    assert fleet.closed_names == ["browser-1", "browser-9"]
    assert fleet.browsers == []


def test_rename_is_refused() -> None:
    source, _ = _source(_snapshot("browser-1"))

    with pytest.raises(NotRenameableError):
        source.rename_instance(InstanceKey("browser-1"), InstanceTitle("Research"))


def test_location_navigates_the_browser_and_returns_its_record() -> None:
    source, fleet = _source(_snapshot("browser-1"))

    record = source.set_location(InstanceKey("browser-1"), _URL)

    assert fleet.navigations == [("browser-1", _URL)]
    assert record.key == "browser-1"
    assert record.url == "/?session=browser-1"


def test_location_refuses_a_rooted_path_for_the_browser() -> None:
    source, fleet = _source(_snapshot("browser-1"))

    with pytest.raises(InvalidInstanceValueError, match="absolute http"):
        source.set_location(InstanceKey("browser-1"), LocationPath("/docs/"))
    assert fleet.navigations == []


def test_location_of_an_unknown_browser_is_unknown() -> None:
    source, _ = _source(_snapshot("browser-1"))

    with pytest.raises(UnknownInstanceError):
        source.set_location(InstanceKey("browser-2"), _URL)
    with pytest.raises(UnknownInstanceError):
        source.set_location(InstanceKey("Not.A.Browser"), _URL)


@pytest.mark.parametrize(
    ("lifecycle", "controller", "detail"),
    [
        (BrowserLifecycle.RUNNING, BrowserController.AGENT, "held by an agent"),
        (BrowserLifecycle.INIT, BrowserController.HUMAN, "is init"),
        (BrowserLifecycle.CRASHED, BrowserController.HUMAN, "is crashed"),
    ],
)
def test_location_is_a_conflict_while_held_launching_or_crashed(
    lifecycle: BrowserLifecycle, controller: BrowserController, detail: str
) -> None:
    source, fleet = _source(_snapshot("browser-1", lifecycle, controller))

    with pytest.raises(InstanceConflictError, match=detail):
        source.set_location(InstanceKey("browser-1"), _URL)
    assert fleet.navigations == []


def test_location_is_a_conflict_when_chromium_refuses_the_navigation() -> None:
    source, fleet = _source(_snapshot("browser-1"))
    fleet.navigation_failure = "net::ERR_NAME_NOT_RESOLVED"

    with pytest.raises(InstanceConflictError, match="ERR_NAME_NOT_RESOLVED"):
        source.set_location(InstanceKey("browser-1"), _URL)


def test_reads_close_and_navigate_are_refused_until_the_init_gate_opens() -> None:
    source, fleet = _source(_snapshot("browser-1"))
    fleet.is_fleet_ready = False

    with pytest.raises(NotReadyError):
        source.list_instances()
    with pytest.raises(NotReadyError):
        source.delete_instance(InstanceKey("browser-1"))
    with pytest.raises(NotReadyError):
        source.set_location(InstanceKey("browser-1"), _URL)
    assert fleet.closed_names == []
    assert fleet.navigations == []
