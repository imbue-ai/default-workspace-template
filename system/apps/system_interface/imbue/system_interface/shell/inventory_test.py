"""Tests for the inventory: the registry read, liveness, the fetched lists, nudging, and the diffed broadcast."""

from pathlib import Path

from app_instances.data_types import InstanceLifetime
from app_instances.data_types import InstanceStatus
from app_instances.testing import StubInstanceSource
from app_instances.testing import wait_until

from imbue.system_interface.shell.inventory import FetchOutcomeKind
from imbue.system_interface.shell.inventory import HttpInstanceFetcher
from imbue.system_interface.shell.inventory import parse_instances_body
from imbue.system_interface.shell.primitives import Address
from imbue.system_interface.shell.testing import FakeInstanceFetcher
from imbue.system_interface.shell.testing import FakeLivenessProber
from imbue.system_interface.shell.testing import build_inventory
from imbue.system_interface.shell.testing import drain_messages
from imbue.system_interface.shell.testing import instance_record
from imbue.system_interface.shell.testing import registry_row_toml
from imbue.system_interface.shell.testing import write_registry
from imbue.system_interface.ws_broadcaster import WebSocketBroadcaster

_TERMINAL_URL = "http://localhost:7681"
_FILES_URL = "http://localhost:7000"


def _registry(tmp_path: Path) -> Path:
    return write_registry(
        tmp_path / "apps.toml",
        registry_row_toml("terminal", _TERMINAL_URL, True, program="terminal", actions=[("new", "New terminal")]),
        registry_row_toml("files", _FILES_URL, program="files"),
    )


def test_the_registry_read_synthesizes_single_instance_records(
    tmp_path: Path, broadcaster: WebSocketBroadcaster
) -> None:
    client_queue = broadcaster.register()
    inventory = build_inventory(_registry(tmp_path), broadcaster)

    entries = inventory.entries()
    assert [str(entry.row.name) for entry in entries] == ["terminal", "files"]
    terminal, files = entries
    assert terminal.instances == ()
    assert len(files.instances) == 1
    assert files.instances[0].key == "" and files.instances[0].status is InstanceStatus.IDLE
    assert files.addresses() == [Address("app:files")]
    assert inventory.find_instance(Address("app:files")) is not None
    assert inventory.find_instance(Address("app:terminal?instance=terminal-1")) is None
    serialized = inventory.serialized()
    assert serialized[0]["actions"] == [{"id": "new", "label": "New terminal"}]
    assert serialized[1]["actions"] == [{"id": "open", "label": "Open Files"}]
    assert serialized[1]["instances"][0]["key"] == ""
    # One broadcast for the read; the liveness probe that found everything running adds none.
    assert [message["type"] for message in drain_messages(client_queue)] == ["apps_updated"]


def test_a_fetched_list_replaces_the_apps_instances_and_reports_what_left(
    tmp_path: Path, broadcaster: WebSocketBroadcaster
) -> None:
    fetcher = FakeInstanceFetcher()
    fetcher.list(
        _TERMINAL_URL, instance_record("terminal-1", "Terminal 1"), instance_record("terminal-2", "Terminal 2")
    )
    inventory = build_inventory(_registry(tmp_path), broadcaster, fetcher=fetcher)
    removed: list[list[Address]] = []
    inventory.add_removed_listener(removed.append)

    inventory.refetch_now("terminal")
    assert inventory.listed_addresses() == {
        Address("app:terminal?instance=terminal-1"),
        Address("app:terminal?instance=terminal-2"),
        Address("app:files"),
    }
    assert removed == []

    fetcher.list(_TERMINAL_URL, instance_record("terminal-2", "Terminal 2"))
    inventory.refetch_now("terminal")
    assert removed == [[Address("app:terminal?instance=terminal-1")]]
    # A single-instance app is never fetched.
    inventory.refetch_now("files")
    assert fetcher.fetched_urls == [_TERMINAL_URL, _TERMINAL_URL]


def test_not_ready_keeps_the_list_and_a_failure_marks_it_error(
    tmp_path: Path, broadcaster: WebSocketBroadcaster
) -> None:
    fetcher = FakeInstanceFetcher()
    fetcher.list(_TERMINAL_URL, instance_record("terminal-1"))
    inventory = build_inventory(_registry(tmp_path), broadcaster, fetcher=fetcher)
    inventory.refetch_now("terminal")

    fetcher.not_ready(_TERMINAL_URL)
    inventory.refetch_now("terminal")
    found = inventory.find_instance(Address("app:terminal?instance=terminal-1"))
    assert found is not None and found[1].status is InstanceStatus.IDLE

    fetcher.fail(_TERMINAL_URL)
    inventory.refetch_now("terminal")
    found = inventory.find_instance(Address("app:terminal?instance=terminal-1"))
    assert found is not None and found[1].status is InstanceStatus.ERROR


