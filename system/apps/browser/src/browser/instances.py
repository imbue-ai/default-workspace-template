from collections.abc import Mapping
from typing import Final, assert_never

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
from app_instances.interfaces import InstanceSourceInterface
from app_instances.primitives import (
    AbsoluteHttpUrl,
    InstanceKey,
    InstanceTitle,
    LocationTarget,
)
from app_manifest.primitives import ActionId
from imbue.imbue_common.pure import pure
from loguru import logger
from pydantic import Field

from browser.data_types import BrowserController, BrowserLifecycle, BrowserSnapshot
from browser.errors import (
    BrowserHeldByAgentError,
    BrowserNotDrivableError,
    FleetCreateRefusedError,
    InvalidBrowserNameValueError,
    NavigationFailedError,
    UnknownBrowserError,
)
from browser.interfaces import FleetInterface
from browser.primitives import (
    BrowserName,
    derive_browser_title,
    instance_url_for_browser,
)

# The one action the manifest declares (system/apps/browser/app.toml); it takes no params.
NEW_ACTION_ID: Final[ActionId] = ActionId("new")


@pure
def instance_status_for_browser(snapshot: BrowserSnapshot) -> InstanceStatus:
    """The contract's status (contracts.md section 4.3): ``working`` while an agent holds the browser, ``error`` once it crashed, else ``idle`` (a launch in progress included)."""
    match snapshot.lifecycle:
        case BrowserLifecycle.CRASHED:
            return InstanceStatus.ERROR
        case BrowserLifecycle.INIT | BrowserLifecycle.RUNNING:
            match snapshot.controller:
                case BrowserController.AGENT:
                    return InstanceStatus.WORKING
                case BrowserController.HUMAN:
                    return InstanceStatus.IDLE
                case _ as unreachable:
                    assert_never(unreachable)
        case _ as unreachable:
            assert_never(unreachable)


@pure
def instance_record_for_browser(snapshot: BrowserSnapshot) -> InstanceRecord:
    return InstanceRecord(
        key=InstanceKey(snapshot.name),
        url=instance_url_for_browser(snapshot.name),
        title=derive_browser_title(snapshot.name),
        status=instance_status_for_browser(snapshot),
        lifetime=InstanceLifetime.EXPLICIT,
        # The fleet clocks agent activity on a monotonic lease timer, not wall time.
        last_active=None,
        renameable=False,
    )


@pure
def _browser_name_for_key(key: InstanceKey) -> BrowserName:
    """The key as a browser name; a key no browser can have names no instance."""
    try:
        return BrowserName(key)
    except InvalidBrowserNameValueError as e:
        raise UnknownInstanceError(f"no browser has the key {key!r}") from e


class FleetInstanceSource(InstanceSourceInterface):
    """The browser's instances: one per browser in the fleet, with status from its ownership state.

    Reads and the close and navigate verbs are refused (NotReadyError, a 503) until the init
    gate opens, as the daemon's own routes are; create is not, because the daemon takes a
    create during restore (it queues behind the serialized relaunches), and the shell's next
    fetch picks the new browser up.
    """

    fleet: FleetInterface = Field(
        frozen=True, description="The fleet, reached from any thread"
    )

    def list_instances(self) -> list[InstanceRecord]:
        self._require_ready()
        return [
            instance_record_for_browser(snapshot)
            for snapshot in self.fleet.list_browsers()
        ]

    def create_instance(
        self, action: ActionId, params: Mapping[str, str]
    ) -> InstanceRecord:
        if action != NEW_ACTION_ID:
            raise UnknownActionError(
                f"unknown action {action!r}: the browser only declares {NEW_ACTION_ID!r}"
            )
        if params:
            raise InvalidParamsError(
                f"unknown params {sorted(params)}: {NEW_ACTION_ID!r} takes none"
            )
        try:
            snapshot = self.fleet.create_browser()
        except FleetCreateRefusedError as e:
            raise InstanceConflictError(str(e)) from e
        return instance_record_for_browser(snapshot)

    def delete_instance(self, key: InstanceKey) -> None:
        self._require_ready()
        try:
            name = _browser_name_for_key(key)
        except UnknownInstanceError:
            # DELETE of an unknown key is a 204 by contract; a key no browser can have is one.
            logger.debug("Ignored deleting {!r}: no browser can have that key", key)
            return
        self.fleet.close_browser(name)

    def rename_instance(self, key: InstanceKey, title: InstanceTitle) -> InstanceRecord:
        raise NotRenameableError("browsers cannot be renamed")

    def set_location(self, key: InstanceKey, path: LocationTarget) -> InstanceRecord:
        self._require_ready()
        if not isinstance(path, AbsoluteHttpUrl):
            raise InvalidInstanceValueError(
                f"invalid path {path!r}: the browser navigates to absolute http(s) URLs only"
            )
        name = _browser_name_for_key(key)
        try:
            self.fleet.navigate_browser(name, path)
        except UnknownBrowserError as e:
            raise UnknownInstanceError(f"no browser has the key {key!r}") from e
        except (
            BrowserNotDrivableError,
            BrowserHeldByAgentError,
            NavigationFailedError,
        ) as e:
            raise InstanceConflictError(str(e)) from e
        return self._record_for_name(name)

    def _require_ready(self) -> None:
        if not self.fleet.is_ready():
            raise NotReadyError(
                "the browser fleet is still restoring the saved browsers; try again in a moment"
            )

    def _record_for_name(self, name: BrowserName) -> InstanceRecord:
        for snapshot in self.fleet.list_browsers():
            if snapshot.name == name:
                return instance_record_for_browser(snapshot)
        raise UnknownInstanceError(f"no browser has the key {name!r}")
