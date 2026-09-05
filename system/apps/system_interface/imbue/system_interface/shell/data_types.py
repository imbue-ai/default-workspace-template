from typing import Any

from app_instances.data_types import InstanceLifetime
from app_instances.data_types import InstanceRecord
from app_instances.data_types import InstanceStatus
from app_instances.primitives import InstanceKey
from app_manifest.manifest import DefaultShortcut
from app_manifest.manifest import ShortcutMode
from app_manifest.primitives import ActionId
from app_manifest.primitives import AppName
from app_manifest.registry import RegistryAction
from app_manifest.registry import RegistryRow
from pydantic import AwareDatetime
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.primitives import NonEmptyStr
from imbue.imbue_common.pure import pure
from imbue.system_interface.shell.primitives import Address
from imbue.system_interface.shell.primitives import ClientId
from imbue.system_interface.shell.primitives import DeviceKind
from imbue.system_interface.shell.primitives import ProjectId
from imbue.system_interface.shell.primitives import TabId
from imbue.system_interface.shell.primitives import ViewId
from imbue.system_interface.shell.primitives import address_for


class Shortcut(FrozenModel):
    """One rail entry of a project: an app's action, in focus or new mode."""

    app: AppName = Field(description="The registered app")
    action: ActionId = Field(description="The action the row runs")
    mode: ShortcutMode = Field(description="Focus the app's most recent tab first, or always run the action")


class Project(FrozenModel):
    """A named view: its display metadata, its shared tab set, and its shortcuts (contracts.md section 6)."""

    id: ProjectId = Field(description="The slugified name, stable across renames")
    name: str = Field(description="Free-form name shown in the UI")
    color: str = Field(description="Accent color as a '#RRGGBB' string")
    glyph: int = Field(description="Index into the frontend's squiggle glyph table")
    tabs: tuple[Address, ...] = Field(description="Every instance the project shows, in the order added")
    shortcuts: tuple[Shortcut, ...] = Field(description="The rail rows, in rail order")


class TabRecord(FrozenModel):
    """What one panel of a client's layout shows (the ``tabs`` map of a layout, contracts.md section 6)."""

    address: Address = Field(description="The instance the tab shows")
    tab_id: TabId = Field(description="The id minted when the panel was created")
    last_focused_ms: int = Field(description="Epoch milliseconds the tab was last the active one, 0 when never")


class LayoutRecord(FrozenModel):
    """One client's arrangement of one view (contracts.md section 6)."""

    dockview: dict[str, Any] | None = Field(description="The serialized dockview grid, None for a never-arranged view")
    tabs: dict[str, TabRecord] = Field(description="Each panel's tab record, keyed by dockview panel id")
    device_kind: DeviceKind = Field(description="The device kind the arrangement was made on")
    updated_at: AwareDatetime | None = Field(
        description="When the arrangement was last saved, None for the empty layout"
    )


class ClientRecord(FrozenModel):
    """What the shell keeps about one browser context (contracts.md section 7)."""

    id: ClientId = Field(description="The client's stored id")
    device_kind: DeviceKind = Field(description="Desktop or mobile")
    active_view: ViewId = Field(description="The view the client is on")
    last_seen: AwareDatetime = Field(description="When the client last reported")


class InventoryInstance(FrozenModel):
    """One instance as the shell lists it: the app's record plus its address; the synthesized record of a single-instance app has an empty key."""

    key: str = Field(description="The app-scoped key; empty for a single-instance app's one record")
    url: str = Field(description="Where the instance's page is, as a path under the app's origin")
    title: str = Field(description="What users see")
    status: InstanceStatus = Field(description="What the instance is doing")
    lifetime: InstanceLifetime = Field(description="Whether it lives until deleted or only while referenced")
    last_active: AwareDatetime | None = Field(description="When it was last active, None when unknown")
    renameable: bool = Field(description="Whether the rename route is accepted")

    @pure
    def address(self, app: AppName) -> Address:
        return address_for(app, None if self.key == "" else InstanceKey(self.key))


@pure
def inventory_instance_from_record(record: InstanceRecord) -> InventoryInstance:
    return InventoryInstance(
        key=str(record.key),
        url=str(record.url),
        title=str(record.title),
        status=record.status,
        lifetime=record.lifetime,
        last_active=record.last_active,
        renameable=record.renameable,
    )


@pure
def synthesized_single_instance(row: RegistryRow, is_running: bool) -> InventoryInstance:
    """The one record a single-instance app carries (contracts.md section 8)."""
    return InventoryInstance(
        key="",
        url="/",
        title=str(row.display_name) if row.display_name is not None else str(row.name),
        status=InstanceStatus.IDLE if is_running else InstanceStatus.STOPPED,
        lifetime=InstanceLifetime.EXPLICIT,
        last_active=None,
        renameable=False,
    )


