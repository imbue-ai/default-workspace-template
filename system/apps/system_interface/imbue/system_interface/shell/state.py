"""``ShellState``: everything the shell's routes and WebSocket loop share, built in ``main.py`` (or by a test)."""

import os
import threading
from collections.abc import Callable
from collections.abc import Sequence
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Final

from app_manifest.manifest import LocationScope
from app_manifest.primitives import LaunchPathId
from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.mutable_model import MutableModel
from imbue.system_interface.avatar.catalog import DEFAULT_AVATAR_CATALOG_DIRECTORY
from imbue.system_interface.avatar.catalog import AvatarCatalogStore
from imbue.system_interface.avatar.selection import AvatarSelectionStore
from imbue.system_interface.avatar.status import AvatarStatusReader
from imbue.system_interface.avatar.status import agent_events_path
from imbue.system_interface.shell.client_activity import ClientActivityLog
from imbue.system_interface.shell.clients import CLIENT_RETENTION
from imbue.system_interface.shell.clients import ClientStore
from imbue.system_interface.shell.data_types import AppInventoryEntry
from imbue.system_interface.shell.data_types import AppPin
from imbue.system_interface.shell.data_types import ClientReportOutcome
from imbue.system_interface.shell.data_types import ClientStateReport
from imbue.system_interface.shell.data_types import Desktop
from imbue.system_interface.shell.data_types import DesktopDeleteOutcome
from imbue.system_interface.shell.data_types import DesktopLayout
from imbue.system_interface.shell.data_types import DesktopShortcut
from imbue.system_interface.shell.data_types import PlacementsEditOutcome
from imbue.system_interface.shell.data_types import PlacementsSaveRequest
from imbue.system_interface.shell.data_types import StoredWindowPath
from imbue.system_interface.shell.data_types import Window
from imbue.system_interface.shell.data_types import WindowOpenOutcome
from imbue.system_interface.shell.data_types import WindowOpenRequest
from imbue.system_interface.shell.data_types import desktop_wire_json
from imbue.system_interface.shell.data_types import effective_launch_paths
from imbue.system_interface.shell.data_types import effective_window
from imbue.system_interface.shell.desktop_document import find_window
from imbue.system_interface.shell.desktop_document import find_window_at
from imbue.system_interface.shell.desktop_document import require_window
from imbue.system_interface.shell.desktop_document import pinned_apps
from imbue.system_interface.shell.desktop_document import pinned_window
from imbue.system_interface.shell.desktop_document import seed_desktop_shortcuts
from imbue.system_interface.shell.desktop_document import with_window_placed_on_open
from imbue.system_interface.shell.desktop_document import with_window_raised
from imbue.system_interface.shell.desktops import DesktopStore
from imbue.system_interface.shell.desktops import resolve_active_desktop
from imbue.system_interface.shell.errors import DesktopNotFoundError
from imbue.system_interface.shell.errors import DesktopValueError
from imbue.system_interface.shell.errors import PinnedWindowError
from imbue.system_interface.shell.inventory import AppInventory
from imbue.system_interface.shell.placements import PlacementStore
from imbue.system_interface.shell.placements import StoredDesktopLayout
from imbue.system_interface.shell.primitives import ClientId
from imbue.system_interface.shell.primitives import DesktopId
from imbue.system_interface.shell.primitives import IfPresent
from imbue.system_interface.shell.primitives import WindowId
from imbue.system_interface.shell.primitives import WindowPath
from imbue.system_interface.shell.primitives import WindowTitle
from imbue.system_interface.shell.primitives import mint_save_id
from imbue.system_interface.shell.primitives import mint_window_id
from imbue.system_interface.shell.wallpapers import DEFAULT_WALLPAPER_FILES_DIRECTORY
from imbue.system_interface.shell.window_paths import WindowPathStore
from imbue.system_interface.ws_broadcaster import WebSocketBroadcaster

CLIENT_ACTIVITY_EVENTS_PATH: Final[str] = "events/client_activity/events.jsonl"
# How often the client prune of desktop contracts.md section 4.3 re-runs after the one at start.
CLIENT_PRUNE_INTERVAL_SECONDS: Final[float] = 24 * 60 * 60.0


