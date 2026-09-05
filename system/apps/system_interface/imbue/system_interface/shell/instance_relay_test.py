import json
from pathlib import Path

import httpx
from app_instances.testing import StubInstanceSource
from app_instances.testing import free_port

from imbue.system_interface.agent_manager import DESTROY_TIMEOUT_SECONDS
from imbue.system_interface.shell.instance_relay import RELAY_TIMEOUT_SECONDS
from imbue.system_interface.shell.instance_relay import relay_create
from imbue.system_interface.shell.instance_relay import relay_delete
from imbue.system_interface.shell.instance_relay import relay_location
from imbue.system_interface.shell.instance_relay import relay_rename
from imbue.system_interface.shell.testing import build_inventory
from imbue.system_interface.shell.testing import instance_record
from imbue.system_interface.shell.testing import registry_row_toml
from imbue.system_interface.shell.testing import write_registry
from imbue.system_interface.ws_broadcaster import WebSocketBroadcaster


def test_the_relay_passes_the_apps_answers_through(
    tmp_path: Path, broadcaster: WebSocketBroadcaster, stub_source: StubInstanceSource, stub_app_url: str
) -> None:
    stub_source.records.append(instance_record("stub-1", "Stub 1"))
    inventory = build_inventory(
        write_registry(
            tmp_path / "apps.toml", registry_row_toml("stub", stub_app_url, True, actions=[("new", "New")])
        ),
        broadcaster,
    )
    entry = inventory.entry("stub")
    assert entry is not None
    with httpx.Client() as client:
        created = relay_create(client, entry, json.dumps({"action": "new", "params": {}}).encode())
        assert created.status_code == 201
        assert json.loads(created.body)["instance"]["key"] == "stub-2"
        refused = relay_create(client, entry, json.dumps({"action": "nope", "params": {}}).encode())
        assert refused.status_code == 400
        renamed = relay_rename(client, entry, "stub-1", json.dumps({"title": "Renamed"}).encode())
        assert renamed.status_code == 200 and json.loads(renamed.body)["instance"]["title"] == "Renamed"
        located = relay_location(client, entry, "stub-1", json.dumps({"path": "/deeper"}).encode())
        assert located.status_code == 200 and json.loads(located.body)["instance"]["url"] == "/deeper"
        assert relay_location(client, entry, "stub-1", json.dumps({"path": "no-slash"}).encode()).status_code == 400
        assert relay_delete(client, entry, "stub-2").status_code == 204
        # A delete of an unknown key is idempotent under the contract.
        assert relay_delete(client, entry, "stub-9").status_code == 204
    assert [str(record.key) for record in stub_source.records] == ["stub-1"]


def test_the_relay_outlives_the_chats_destroy() -> None:
    """A chat's delete runs ``mngr destroy`` to completion; the relay must not give up on it first."""
    assert RELAY_TIMEOUT_SECONDS > DESTROY_TIMEOUT_SECONDS


def test_an_unreachable_app_is_a_503_with_a_detail(tmp_path: Path, broadcaster: WebSocketBroadcaster) -> None:
    inventory = build_inventory(
        write_registry(tmp_path / "apps.toml", registry_row_toml("gone", f"http://127.0.0.1:{free_port()}", True)),
        broadcaster,
    )
    entry = inventory.entry("gone")
    assert entry is not None
    with httpx.Client() as client:
        outcome = relay_delete(client, entry, "k")
    assert outcome.status_code == 503
    assert json.loads(outcome.body) == {"detail": "The app gone is unreachable"}