class AppInventoryEntry(FrozenModel):
    """One app of the inventory: its registry row, whether it runs, and its instances as last fetched."""

    row: RegistryRow = Field(description="The registry row, validated on read")
    is_running: bool = Field(description="Derived from supervisord or a TCP probe, never stored")
    instances: tuple[InventoryInstance, ...] = Field(description="The app's instances, in the app's list order")
    # A record the shell has held for less than the grace period is not deleted for being
    # unreferenced: the create that made it has returned but the tab docking it may not have
    # been saved yet.
    first_seen_at_by_key: dict[str, float] = Field(
        description="Monotonic seconds each key was first listed, for the referenced-deletion grace"
    )

    @pure
    def address_of(self, instance: InventoryInstance) -> Address:
        return instance.address(self.row.name)

    @pure
    def addresses(self) -> list[Address]:
        return [self.address_of(instance) for instance in self.instances]


@pure
def app_wire_json(entry: AppInventoryEntry) -> dict[str, Any]:
    """The ``app`` object of contracts.md section 8."""
    row = entry.row
    return {
        "name": str(row.name),
        "display_name": str(row.display_name) if row.display_name is not None else str(row.name),
        "icon": row.icon or "",
        "label": row.label,
        "url": str(row.url),
        "internal": row.internal,
        "program": row.program or "",
        "critical": row.critical,
        "instances_url": str(row.instances_url) if row.instances_url is not None else str(row.url),
        "has_instances": row.instances,
        "actions": [action_wire_json(action) for action in effective_actions(row)],
        "default_shortcut": default_shortcut_wire_json(row.default_shortcut),
        "is_running": entry.is_running,
        "instances": [instance.model_dump(mode="json") for instance in entry.instances],
    }


@pure
def action_wire_json(action: RegistryAction) -> dict[str, str]:
    return {"id": str(action.id), "label": str(action.label)}


@pure
def default_shortcut_wire_json(shortcut: DefaultShortcut | None) -> dict[str, str] | None:
    if shortcut is None:
        return None
    return {"action": str(shortcut.action), "mode": shortcut.mode.value}


# The one action every single-instance app has, synthesized by the shell (contracts.md section 2).
OPEN_ACTION: RegistryAction = RegistryAction(id=ActionId("open"), label=NonEmptyStr("Open"))


@pure
def effective_actions(row: RegistryRow) -> tuple[RegistryAction, ...]:
    """The actions an app offers: its declared ones, or the synthesized ``open`` for a single-instance app."""
    if row.instances:
        return row.actions
    display = str(row.display_name) if row.display_name is not None else str(row.name)
    return (RegistryAction(id=OPEN_ACTION.id, label=NonEmptyStr(f"Open {display}")),)


class ClientStateReport(FrozenModel):
    """The inbound ``client_state`` WebSocket message (contracts.md section 8)."""

    client_id: ClientId = Field(description="The reporting client")
    device_kind: DeviceKind = Field(description="Desktop or mobile")
    active_view: ViewId = Field(description="The view the client is on now")
    previous_view: str = Field(default="", description="The view it was on before, empty on connect")


class ClientActivityReport(FrozenModel):
    """The body of ``POST /api/client-activity`` (contracts.md section 5)."""

    client_id: ClientId = Field(description="The client the activity belongs to")
    device_kind: DeviceKind = Field(description="Desktop or mobile")
    view_id: ViewId = Field(description="The view the client was on")
    kind: str = Field(description="'message' or 'view_switch'")
    app: str = Field(default="", description="The app a message went to")
    key: str = Field(default="", description="The instance key a message went to")
    text: str = Field(default="", description="The message text, truncated at write time")
    from_view_id: str = Field(default="", description="For a view switch, the view left")


class TabInstanceReport(FrozenModel):
    """The body of ``POST /api/tabs/<tab_id>/instance`` (contracts.md section 5)."""

    app: AppName = Field(description="The app that owns the tab's instance")
    key: str = Field(description="The key the tab now shows")


class LayoutSaveRequest(FrozenModel):
    """The body of ``POST /api/layouts/<view_id>`` (contracts.md section 6)."""

    client_id: ClientId = Field(description="The saving client")
    save_id: str = Field(default="", description="The save id the window minted")
    device_kind: DeviceKind = Field(description="The device kind the arrangement was made on")
    dockview: dict[str, Any] | None = Field(description="The serialized dockview grid")
    tabs: dict[str, TabRecord] = Field(description="Each panel's tab record")
