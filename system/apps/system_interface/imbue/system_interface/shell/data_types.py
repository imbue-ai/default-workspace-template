from typing import Any
from typing import Final

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
from loguru import logger
from pydantic import AwareDatetime
from pydantic import ConfigDict
from pydantic import Field
from pydantic import ValidationError
from pydantic import model_validator

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.primitives import NonEmptyStr
from imbue.imbue_common.pure import pure
from imbue.system_interface.shell.primitives import Address
from imbue.system_interface.shell.primitives import ClientActivityKind
from imbue.system_interface.shell.primitives import ClientId
from imbue.system_interface.shell.primitives import DeviceKind
from imbue.system_interface.shell.primitives import ProjectId
from imbue.system_interface.shell.primitives import SaveId
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


# The ``kind`` the ``params`` of a dockview panel showing an instance carry (contracts.md section 6). A
# launcher panel (the New Tab page) carries another kind and names no instance, so the shell never looks for it.
INSTANCE_PANEL_KIND: Final[str] = "instance"


class InstancePanelParams(FrozenModel):
    """The ``params`` dockview stores on a panel showing an instance: the one place a tab's identity lives (contracts.md section 6)."""

    # The browser owns this object and may add keys the shell does not know; reading tolerates them, and
    # the shell edits the stored dict in place rather than round-tripping it through this model.
    model_config = ConfigDict(frozen=True, extra="ignore")

    address: Address = Field(description="The instance the panel shows")
    tab_id: TabId = Field(
        alias="tabId",
        description="The page's id: minted when the page was first opened, shared by every panel showing it",
    )
    last_focused_ms: int = Field(
        default=0,
        alias="lastFocusedMs",
        description="Epoch milliseconds the panel was last the active one, 0 when never",
    )


@pure
def _panel_entries(dockview: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    panels = dockview.get("panels") if dockview is not None else None
    if not isinstance(panels, dict):
        return {}
    return {panel_id: entry for panel_id, entry in panels.items() if isinstance(entry, dict)}


def instance_panel_params_by_id(dockview: dict[str, Any] | None) -> dict[str, InstancePanelParams]:
    """Each instance panel's params, keyed by dockview panel id. Launchers are skipped; an instance panel whose params
    do not parse is skipped with a warning, so a damaged entry costs one tab rather than the whole arrangement."""
    parsed: dict[str, InstancePanelParams] = {}
    for panel_id, entry in _panel_entries(dockview).items():
        params = entry.get("params")
        if not isinstance(params, dict) or params.get("kind") != INSTANCE_PANEL_KIND:
            continue
        try:
            parsed[panel_id] = InstancePanelParams.model_validate(params)
        except ValidationError as e:
            logger.warning("Skipped panel {} with unreadable params: {}", panel_id, e.errors()[0]["msg"])
    return parsed


@pure
def instance_panel_params_json(address: Address, tab_id: TabId, last_focused_ms: int) -> dict[str, Any]:
    """The ``params`` entry the shell writes for an instance panel, in the browser's spelling."""
    return {
        "kind": INSTANCE_PANEL_KIND,
        "address": str(address),
        "tabId": str(tab_id),
        "lastFocusedMs": last_focused_ms,
    }


@pure
def with_panel_params_address(dockview: dict[str, Any], panel_id: str, address: Address) -> dict[str, Any]:
    """The document with the params of ``panel_id`` pointed at ``address``, every other key of the params kept."""
    panels = dockview["panels"]
    entry = panels[panel_id]
    return {
        **dockview,
        "panels": {**panels, panel_id: {**entry, "params": {**entry["params"], "address": str(address)}}},
    }


# CLEANUP: drop this fold (and the ``tabs`` mention in the docstrings that cite it) once every workspace has saved
# a layout with a shell from after the workspace app model's params-only layout files: a file written by the
# older shell carried a ``tabs`` block beside the dockview document, and that block was the truth of each
# panel's identity.
@pure
def fold_legacy_tabs_into_dockview(data: Any) -> Any:
    """A layout body in the older shape, with its ``tabs`` block folded into each panel's ``params``; any other value unchanged."""
    if not isinstance(data, dict) or "tabs" not in data:
        return data
    without_tabs = {key: value for key, value in data.items() if key != "tabs"}
    tabs = data["tabs"]
    dockview = without_tabs.get("dockview")
    if not isinstance(tabs, dict) or not isinstance(dockview, dict) or not isinstance(dockview.get("panels"), dict):
        return without_tabs
    panels = dict(dockview["panels"])
    for panel_id, tab in tabs.items():
        entry = panels.get(panel_id)
        if not isinstance(tab, dict) or not isinstance(entry, dict):
            continue
        panels[panel_id] = {
            **entry,
            "params": {
                "kind": INSTANCE_PANEL_KIND,
                "address": tab.get("address"),
                "tabId": tab.get("tab_id"),
                "lastFocusedMs": tab.get("last_focused_ms", 0),
            },
        }
    return {**without_tabs, "dockview": {**dockview, "panels": panels}}


class LayoutRecord(FrozenModel):
    """One client's arrangement of one view (contracts.md section 6): dockview's own document, whose per-panel ``params`` name what each tab shows."""

    dockview: dict[str, Any] | None = Field(description="The serialized dockview grid, None for a never-arranged view")
    device_kind: DeviceKind = Field(description="The device kind the arrangement was made on")
    updated_at: AwareDatetime | None = Field(
        description="When the arrangement was last saved, None for the empty layout"
    )

    @model_validator(mode="before")
    @classmethod
    def _fold_legacy_tabs(cls, data: Any) -> Any:
        return fold_legacy_tabs_into_dockview(data)


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
    stoppable: bool = Field(description="Whether the stop and start routes are accepted")

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
        stoppable=record.stoppable,
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
        # The app-level Stop and Start are the single-instance app's; its one record has none of its own.
        renameable=False,
        stoppable=False,
    )


