"""The shell and the chat app running together, as a workspace runs them.

The chat is an ordinary app to the shell: registered at its own URL, listed in the inventory with
its launch paths and liveness, opened as windows of its pages. These tests serve both apps side by
side (the chat package's ``running_workspace``, in-process on two ports) and read the shell's
inventory and op route to check the seam between them; this is also the one module that may
import both packages, so the invariants that span them are pinned here.
"""

from __future__ import annotations

import json
import tomllib
import urllib.request
from pathlib import Path
from typing import Any

import pytest
from mngr_cli_contract.contract import assert_mngr_argv_valid

from imbue.chat.agent_manager import _build_chat_create_command
from imbue.chat.documents import FRONTEND_BUILT_HEADER
from imbue.chat.harnesses.harness_type import HarnessType
from imbue.chat.primitives import ChatId
from imbue.chat.testing import FIXTURE_AGENT_ID
from imbue.chat.testing import free_port
from imbue.chat.testing import running_workspace
from imbue.mngr.utils.polling import wait_for
from imbue.system_interface.server import _NOT_BUILT_REPAIR_ARGV
from imbue.system_interface.update_staleness import WORKSPACE_ROOT_DIRECTORY

# The default desktop every shell starts with (``shell/desktops.py``).
_HOME_DESKTOP_ID = "home"


def _inventory(shell_url: str) -> dict[str, Any]:
    with urllib.request.urlopen(f"{shell_url}/api/inventory", timeout=5) as response:
        return json.loads(response.read())


def _post_json(url: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.status, json.loads(response.read())


def _chat_app(shell_url: str) -> dict[str, Any] | None:
    return next((app for app in _inventory(shell_url)["apps"] if app["name"] == "chat"), None)


def _wait_for_the_chat_app(shell_url: str) -> dict[str, Any]:
    wait_for(
        lambda: _chat_app(shell_url) is not None and _chat_app(shell_url)["is_running"] is True,
        timeout=15.0,
        poll_interval=0.2,
        error_message="the shell never listed the chat app as running",
    )
    listed = _chat_app(shell_url)
    assert listed is not None
    return listed


@pytest.mark.timeout(60, func_only=False)
def test_the_shells_inventory_lists_the_chat_app_with_its_launch_path(tmp_path: Path) -> None:
    """The chat reaches the shell's inventory as an app: running, with the ``new`` launch path of its manifest and
    its default shortcut, and nothing about the chats inside it."""
    with running_workspace(tmp_path, free_port(), free_port()) as workspace:
        listed = _wait_for_the_chat_app(workspace.shell_url)
        assert listed["display_name"] == "Chat"
        assert listed["critical"] is True
        assert [(launch["id"], launch["path"]) for launch in listed["launch_paths"]] == [("new", "/new")]
        assert listed["default_shortcut"]["launch"] == "new"
        assert listed["default_shortcut"]["mode"] == "new"


@pytest.mark.timeout(60, func_only=False)
def test_an_agents_open_of_a_chat_page_lands_a_window_the_chat_serves(tmp_path: Path) -> None:
    """The desktop ``open`` op with the chat's name and a chat page's path puts a window on the desktop for the
    target client, and the path it names is one the chat app answers with the chat document."""
    with running_workspace(tmp_path, free_port(), free_port()) as workspace:
        _wait_for_the_chat_app(workspace.shell_url)
        # A client the shell knows, registered through the socket's own bookkeeping.
        client_queue = workspace.shell_state.shell.broadcaster.register()
        workspace.shell_state.shell.broadcaster.set_client_info(client_queue, "client-1", _HOME_DESKTOP_ID)
        try:
            status, answer = _post_json(
                f"{workspace.shell_url}/api/layout/broadcast",
                {
                    "op": "open",
                    "args": {"app": "chat", "path": f"/{FIXTURE_AGENT_ID}", "client": "client-1"},
                    "requester": None,
                },
            )
        finally:
            workspace.shell_state.shell.broadcaster.unregister(client_queue)
        assert status == 200
        (window,) = answer["desktop"]["windows"]
        assert window["app"] == "chat" and window["path"] == f"/{FIXTURE_AGENT_ID}"
        assert [placement["window_id"] for placement in answer["layout"]["placements"]] == [window["id"]]
        # The chat serves the page at that path (the document, or its not-built placeholder in a checkout with no
        # bundle; both carry the header), so the window's frame lands on the chat's own origin.
        with urllib.request.urlopen(f"{workspace.chat_url}{window['path']}", timeout=5) as response:
            assert response.status == 200
            assert response.headers[FRONTEND_BUILT_HEADER] in ("true", "false")


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
