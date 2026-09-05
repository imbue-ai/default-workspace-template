"""Tests for the inventory: the registry read, liveness, the fetched lists, nudging, and the diffed broadcast."""

import threading
from pathlib import Path

from app_instances.data_types import InstanceLifetime
from app_instances.data_types import InstanceStatus
from app_instances.testing import StubInstanceSource
from app_instances.testing import wait_until
from pydantic import Field
from pydantic import PrivateAttr
from watchdog.events import DirModifiedEvent
from watchdog.events import FileModifiedEvent
from watchdog.events import FileMovedEvent

from imbue.system_interface.shell.errors import ShellStateError
from imbue.system_interface.shell.inventory import AppInventory
from imbue.system_interface.shell.inventory import FetchOutcomeKind
from imbue.system_interface.shell.inventory import HttpInstanceFetcher
from imbue.system_interface.shell.inventory import InstanceFetchOutcome
from imbue.system_interface.shell.inventory import InstanceFetcherInterface
from imbue.system_interface.shell.inventory import _make_registry_file_handler
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


def test_an_app_counts_as_listed_once_its_list_has_arrived(tmp_path: Path, broadcaster: WebSocketBroadcaster) -> None:
    fetcher = FakeInstanceFetcher()
    inventory = build_inventory(_registry(tmp_path), broadcaster, fetcher=fetcher)
    terminal, files = inventory.entries()
    # The synthesized record of a single-instance app is its list; an instances app's seed is not.
    assert files.is_listed and not terminal.is_listed
    assert inventory.serialized()[0]["is_listed"] is False
    fetcher.fail(_TERMINAL_URL)
    inventory.refetch_now("terminal")
    assert not inventory.entries()[0].is_listed
    fetcher.list(_TERMINAL_URL)
    inventory.refetch_now("terminal")
    assert inventory.entries()[0].is_listed
    # A registry re-read keeps the flag with the list it describes.
    inventory.reload_registry()
    assert inventory.entries()[0].is_listed


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


def test_a_row_whose_instances_flag_flips_starts_its_list_over(
    tmp_path: Path, broadcaster: WebSocketBroadcaster
) -> None:
    fetcher = FakeInstanceFetcher()
    fetcher.list(_FILES_URL, instance_record("files-1"))
    registry_path = _registry(tmp_path)
    inventory = build_inventory(registry_path, broadcaster, fetcher=fetcher)
    assert inventory.entries()[1].is_listed

    write_registry(
        registry_path,
        registry_row_toml("terminal", _TERMINAL_URL, True, program="terminal"),
        registry_row_toml("files", _FILES_URL, True, program="files"),
    )
    inventory.reload_registry()
    files = inventory.entries()[1]
    assert files.instances == () and not files.is_listed
    inventory.refetch_now("files")
    assert inventory.entries()[1].addresses() == [Address("app:files?instance=files-1")]

    write_registry(registry_path, registry_row_toml("files", _FILES_URL, program="files"))
    inventory.reload_registry()
    (files_again,) = inventory.entries()
    assert files_again.is_listed and files_again.addresses() == [Address("app:files")]


def test_an_unreadable_registry_keeps_the_last_good_read(tmp_path: Path, broadcaster: WebSocketBroadcaster) -> None:
    fetcher = FakeInstanceFetcher()
    fetcher.list(_TERMINAL_URL, instance_record("terminal-1"))
    registry_path = _registry(tmp_path)
    inventory = build_inventory(registry_path, broadcaster, fetcher=fetcher)
    inventory.refetch_now("terminal")
    client_queue = broadcaster.register()

    registry_path.write_text("[[apps]\nname = ")
    inventory.reload_registry()

    entries = inventory.entries()
    assert [str(entry.row.name) for entry in entries] == ["terminal", "files"]
    assert entries[0].addresses() == [Address("app:terminal?instance=terminal-1")]
    assert drain_messages(client_queue) == []


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
    fetcher = FakeInstanceFetcher()
    fetcher.list(_TERMINAL_URL, instance_record("terminal-1"))
    files_row = registry_row_toml("files", _FILES_URL, program="files")
    registry_path = write_registry(tmp_path / "apps.toml", files_row)
    inventory = AppInventory(
        registry_path=registry_path,
        broadcaster=broadcaster,
        liveness_prober=FakeLivenessProber(),
        fetcher=fetcher,
        coalesce_seconds=0.01,
        sweep_interval_seconds=60.0,
    )
    inventory.start()
    try:
        assert [str(entry.row.name) for entry in inventory.entries()] == ["files"]
        write_registry(
            registry_path,
            files_row,
            registry_row_toml("terminal", _TERMINAL_URL, True, program="terminal", actions=[("new", "New terminal")]),
        )
        assert wait_until(
            lambda: inventory.find_instance(Address("app:terminal?instance=terminal-1")) is not None,
            timeout_seconds=5.0,
        )
    finally:
        inventory.stop()