def test_liveness_rewrites_statuses_and_refetches_an_app_that_came_back(
    tmp_path: Path, broadcaster: WebSocketBroadcaster
) -> None:
    fetcher = FakeInstanceFetcher()
    fetcher.list(_TERMINAL_URL, instance_record("terminal-1"))
    prober = FakeLivenessProber()
    inventory = build_inventory(_registry(tmp_path), broadcaster, fetcher=fetcher, prober=prober)
    inventory.refetch_now("terminal")
    client_queue = broadcaster.register()

    prober.is_running_by_name = {"terminal": False, "files": False}
    inventory.refresh_liveness()
    terminal, files = inventory.entries()
    assert not terminal.is_running and terminal.instances[0].status is InstanceStatus.STOPPED
    assert not files.is_running and files.instances[0].status is InstanceStatus.STOPPED
    assert [message["type"] for message in drain_messages(client_queue)] == ["apps_updated"]
    # A stopped app is not fetched.
    inventory.refetch_now("terminal")
    assert fetcher.fetched_urls == [_TERMINAL_URL]

    prober.is_running_by_name = {}
    inventory.refresh_liveness()
    terminal, files = inventory.entries()
    assert terminal.is_running and files.instances[0].status is InstanceStatus.IDLE
    assert fetcher.fetched_urls == [_TERMINAL_URL, _TERMINAL_URL]
    # Nothing changed since, so nothing is broadcast again.
    drain_messages(client_queue)
    inventory.refresh_liveness()
    assert drain_messages(client_queue) == []


def test_nudges_coalesce_into_one_fetch(tmp_path: Path, broadcaster: WebSocketBroadcaster) -> None:
    fetcher = FakeInstanceFetcher()
    fetcher.list(_TERMINAL_URL, instance_record("terminal-1"))
    inventory = build_inventory(_registry(tmp_path), broadcaster, fetcher=fetcher)
    try:
        assert inventory.nudge("terminal") is True
        assert inventory.nudge("terminal") is True
        assert inventory.nudge("unknown") is False
        assert wait_until(lambda: fetcher.fetched_urls == [_TERMINAL_URL], timeout_seconds=2.0)
        assert wait_until(
            lambda: inventory.find_instance(Address("app:terminal?instance=terminal-1")) is not None,
            timeout_seconds=2.0,
        )
        assert fetcher.fetched_urls == [_TERMINAL_URL]
    finally:
        inventory.stop()


def test_a_registry_change_keeps_known_lists_and_drops_removed_rows(
    tmp_path: Path, broadcaster: WebSocketBroadcaster
) -> None:
    fetcher = FakeInstanceFetcher()
    fetcher.list(_TERMINAL_URL, instance_record("terminal-1"))
    registry_path = _registry(tmp_path)
    inventory = build_inventory(registry_path, broadcaster, fetcher=fetcher)
    inventory.refetch_now("terminal")

    write_registry(
        registry_path,
        registry_row_toml("terminal", _TERMINAL_URL, True, program="terminal", display_name="Shells"),
    )
    inventory.reload_registry()
    entries = inventory.entries()
    assert [str(entry.row.name) for entry in entries] == ["terminal"]
    assert str(entries[0].row.display_name) == "Shells"
    assert entries[0].addresses() == [Address("app:terminal?instance=terminal-1")]


def test_the_grace_period_follows_the_first_listing(tmp_path: Path, broadcaster: WebSocketBroadcaster) -> None:
    now = [1000.0]
    fetcher = FakeInstanceFetcher()
    fetcher.list(_TERMINAL_URL, instance_record("terminal-1", lifetime=InstanceLifetime.REFERENCED))
    inventory = build_inventory(_registry(tmp_path), broadcaster, fetcher=fetcher, clock=lambda: now[0])
    inventory.refetch_now("terminal")
    found = inventory.find_instance(Address("app:terminal?instance=terminal-1"))
    assert found is not None and inventory.is_within_grace(*found)
    now[0] += 60.0
    inventory.refetch_now("terminal")
    found = inventory.find_instance(Address("app:terminal?instance=terminal-1"))
    assert found is not None and not inventory.is_within_grace(*found)


def test_parsing_an_instances_body() -> None:
    listed = parse_instances_body(
        "u", b'{"instances": [%s, {"key": "bad"}]}' % instance_record("k1").model_dump_json().encode()
    )
    assert listed.kind is FetchOutcomeKind.LISTED
    assert [str(record.key) for record in listed.records] == ["k1"]
    assert parse_instances_body("u", b"not json").kind is FetchOutcomeKind.FAILED
    assert parse_instances_body("u", b'{"nope": []}').kind is FetchOutcomeKind.FAILED


def test_the_fetcher_reads_a_503_as_not_ready_and_a_listing_as_listed(
    stub_source: StubInstanceSource, stub_app_url: str
) -> None:
    stub_source.is_ready = False
    assert HttpInstanceFetcher().fetch(stub_app_url).kind is FetchOutcomeKind.NOT_READY
    stub_source.is_ready = True
    stub_source.records.append(instance_record("k1"))
    listed = HttpInstanceFetcher().fetch(stub_app_url)
    assert listed.kind is FetchOutcomeKind.LISTED
    assert [str(record.key) for record in listed.records] == ["k1"]
    assert HttpInstanceFetcher().fetch("http://127.0.0.1:1").kind is FetchOutcomeKind.FAILED
