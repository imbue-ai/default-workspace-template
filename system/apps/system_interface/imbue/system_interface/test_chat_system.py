"""The shell and the chat app running together, as a workspace runs them.

The chat is an ordinary app to the shell: registered at its own URL, listed through its
instances API, verbed through the relay. These tests serve both apps side by side (the chat
package's ``running_workspace``, in-process on two ports) and read the shell's inventory to
check the seam between them; this is also the one module that may import both packages, so
the invariants that span them are pinned here.
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path
from typing import Any

import pytest
from app_instances.testing import free_port

from imbue.chat.agent_manager import DESTROY_TIMEOUT_SECONDS
from imbue.chat.testing import FIXTURE_AGENT_ID
from imbue.chat.testing import FIXTURE_AGENT_NAME
from imbue.chat.testing import FIXTURE_CHAT_ADDRESS
from imbue.chat.testing import running_workspace
from imbue.mngr.utils.polling import wait_for
from imbue.system_interface.shell.instance_relay import RELAY_TIMEOUT_SECONDS


def _inventory(shell_url: str) -> dict[str, Any]:
    with urllib.request.urlopen(f"{shell_url}/api/inventory", timeout=5) as response:
        return json.loads(response.read())


def _chat_instances(shell_url: str) -> list[dict[str, Any]]:
    apps = {app["name"]: app for app in _inventory(shell_url)["apps"]}
    return list(apps["chat"]["instances"]) if "chat" in apps and apps["chat"]["is_listed"] else []


def _post_json(url: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.status, json.loads(response.read())


def _wait_for_fixture_chat(shell_url: str) -> None:
    wait_for(
        lambda: any(instance["key"] == FIXTURE_AGENT_ID for instance in _chat_instances(shell_url)),
        timeout=15.0,
        poll_interval=0.2,
        error_message="the shell never listed the fixture chat",
    )


@pytest.mark.timeout(60, func_only=False)
def test_the_shells_inventory_lists_the_chats_instances_with_status(tmp_path: Path) -> None:
    """The chat's agents reach the shell's inventory as instances, with the status the chat reports."""
    with running_workspace(tmp_path, free_port(), free_port(), project_names=()) as workspace:
        _wait_for_fixture_chat(workspace.shell_url)
        (instance,) = [
            instance for instance in _chat_instances(workspace.shell_url) if instance["key"] == FIXTURE_AGENT_ID
        ]
        assert instance["title"] == FIXTURE_AGENT_NAME
        assert instance["status"] == "idle"
        assert instance["lifetime"] == "explicit"
        assert instance["renameable"] is True
        assert instance["url"] == f"/{FIXTURE_AGENT_ID}"
        assert FIXTURE_CHAT_ADDRESS in _inventory(workspace.shell_url)["everything"]["tabs"]


@pytest.mark.timeout(60, func_only=False)
def test_a_rename_through_the_shells_relay_reaches_the_chat_and_relists(tmp_path: Path) -> None:
    """The shell's relay verbs land on the chat's own server, and the shell's inventory follows."""
    with running_workspace(tmp_path, free_port(), free_port(), project_names=()) as workspace:
        _wait_for_fixture_chat(workspace.shell_url)
        status, body = _post_json(
            f"{workspace.shell_url}/api/apps/chat/instances/{FIXTURE_AGENT_ID}/rename", {"title": "Design notes"}
        )
        assert status == 200
        assert body["instance"]["title"] == "Design notes"
        wait_for(
            lambda: [
                instance["title"]
                for instance in _chat_instances(workspace.shell_url)
                if instance["key"] == FIXTURE_AGENT_ID
            ]
            == ["Design notes"],
            timeout=15.0,
            poll_interval=0.2,
            error_message="the shell's inventory never picked up the rename",
        )


def test_the_relay_outlives_the_chats_destroy() -> None:
    """A chat delete runs ``mngr destroy`` for up to its own timeout inside the relayed request, so the
    shell's relay must wait at least that long before giving up on the app."""
    assert RELAY_TIMEOUT_SECONDS > DESTROY_TIMEOUT_SECONDS
