"""``ShellState``: everything the shell's routes and WebSocket loop share, built in ``main.py`` (or by a test)."""

import threading
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Final

import httpx
from app_instances.data_types import InstanceLifetime
from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.mutable_model import MutableModel
from imbue.system_interface.shell.client_activity import ClientActivityLog
from imbue.system_interface.shell.clients import ClientStore
from imbue.system_interface.shell.data_types import AppInventoryEntry
from imbue.system_interface.shell.data_types import InventoryInstance
from imbue.system_interface.shell.instance_relay import relay_delete
from imbue.system_interface.shell.inventory import AppInventory
from imbue.system_interface.shell.layout_ops import LayoutMutex
from imbue.system_interface.shell.layouts import LayoutStore
from imbue.system_interface.shell.layouts import unreferenced_addresses
from imbue.system_interface.shell.primitives import Address
from imbue.system_interface.shell.projects import ProjectStore
from imbue.system_interface.shell.projects import project_wire_json
from imbue.system_interface.ws_broadcaster import WebSocketBroadcaster

CLIENT_ACTIVITY_EVENTS_PATH: Final[str] = "events/client_activity/events.jsonl"


class ShellState(MutableModel):
    """The shell's collaborators: the inventory, the three stores, the activity log, and the broadcaster."""

    model_config = {"arbitrary_types_allowed": True, "extra": "forbid", "frozen": False}

    state_directory: Path = Field(frozen=True, description="Where the state files live (contracts.md section 7)")
    inventory: AppInventory = Field(frozen=True, description="The registry, liveness, and instance lists")
    projects: ProjectStore = Field(frozen=True, description="projects.json")
    layouts: LayoutStore = Field(frozen=True, description="The per-client layouts and seeds")
    clients: ClientStore = Field(frozen=True, description="clients.json")
    activity: ClientActivityLog = Field(frozen=True, description="The client-activity event log")
    broadcaster: WebSocketBroadcaster = Field(
        frozen=True, description="The WebSocket fan-out, shared with the chat app"
    )
    layout_mutex: LayoutMutex = Field(frozen=True, description="Serializes layout-mutating ops")
    http_client: httpx.Client = Field(frozen=True, description="The client the relay uses to reach the apps")

    _sweep_lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    def start(self) -> None:
        """Prune stale clients, then start the inventory (registry watch, liveness, instance lists)."""
        now = datetime.now(timezone.utc)
        for client_id in self.clients.prune_unseen(now):
            removed = self.layouts.delete_client_layouts(client_id)
            logger.info("Pruned client {} unseen for 90 days ({} layout file(s))", client_id, removed)
        self.inventory.add_removed_listener(self.on_instances_removed)
        self.inventory.start()

    def stop(self) -> None:
        self.inventory.stop()
        try:
            self.http_client.close()
        except (httpx.HTTPError, RuntimeError) as e:
            logger.debug("Skipped closing the relay http client during shutdown: {}", e)

    def broadcast_projects_updated(self) -> None:
        self.broadcaster.broadcast_projects_updated(
            [project_wire_json(project) for project in self.projects.list_projects()]
        )

    def on_instances_removed(self, addresses: list[Address]) -> None:
        """An app stopped listing these instances: drop them from every tab set and every client layout."""
        now = datetime.now(timezone.utc)
        changed_projects = self.projects.remove_addresses_everywhere(addresses)
        self.layouts.remove_addresses_everywhere(addresses, now)
        if changed_projects:
            self.broadcast_projects_updated()

    def delete_unreferenced_instances(self) -> list[Address]:
        """Ask each app to delete its ``referenced`` instances nothing references any more (the rule of contracts.md section 4.1).

        Runs after every layout save and tab-set removal, and holds one lock so two saves cannot
        double-delete. A fresh instance keeps its grace period, and a failed delete leaves the
        instance listed, so no second accounting is needed.
        """
        with self._sweep_lock:
            referenced = self.projects.referenced_addresses() | self.layouts.referenced_addresses()
            candidates: list[tuple[AppInventoryEntry, InventoryInstance]] = [
                (entry, instance)
                for entry in self.inventory.entries()
                if entry.is_running and entry.row.instances
                for instance in entry.instances
                if instance.lifetime is InstanceLifetime.REFERENCED
                and not self.inventory.is_within_grace(entry, instance)
            ]
            doomed = unreferenced_addresses([entry.address_of(instance) for entry, instance in candidates], referenced)
            deleted: list[Address] = []
            for entry, instance in candidates:
                address = entry.address_of(instance)
                if address not in doomed:
                    continue
                outcome = relay_delete(self.http_client, entry, instance.key)
                if outcome.status_code >= 400:
                    logger.warning("Could not delete the unreferenced instance {}: {}", address, outcome.status_code)
                    continue
                deleted.append(address)
            for app_name in sorted(
                {str(entry.row.name) for entry, instance in candidates if entry.address_of(instance) in deleted}
            ):
                self.inventory.refetch_now(app_name)
        return deleted


def build_shell_state(
    state_directory: Path,
    registry_path: Path,
    broadcaster: WebSocketBroadcaster,
    inventory: AppInventory | None = None,
) -> ShellState:
    """Wire the shell's collaborators over ``state_directory``; ``inventory`` is injectable for tests."""
    return ShellState(
        state_directory=state_directory,
        inventory=inventory
        if inventory is not None
        else AppInventory(registry_path=registry_path, broadcaster=broadcaster),
        projects=ProjectStore(state_directory=state_directory),
        layouts=LayoutStore(state_directory=state_directory),
        clients=ClientStore(state_directory=state_directory),
        activity=ClientActivityLog(events_path=state_directory / CLIENT_ACTIVITY_EVENTS_PATH),
        broadcaster=broadcaster,
        layout_mutex=LayoutMutex(),
        http_client=httpx.Client(follow_redirects=False, timeout=30.0),
    )
