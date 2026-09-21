"""``ShellState``: everything the shell's routes and WebSocket loop share, built in ``main.py`` (or by a test)."""

import threading
from collections.abc import Callable
from collections.abc import Sequence
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Final

from app_manifest.primitives import AppName
from app_manifest.primitives import LaunchPathId
from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.mutable_model import MutableModel
from imbue.system_interface.shell.client_activity import ClientActivityLog
from imbue.system_interface.shell.clients import CLIENT_RETENTION
from imbue.system_interface.shell.clients import ClientStore
from imbue.system_interface.shell.close_hints import WindowClosedHint
from imbue.system_interface.shell.close_hints import post_window_closed_hint
from imbue.system_interface.shell.close_hints import window_closed_hint
from imbue.system_interface.shell.data_types import AppInventoryEntry
from imbue.system_interface.shell.data_types import ClientArrivalOutcome
from imbue.system_interface.shell.data_types import ClientRecord
from imbue.system_interface.shell.data_types import ClientReportOutcome
from imbue.system_interface.shell.data_types import ClientStateReport
from imbue.system_interface.shell.data_types import Desktop
from imbue.system_interface.shell.data_types import DesktopDeleteOutcome
from imbue.system_interface.shell.data_types import DesktopLayout
from imbue.system_interface.shell.data_types import DesktopShortcut
from imbue.system_interface.shell.data_types import PlacementsEditOutcome
from imbue.system_interface.shell.data_types import PlacementsSaveRequest
from imbue.system_interface.shell.data_types import UserRecord
from imbue.system_interface.shell.data_types import Window
from imbue.system_interface.shell.data_types import WindowOpenOutcome
from imbue.system_interface.shell.data_types import WindowOpenRequest
from imbue.system_interface.shell.data_types import desktop_wire_json
from imbue.system_interface.shell.data_types import effective_launch_paths
from imbue.system_interface.shell.desktop_document import desktop_seeded_from
from imbue.system_interface.shell.desktop_document import find_window_at
from imbue.system_interface.shell.desktop_document import seed_desktop_shortcuts
from imbue.system_interface.shell.desktop_document import settled_windows
from imbue.system_interface.shell.desktop_document import with_window_placed_on_open
from imbue.system_interface.shell.desktop_document import with_window_raised
from imbue.system_interface.shell.desktops import DESKTOP_GLYPH_COLORS
from imbue.system_interface.shell.desktops import DesktopStore
from imbue.system_interface.shell.desktops import desktop_kept_by_returning_client
from imbue.system_interface.shell.desktops import desktop_name_for_user
from imbue.system_interface.shell.desktops import next_glyph_index
from imbue.system_interface.shell.desktops import resolve_active_desktop
from imbue.system_interface.shell.desktops import slugify_desktop_name
from imbue.system_interface.shell.errors import DesktopNotFoundError
from imbue.system_interface.shell.errors import DesktopValueError
from imbue.system_interface.shell.identity import RequestIdentity
from imbue.system_interface.shell.identity import visiting_user_id
from imbue.system_interface.shell.inventory import AppInventory
from imbue.system_interface.shell.placements import PlacementStore
from imbue.system_interface.shell.placements import StoredDesktopLayout
from imbue.system_interface.shell.primitives import ClientId
from imbue.system_interface.shell.primitives import DesktopId
from imbue.system_interface.shell.primitives import IfPresent
from imbue.system_interface.shell.primitives import UserId
from imbue.system_interface.shell.primitives import WindowId
from imbue.system_interface.shell.primitives import WindowPath
from imbue.system_interface.shell.primitives import WindowTitle
from imbue.system_interface.shell.primitives import mint_save_id
from imbue.system_interface.shell.primitives import mint_window_id
from imbue.system_interface.shell.state_files import STATE_FILES_LOCK
from imbue.system_interface.shell.users import UserStore
from imbue.system_interface.shell.wallpapers import DEFAULT_WALLPAPER_FILES_DIRECTORY
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
    wallpaper_files_directory: Path = Field(
        frozen=True, description="Where the workspace's own wallpaper files are read from"
    )
    clients: ClientStore = Field(frozen=True, description="clients.json")
    users: UserStore = Field(frozen=True, description="users.json: the desktop made for each signed-in visitor")
    activity: ClientActivityLog = Field(frozen=True, description="The client-activity event log")
    broadcaster: WebSocketBroadcaster = Field(frozen=True, description="The WebSocket fan-out to the shell's windows")
    client_prune_interval_seconds: float = Field(
        default=CLIENT_PRUNE_INTERVAL_SECONDS, frozen=True, description="How often stale clients are pruned"
    )
    close_hint_poster: Callable[[WindowClosedHint], None] = Field(
        default=post_window_closed_hint,
        frozen=True,
        description="How an app is told a window of its closed; a test records the hints instead",
    )

    _prune_stop: threading.Event = PrivateAttr(default_factory=threading.Event)
    _prune_thread: threading.Thread | None = PrivateAttr(default=None)

    def start(self) -> None:
        """Prune stale clients (now, and daily from here on), then start the inventory (registry watch, liveness)."""
        self.prune_unseen_clients()
        thread = threading.Thread(target=self._run_client_prune, daemon=True, name="shell-client-prune")
        self._prune_thread = thread
        thread.start()
        self.inventory.start()

    def stop(self) -> None:
        self._prune_stop.set()
        if self._prune_thread is not None:
            self._prune_thread.join(timeout=5)
            self._prune_thread = None
        self.inventory.stop()

    def prune_unseen_clients(self) -> None:
        """Drop every client unseen for the retention period, together with the layouts it owns (desktop contracts.md section 4.3)."""
        now = datetime.now(timezone.utc)
        for client_id in self.clients.prune_unseen(now):
            removed = self.placements.delete_client_layouts(client_id)
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
        so its shortcuts are seeded from the apps that are actually registered (desktop plan section 3.2)."""
        if not self.inventory.is_registry_read:
            return self.desktops.list_desktops()
        return self.desktops.ensure_default(self.seed_shortcuts)

    def seed_shortcuts(self) -> tuple[DesktopShortcut, ...]:
        return seed_desktop_shortcuts([entry.row for entry in self.inventory.entries()])

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

    def open_window(self, desktop_id: str, request: WindowOpenRequest, is_minimized: bool) -> WindowOpenOutcome:
        """Open a window of ``request.app`` at ``request.path`` on the desktop for everyone, placed at once in the
        requesting client's layout (shown, or minimized when asked); with ``if_present`` focus, a window of the app
        already at that exact path is answered instead, restored and raised there unless the open asked for
        minimized, in which case it is left as placed (desktop plan section 4.1)."""
        desktop = self.get_desktop(desktop_id)
        self._require_open_target(request.app, request.launch)
        if request.if_present is IfPresent.FOCUS:
            existing = find_window_at(desktop, request.app, request.path)
            if existing is not None:
                if not is_minimized:
                    self.edit_desktop_layout(
                        desktop, request.client_id, lambda layout: with_window_raised(layout, existing.id)
                    )
                return WindowOpenOutcome(window=existing, is_new=False)
        opened_on, window = self._append_window(desktop, request.app, request.path, request.launch)
        placed = self._edit_placements(
            opened_on,
            request.client_id,
            lambda layout: with_window_placed_on_open(layout, window.id, is_minimized),
        )
        # The window is announced before the placement that arranges it, so no client reads the placement as one of
        # a window its desktop does not hold.
        self.broadcast_desktops_updated()
        self._announce_placements_edit(opened_on, request.client_id, placed)
        logger.info("Opened window {} of {} at {} on desktop {}", window.id, window.app, window.path, desktop.id)
        return WindowOpenOutcome(window=window, is_new=True)

    def open_window_unplaced(
        self, desktop_id: str, app: AppName, path: WindowPath, launch: LaunchPathId | None, if_present: IfPresent
    ) -> WindowOpenOutcome:
        """An open with no client to place it for (an agent's, with nobody connected): the window exists on the
        desktop for everyone and reads as minimized in every layout; with ``if_present`` focus, a window of the app
        already at the path is answered as it stands."""
        desktop = self.get_desktop(desktop_id)
        self._require_open_target(app, launch)
        if if_present is IfPresent.FOCUS:
            existing = find_window_at(desktop, app, path)
            if existing is not None:
                return WindowOpenOutcome(window=existing, is_new=False)
        _, window = self._append_window(desktop, app, path, launch)
        self.broadcast_desktops_updated()
        logger.info("Opened window {} of {} at {} on desktop {} for no client", window.id, app, path, desktop.id)
        return WindowOpenOutcome(window=window, is_new=True)

    def _require_open_target(self, app: AppName, launch: LaunchPathId | None) -> None:
        entry = self.require_app_entry(str(app))
        if launch is not None:
            self.require_launch_path(entry, launch)

    def _append_window(
        self, desktop: Desktop, app: AppName, path: WindowPath, launch: LaunchPathId | None
    ) -> tuple[Desktop, Window]:
        """Mint a window and write it onto the desktop; a window opened at a launch path settles until its page reports."""
        window = Window(
            id=mint_window_id(),
            app=app,
            path=path,
            title=WindowTitle(""),
            opened_at=datetime.now(timezone.utc),
            is_settling=launch is not None,
        )
        return self.desktops.open_window(desktop.id, window), window

    def close_window(self, desktop_id: str, window_id: WindowId) -> bool:
        """Close a window for everyone: off the desktop and out of every client's layout of it, and its app told;
        False when the desktop did not hold it (idempotent)."""
        desktop = self.get_desktop(desktop_id)
        closing = next((window for window in desktop.windows if window.id == window_id), None)
        if closing is None:
            return False
        outcome = self.desktops.close_window(desktop_id, window_id)
        if not outcome.is_written:
            return False
        rewritten = self.placements.drop_window_everywhere(desktop_id, window_id, datetime.now(timezone.utc))
        self.broadcast_desktops_updated()
        self._broadcast_placements_written(rewritten)
        logger.info("Closed window {} on desktop {} ({} layout(s) rewritten)", window_id, desktop_id, len(rewritten))
        self._hint_windows_closed(desktop.id, (closing,))
        return True

    def _hint_windows_closed(self, desktop_id: DesktopId, windows: Sequence[Window]) -> None:
        """Tell each closed window's app, when its row names a window_closed_path (spec section 4.6)."""
        for window in windows:
            entry = self.inventory.entry(str(window.app))
            if entry is None:
                continue
            hint = window_closed_hint(entry, desktop_id, window)
            if hint is not None:
                self.close_hint_poster(hint)

    def report_window_location(
        self, desktop_id: str, window_id: WindowId, path: WindowPath, title: WindowTitle
    ) -> Window:
        """Store what a page reported for its window and tell every client; a report that changes nothing is silent."""
        outcome = self.desktops.set_window_location(desktop_id, window_id, path, title)
        if outcome.is_written:
            self.broadcast_desktops_updated()
        window = next(candidate for candidate in outcome.desktop.windows if candidate.id == window_id)
        return window

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
        self._hint_windows_closed(outcome.deleted.id, outcome.deleted.windows)
        return outcome

    def set_client_active_desktop(self, client_id: ClientId, desktop_id: DesktopId) -> bool:
        """Move a client onto a desktop and tell its windows; answers whether the stored desktop actually moved."""
        outcome = self.clients.set_active_desktop(client_id, desktop_id, datetime.now(timezone.utc))
        if outcome.is_active_desktop_changed:
            self.broadcaster.broadcast_active_desktop_changed(str(client_id), str(desktop_id))
        return outcome.is_active_desktop_changed

    def arrive_client(self, client_id: ClientId, identity: RequestIdentity) -> ClientArrivalOutcome | None:
        """Where a client whose shell page just loaded lands (desktop plan section 3.10): the owner and anonymous
        clients follow the rule of contracts.md section 4.3; a signed-in visitor lands on the desktop made for them,
        seeded from the first desktop on their first arrival (and again, with a notice, if it has since been deleted),
        while a returning client of theirs keeps the desktop it was on. None while the workspace has no desktop."""
        # The whole read-decide-write runs under the state lock (re-entrant, so the stores' own takes nest): two
        # arrivals of one new user at once (a browser restoring its tabs) must not both seed a desktop for them.
        with STATE_FILES_LOCK:
            desktops = self.list_desktops()
            record = self.clients.get_client(client_id)
            shared_landing = resolve_active_desktop(record, desktops)
            if shared_landing is None:
                return None
            now = datetime.now(timezone.utc)
            user_id = visiting_user_id(identity)
            outcome = (
                self._land_visiting_user(user_id, identity, record, desktops, now)
                if user_id is not None
                else ClientArrivalOutcome(desktop_id=shared_landing, created_desktop=None, replaced_desktop_name=None)
            )
            # Every arrival stamps the user it came as (None for the owner), so the returning-client rule never
            # reads a user the browser has since stopped being.
            recorded = self.clients.record_arrival(client_id, user_id, outcome.desktop_id, now)
        if outcome.created_desktop is not None:
            self.broadcast_desktops_updated()
        # A client that already had a record may have other windows open on the desktop it was moved off.
        if record is not None and recorded.is_active_desktop_changed:
            self.broadcaster.broadcast_active_desktop_changed(str(client_id), str(outcome.desktop_id))
        return outcome

    def _land_visiting_user(
        self,
        user_id: UserId,
        identity: RequestIdentity,
        record: ClientRecord | None,
        desktops: Sequence[Desktop],
        now: datetime,
    ) -> ClientArrivalOutcome:
        """Where a visiting user's client lands, with the user's record brought up to date: the desktop made for
        them, seeded now when they have none or when the one they had has been deleted (which the outcome names),
        unless the client is a returning one of theirs, which keeps the desktop it was on. Runs under the state lock."""
        known = self.users.get_user(user_id)
        desktop_by_id = {desktop.id: desktop for desktop in desktops}
        own_desktop = desktop_by_id.get(known.desktop_id) if known is not None else None
        created: Desktop | None = None
        replaced_desktop_name: str | None = None
        if own_desktop is not None:
            kept = desktop_kept_by_returning_client(record, user_id, desktop_by_id.keys())
            landing = kept if kept is not None else own_desktop.id
        else:
            created = self._create_desktop_for_user(identity, desktops, now)
            own_desktop = created
            landing = created.id
            replaced_desktop_name = known.desktop_name if known is not None else None
        # The record carries the desktop's name as it stands at this arrival, so a notice after a deletion names
        # the desktop as the user last saw it, renames included.
        self.users.record_user(
            UserRecord(
                user_id=user_id,
                desktop_id=own_desktop.id,
                desktop_name=own_desktop.name,
                email=identity.email,
                display_name=identity.display_name,
                last_seen=now,
            )
        )
        return ClientArrivalOutcome(
            desktop_id=landing, created_desktop=created, replaced_desktop_name=replaced_desktop_name
        )

    def _create_desktop_for_user(
        self, identity: RequestIdentity, desktops: Sequence[Desktop], now: datetime
    ) -> Desktop:
        """A desktop named after the user, seeded from the first desktop (its shortcuts, wallpaper, and settled
        windows as new windows), with the next free glyph and that glyph's colour."""
        name = desktop_name_for_user(identity, desktops)
        glyph = next_glyph_index([desktop.glyph for desktop in desktops])
        source = desktops[0]
        seeded = desktop_seeded_from(
            source,
            slugify_desktop_name(name),
            name,
            DESKTOP_GLYPH_COLORS[glyph],
            glyph,
            [mint_window_id() for _ in settled_windows(source)],
            now,
        )
        created = self.desktops.add_desktop(seeded)
        logger.info(
            "Seeded desktop {!r} for user {} from {!r} ({} shortcut(s), {} window(s))",
            created.name,
            identity.user_id,
            source.name,
            len(created.shortcuts),
            len(created.windows),
        )
        return created

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
) -> ShellState:
    """Wire the shell's collaborators over ``state_directory``; ``inventory`` is injectable for tests."""
    return ShellState(
        state_directory=state_directory,
        inventory=inventory
        if inventory is not None
        else AppInventory(registry_path=registry_path, broadcaster=broadcaster),
        desktops=DesktopStore(state_directory=state_directory),
        placements=PlacementStore(state_directory=state_directory),
        wallpaper_files_directory=wallpaper_files_directory,
        clients=ClientStore(state_directory=state_directory),
        users=UserStore(state_directory=state_directory),
        activity=ClientActivityLog(events_path=state_directory / CLIENT_ACTIVITY_EVENTS_PATH),
        broadcaster=broadcaster,
    )
