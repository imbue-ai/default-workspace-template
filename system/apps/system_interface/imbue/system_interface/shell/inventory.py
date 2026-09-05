"""The app-agnostic inventory: the registry, each app's liveness, and each app's instance list.

The registry (``data/.state/apps.toml``) is watched for changes; liveness is re-derived on a
sweep and after a stop or start; instance lists are fetched from each app's instances API when
the app nudges the shell (``POST /api/apps/<name>/changed``, coalesced per app), after a relay
verb, and on a slow reconciliation sweep (contracts.md section 5). Every change of the merged
inventory is broadcast as one ``apps_updated`` message, diffed against the last one sent.
"""

import json
import os
import threading
import time
from abc import ABC
from abc import abstractmethod
from collections.abc import Callable
from collections.abc import Sequence
from enum import auto
from pathlib import Path
from typing import Any
from typing import Final

import httpx
from app_instances.data_types import InstanceRecord
from app_instances.data_types import InstanceStatus
from app_manifest.registry import RegistryRow
from app_manifest.registry import read_registry
from loguru import logger
from pydantic import Field
from pydantic import ValidationError
from watchdog.events import FileMovedEvent
from watchdog.events import FileSystemEvent
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer as _Observer

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.system_interface.shell.data_types import AppInventoryEntry
from imbue.system_interface.shell.data_types import InventoryInstance
from imbue.system_interface.shell.data_types import app_wire_json
from imbue.system_interface.shell.data_types import inventory_instance_from_record
from imbue.system_interface.shell.data_types import synthesized_single_instance
from imbue.system_interface.shell.liveness import probe_all_app_liveness
from imbue.system_interface.shell.primitives import Address
from imbue.system_interface.ws_broadcaster import WebSocketBroadcaster

INSTANCES_PATH: Final[str] = "/_instances"

# The nudge window of contracts.md section 5: the first nudge for an app starts it, one refetch
# runs when it closes.
NUDGE_COALESCE_SECONDS: Final[float] = 0.25
# How often liveness is re-derived (the old agent manager's interval) and, every third pass,
# every running app's list refetched (the 30 second reconciliation sweep of section 5).
LIVENESS_SWEEP_INTERVAL_SECONDS: Final[float] = 10.0
RECONCILE_EVERY_SWEEPS: Final[int] = 3
# A fetch is one loopback request an app answers from memory; past the first threshold it is
# suspicious, past the second broken.
FETCH_SLOW_SECONDS: Final[float] = 1.0
FETCH_TIMEOUT_SECONDS: Final[float] = 5.0
# How long a freshly listed referenced instance is exempt from the unreferenced-deletion sweep,
# so the tab docking it has time to be saved.
NEW_INSTANCE_GRACE_SECONDS: Final[float] = 30.0

HTTP_SERVICE_UNAVAILABLE: Final[int] = 503


class FetchOutcomeKind(UpperCaseStrEnum):
    """What one fetch of an app's list came to."""

    LISTED = auto()
    NOT_READY = auto()
    FAILED = auto()


class InstanceFetchOutcome(FrozenModel):
    """The result of asking an app for its instance list."""

    kind: FetchOutcomeKind = Field(description="Listed, still initialising (a 503), or unreachable and refusing")
    records: tuple[InstanceRecord, ...] = Field(description="The listed records when listed, else empty")


class InstanceFetcherInterface(MutableModel, ABC):
    """Fetches one app's instance list from its instances API."""

    @abstractmethod
    def fetch(self, instances_url: str) -> InstanceFetchOutcome:
        """GET ``<instances_url>/_instances``; never raises for an unreachable app."""


class HttpInstanceFetcher(InstanceFetcherInterface):
    """The production fetcher: one loopback GET per call."""

    def fetch(self, instances_url: str) -> InstanceFetchOutcome:
        url = f"{instances_url.rstrip('/')}{INSTANCES_PATH}"
        started_at = time.monotonic()
        try:
            response = httpx.get(url, timeout=FETCH_TIMEOUT_SECONDS)
        except httpx.HTTPError as e:
            logger.debug("Failed to fetch instances from {}: {}", url, e)
            return InstanceFetchOutcome(kind=FetchOutcomeKind.FAILED, records=())
        elapsed = time.monotonic() - started_at
        if elapsed > FETCH_SLOW_SECONDS:
            logger.warning("Fetched instances from {} slowly, in {:.1f}s", url, elapsed)
        if response.status_code == HTTP_SERVICE_UNAVAILABLE:
            return InstanceFetchOutcome(kind=FetchOutcomeKind.NOT_READY, records=())
        if response.is_error:
            logger.debug("Fetching instances from {} answered {}", url, response.status_code)
            return InstanceFetchOutcome(kind=FetchOutcomeKind.FAILED, records=())
        return parse_instances_body(url, response.content)


