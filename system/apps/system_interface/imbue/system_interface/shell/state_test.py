"""Tests for ``ShellState``: the stale-client prune it runs at start and on its interval."""

from datetime import timedelta
from pathlib import Path

from imbue.imbue_common.model_update import to_update
from imbue.mngr.utils.polling import wait_for
from imbue.system_interface.shell.clients import CLIENT_RETENTION
from imbue.system_interface.shell.data_types import ClientStateReport
from imbue.system_interface.shell.data_types import StoredWindowPath
from imbue.system_interface.shell.primitives import ClientId
from imbue.system_interface.shell.primitives import DesktopId
from imbue.system_interface.shell.primitives import WindowId
from imbue.system_interface.shell.primitives import WindowPath
from imbue.system_interface.shell.primitives import WindowTitle
from imbue.system_interface.shell.state import build_shell_state
from imbue.system_interface.shell.testing import TEST_NOW
from imbue.system_interface.shell.testing import build_inventory
from imbue.system_interface.shell.testing import placement_record
from imbue.system_interface.shell.testing import write_two_app_registry
from imbue.system_interface.ws_broadcaster import WebSocketBroadcaster


def test_start_prunes_stale_clients_and_their_layouts_now_and_on_the_interval(
    tmp_path: Path, broadcaster: WebSocketBroadcaster
) -> None:
    registry_path = write_two_app_registry(tmp_path)
    inventory = build_inventory(registry_path, broadcaster)
    built = build_shell_state(tmp_path / "state", registry_path, broadcaster, inventory=inventory)
    shell = built.model_copy_update(to_update(built.field_ref().client_prune_interval_seconds, 0.05))
    stale_at = TEST_NOW - CLIENT_RETENTION - timedelta(days=1)
    (home,) = shell.list_desktops()
    window_id = WindowId("win-0000000000000001")
    shell.clients.record_report(
        ClientStateReport(client_id=ClientId("old"), active_desktop=DesktopId("home")), stale_at
    )
    shell.placements.save_browser_layout("home", "old", (placement_record(window_id),), None, {window_id}, stale_at)
    shell.window_paths.set_path(
        ClientId("old"), window_id, StoredWindowPath(path=WindowPath("/x"), title=WindowTitle("")), {window_id}
    )
    shell.start()
    try:
        # The prune at start took the stale client, its layout file, and its window paths.
        assert shell.clients.get_client("old") is None
        assert shell.placements.read_layout(home.id, "old", {window_id}).placements == ()
        assert shell.window_paths.read_paths("old", {window_id}) == {}
        # A client that goes stale while the shell runs is taken by the periodic prune.
        shell.clients.record_report(
            ClientStateReport(client_id=ClientId("later"), active_desktop=DesktopId("home")), stale_at
        )
        wait_for(
            lambda: shell.clients.get_client("later") is None,
            timeout=5.0,
            poll_interval=0.02,
            error_message="the periodic prune never took the stale client",
        )
    finally:
        shell.stop()
