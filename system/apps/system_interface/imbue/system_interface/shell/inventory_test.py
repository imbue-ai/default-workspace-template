"""Tests for the inventory: the registry read, liveness, the registry watch, and the diffed broadcast."""

from collections.abc import Sequence
from pathlib import Path

from watchdog.events import DirModifiedEvent
from watchdog.events import FileModifiedEvent
from watchdog.events import FileMovedEvent

from imbue.mngr.utils.polling import wait_for
from imbue.system_interface.shell.inventory import AppInventory
from imbue.system_interface.shell.inventory import _make_registry_file_handler
from imbue.system_interface.shell.testing import FakeLivenessProber
from imbue.system_interface.shell.testing import TEST_FILES_URL
from imbue.system_interface.shell.testing import TEST_TERMINAL_URL
from imbue.system_interface.shell.testing import build_inventory
from imbue.system_interface.shell.testing import drain_messages
from imbue.system_interface.shell.testing import registry_row_toml
from imbue.system_interface.shell.testing import write_registry
from imbue.system_interface.shell.testing import write_two_app_registry
from imbue.system_interface.ws_broadcaster import WebSocketBroadcaster


def test_the_registry_read_lists_every_app_with_its_launch_paths(
    tmp_path: Path, broadcaster: WebSocketBroadcaster
) -> None:
    client_queue = broadcaster.register()
    inventory = build_inventory(write_two_app_registry(tmp_path), broadcaster)

    entries = inventory.entries()
    assert [str(entry.row.name) for entry in entries] == ["terminal", "files"]
    assert all(entry.is_running for entry in entries)
    assert inventory.entry("files") is not None and inventory.entry("nope") is None
    serialized = inventory.serialized()
    assert serialized[0]["launch_paths"] == [{"id": "new", "label": "New terminal", "path": "/new", "params": []}]
    assert serialized[0]["default_shortcut"] == {"launch": "new", "mode": "new"}
    # An app declaring no launch path offers the synthesized ``open`` at its root.
    assert serialized[1]["launch_paths"] == [{"id": "open", "label": "Open Files", "path": "/", "params": []}]
    assert set(serialized[1]) == {
        "name",
        "display_name",
        "icon",
        "label",
        "url",
        "internal",
        "program",
        "critical",
        "launch_paths",
        "default_shortcut",
        "launcher_rank",
        "pin",
        "is_running",
    }
    assert serialized[1]["pin"] is None
    # One broadcast for the read; the liveness probe that found everything running adds none.
    assert [message["type"] for message in drain_messages(client_queue)] == ["apps_updated"]
    assert inventory.is_registry_read is True


def test_liveness_changes_are_broadcast_once_and_kept_across_a_registry_read(
    tmp_path: Path, broadcaster: WebSocketBroadcaster
) -> None:
    prober = FakeLivenessProber()
    registry_path = write_two_app_registry(tmp_path)
    inventory = build_inventory(registry_path, broadcaster, prober=prober)
    client_queue = broadcaster.register()

    prober.is_running_by_name["terminal"] = False
    inventory.refresh_liveness()
    inventory.refresh_liveness()
    assert [entry.is_running for entry in inventory.entries()] == [False, True]
    assert [message["apps"][0]["is_running"] for message in drain_messages(client_queue)] == [False]

    # A registry read keeps what the probe found, and a row that disappears is dropped.
    write_registry(registry_path, registry_row_toml("terminal", TEST_TERMINAL_URL, program="terminal"))
    inventory.reload_registry()
    (terminal,) = inventory.entries()
    assert terminal.is_running is False
    assert [message["type"] for message in drain_messages(client_queue)] == ["apps_updated"]


def test_an_unreadable_registry_keeps_the_last_good_read(tmp_path: Path, broadcaster: WebSocketBroadcaster) -> None:
    registry_path = write_two_app_registry(tmp_path)
    inventory = build_inventory(registry_path, broadcaster)
    client_queue = broadcaster.register()

    registry_path.write_text("[[apps]\nname = ")
    inventory.reload_registry()

    assert [str(entry.row.name) for entry in inventory.entries()] == ["terminal", "files"]
    assert drain_messages(client_queue) == []


def test_the_registry_watch_fires_for_the_registry_file_alone(tmp_path: Path) -> None:
    fired: list[bool] = []
    handler = _make_registry_file_handler("apps.toml", lambda: fired.append(True))
    handler.on_modified(FileModifiedEvent(str(tmp_path / "apps.toml")))
    # forward_port.py replaces the file atomically: the move's destination is the registry.
    handler.on_moved(FileMovedEvent(str(tmp_path / "apps.toml.tmp-1"), str(tmp_path / "apps.toml")))
    handler.on_modified(FileModifiedEvent(str(tmp_path / "apps.toml.tmp-2")))
    handler.on_modified(DirModifiedEvent(str(tmp_path)))
    assert len(fired) == 2


def test_start_watches_the_registry_and_lists_a_row_that_appears(
    tmp_path: Path, broadcaster: WebSocketBroadcaster
) -> None:
    files_row = registry_row_toml("files", TEST_FILES_URL, program="files")
    registry_path = write_registry(tmp_path / "apps.toml", files_row)
    inventory = AppInventory(
        registry_path=registry_path,
        broadcaster=broadcaster,
        liveness_prober=FakeLivenessProber(),
        sweep_interval_seconds=60.0,
    )
    inventory.start()
    try:
        assert [str(entry.row.name) for entry in inventory.entries()] == ["files"]
        write_registry(registry_path, files_row, registry_row_toml("terminal", TEST_TERMINAL_URL, program="terminal"))
        wait_for(
            lambda: inventory.entry("terminal") is not None,
            timeout=5.0,
            poll_interval=0.02,
            error_message="the registry watch never listed the new row",
        )
    finally:
        inventory.stop()


def test_a_failing_pass_does_not_end_the_sweep(tmp_path: Path, broadcaster: WebSocketBroadcaster) -> None:
    is_probe_failing = [False]
    probed: list[int] = []

    def prober(targets: Sequence[tuple[str, str, str]]) -> dict[str, bool]:
        probed.append(len(targets))
        if is_probe_failing[0]:
            raise OSError("the supervisor socket is gone")
        return {name: True for name, _program, _url in targets}

    inventory = build_inventory(write_two_app_registry(tmp_path), broadcaster, prober=prober)

    is_probe_failing[0] = True
    inventory.sweep_once()
    is_probe_failing[0] = False
    inventory.sweep_once()
    assert probed == [2, 2, 2]
    assert all(entry.is_running for entry in inventory.entries())