def parse_instances_body(url: str, body: bytes) -> InstanceFetchOutcome:
    """The records in a ``GET /_instances`` body; a body that is not the contract's shape counts as a failed fetch."""
    try:
        parsed = json.loads(body)
    except ValueError as e:
        logger.warning("Fetched instances from {} but the body is not JSON: {}", url, e)
        return InstanceFetchOutcome(kind=FetchOutcomeKind.FAILED, records=())
    raw_records = parsed.get("instances") if isinstance(parsed, dict) else None
    if not isinstance(raw_records, list):
        logger.warning("Fetched instances from {} but the body carries no 'instances' list", url)
        return InstanceFetchOutcome(kind=FetchOutcomeKind.FAILED, records=())
    records: list[InstanceRecord] = []
    for raw_record in raw_records:
        try:
            records.append(InstanceRecord.model_validate(raw_record))
        except ValidationError as e:
            logger.warning("Skipped an instance record from {}: {}", url, e.errors()[0]["msg"])
    return InstanceFetchOutcome(kind=FetchOutcomeKind.LISTED, records=tuple(records))


class _RegistryFileHandler(FileSystemEventHandler):
    """Fires ``on_change`` on mutating events whose path is the registry file (the old agent manager's watcher)."""

    def __init__(self, basename: str, on_change: Callable[[], None]) -> None:
        super().__init__()
        self._basename = basename
        self._on_change = on_change

    def _maybe_fire(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        paths = [event.src_path]
        if isinstance(event, FileMovedEvent):
            paths.append(event.dest_path)
        if any(os.path.basename(str(path)) == self._basename for path in paths):
            self._on_change()

    on_modified = _maybe_fire
    on_created = _maybe_fire
    on_deleted = _maybe_fire
    on_moved = _maybe_fire
    on_closed = _maybe_fire


@pure
def _instances_url(row: RegistryRow) -> str:
    return str(row.instances_url) if row.instances_url is not None else str(row.url)


@pure
def _with_status(instances: Sequence[InventoryInstance], status: InstanceStatus) -> tuple[InventoryInstance, ...]:
    return tuple(instance.model_copy_update(to_update(instance.field_ref().status, status)) for instance in instances)


@pure
def serialize_apps(entries: Sequence[AppInventoryEntry]) -> list[dict[str, Any]]:
    return [app_wire_json(entry) for entry in entries]


class AppInventory:
    """Holds the merged inventory and keeps it current; see the module docstring."""

    def __init__(
        self,
        registry_path: Path,
        broadcaster: WebSocketBroadcaster,
        liveness_prober: Callable[[Sequence[tuple[str, str, str]]], dict[str, bool]] = probe_all_app_liveness,
        fetcher: InstanceFetcherInterface | None = None,
        coalesce_seconds: float = NUDGE_COALESCE_SECONDS,
        sweep_interval_seconds: float = LIVENESS_SWEEP_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._registry_path = registry_path
        self._broadcaster = broadcaster
        self._liveness_prober = liveness_prober
        self._fetcher = fetcher if fetcher is not None else HttpInstanceFetcher()
        self._coalesce_seconds = coalesce_seconds
        self._sweep_interval_seconds = sweep_interval_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._entry_by_name: dict[str, AppInventoryEntry] = {}
        self._registry_order: list[str] = []
        self._pending_nudge_by_name: dict[str, threading.Timer] = {}
        self._last_broadcast_json: str | None = None
        self._observer: Any | None = None
        self._sweep_stop = threading.Event()
        self._sweep_wake = threading.Event()
        self._sweep_thread: threading.Thread | None = None
        self._removed_listeners: list[Callable[[list[Address]], None]] = []

    # ---------- lifecycle ----------

    def start(self) -> None:
        """Read the registry and probe liveness now, then watch the registry and start the sweep."""
        self.reload_registry()
        self.refresh_liveness()
        self._start_registry_watch()
        thread = threading.Thread(target=self._run_sweep, daemon=True, name="app-inventory-sweep")
        self._sweep_thread = thread
        thread.start()

    def stop(self) -> None:
        self._sweep_stop.set()
        self._sweep_wake.set()
        if self._sweep_thread is not None:
            self._sweep_thread.join(timeout=5)
            self._sweep_thread = None
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None
        with self._lock:
            timers = list(self._pending_nudge_by_name.values())
            self._pending_nudge_by_name.clear()
        for timer in timers:
            timer.cancel()

    def add_removed_listener(self, listener: Callable[[list[Address]], None]) -> None:
        """Register a callback for addresses that a running app stopped listing (the observation that prunes references)."""
        self._removed_listeners.append(listener)

    # ---------- reads ----------

    def entries(self) -> list[AppInventoryEntry]:
        with self._lock:
            return [self._entry_by_name[name] for name in self._registry_order]

    def entry(self, app_name: str) -> AppInventoryEntry | None:
        with self._lock:
            return self._entry_by_name.get(app_name)

    def serialized(self) -> list[dict[str, Any]]:
        return serialize_apps(self.entries())

    def listed_addresses(self) -> set[Address]:
        return {address for entry in self.entries() for address in entry.addresses()}

    def find_instance(self, address: Address) -> tuple[AppInventoryEntry, InventoryInstance] | None:
        entry = self.entry(str(address.app))
        if entry is None:
            return None
        for instance in entry.instances:
            if entry.address_of(instance) == address:
                return entry, instance
        return None

    def is_within_grace(self, entry: AppInventoryEntry, instance: InventoryInstance) -> bool:
        first_seen = entry.first_seen_at_by_key.get(instance.key)
        return first_seen is not None and self._clock() - first_seen < NEW_INSTANCE_GRACE_SECONDS

    # ---------- the registry ----------

    def reload_registry(self) -> None:
        """Re-read the registry, keeping each known app's liveness and list across the read."""
        rows = read_registry(self._registry_path)
        is_changed = False
        with self._lock:
            previous = dict(self._entry_by_name)
            self._entry_by_name = {}
            self._registry_order = []
            for row in rows:
                name = str(row.name)
                known = previous.get(name)
                if known is not None:
                    entry = known.model_copy_update(to_update(known.field_ref().row, row))
                    is_changed = is_changed or known.row != row
                else:
                    entry = AppInventoryEntry(row=row, is_running=True, instances=(), first_seen_at_by_key={})
                    is_changed = True
                if not row.instances:
                    entry = entry.model_copy_update(
                        to_update(entry.field_ref().instances, (synthesized_single_instance(row, entry.is_running),))
                    )
                self._entry_by_name[name] = entry
                self._registry_order.append(name)
            is_changed = is_changed or set(previous) != set(self._entry_by_name)
        if is_changed:
            self._sweep_wake.set()
            self._broadcast_if_changed()

    def _start_registry_watch(self) -> None:
        watch_dir = self._registry_path.parent
        watch_dir.mkdir(parents=True, exist_ok=True)
        observer = _Observer()
        observer.schedule(_RegistryFileHandler(self._registry_path.name, self._on_registry_changed), str(watch_dir))
        observer.daemon = True
        try:
            observer.start()
        except OSError as e:
            logger.opt(exception=e).error("Failed to watch the app registry at {}", self._registry_path)
            return
        self._observer = observer

    def _on_registry_changed(self) -> None:
        self.reload_registry()
        # A row that just appeared has no list yet; fetch it rather than wait for the sweep.
        for entry in self.entries():
            if entry.row.instances and not entry.first_seen_at_by_key and not entry.instances:
                self.refetch_now(str(entry.row.name))

    # ---------- liveness ----------

    def refresh_liveness(self) -> None:
        """Re-derive every app's ``is_running``; a change rewrites its statuses and broadcasts."""
        with self._lock:
            targets = [
                (name, self._entry_by_name[name].row.program or "", str(self._entry_by_name[name].row.url))
                for name in self._registry_order
            ]
        is_running_by_name = self._liveness_prober(targets)
        newly_running: list[str] = []
        with self._lock:
            for name, entry in self._entry_by_name.items():
                probed = is_running_by_name.get(name)
                if probed is None or probed == entry.is_running:
                    continue
                self._entry_by_name[name] = self._entry_with_liveness(entry, probed)
                if probed and entry.row.instances:
                    newly_running.append(name)
        self._broadcast_if_changed()
        for name in newly_running:
            self.refetch_now(name)

    @pure
    def _entry_with_liveness(self, entry: AppInventoryEntry, is_running: bool) -> AppInventoryEntry:
        if not entry.row.instances:
            instances: tuple[InventoryInstance, ...] = (synthesized_single_instance(entry.row, is_running),)
        elif is_running:
            instances = entry.instances
        else:
            instances = _with_status(entry.instances, InstanceStatus.STOPPED)
        return entry.model_copy_update(
            to_update(entry.field_ref().is_running, is_running),
            to_update(entry.field_ref().instances, instances),
        )

    # ---------- instance lists ----------

    def nudge(self, app_name: str) -> bool:
        """An app said its list changed: refetch once the coalescing window closes. False for an unknown app."""
        with self._lock:
            entry = self._entry_by_name.get(app_name)
            if entry is None:
                return False
            if app_name in self._pending_nudge_by_name:
                return True
            timer = threading.Timer(self._coalesce_seconds, self._on_nudge_window_closed, args=(app_name,))
            timer.daemon = True
            self._pending_nudge_by_name[app_name] = timer
        timer.start()
        return True

    def _on_nudge_window_closed(self, app_name: str) -> None:
        with self._lock:
            self._pending_nudge_by_name.pop(app_name, None)
        self.refetch_now(app_name)

    def refetch_now(self, app_name: str) -> None:
        """Fetch one app's list right away (after a relay verb, or when a nudge window closes) and broadcast a change."""
        entry = self.entry(app_name)
        if entry is None or not entry.row.instances:
            return
        if not entry.is_running:
            return
        outcome = self._fetcher.fetch(_instances_url(entry.row))
        self._fold_fetch(app_name, outcome)
        self._broadcast_if_changed()

    def refetch_all(self) -> None:
        for entry in self.entries():
            if entry.row.instances and entry.is_running:
                outcome = self._fetcher.fetch(_instances_url(entry.row))
                self._fold_fetch(str(entry.row.name), outcome)
        self._broadcast_if_changed()

    def _fold_fetch(self, app_name: str, outcome: InstanceFetchOutcome) -> None:
        removed: list[Address] = []
        with self._lock:
            entry = self._entry_by_name.get(app_name)
            if entry is None:
                return
            match outcome.kind:
                case FetchOutcomeKind.LISTED:
                    listed = tuple(inventory_instance_from_record(record) for record in outcome.records)
                    now = self._clock()
                    listed_keys = {instance.key for instance in listed}
                    first_seen = {key: entry.first_seen_at_by_key.get(key, now) for key in listed_keys}
                    removed = [
                        entry.address_of(instance) for instance in entry.instances if instance.key not in listed_keys
                    ]
                    updated = entry.model_copy_update(
                        to_update(entry.field_ref().instances, listed),
                        to_update(entry.field_ref().first_seen_at_by_key, first_seen),
                    )
                case FetchOutcomeKind.NOT_READY:
                    updated = entry
                case FetchOutcomeKind.FAILED:
                    updated = entry.model_copy_update(
                        to_update(entry.field_ref().instances, _with_status(entry.instances, InstanceStatus.ERROR))
                    )
            self._entry_by_name[app_name] = updated
        if removed:
            for listener in self._removed_listeners:
                listener(removed)

    # ---------- the sweep ----------

    def _run_sweep(self) -> None:
        sweep_count = 0
        # The first pass waits one interval: the process is still bringing its server up when
        # ``start`` returns, and the chat row's list is served by that very server.
        while not self._sweep_stop.is_set():
            self._sweep_wake.wait(timeout=self._sweep_interval_seconds)
            self._sweep_wake.clear()
            if self._sweep_stop.is_set():
                return
            self.refresh_liveness()
            if sweep_count % RECONCILE_EVERY_SWEEPS == 0:
                self.refetch_all()
            sweep_count += 1

    def wake_sweep(self) -> None:
        """Cut the current sweep interval short (a stop or start just landed)."""
        self._sweep_wake.set()

    # ---------- the broadcast ----------

    def _broadcast_if_changed(self) -> None:
        serialized = self.serialized()
        encoded = json.dumps(serialized, sort_keys=True)
        with self._lock:
            if encoded == self._last_broadcast_json:
                return
            self._last_broadcast_json = encoded
        self._broadcaster.broadcast_apps_updated(serialized)
