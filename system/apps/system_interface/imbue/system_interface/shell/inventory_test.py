"""Tests for the inventory: the registry read, liveness, the registry watch, and the diffed broadcast."""

import os
import threading
from collections.abc import Sequence
from pathlib import Path

from imbue.mngr.utils.polling import wait_for
from imbue.system_interface.shell.inventory import AppInventory
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
    serialized = [view.model_dump(mode="json") for view in inventory.views()]
    assert serialized[0]["launch_paths"] == [
        {
            "id": "new",
            "label": "New terminal",
            "path": "/new",
            "method": "GET",
            "params": ["workdir"],
            "presets": {},
            "text_param": None,
            "draft_param": None,
        }
    ]
    assert serialized[0]["default_shortcut"] == {"launch": "new", "mode": "new"}
    # An app declaring no launch path offers the synthesized ``open`` at its root.
    assert serialized[1]["launch_paths"] == [
        {
            "id": "open",
            "label": "Files",
            "path": "/",
            "method": "GET",
            "params": [],
            "presets": {},
            "text_param": None,
            "draft_param": None,
        }
    ]
    assert set(serialized[1]) == {
        "name",
        "display_name",
        "icon",
        "label",
        "url",
        "internal",
        "program",
        "critical",
        "stop_when_no_windows",
        "launch_paths",
        "default_shortcut",
        "launcher_rank",
        "pin",
        "message_handlers",
        "is_running",
        "share_url",
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


def test_every_registry_read_is_handed_to_the_hook(tmp_path: Path, broadcaster: WebSocketBroadcaster) -> None:
    registry_path = write_two_app_registry(tmp_path)
    handed: list[list[str]] = []
    inventory = AppInventory(
        registry_path=registry_path,
        broadcaster=broadcaster,
        liveness_prober=FakeLivenessProber(),
        on_registry_read=lambda rows: handed.append([str(row.name) for row in rows]),
    )

    inventory.reload_registry()
    write_registry(registry_path, registry_row_toml("files", TEST_FILES_URL, program="files"))
    inventory.reload_registry()
    registry_path.write_text("[[apps]\nname = ")
    inventory.reload_registry()

    # An unreadable registry keeps the last read and hands nothing on.
    assert handed == [["terminal", "files"], ["files"]]


def test_a_registry_change_listener_hears_each_read_that_changed_the_rows_with_no_inventory_lock_held(
    tmp_path: Path, broadcaster: WebSocketBroadcaster
) -> None:
    registry_path = write_two_app_registry(tmp_path)
    inventory = AppInventory(
        registry_path=registry_path, broadcaster=broadcaster, liveness_prober=FakeLivenessProber()
    )
    heard: list[list[str]] = []

    def listen() -> None:
        # Reading the inventory, and even reading the registry again, would deadlock under one of its locks.
        inventory.reload_registry()
        heard.append([str(entry.row.name) for entry in inventory.entries()])

    inventory.add_registry_change_listener(listen)

    def read_the_registry_as_it_changes() -> None:
        inventory.reload_registry()
        inventory.reload_registry()
        write_registry(registry_path, registry_row_toml("files", TEST_FILES_URL, program="files"))
        inventory.reload_registry()
        registry_path.write_text("[[apps]\nname = ")
        inventory.reload_registry()

    reader = threading.Thread(target=read_the_registry_as_it_changes, daemon=True)
    reader.start()
    reader.join(timeout=5.0)

    assert not reader.is_alive(), "a listener ran under one of the inventory's locks"
    # The first read and the one that dropped a row changed the rows; the unchanged and the unreadable ones did not.
    assert heard == [["terminal", "files"], ["files"]]


def test_the_sweep_re_reads_a_registry_whose_mtime_moved(tmp_path: Path, broadcaster: WebSocketBroadcaster) -> None:
    """The backstop for a write no watch event reported: a sweep pass compares the file's mtime with the
    last read's and reads again when it moved."""
    registry_path = write_two_app_registry(tmp_path)
    inventory = build_inventory(registry_path, broadcaster)

    write_registry(registry_path, registry_row_toml("files", TEST_FILES_URL, program="files"))
    os.utime(registry_path, ns=(1, 1))
    inventory.sweep_once()
    assert [str(entry.row.name) for entry in inventory.entries()] == ["files"]

    # A pass over an unchanged file reads nothing again.
    handed: list[int] = []
    inventory_with_hook = AppInventory(
        registry_path=registry_path,
        broadcaster=broadcaster,
        liveness_prober=FakeLivenessProber(),
        on_registry_read=lambda rows: handed.append(len(rows)),
    )
    inventory_with_hook.reload_registry()
    inventory_with_hook.sweep_once()
    inventory_with_hook.sweep_once()
    assert handed == [1]


def test_a_broken_registry_is_read_again_only_once_it_changes(
    tmp_path: Path, broadcaster: WebSocketBroadcaster
) -> None:
    """A read that fails still records the file's mtime, so the sweep's backstop does not parse (and log) the
    same broken file on every pass; the next change to the file is what brings a re-read."""
    registry_path = write_two_app_registry(tmp_path)
    inventory = build_inventory(registry_path, broadcaster)

    registry_path.write_text("[[apps]\nname = ")
    os.utime(registry_path, ns=(1, 1))
    inventory.sweep_once()
    assert [str(entry.row.name) for entry in inventory.entries()] == ["terminal", "files"]

    # A good registry written at the same mtime is not read: the failed read was recorded against that mtime.
    write_registry(registry_path, registry_row_toml("files", TEST_FILES_URL, program="files"))
    os.utime(registry_path, ns=(1, 1))
    inventory.sweep_once()
    assert [str(entry.row.name) for entry in inventory.entries()] == ["terminal", "files"]

    os.utime(registry_path, ns=(2, 2))
    inventory.sweep_once()
    assert [str(entry.row.name) for entry in inventory.entries()] == ["files"]


def test_a_registry_that_cannot_be_stat_ed_is_warned_about_and_does_not_end_the_sweep(
    tmp_path: Path, broadcaster: WebSocketBroadcaster, loguru_records: list[str]
) -> None:
    """A missing registry is the quiet, expected case; any other stat failure (here, a parent that is a file) is
    logged, so the mtime backstop failing to see a write does not pass in silence."""
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where the registry's directory should be")
    inventory = build_inventory(blocker / "apps.toml", broadcaster)

    inventory.sweep_once()

    assert inventory.entries() == []
    warnings = [record for record in loguru_records if record.startswith("WARNING")]
    assert warnings and all("Could not stat the app registry" in record for record in warnings)
    absent = build_inventory(tmp_path / "absent" / "apps.toml", broadcaster)
    absent.sweep_once()
    assert len([record for record in loguru_records if record.startswith("WARNING")]) == len(warnings)


_SHARE_DOMAIN = "0123456789abcdef0123456789abcdef.fedcba9876543210fedcba9876543210.us1.personal-imbue.com"


def test_each_labelled_app_lists_its_address_on_the_domain_the_workspace_was_last_shared_under(
    tmp_path: Path, broadcaster: WebSocketBroadcaster
) -> None:
    registry_path = write_registry(
        tmp_path / "apps.toml",
        registry_row_toml("files", TEST_FILES_URL, program="files", label="files-ab12cd34"),
        registry_row_toml("terminal", TEST_TERMINAL_URL, program="terminal"),
    )
    share_domain_path = tmp_path / "share_domain"
    inventory = build_inventory(registry_path, broadcaster, share_domain_path=share_domain_path)
    client_queue = broadcaster.register()
    assert [view.share_url for view in inventory.views()] == [None, None]

    share_domain_path.write_text(f"{_SHARE_DOMAIN.upper()}\n")
    inventory.sweep_once()

    assert [view.share_url for view in inventory.views()] == [f"https://files-ab12cd34.{_SHARE_DOMAIN}/", None]
    assert [[app["share_url"] for app in message["apps"]] for message in drain_messages(client_queue)] == [
        [f"https://files-ab12cd34.{_SHARE_DOMAIN}/", None]
    ]


def test_a_share_domain_file_that_names_no_domain_gives_no_app_a_share_address(
    tmp_path: Path, broadcaster: WebSocketBroadcaster
) -> None:
    registry_path = write_registry(
        tmp_path / "apps.toml", registry_row_toml("files", TEST_FILES_URL, program="files", label="files-ab12cd34")
    )
    share_domain_path = tmp_path / "share_domain"
    inventory = build_inventory(registry_path, broadcaster, share_domain_path=share_domain_path)

    for text in ("", "evil.example/path", "two words.example", "https://evil.example"):
        share_domain_path.write_text(text)
        assert [view.share_url for view in inventory.views()] == [None], text
