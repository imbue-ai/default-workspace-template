"""Test helpers for the shell subpackage: registry rows and files, a fake instance fetcher, and an inventory over them."""

import json
import queue
from collections.abc import Callable
from collections.abc import Sequence
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

from app_instances.data_types import InstanceLifetime
from app_instances.data_types import InstanceRecord
from app_instances.data_types import InstanceStatus
from app_instances.primitives import InstanceKey
from app_instances.primitives import InstanceTitle
from app_instances.primitives import InstanceUrl
from app_manifest.registry import RegistryRow
from pydantic import Field

from imbue.system_interface.shell.inventory import AppInventory
from imbue.system_interface.shell.inventory import FetchOutcomeKind
from imbue.system_interface.shell.inventory import InstanceFetchOutcome
from imbue.system_interface.shell.inventory import InstanceFetcherInterface
from imbue.system_interface.ws_broadcaster import WebSocketBroadcaster


def registry_row_toml(
    name: str,
    url: str,
    is_multi_instance: bool = False,
    program: str | None = None,
    is_critical: bool = False,
    is_internal: bool = False,
    actions: Sequence[tuple[str, str]] = (),
    default_shortcut: tuple[str, str] | None = None,
    display_name: str | None = None,
    label: str = "",
) -> str:
    """One ``[[apps]]`` row as ``forward_port.py`` writes it, with the manifest-derived keys the shell reads."""
    lines = [
        "[[apps]]",
        f'name = "{name}"',
        f'url = "{url}"',
        f'label = "{label}"',
        f'display_name = "{display_name if display_name is not None else name.capitalize()}"',
        f"instances = {'true' if is_multi_instance else 'false'}",
        f"critical = {'true' if is_critical else 'false'}",
        f"internal = {'true' if is_internal else 'false'}",
    ]
    if program is not None:
        lines.append(f'program = "{program}"')
    if default_shortcut is not None:
        lines.append(f'default_shortcut = {{ action = "{default_shortcut[0]}", mode = "{default_shortcut[1]}" }}')
    for action_id, label_text in actions:
        lines.append("[[apps.actions]]")
        lines.append(f'id = "{action_id}"')
        lines.append(f'label = "{label_text}"')
    return "\n".join(lines) + "\n"


def write_registry(path: Path, *rows: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(rows))
    return path


def instance_record(
    key: str,
    title: str | None = None,
    status: InstanceStatus = InstanceStatus.IDLE,
    lifetime: InstanceLifetime = InstanceLifetime.EXPLICIT,
    url: str = "/",
) -> InstanceRecord:
    return InstanceRecord(
        key=InstanceKey(key),
        url=InstanceUrl(url),
        title=InstanceTitle(title if title is not None else key),
        status=status,
        lifetime=lifetime,
        last_active=datetime(2026, 9, 4, tzinfo=timezone.utc),
        renameable=True,
    )


class FakeInstanceFetcher(InstanceFetcherInterface):
    """Answers each instances URL from a table and records every fetch."""

    outcome_by_url: dict[str, InstanceFetchOutcome] = Field(default_factory=dict, description="What each URL answers")
    fetched_urls: list[str] = Field(default_factory=list, description="Every URL fetched, in order")

    def fetch(self, instances_url: str) -> InstanceFetchOutcome:
        self.fetched_urls.append(instances_url)
        outcome = self.outcome_by_url.get(instances_url)
        if outcome is None:
            return InstanceFetchOutcome(kind=FetchOutcomeKind.FAILED, records=())
        return outcome

    def list(self, instances_url: str, *records: InstanceRecord) -> None:
        self.outcome_by_url[instances_url] = InstanceFetchOutcome(kind=FetchOutcomeKind.LISTED, records=records)

    def not_ready(self, instances_url: str) -> None:
        self.outcome_by_url[instances_url] = InstanceFetchOutcome(kind=FetchOutcomeKind.NOT_READY, records=())

    def fail(self, instances_url: str) -> None:
        self.outcome_by_url[instances_url] = InstanceFetchOutcome(kind=FetchOutcomeKind.FAILED, records=())


class FakeLivenessProber:
    """Answers the liveness sweep from a table; every app not in it counts as running."""

    def __init__(self) -> None:
        self.is_running_by_name: dict[str, bool] = {}
        self.call_count = 0

    def __call__(self, rows: Sequence[tuple[str, str, str]]) -> dict[str, bool]:
        self.call_count += 1
        return {name: self.is_running_by_name.get(name, True) for name, _program, _url in rows}


def build_inventory(
    registry_path: Path,
    broadcaster: WebSocketBroadcaster,
    fetcher: InstanceFetcherInterface | None = None,
    prober: Callable[[Sequence[tuple[str, str, str]]], dict[str, bool]] | None = None,
    clock: Any = None,
) -> AppInventory:
    """An inventory that has read the registry and probed liveness once, with no watcher or sweep running."""
    inventory = AppInventory(
        registry_path,
        broadcaster,
        liveness_prober=prober if prober is not None else FakeLivenessProber(),
        fetcher=fetcher if fetcher is not None else FakeInstanceFetcher(),
        coalesce_seconds=0.01,
        **({"clock": clock} if clock is not None else {}),
    )
    inventory.reload_registry()
    inventory.refresh_liveness()
    return inventory


def drain_messages(client_queue: "queue.Queue[str | None]") -> list[dict[str, Any]]:
    """Every message a registered fake client has been sent so far, parsed."""
    messages: list[dict[str, Any]] = []
    while True:
        try:
            raw = client_queue.get_nowait()
        except queue.Empty:
            return messages
        if raw is not None:
            messages.append(json.loads(raw))


def registry_rows_of(inventory: AppInventory) -> list[RegistryRow]:
    return [entry.row for entry in inventory.entries()]
