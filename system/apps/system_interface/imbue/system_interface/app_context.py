from pathlib import Path

from flask import Flask
from flask import current_app
from pydantic import Field
from workspace_backup_setup.state import backup_lease_path
from workspace_update_control.channel import ChannelFileFetcher
from workspace_update_control.channel import fetch_channel_file
from workspace_update_control.protocol import LIVE_CRON_DIRECTORY

from imbue.imbue_common.mutable_model import MutableModel
from imbue.system_interface.config import Config
from imbue.system_interface.presence import PresenceStore
from imbue.system_interface.presence import PresenceSweep
from imbue.system_interface.profiles import ProfileResolver
from imbue.system_interface.roster import RosterAnnouncer
from imbue.system_interface.roster import RosterSource
from imbue.system_interface.roster import RosterWatch
from imbue.system_interface.shell.state import ShellState
from imbue.system_interface.update_staleness import UpdateStalenessTracker
from imbue.system_interface.workspace_name import WorkspaceNameSource

# Key under which the single SystemInterfaceState is stored on ``app.config`` so
# handlers can fetch it via ``get_state()``.
_STATE_CONFIG_KEY = "SYSTEM_INTERFACE_STATE"


class SystemInterfaceStateError(RuntimeError):
    """Raised when the SystemInterfaceState is not attached to a Flask app."""


# The frontend build's output, inside the package: what the shell routes serve
# in production.
DEFAULT_STATIC_DIRECTORY = Path(__file__).parent / "static"


class SystemInterfaceState(MutableModel):
    """Holds the shell's config and collaborators for one system-interface app.

    Built once in ``main.build_production_state`` (or by a test) and stored on the Flask
    app; handlers read it via ``get_state()``.
    """

    model_config = {"arbitrary_types_allowed": True, "extra": "forbid", "frozen": False}

    config: Config
    # The shell's own collaborators (the inventory, the stores, the activity log).
    shell: ShellState
    # Who is connected right now: the per-user files under data/.state/presence.
    presence: PresenceStore
    # The periodic check that notices a silent departure and tells the windows.
    presence_sweep: PresenceSweep
    # Each account's display name and avatar, from imbue_cloud through the on-disk cache.
    profiles: ProfileResolver
    # Everyone who may be here, computed from the grants, presence, and the profiles.
    roster: RosterSource
    # How the roster reaches every window, once per change: the heartbeat route, the sweep, and the watch all push
    # through it.
    roster_announcer: RosterAnnouncer
    # Re-announces the roster when a grants file or the gateway's last-seen record changes.
    roster_watch: RosterWatch
    # The workspace's name, from the config or the workspace's mngr records.
    workspace_name: WorkspaceNameSource
    # Captures the tree HEAD this process started from, so the app shell can
    # say when the served tree has moved under it (see update_staleness.py).
    # A factory (not a shared default): the HEAD read happens per state build,
    # not at import.
    update_staleness: UpdateStalenessTracker = Field(default_factory=UpdateStalenessTracker.capture)
    static_directory: Path = Field(
        default=DEFAULT_STATIC_DIRECTORY,
        description="The bundle directory the shell routes serve from: the package's own static/ unless the "
        "state is built with another (a test serving a shell it wrote)",
    )
    # A preview shell is the real desktop with one app swapped, booted over a copy of the
    # live state: it refuses the verbs that would reach the live workspace (an app's stop
    # and start, the update notice's), tells its page so, and raises no staleness banner.
    is_preview: bool = Field(default=False, description="Whether this shell is a preview of a proposed change")
    live_cron_directory: Path = Field(
        default=Path(LIVE_CRON_DIRECTORY),
        description="The directory the cron daemon reads its drop-ins from, where the update routes install the "
        "dispatcher's line; another one only where a test stands in for it",
    )
    backup_lease_path: Path = Field(
        default_factory=backup_lease_path,
        description="The file the backup lease is kept in, which keeps two backup operations apart; another one only "
        "where a test stands in for /run",
    )
    channel_file_fetcher: ChannelFileFetcher = Field(
        default=fetch_channel_file,
        description="Reads a release channel's file off the feed, for an update request that names no version in a "
        "workspace that has read none yet; another one only where a test stands in for the feed",
    )

    def shutdown(self) -> None:
        """Tear down every owned resource. Idempotent."""
        self.presence_sweep.stop()
        self.roster_watch.stop()
        self.shell.broadcaster.shutdown()
        self.shell.stop()


def attach_state(app: Flask, state: SystemInterfaceState) -> None:
    app.config[_STATE_CONFIG_KEY] = state


def get_state() -> SystemInterfaceState:
    """Return the SystemInterfaceState for the current Flask app."""
    state = current_app.config.get(_STATE_CONFIG_KEY)
    if not isinstance(state, SystemInterfaceState):
        raise SystemInterfaceStateError("SystemInterfaceState is not attached to the current app")
    return state


def state_of(app: Flask) -> SystemInterfaceState:
    """Return the SystemInterfaceState attached to ``app`` without needing an app context."""
    state = app.config.get(_STATE_CONFIG_KEY)
    if not isinstance(state, SystemInterfaceState):
        raise SystemInterfaceStateError("SystemInterfaceState is not attached to the app")
    return state