class ShellState(MutableModel):
    """The shell's collaborators: the inventory, the desktop stores, the activity log, and the broadcaster."""

    model_config = {"arbitrary_types_allowed": True, "extra": "forbid", "frozen": False}

    state_directory: Path = Field(
        frozen=True, description="Where the state files live (desktop contracts.md section 4)"
    )
    inventory: AppInventory = Field(frozen=True, description="The registry and each app's liveness")
    desktops: DesktopStore = Field(frozen=True, description="desktops.json")
    placements: PlacementStore = Field(frozen=True, description="The per-client layouts of each desktop")
    window_paths: WindowPathStore = Field(
        frozen=True, description="The per-client paths and titles of independent windows"
    )
    wallpaper_files_directory: Path = Field(
        frozen=True, description="Where the workspace's own wallpaper files are read from"
    )
    clients: ClientStore = Field(frozen=True, description="clients.json")
    activity: ClientActivityLog = Field(frozen=True, description="The client-activity event log")
    broadcaster: WebSocketBroadcaster = Field(frozen=True, description="The WebSocket fan-out to the shell's windows")
    avatar_catalog: AvatarCatalogStore = Field(frozen=True, description="The avatar designs registered in the workspace")
    avatar_selection: AvatarSelectionStore = Field(frozen=True, description="avatar_selection.json")
    avatar_status: AvatarStatusReader = Field(frozen=True, description="The avatar's mood, read from mngr's event file")
    client_prune_interval_seconds: float = Field(
        default=CLIENT_PRUNE_INTERVAL_SECONDS, frozen=True, description="How often stale clients are pruned"
    )

    _prune_stop: threading.Event = PrivateAttr(default_factory=threading.Event)
    _prune_thread: threading.Thread | None = PrivateAttr(default=None)

    def start(self) -> None:
        """Prune stale clients (now, and daily from here on), then start the inventory (registry watch, liveness)
        and the avatar status reader (the event file watch)."""
        self.prune_unseen_clients()
        thread = threading.Thread(target=self._run_client_prune, daemon=True, name="shell-client-prune")
        self._prune_thread = thread
        thread.start()
        self.inventory.start()
        self.avatar_status.start()

    def stop(self) -> None:
        self._prune_stop.set()
        if self._prune_thread is not None:
            self._prune_thread.join(timeout=5)
            self._prune_thread = None
        self.inventory.stop()
        self.avatar_status.stop()

    def prune_unseen_clients(self) -> None:
        """Drop every client unseen for the retention period, together with the layouts it owns (desktop contracts.md section 4.3)."""
        now = datetime.now(timezone.utc)
        for client_id in self.clients.prune_unseen(now):
            removed = self.placements.delete_client_layouts(client_id)
            self.window_paths.delete_client_paths(client_id)
            logger.info(
                "Pruned client {} unseen for {} days ({} layout file(s))", client_id, CLIENT_RETENTION.days, removed
            )

    def _run_client_prune(self) -> None:
        while not self._prune_stop.wait(timeout=self.client_prune_interval_seconds):
            # A state file that cannot be written today is logged and retried tomorrow.
            try:
                self.prune_unseen_clients()
            except (OSError, ValueError) as e:
                logger.opt(exception=e).error("The stale-client prune failed; the next run will retry")

    # The desktop model (desktop-interface contracts.md sections 4 to 6)

    def list_desktops(self) -> list[Desktop]:
        """Every desktop, the default one created on the first read after the inventory has read the registry once,
        so its shortcuts are seeded from the apps that are actually registered (desktop plan section 3.2), and every
        desktop holding one pinned window per pinned app (pinned-taskbar-entries plan section 3.2). A reconcile
        that wrote is announced once, after the read, so nothing here recurses into itself."""
        if not self.inventory.is_registry_read:
            return self.desktops.list_desktops()
        self.desktops.ensure_default(self.seed_shortcuts)
        outcome = self.desktops.ensure_pinned_windows(self.pinned_apps(), datetime.now(timezone.utc))
        if outcome.is_written:
            self.broadcaster.broadcast_desktops_updated([desktop_wire_json(desktop) for desktop in outcome.desktops])
        return list(outcome.desktops)

    def seed_shortcuts(self) -> tuple[DesktopShortcut, ...]:
        return seed_desktop_shortcuts([entry.row for entry in self.inventory.entries()])

    def pinned_apps(self) -> tuple[AppPin, ...]:
        return pinned_apps([entry.row for entry in self.inventory.entries()])

    def create_desktop(self, name: str, color: str, glyph: int) -> Desktop:
        """Register a desktop born with its seeded shortcuts and one pinned window per pinned app, and tell everyone."""
        now = datetime.now(timezone.utc)
        desktop = self.desktops.create_desktop(
            name, color, glyph, self.seed_shortcuts(), [pinned_window(app_pin, now) for app_pin in self.pinned_apps()]
        )
        self.broadcast_desktops_updated()
        return desktop

    def require_app_entry(self, app: str) -> AppInventoryEntry:
        """The inventory entry of a registered app; raises DesktopValueError (a 400) for any other name."""
        entry = self.inventory.entry(app)
        if entry is None:
            raise DesktopValueError(f"No registered app named {app!r}")
        return entry

    def require_launch_path(self, entry: AppInventoryEntry, launch: LaunchPathId) -> None:
        """Raises DesktopValueError (a 400) unless the app offers the launch path (the synthesized ``open`` included)."""
        if launch not in {launch_path.id for launch_path in effective_launch_paths(entry.row)}:
            raise DesktopValueError(f"App {str(entry.row.name)!r} declares no launch path {str(launch)!r}")

    def get_desktop(self, desktop_id: str) -> Desktop:
        for desktop in self.list_desktops():
            if desktop.id == desktop_id:
                return desktop
        raise DesktopNotFoundError(desktop_id)

    def broadcast_desktops_updated(self) -> None:
        self.broadcaster.broadcast_desktops_updated([desktop_wire_json(desktop) for desktop in self.list_desktops()])

    def read_desktop_layout(self, desktop_id: str, client_id: str) -> DesktopLayout:
        """The client's layout of the desktop as it reads: its own file with stale placements dropped, else empty."""
        desktop = self.get_desktop(desktop_id)
        return self.placements.read_layout(desktop_id, client_id, {window.id for window in desktop.windows})

    def read_window_paths(self, desktop: Desktop, client_id: str) -> dict[WindowId, StoredWindowPath]:
        """The client's stored paths and titles for the desktop's independent windows, by window id."""
        independent = {window.id for window in desktop.windows if window.scope is LocationScope.INDEPENDENT}
        return self.window_paths.read_paths(client_id, independent)

    def effective_window_for_client(self, desktop: Desktop, window: Window, client_id: str) -> Window:
        """The window as ``client_id`` sees it: an independent window at the client's own path and title."""
        return effective_window(window, self.read_window_paths(desktop, client_id).get(window.id))

    def _broadcast_placements_written(self, rewritten: Sequence[StoredDesktopLayout]) -> None:
        for stored in rewritten:
            self.broadcaster.broadcast_placements_updated(
                str(stored.desktop_id), str(stored.client_id), mint_save_id()
            )

    def _edit_placements(
        self, desktop: Desktop, client_id: ClientId, transform: Callable[[DesktopLayout], DesktopLayout]
    ) -> PlacementsEditOutcome:
        return self.placements.edit_layout(
            desktop.id, client_id, {window.id for window in desktop.windows}, transform, datetime.now(timezone.utc)
        )

    def _announce_placements_edit(self, desktop: Desktop, client_id: ClientId, outcome: PlacementsEditOutcome) -> None:
        if outcome.is_written:
            self._broadcast_placements_written(
                [StoredDesktopLayout(desktop_id=desktop.id, client_id=client_id, layout=outcome.layout)]
            )

    def edit_desktop_layout(
        self, desktop: Desktop, client_id: ClientId, transform: Callable[[DesktopLayout], DesktopLayout]
    ) -> DesktopLayout:
        """Apply the shell's own edit (an open, an agent op) to one client's layout of the desktop, and announce a
        write with a save id the shell minted; an edit that changes nothing writes and announces nothing."""
        outcome = self._edit_placements(desktop, client_id, transform)
        self._announce_placements_edit(desktop, client_id, outcome)
        return outcome.layout

    def save_browser_placements(self, desktop_id: str, request: PlacementsSaveRequest) -> DesktopLayout | None:
        """A browser's save of its layout, announced with the window's own save id; None when it changed nothing.
        Raises StalePlacementsSaveError for a save based on an older layout."""
        desktop = self.get_desktop(desktop_id)
        saved = self.placements.save_browser_layout(
            desktop.id,
            request.client_id,
            request.placements,
            request.base_updated_at,
            {window.id for window in desktop.windows},
            datetime.now(timezone.utc),
        )
        if saved is not None:
            self.broadcaster.broadcast_placements_updated(
                str(desktop.id), str(request.client_id), str(request.save_id)
            )
        return saved

    def open_window(self, desktop_id: str, request: WindowOpenRequest) -> WindowOpenOutcome:
        """Open a window of ``request.app`` at ``request.path`` on the desktop for everyone, placed at once in the
        requesting client's layout; with ``if_present`` focus, a window of the app already at that exact path is
        restored and raised there instead (desktop plan section 4.1)."""
        desktop = self.get_desktop(desktop_id)
        entry = self.require_app_entry(str(request.app))
        if request.launch is not None:
            self.require_launch_path(entry, request.launch)
        if request.if_present is IfPresent.FOCUS:
            existing = find_window_at(desktop, request.app, request.path)
            if existing is not None:
                self.edit_desktop_layout(
                    desktop, request.client_id, lambda layout: with_window_raised(layout, existing.id)
                )
                return WindowOpenOutcome(window=existing, is_new=False)
        window = Window(
            id=mint_window_id(),
            app=request.app,
            path=request.path,
            title=WindowTitle(""),
            opened_at=datetime.now(timezone.utc),
            is_settling=request.launch is not None,
        )
        opened_on = self.desktops.open_window(desktop.id, window)
        placed = self._edit_placements(
            opened_on, request.client_id, lambda layout: with_window_placed_on_open(layout, window.id)
        )
        # The window is announced before the placement that arranges it, so no client reads the placement as one of
        # a window its desktop does not hold.
        self.broadcast_desktops_updated()
        self._announce_placements_edit(opened_on, request.client_id, placed)
        logger.info("Opened window {} of {} at {} on desktop {}", window.id, window.app, window.path, desktop.id)
        return WindowOpenOutcome(window=window, is_new=True)

    def close_window(self, desktop_id: str, window_id: WindowId) -> bool:
        """Close a window for everyone: off the desktop and out of every client's layout of it; False when the
        desktop did not hold it (idempotent). Raises PinnedWindowError (a 409) for a pinned window, which is never
        closed."""
        window = find_window(self.get_desktop(desktop_id), window_id)
        if window is not None and window.is_pinned:
            raise PinnedWindowError(f"Window {window_id} is pinned and cannot be closed; minimize it instead")
        outcome = self.desktops.close_window(desktop_id, window_id)
        if not outcome.is_written:
            return False
        rewritten = self.placements.drop_window_everywhere(desktop_id, window_id, datetime.now(timezone.utc))
        self.broadcast_desktops_updated()
        self._broadcast_placements_written(rewritten)
        logger.info("Closed window {} on desktop {} ({} layout(s) rewritten)", window_id, desktop_id, len(rewritten))
        return True

    def report_window_location(
        self, desktop_id: str, window_id: WindowId, client_id: ClientId, path: WindowPath, title: WindowTitle
    ) -> Window:
        """Store what a page reported for its window and tell whoever follows it: a linked window's record for
        everyone, an independent window's path for the reporting client alone (its other windows refetch the
        layout, announced with a save id the shell minted). Answers the window as the client sees it; a report that
        changes nothing is silent."""
        desktop = self.get_desktop(desktop_id)
        window = require_window(desktop, window_id)
        if window.scope is LocationScope.LINKED:
            outcome = self.desktops.set_window_location(desktop_id, window_id, path, title)
            if outcome.is_written:
                self.broadcast_desktops_updated()
            return next(candidate for candidate in outcome.desktop.windows if candidate.id == window_id)
        stored = StoredWindowPath(path=path, title=title)
        independent = {candidate.id for candidate in desktop.windows if candidate.scope is LocationScope.INDEPENDENT}
        if self.window_paths.set_path(client_id, window_id, stored, independent):
            self.broadcaster.broadcast_placements_updated(str(desktop.id), str(client_id), mint_save_id())
        return effective_window(window, stored)

    def delete_desktop(self, desktop_id: str) -> DesktopDeleteOutcome:
        """Delete a desktop with its windows and every client's layout of it, and move the clients on it to the
        first remaining desktop. Raises LastDesktopError for the last one."""
        outcome = self.desktops.delete_desktop(desktop_id)
        self.placements.delete_desktop_layouts(desktop_id)
        for client in self.clients.list_clients():
            if client.active_desktop == outcome.deleted.id:
                self.set_client_active_desktop(client.id, outcome.fallback_desktop_id)
        self.broadcast_desktops_updated()
        logger.info("Deleted desktop {} (fallback {})", desktop_id, outcome.fallback_desktop_id)
        return outcome

    def set_client_active_desktop(self, client_id: ClientId, desktop_id: DesktopId) -> bool:
        """Move a client onto a desktop and tell its windows; answers whether the stored desktop actually moved."""
        outcome = self.clients.set_active_desktop(client_id, desktop_id, datetime.now(timezone.utc))
        if outcome.is_active_desktop_changed:
            self.broadcaster.broadcast_active_desktop_changed(str(client_id), str(desktop_id))
        return outcome.is_active_desktop_changed

    def active_desktop_of_client(self, client_id: str) -> DesktopId | None:
        """The desktop a client is on by the rule of desktop contracts.md section 4.3; None with no desktops."""
        return resolve_active_desktop(self.clients.get_client(client_id), self.list_desktops())

    def record_client_report(self, report: ClientStateReport) -> ClientReportOutcome:
        """Record a ``client_state`` report and announce what moved; a report naming a desktop that no longer exists
        lands the client on the first desktop instead (desktop plan section 3.5)."""
        desktops = self.list_desktops()
        resolved = report
        if desktops and report.active_desktop not in {desktop.id for desktop in desktops}:
            resolved = report.model_copy_update(to_update(report.field_ref().active_desktop, desktops[0].id))
        outcome = self.clients.record_report(resolved, datetime.now(timezone.utc))
        # Only a report that moved the stored desktop, or that was redirected off a desktop that no longer exists,
        # is broadcast: a window following a push reports what it was pushed to, which matches the record, so the
        # chain ends after one hop.
        is_redirected = resolved is not report
        if outcome.is_active_desktop_changed or is_redirected:
            self.broadcaster.broadcast_active_desktop_changed(
                str(report.client_id), str(outcome.record.active_desktop)
            )
        return outcome


