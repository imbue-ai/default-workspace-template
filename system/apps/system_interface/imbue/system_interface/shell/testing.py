"""Test helpers for the shell subpackage: registry rows and files, a fake instance fetcher, and an inventory over them."""

import json
import queue
import time
from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import Final

from app_instances.data_types import InstanceLifetime
from app_instances.data_types import InstanceRecord
from app_instances.data_types import InstanceStatus
from app_instances.primitives import InstanceKey
from app_instances.primitives import InstanceTitle
from app_instances.primitives import InstanceUrl
from flask import Flask
from pydantic import Field

from imbue.system_interface.server import create_application
from imbue.system_interface.shell.data_types import LayoutRecord
from imbue.system_interface.shell.data_types import instance_panel_params_by_id
from imbue.system_interface.shell.data_types import instance_panel_params_json
from imbue.system_interface.shell.inventory import AppInventory
from imbue.system_interface.shell.inventory import FetchOutcomeKind
from imbue.system_interface.shell.inventory import InstanceFetchOutcome
from imbue.system_interface.shell.inventory import InstanceFetcherInterface
from imbue.system_interface.shell.primitives import Address
from imbue.system_interface.shell.primitives import DeviceKind
from imbue.system_interface.shell.primitives import TabId
from imbue.system_interface.shell.update_notice import LAST_GOOD_RECORD_REL
from imbue.system_interface.shell.update_notice import UPDATE_SELF_SCRIPT_REL
from imbue.system_interface.testing import build_test_state
from imbue.system_interface.ws_broadcaster import WebSocketBroadcaster

# The one clock the shell tests stamp records with.
TEST_NOW: Final[datetime] = datetime(2026, 9, 4, tzinfo=timezone.utc)
# The instances URL of the supervised multi-instance app of ``write_two_app_registry``.
TEST_TERMINAL_URL: Final[str] = "http://localhost:7681"
# The URL of the single-instance app of ``write_two_app_registry``.
TEST_FILES_URL: Final[str] = "http://localhost:7000"


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
    action_params: Mapping[str, Sequence[str]] | None = None,
    launcher_rank: int | None = None,
) -> str:
    """One ``[[apps]]`` row as ``forward_port.py`` writes it, with the manifest-derived keys the shell reads.
    ``action_params`` names each action's params by action id."""
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
    if launcher_rank is not None:
        lines.append(f"launcher_rank = {launcher_rank}")
    if default_shortcut is not None:
        lines.append(f'default_shortcut = {{ action = "{default_shortcut[0]}", mode = "{default_shortcut[1]}" }}')
    for action_id, label_text in actions:
        lines.append("[[apps.actions]]")
        lines.append(f'id = "{action_id}"')
        lines.append(f'label = "{label_text}"')
        params = (action_params or {}).get(action_id, ())
        if params:
            lines.append("params = [" + ", ".join(f'"{param}"' for param in params) + "]")
    return "\n".join(lines) + "\n"


def write_registry(path: Path, *rows: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(rows))
    return path


def write_two_app_registry(tmp_path: Path, *extra_rows: str) -> Path:
    """``apps.toml`` under ``tmp_path`` with a supervised multi-instance ``terminal`` row, a single-instance ``files`` row, and ``extra_rows``."""
    return write_registry(
        tmp_path / "apps.toml",
        registry_row_toml(
            "terminal",
            TEST_TERMINAL_URL,
            True,
            program="terminal",
            actions=[("new", "New terminal")],
            default_shortcut=("new", "new"),
        ),
        registry_row_toml("files", TEST_FILES_URL, program="files", default_shortcut=("open", "focus")),
        *extra_rows,
    )


def shell_application(
    tmp_path: Path, inventory: AppInventory, broadcaster: WebSocketBroadcaster, is_preview: bool = False
) -> Flask:
    """The shell app over ``inventory``, its state under ``tmp_path/state`` and the update notice's workspace at
    ``tmp_path/repo``, sharing the inventory's broadcaster as in production."""
    state = build_test_state(
        broadcaster=broadcaster,
        shell_state_directory=tmp_path / "state",
        inventory=inventory,
        is_preview=is_preview,
        repo_root=tmp_path / "repo",
    )
    return create_application(state)


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
    clock: Callable[[], float] | None = None,
) -> AppInventory:
    """An inventory that has read the registry and probed liveness once, with no watcher or sweep running."""
    inventory = AppInventory(
        registry_path=registry_path,
        broadcaster=broadcaster,
        liveness_prober=prober if prober is not None else FakeLivenessProber(),
        fetcher=fetcher if fetcher is not None else FakeInstanceFetcher(),
        coalesce_seconds=0.01,
        clock=clock if clock is not None else time.monotonic,
    )
    inventory.reload_registry()
    inventory.refresh_liveness()
    return inventory