def test_refetch_all_fetches_every_running_app_with_instances(
    tmp_path: Path, broadcaster: WebSocketBroadcaster
) -> None:
    fetcher = FakeInstanceFetcher()
    fetcher.list(_TERMINAL_URL, instance_record("terminal-1"))
    prober = FakeLivenessProber()
    inventory = build_inventory(_registry(tmp_path), broadcaster, fetcher=fetcher, prober=prober)
    inventory.refetch_all()
    # The single-instance files app is never fetched.
    assert fetcher.fetched_urls == [_TERMINAL_URL]
    prober.is_running_by_name = {"terminal": False}
    inventory.refresh_liveness()
    inventory.refetch_all()
    # A stopped app is skipped too.
    assert fetcher.fetched_urls == [_TERMINAL_URL]


def _raise_state_error(addresses: list[Address]) -> None:
    raise ShellStateError(f"cannot write the tab sets after {addresses} left")


def test_a_failing_pass_does_not_end_the_sweep(tmp_path: Path, broadcaster: WebSocketBroadcaster) -> None:
    fetcher = FakeInstanceFetcher()
    fetcher.list(_TERMINAL_URL, instance_record("terminal-1"), instance_record("terminal-2"))
    inventory = build_inventory(_registry(tmp_path), broadcaster, fetcher=fetcher)
    inventory.refetch_now("terminal")
    inventory.add_removed_listener(_raise_state_error)

    # The app dropped an instance, so folding the next list fires the listener, which raises.
    fetcher.list(_TERMINAL_URL, instance_record("terminal-2"))
    inventory.sweep_once(is_reconciling=True)
    inventory.sweep_once(is_reconciling=True)
    assert fetcher.fetched_urls == [_TERMINAL_URL] * 3
    assert inventory.listed_addresses() == {Address("app:terminal?instance=terminal-2"), Address("app:files")}


class _GatedFetcher(InstanceFetcherInterface):
    """Answers the queued outcomes in call order; the first call waits for the gate before answering."""

    model_config = {"arbitrary_types_allowed": True, "extra": "forbid", "frozen": False}

    outcomes: list[InstanceFetchOutcome] = Field(description="What each call answers, in order")
    gate: threading.Event = Field(default_factory=threading.Event, description="Released to let the first call answer")
    first_call_started: threading.Event = Field(
        default_factory=threading.Event, description="Set when the first call begins"
    )
    second_call_started: threading.Event = Field(
        default_factory=threading.Event, description="Set when the second call begins"
    )
    _call_count: int = PrivateAttr(default=0)

    def fetch(self, instances_url: str) -> InstanceFetchOutcome:
        self._call_count += 1
        call_number = self._call_count
        if call_number == 1:
            self.first_call_started.set()
            self.gate.wait(timeout=5)
        elif call_number == 2:
            self.second_call_started.set()
        return self.outcomes[call_number - 1]


def test_the_folds_of_one_app_land_in_fetch_order(tmp_path: Path, broadcaster: WebSocketBroadcaster) -> None:
    stale = InstanceFetchOutcome(kind=FetchOutcomeKind.LISTED, records=(instance_record("terminal-1"),))
    fresh = InstanceFetchOutcome(
        kind=FetchOutcomeKind.LISTED, records=(instance_record("terminal-1"), instance_record("terminal-2"))
    )
    fetcher = _GatedFetcher(outcomes=[stale, fresh])
    inventory = build_inventory(_registry(tmp_path), broadcaster, fetcher=fetcher)
    removed: list[list[Address]] = []
    inventory.add_removed_listener(removed.append)

    # The sweep's fetch is in flight (blocked on the gate) when a create's refetch arrives.
    sweep = threading.Thread(target=inventory.refetch_now, args=("terminal",))
    sweep.start()
    assert fetcher.first_call_started.wait(timeout=5)
    after_create = threading.Thread(target=inventory.refetch_now, args=("terminal",))
    after_create.start()
    # The second fetch must wait for the first: it has not started by the time the gate opens.
    assert not fetcher.second_call_started.wait(timeout=0.2)
    fetcher.gate.set()
    sweep.join(timeout=5)
    after_create.join(timeout=5)

    assert inventory.listed_addresses() == {
        Address("app:terminal?instance=terminal-1"),
        Address("app:terminal?instance=terminal-2"),
        Address("app:files"),
    }
    assert removed == []
