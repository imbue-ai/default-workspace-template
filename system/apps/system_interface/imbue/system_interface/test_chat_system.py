"""The shell and the chat app running together, as a workspace runs them.

The chat is an ordinary app to the shell: registered at its own URL, listed through its
instances API, verbed through the relay. These tests serve both apps side by side (the chat
package's ``running_workspace``, in-process on two ports) and read the shell's inventory to
check the seam between them; this is also the one module that may import both packages, so
the invariants that span them are pinned here.
"""

from __future__ import annotations

import json
import tomllib
import urllib.request
from pathlib import Path
from typing import Any

import pytest
from app_instances.testing import free_port
from mngr_cli_contract.contract import assert_mngr_argv_valid

from imbue.chat.agent_manager import DESTROY_TIMEOUT_SECONDS
from imbue.chat.agent_manager import _build_chat_create_command
from imbue.chat.harnesses.harness_type import HarnessType
from imbue.chat.primitives import ChatId
from imbue.chat.testing import FIXTURE_AGENT_ID
from imbue.chat.testing import FIXTURE_AGENT_NAME
from imbue.chat.testing import FIXTURE_CHAT_ADDRESS
from imbue.chat.testing import running_workspace
from imbue.mngr.utils.polling import wait_for
from imbue.system_interface.server import _NOT_BUILT_REPAIR_ARGV
from imbue.system_interface.shell.instance_relay import RELAY_TIMEOUT_SECONDS
from imbue.system_interface.update_staleness import WORKSPACE_ROOT_DIRECTORY


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
            lambda: (
                [
                    instance["title"]
                    for instance in _chat_instances(workspace.shell_url)
                    if instance["key"] == FIXTURE_AGENT_ID
                ]
                == ["Design notes"]
            ),
            timeout=15.0,
            poll_interval=0.2,
            error_message="the shell's inventory never picked up the rename",
        )


def test_the_relay_outlives_the_chats_destroy() -> None:
    """A chat delete runs ``mngr destroy`` for up to its own timeout inside the relayed request, so the
    shell's relay must wait at least that long before giving up on the app."""
    assert RELAY_TIMEOUT_SECONDS > DESTROY_TIMEOUT_SECONDS


def _chat_create_template() -> dict[str, object]:
    """The workspace's own ``[create_templates.chat]`` block, read from its settings.

    Parsed straight out of the TOML rather than through mngr's config loader: the
    question is what this repo ships, not what a particular machine resolves, and
    the loader would fold in user and local layers that a workspace being repaired
    may not have. ``server.py`` resolves the workspace root the same way.
    """
    settings = tomllib.loads((WORKSPACE_ROOT_DIRECTORY / ".mngr" / "settings.toml").read_text())
    return settings["create_templates"]["chat"]


def test_not_built_repair_command_is_the_one_the_app_runs_for_a_chat() -> None:
    """The suggested agent has to come up as a chat, or the suggestion misleads.

    The page tells a reader to create an agent to repair the workspace, and an
    agent created with the wrong flags is a different thing: a worktree of the
    tree instead of the tree itself, in the wrong memory band, without the chat
    role. So every flag the page suggests must be one the app itself passes
    when it creates a chat, and the command must be one the live CLI accepts.
    """
    argv = list(_NOT_BUILT_REPAIR_ARGV)
    assert_mngr_argv_valid(argv)

    real = _build_chat_create_command(
        mngr_binary="mngr",
        name="repair",
        chat_id=ChatId("agent-123"),
        agent_id="agent-123",
        primary_labels={},
        harness=HarnessType.CLAUDE,
    )
    assert argv[argv.index("--template") + 1] == real[real.index("--template") + 1]
    assert "user_created=true" in real

    # ``--no-connect`` is the one flag deliberately inverted: it exists to stop a
    # headless caller attaching, and a reader typing this wants to land in the
    # conversation.
    assert "--no-connect" in real
    assert "--connect" in argv
    assert "--no-connect" not in argv

    # ``--type`` is the one the builder must pass and the page must not: the app
    # is serving a harness the user picked from a menu, while the page has no
    # such choice to carry and would be pinning every reader to whichever harness
    # was current when this string was written. Omitted, mngr resolves it from
    # ``[commands.create] type``, so the repair agent comes up on whatever this
    # workspace opens chats as.
    assert "--type" in real
    assert "--type" not in argv

    # ``--transfer`` is left out for a different reason, and a weaker one: the
    # ``chat`` template already sets it, so the line does not have to. Unlike the
    # harness this is not the reader's to choose -- an agent in a worktree would
    # repair a copy of the workspace instead of the workspace -- so the template
    # is read rather than assumed. Losing that setting has to fail here and not
    # in a workspace that has already lost its interface.
    assert "--transfer" in real
    assert "--transfer" not in argv
    assert _chat_create_template()["transfer"] == "none"

    # No agent name, so mngr mints one and nothing collides with an earlier run.
    # The whole line has to stay flags-only for that: ``mngr create`` reads bare
    # words as positionals (the name, then the agent type), so one anywhere past
    # the subcommand -- not just directly after it -- puts the collision back.
    # ``assert_mngr_argv_valid`` does not catch that: it checks option shape and
    # throws the positionals away. A value-taking flag added to the line without
    # being named here reports its value as a positional, which fails in the
    # direction that gets looked at.
    assert argv[:2] == ["mngr", "create"]
    flags_taking_a_value = {"--template", "--transfer", "--label", "--message"}
    positionals = [
        token
        for index, token in enumerate(argv[2:], start=2)
        if not token.startswith("--") and argv[index - 1] not in flags_taking_a_value
    ]
    assert positionals == [], f"the suggested line passes positional arguments: {positionals}"

    # The message is what makes the created agent useful without the reader
    # having to describe anything, so it has to survive the shell as one word of
    # plain prose -- an escape dropped from the line above splits it into several
    # words, or leaves the escapes themselves in what the agent is told.
    assert argv[argv.index("--message") + 1] == (
        "i'm seeing \"this workspace's interface needs to be rebuilt, can you fix it?\""
    )