def drain_messages(client_queue: "queue.Queue[str | None]") -> list[dict[str, Any]]:
    """Every message a registered fake client has been sent so far, parsed."""
    messages: list[dict[str, Any]] = []
    while not client_queue.empty():
        raw = client_queue.get_nowait()
        if raw is not None:
            messages.append(json.loads(raw))
    return messages


def addresses_by_panel_id(dockview: dict[str, Any] | None) -> dict[str, Address]:
    """Each instance panel's address, keyed by dockview panel id: how the layout assertions read a document."""
    return {panel_id: params.address for panel_id, params in instance_panel_params_by_id(dockview).items()}


def layout_showing(*addresses: Address) -> LayoutRecord:
    """A desktop arrangement with one panel per address (``p0``, ``p1``, ...), each panel's params carrying a fixed tab id."""
    panels = {
        f"p{index}": {"id": f"p{index}", "params": instance_panel_params_json(address, TabId(f"tab-{index:016x}"), 0)}
        for index, address in enumerate(addresses)
    }
    return LayoutRecord(
        dockview={
            "grid": {
                "root": {
                    "type": "branch",
                    "data": [
                        {"type": "leaf", "data": {"views": list(panels), "activeView": "p0", "id": "g0"}, "size": 1200}
                    ],
                    "size": 800,
                },
                "width": 1200,
                "height": 800,
                "orientation": "HORIZONTAL",
            },
            "panels": panels,
            "activeGroup": "g0",
        }
        if addresses
        else None,
        device_kind=DeviceKind.DESKTOP,
        updated_at=None,
    )


# ---------- the update notice ----------

# Where the stub update-self script records each call it took.
STUB_UPDATE_SELF_CALLS_REL: Final[str] = "data/.state/update-apply/stub-calls.jsonl"

# A stand-in for ``update_self.py`` that records its argv, working directory, and session id, closes
# the record on ``confirm-last`` the way the real script does, and exits as told. Written under the
# test's workspace root so the shell finds it where it finds the real one.
_STUB_UPDATE_SELF_SCRIPT = """\
import json
import os
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[4]
record = root / "data/.state/update-apply/last-good.json"
with (root / "{calls_rel}").open("a") as calls:
    calls.write(json.dumps({{"argv": sys.argv[1:], "cwd": os.getcwd(), "sid": os.getsid(0)}}) + "\\n")
if sys.argv[1:] == ["confirm-last"] and {exit_code} == 0:
    record.unlink(missing_ok=True)
if {exit_code} != 0:
    sys.exit("the stub was told to fail")
sys.exit(0)
"""


def write_rollback_point(
    repo_root: Path,
    *,
    apps: Sequence[str] = ("terminal",),
    programs: Sequence[str] | None = None,
    needs_services_restart: bool = False,
    progress: str | None = None,
    outcome: str | None = None,
) -> Path:
    """The record an apply run with ``--keep-rollback-point`` leaves, in the apply's own shape (its extra
    fields included), under ``repo_root``."""
    path = repo_root / LAST_GOOD_RECORD_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "merge_sha": "abc1234abc1234abc1234abc1234abc1234abc12",
                "rollback_to": "def5678def5678def5678def5678def5678def56",
                "applied_at": 1_780_000_000.0,
                "driven_by": "mngr/update-widgets",
                "snapshots": [
                    {"name": "bundle", "source": "system/x", "copy": "data/.state/update-apply/snapshots/bundle"}
                ],
                "programs": list(programs) if programs is not None else list(apps),
                "apps": list(apps),
                "needs_services_restart": needs_services_restart,
                "progress": progress,
                "outcome": outcome,
            }
        )
    )
    return path


def write_stub_update_self_script(repo_root: Path, exit_code: int = 0) -> Path:
    path = repo_root / UPDATE_SELF_SCRIPT_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_STUB_UPDATE_SELF_SCRIPT.format(calls_rel=STUB_UPDATE_SELF_CALLS_REL, exit_code=exit_code))
    return path


def read_stub_update_self_calls(repo_root: Path) -> list[dict[str, Any]]:
    path = repo_root / STUB_UPDATE_SELF_CALLS_REL
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]