def build_shell_state(
    state_directory: Path,
    registry_path: Path,
    broadcaster: WebSocketBroadcaster,
    inventory: AppInventory | None = None,
    wallpaper_files_directory: Path = DEFAULT_WALLPAPER_FILES_DIRECTORY,
    avatar_catalog_directory: Path = DEFAULT_AVATAR_CATALOG_DIRECTORY,
    agent_events_path: Path | None = None,
) -> ShellState:
    """Wire the shell's collaborators over ``state_directory``; ``inventory`` is injectable for tests, and
    ``agent_events_path`` (the mngr observer's file the avatar's mood is read from) defaults to the one the
    environment names."""
    return ShellState(
        state_directory=state_directory,
        inventory=inventory
        if inventory is not None
        else AppInventory(registry_path=registry_path, broadcaster=broadcaster),
        desktops=DesktopStore(state_directory=state_directory),
        placements=PlacementStore(state_directory=state_directory),
        window_paths=WindowPathStore(state_directory=state_directory),
        wallpaper_files_directory=wallpaper_files_directory,
        clients=ClientStore(state_directory=state_directory),
        activity=ClientActivityLog(events_path=state_directory / CLIENT_ACTIVITY_EVENTS_PATH),
        broadcaster=broadcaster,
        avatar_catalog=AvatarCatalogStore(directory=avatar_catalog_directory),
        avatar_selection=AvatarSelectionStore(state_directory=state_directory),
        avatar_status=AvatarStatusReader(
            events_path=agent_events_path if agent_events_path is not None else agent_events_path_from_environment(),
            broadcaster=broadcaster,
        ),
    )


def agent_events_path_from_environment() -> Path:
    return agent_events_path(os.environ)