class AppInventoryEntry(FrozenModel):
    """One app of the inventory: its registry row, whether it runs, and its instances as last fetched."""

    row: RegistryRow = Field(description="The registry row, validated on read")
    is_running: bool = Field(description="Derived from supervisord or a TCP probe, never stored")
    instances: tuple[InventoryInstance, ...] = Field(description="The app's instances, in the app's list order")
    # False until the app's instances API has answered a list once (a single-instance app's one
    # record is synthesized, so it counts as listed): an empty list that was never fetched is
    # not evidence that an address is gone, and a client must not prune on it.
    is_listed: bool = Field(description="Whether the instance list is the app's own answer rather than the seed")
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
        "instances_url": instances_url_of(row),
        "has_instances": row.instances,
        "actions": [action_wire_json(action) for action in effective_actions(row)],
        "default_shortcut": default_shortcut_wire_json(row.default_shortcut),
        "launcher_rank": row.launcher_rank,
        "is_running": entry.is_running,
        "is_listed": entry.is_listed,
        "instances": [instance.model_dump(mode="json") for instance in entry.instances],
    }


@pure
def instances_url_of(row: RegistryRow) -> str:
    """Where the app's instances API is reached: its ``instances_url``, else its ``url`` (contracts.md section 3)."""
    return str(row.instances_url) if row.instances_url is not None else str(row.url)


@pure
def action_wire_json(action: RegistryAction) -> dict[str, Any]:
    return {"id": str(action.id), "label": str(action.label), "params": [str(param) for param in action.params]}


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
    kind: ClientActivityKind = Field(description="A message sent to an instance, or a view switch")
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
    save_id: SaveId = Field(description="The save id the window minted, echoed in the layout_updated broadcast")
    base_updated_at: AwareDatetime | None = Field(
        default=None,
        description="The updated_at of the arrangement the window last fetched or saved; None for one it only saw empty",
    )
    device_kind: DeviceKind = Field(description="The device kind the arrangement was made on")
    dockview: dict[str, Any] | None = Field(description="The serialized dockview grid, its panels' params included")

    @model_validator(mode="before")
    @classmethod
    def _fold_legacy_tabs(cls, data: Any) -> Any:
        return fold_legacy_tabs_into_dockview(data)


class ClientReportOutcome(FrozenModel):
    """What recording a ``client_state`` report came to: the record, and whether its active view moved."""

    record: ClientRecord = Field(description="The client record as written")
    is_active_view_changed: bool = Field(description="Whether the stored active view differs from before the report")


class LayoutEditOutcome(FrozenModel):
    """What editing a client's layout under the state lock came to: the arrangement now in force, and whether it was written."""

    layout: LayoutRecord = Field(description="The arrangement after the edit, stamped when it was written")
    is_written: bool = Field(
        description="Whether the edit changed the arrangement and was written to the client's file"
    )
