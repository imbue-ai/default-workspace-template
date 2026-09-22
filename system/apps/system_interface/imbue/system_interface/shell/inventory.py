"""The app inventory: the registry and each app's liveness.

The registry (``data/.state/apps.toml``) is watched for changes; liveness is re-derived on a
sweep and after a stop or start. Every change of the inventory is broadcast as one
``apps_updated`` message, diffed against the last one sent (desktop contracts.md section 6).
"""

import json
import threading
from collections.abc import Callable
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from typing import Final

from app_manifest.errors import RegistryReadError
from app_manifest.registry import read_registry
from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr
from watchdog.observers.api import BaseObserver

from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.system_interface.file_watch import start_file_watch
from imbue.system_interface.file_watch import stop_file_watch
from imbue.system_interface.shell.data_types import AppInventoryEntry
from imbue.system_interface.shell.data_types import app_wire_json
from imbue.system_interface.shell.liveness import probe_all_app_liveness
from imbue.system_interface.ws_broadcaster import WebSocketBroadcaster

# How often liveness is re-derived.
LIVENESS_SWEEP_INTERVAL_SECONDS: Final[float] = 10.0


@pure
def serialize_apps(entries: Sequence[AppInventoryEntry]) -> list[dict[str, Any]]:
    return [app_wire_json(entry) for entry in entries]


class AppInventory(MutableModel):
    """Holds the inventory and keeps it current; see the module docstring."""

    model_config = {"arbitrary_types_allowed": True, "extra": "forbid", "frozen": False}

    registry_path: Path = Field(frozen=True, description="The apps.toml to read and watch")
    broadcaster: WebSocketBroadcaster = Field(frozen=True, description="Where ``apps_updated`` goes")
    liveness_prober: Callable[[Sequence[tuple[str, str, str]]], dict[str, bool]] = Field(
        default=probe_all_app_liveness, frozen=True, description="Derives is_running for every (name, program, url)"
    )
    sweep_interval_seconds: float = Field(
        default=LIVENESS_SWEEP_INTERVAL_SECONDS, frozen=True, description="How often the sweep runs"
    )

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    # Held across serialize, compare, and broadcast, so two threads that snapshot the inventory
    # in one order cannot broadcast in the other and leave the clients on the older one.
    _broadcast_lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _entry_by_name: dict[str, AppInventoryEntry] = PrivateAttr(default_factory=dict)
    _registry_order: list[str] = PrivateAttr(default_factory=list)
    _last_broadcast_json: str | None = PrivateAttr(default=None)
    _is_registry_read: bool = PrivateAttr(default=False)
    _observer: BaseObserver | None = PrivateAttr(default=None)
    _sweep_stop: threading.Event = PrivateAttr(default_factory=threading.Event)
    _sweep_wake: threading.Event = PrivateAttr(default_factory=threading.Event)
    _sweep_thread: threading.Thread | None = PrivateAttr(default=None)

    # Lifecycle

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
            stop_file_watch(self._observer)
            self._observer = None

    # Reads

    def entries(self) -> list[AppInventoryEntry]:
        with self._lock:
            return [self._entry_by_name[name] for name in self._registry_order]

    def entry(self, app_name: str) -> AppInventoryEntry | None:
        with self._lock:
            return self._entry_by_name.get(app_name)

    def serialized(self) -> list[dict[str, Any]]:
        return serialize_apps(self.entries())

    @property
    def is_registry_read(self) -> bool:
        """Whether the registry has been read once, so an inventory of no apps means no apps rather than not yet."""
        with self._lock:
            return self._is_registry_read

    # The registry

    def reload_registry(self) -> None:
        """Re-read the registry, keeping each known app's liveness across the read.

        A file that cannot be read or parsed keeps the last good read (logged): a hand-edited
        registry must degrade to a stale inventory, not crash the shell or end the watch.
        """
        try:
            rows = read_registry(self.registry_path)
        except RegistryReadError as e:
            logger.opt(exception=e).error("Kept the last app registry read: {} is unreadable", self.registry_path)
            return
        is_changed = False
        with self._lock:
            self._is_registry_read = True
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
                    entry = AppInventoryEntry(row=row, is_running=True)
                    is_changed = True
                self._entry_by_name[name] = entry
                self._registry_order.append(name)
            is_changed = is_changed or set(previous) != set(self._entry_by_name)
        if is_changed:
            self._sweep_wake.set()
            self._broadcast_if_changed()

    def _start_registry_watch(self) -> None:
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        self._observer = start_file_watch(self.registry_path, self.reload_registry)

    # Liveness

    def refresh_liveness(self) -> None:
        """Re-derive every app's ``is_running``; a change broadcasts."""
        with self._lock:
            targets = [
                (name, self._entry_by_name[name].row.program or "", str(self._entry_by_name[name].row.url))
                for name in self._registry_order
            ]
        is_running_by_name = self.liveness_prober(targets)
        with self._lock:
            for name, entry in self._entry_by_name.items():
                probed = is_running_by_name.get(name)
                if probed is None or probed == entry.is_running:
                    continue
                self._entry_by_name[name] = entry.model_copy_update(to_update(entry.field_ref().is_running, probed))
        self._broadcast_if_changed()

    # The sweep

    def _run_sweep(self) -> None:
        # The first pass runs as soon as the registry read in ``start`` woke it.
        while not self._sweep_stop.is_set():
            self._sweep_wake.wait(timeout=self.sweep_interval_seconds)
            self._sweep_wake.clear()
            if self._sweep_stop.is_set():
                return
            self.sweep_once()

    def sweep_once(self) -> None:
        """One pass of the sweep: re-derive liveness.

        A pass that raises (a registry url the probe cannot parse) is logged and the next pass
        runs: the sweep is what keeps every status current, so it must outlive one bad pass.
        """
        try:
            self.refresh_liveness()
        except (OSError, ValueError) as e:
            logger.opt(exception=e).error("The app inventory sweep failed; the next pass will retry")

    # The broadcast

    def _broadcast_if_changed(self) -> None:
        with self._broadcast_lock:
            serialized = self.serialized()
            encoded = json.dumps(serialized, sort_keys=True)
            if encoded == self._last_broadcast_json:
                return
            self._last_broadcast_json = encoded
            self.broadcaster.broadcast_apps_updated(serialized)
