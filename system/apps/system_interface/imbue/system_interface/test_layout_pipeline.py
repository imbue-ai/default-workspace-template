"""Acceptance test for the agent-driven layout pipeline over addresses.

Exercises the full backend path the agent-facing helper depends on:
``system/scripts/layout.py`` (subprocess) -> ``POST /api/layout/broadcast``
(loopback Flask route) -> ``WebSocketBroadcaster.broadcast_layout_op``, and the
relay verbs (``rename``, ``delete``) -> ``POST /api/apps/<app>/instances/...``
-> the app's own instances API. The WS-to-DOM step is ``test_e2e.py``'s.

The machine the script sees is a registry with two rows: the chat app at the
shell's own URL (so the seeded agent lists as ``app:chat?instance=<id>``) and a
stub app served by ``app_instances``' in-memory source over loopback (so the
relay has a real instances API to reach). Broadcaster output is observed via
the broadcaster's own queue-registration API rather than a live WebSocket.
"""

from __future__ import annotations

import json
import os
import queue as queue_module
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from typing import Generator

import pytest
from app_instances.blueprint import build_instances_app
from app_instances.sidecar import serve_in_background
from app_instances.testing import LOOPBACK_HOST
from app_instances.testing import RecordingNudger
from app_instances.testing import StubInstanceSource
from app_instances.testing import free_port
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.utils.polling import wait_for
from imbue.system_interface.agent_manager import AgentManager
from imbue.system_interface.config import Config
from imbue.system_interface.models import AgentStateItem
from imbue.system_interface.server import create_application
from imbue.system_interface.shell.testing import instance_record
from imbue.system_interface.shell.testing import registry_row_toml
from imbue.system_interface.shell.testing import write_registry
from imbue.system_interface.testing import build_test_state
from imbue.system_interface.ws_broadcaster import WebSocketBroadcaster
from imbue.system_interface.wsgi import make_threaded_server

pytestmark = pytest.mark.acceptance

_PORT = 18766
_BASE_URL = f"http://127.0.0.1:{_PORT}"
_AGENT_ID = "agent-test-alice"
_AGENT_NAME = "alice"
_CHAT_ADDRESS = f"app:chat?instance={_AGENT_ID}"
_STUB_APP_NAME = "docs"

_REPO_ROOT = Path(__file__).resolve().parents[5]
_LAYOUT_SCRIPT = _REPO_ROOT / "system" / "scripts" / "layout.py"


def _server_is_up(url: str) -> bool:
    """Any HTTP answer from ``/api/projects`` means the app is serving."""
    try:
        urllib.request.urlopen(f"{url}/api/projects", timeout=0.5)
        return True
    except urllib.error.HTTPError:
        return True
    except OSError:
        return False


class PipelineHarness(FrozenModel):
    """What one test gets: the shell's URL, its broadcaster, the registry file, and the stub app's source."""

    model_config = {"arbitrary_types_allowed": True}

    base_url: str = Field(description="The shell's loopback URL")
    broadcaster: WebSocketBroadcaster = Field(description="The shell's broadcaster, for fake clients")
    registry_path: Path = Field(description="The registry file the shell and the script read")
    stub_source: StubInstanceSource = Field(description="The stub app's in-memory instances")


@pytest.fixture
def layout_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[PipelineHarness, None, None]:
    """A workspace server over a registry of two apps, with one seeded agent and a started shell."""
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path / "host"))
    monkeypatch.setenv("MNGR_AGENT_ID", "")
    monkeypatch.setenv("MNGR_AGENT_WORK_DIR", str(tmp_path / "work"))
    registry_path = tmp_path / "apps.toml"
    monkeypatch.setenv("MINDS_APPS_FILE", str(registry_path))

    stub_source = StubInstanceSource()
    stub_port = free_port()
    stub_url = f"http://{LOOPBACK_HOST}:{stub_port}"
    write_registry(
        registry_path,
        registry_row_toml(
            "chat",
            _BASE_URL,
            is_multi_instance=True,
            is_critical=True,
            actions=(("new", "New Chat"), ("subagent", "Open subagent")),
            default_shortcut=("new", "new"),
        ),
        registry_row_toml(_STUB_APP_NAME, stub_url, is_multi_instance=True, actions=(("new", "New docs"),)),
    )

    broadcaster = WebSocketBroadcaster()
    manager = AgentManager.build(broadcaster)
    manager._agents[_AGENT_ID] = AgentStateItem(
        id=_AGENT_ID,
        name=_AGENT_NAME,
        state="running",
        labels={},
        work_dir=str(tmp_path / "work"),
    )
    manager.note_agent_list_known()

    config = Config(system_interface_host="127.0.0.1", system_interface_port=_PORT)
    state = build_test_state(config=config, agent_manager=manager, shell_state_directory=tmp_path / "shell")
    app = create_application(state)

    server = make_threaded_server("127.0.0.1", _PORT, app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    with serve_in_background(LOOPBACK_HOST, stub_port, build_instances_app(stub_source, RecordingNudger())):
        try:
            wait_for(
                lambda: _server_is_up(_BASE_URL),
                timeout=5.0,
                poll_interval=0.05,
                error_message=f"workspace server did not come up at {_BASE_URL}",
            )
            state.shell.start()
            try:
                yield PipelineHarness(
                    base_url=_BASE_URL, broadcaster=broadcaster, registry_path=registry_path, stub_source=stub_source
                )
            finally:
                state.shell.stop()
        finally:
            server.shutdown()
            thread.join(timeout=5.0)


def _run_layout_script(args: list[str], harness: PipelineHarness, cwd: Path) -> subprocess.CompletedProcess[str]:
    """Invoke ``system/scripts/layout.py`` as a subprocess against the test server.

    ``cwd`` is a sandbox so nothing relative resolves into the repo; the registry the
    script reads is the fixture's, through ``MINDS_APPS_FILE``.
    """
    return subprocess.run(
        [sys.executable, str(_LAYOUT_SCRIPT), *args],
        capture_output=True,
        text=True,
        cwd=str(cwd),
        env={
            "PATH": sys.exec_prefix + "/bin:/usr/bin:/bin",
            "PYTHONPATH": "",
            "MINDS_WORKSPACE_SERVER_URL": harness.base_url,
            "MINDS_APPS_FILE": str(harness.registry_path),
            "MNGR_AGENT_ID": _AGENT_ID,
            # Mutating ops in production block until the layout state changes are observable
            # via inspect; there is no frontend here to apply the op, so the script is told not
            # to wait (``ENV_NO_WAIT_STABLE`` in ``system/scripts/layout.py``).
            "MINDS_LAYOUT_NO_WAIT_STABLE": "1",
        },
        timeout=15,
    )


def _await_layout_op(client_queue: queue_module.Queue[str | None], timeout: float) -> dict[str, Any]:
    """Block until a ``layout_op`` message arrives, returning the parsed payload; other pushes are skipped."""
    parsed_result: dict[str, Any] = {}

    def _drain_once() -> bool:
        try:
            msg = client_queue.get(timeout=0.05)
        except queue_module.Empty:
            return False
        assert msg is not None, "broadcaster shut down before a layout_op arrived"
        parsed = json.loads(msg)
        if parsed.get("type") != "layout_op":
            return False
        parsed_result.update(parsed)
        return True

    wait_for(_drain_once, timeout=timeout, poll_interval=0.0, error_message=f"no layout_op arrived within {timeout}s")
    return parsed_result


def _assert_no_layout_op(client_queue: queue_module.Queue[str | None]) -> None:
    """Nothing addressed to the dock reached the client within half a second; unrelated pushes are fine."""
    deadline = time.monotonic() + 0.5
    while time.monotonic() < deadline:
        try:
            raw = client_queue.get(timeout=0.05)
        except queue_module.Empty:
            continue
        assert raw is None or json.loads(raw).get("type") != "layout_op"


def _listing(harness: PipelineHarness, cwd: Path) -> dict[str, dict[str, Any]]:
    """``layout.py list --json`` as ``{app name: entry}``."""
    result = _run_layout_script(["list", "--json"], harness, cwd)
    assert result.returncode == 0, f"stderr={result.stderr!r}"
    return {entry["name"]: entry for entry in json.loads(result.stdout)}


def _nudge(harness: PipelineHarness, app: str) -> None:
    """Tell the shell an app's list changed, as the app itself would (``POST /api/apps/<name>/changed``)."""
    request = urllib.request.Request(f"{harness.base_url}/api/apps/{app}/changed", method="POST")
    with urllib.request.urlopen(request, timeout=5) as response:
        assert response.status == 204


def _wait_for_instance_listed(harness: PipelineHarness, cwd: Path, app: str, address: str) -> None:
    """The inventory fetches instance lists off the request thread, so a listing is polled for."""
    wait_for(
        lambda: address
        in {instance["address"] for instance in _listing(harness, cwd).get(app, {"instances": []})["instances"]},
        timeout=10.0,
        poll_interval=0.2,
        error_message=f"{address} never listed under {app}",
    )


def _sandbox(tmp_path: Path) -> Path:
    sandbox = tmp_path / "cwd"
    sandbox.mkdir(exist_ok=True)
    return sandbox


def test_inspect_and_context_round_trip_through_script_and_endpoint(
    layout_server: PipelineHarness, tmp_path: Path
) -> None:
    """``inspect --json`` and ``context --json`` answer with the empty shapes for a machine nobody has opened."""
    sandbox = _sandbox(tmp_path)

    inspect = _run_layout_script(["inspect", "--json"], layout_server, sandbox)
    assert inspect.returncode == 0, f"stderr={inspect.stderr!r}"
    assert json.loads(inspect.stdout)["panels"] == []

    context = _run_layout_script(["context", "--json"], layout_server, sandbox)
    assert context.returncode == 0, f"stderr={context.stderr!r}"
    assert json.loads(context.stdout) == []


def test_mutating_op_without_client_on_view_fails_with_412(layout_server: PipelineHarness, tmp_path: Path) -> None:
    """With no connected client on the target view, the script reports the 412 clearly."""
    result = _run_layout_script(["close", _CHAT_ADDRESS, "--view", "Everything"], layout_server, _sandbox(tmp_path))

    assert result.returncode == 1
    assert "No connected client has view" in result.stderr


def test_list_shows_every_app_with_the_seeded_agent_as_a_chat_instance(
    layout_server: PipelineHarness, tmp_path: Path
) -> None:
    """``list --json`` is the inventory: the chat app with the agent's address, and the stub app with none yet."""
    sandbox = _sandbox(tmp_path)
    _wait_for_instance_listed(layout_server, sandbox, "chat", _CHAT_ADDRESS)

    listing = _listing(layout_server, sandbox)
    chat = listing["chat"]
    assert [instance["title"] for instance in chat["instances"] if instance["address"] == _CHAT_ADDRESS] == [
        _AGENT_NAME
    ]
    assert [action["id"] for action in chat["actions"]] == ["new", "subagent"]
    assert listing[_STUB_APP_NAME]["instances"] == []
    assert listing[_STUB_APP_NAME]["is_running"] is True


def test_open_of_a_bare_app_with_instances_broadcasts_the_creating_op(
    layout_server: PipelineHarness, tmp_path: Path
) -> None:
    """``open docs`` (a bare name, expanded to ``app:docs``) reaches the client as an ``open`` of that app.

    The instance is minted when the frontend runs the action, so the broadcast carries the
    app's address and the frontend creates through the relay from there.
    """
    client_queue = layout_server.broadcaster.register()
    layout_server.broadcaster.set_client_info(client_queue, "client-1", "everything", "desktop")
    try:
        result = _run_layout_script(
            ["open", _STUB_APP_NAME, "--view", "Everything"], layout_server, _sandbox(tmp_path)
        )
        assert result.returncode == 0, f"stderr={result.stderr!r}"

        msg = _await_layout_op(client_queue, timeout=2.0)
        assert msg["op"] == "open"
        assert msg["args"] == {"address": f"app:{_STUB_APP_NAME}", "new_group": False}
    finally:
        layout_server.broadcaster.unregister(client_queue)


def test_open_close_of_an_instance_address_broadcasts_layout_ops(
    layout_server: PipelineHarness, tmp_path: Path
) -> None:
    """``open`` and ``close`` against an instance address reach the broadcaster intact."""
    sandbox = _sandbox(tmp_path)
    client_queue = layout_server.broadcaster.register()
    layout_server.broadcaster.set_client_info(client_queue, "client-1", "everything", "desktop")
    try:
        open_result = _run_layout_script(["open", _CHAT_ADDRESS, "--view", "Everything"], layout_server, sandbox)
        assert open_result.returncode == 0, f"stderr={open_result.stderr!r}"
        open_msg = _await_layout_op(client_queue, timeout=2.0)
        assert open_msg["op"] == "open"
        assert open_msg["args"] == {"address": _CHAT_ADDRESS, "new_group": False}

        close_result = _run_layout_script(["close", _CHAT_ADDRESS, "--view", "Everything"], layout_server, sandbox)
        assert close_result.returncode == 0, f"stderr={close_result.stderr!r}"
        close_msg = _await_layout_op(client_queue, timeout=2.0)
        assert close_msg["op"] == "close"
        assert close_msg["args"] == {"address": _CHAT_ADDRESS}
    finally:
        layout_server.broadcaster.unregister(client_queue)


@pytest.mark.parametrize(
    ("spelling", "expected_hint"),
    [
        (f"chat:{_AGENT_NAME}", f"app:chat?instance={_AGENT_NAME}"),
        ("service:docs?instance=docs-1", "app:docs?instance=docs-1"),
        ("terminal:terminal-1", "app:terminal?instance=terminal-1"),
        ("https://example.com/", "phase 8"),
    ],
)
def test_retired_spellings_and_external_urls_are_refused_before_they_broadcast(
    layout_server: PipelineHarness, tmp_path: Path, spelling: str, expected_hint: str
) -> None:
    """A retired ref or an external URL fails at the script, naming the new form, and never reaches a client."""
    client_queue = layout_server.broadcaster.register()
    layout_server.broadcaster.set_client_info(client_queue, "client-1", "everything", "desktop")
    try:
        result = _run_layout_script(["open", spelling, "--view", "Everything"], layout_server, _sandbox(tmp_path))
        assert result.returncode != 0
        assert spelling in result.stderr
        assert expected_hint in result.stderr
        _assert_no_layout_op(client_queue)
    finally:
        layout_server.broadcaster.unregister(client_queue)


def test_unknown_app_is_refused_by_name(layout_server: PipelineHarness, tmp_path: Path) -> None:
    """``open app:nowhere`` names the missing registration rather than broadcasting an op nobody can carry out."""
    client_queue = layout_server.broadcaster.register()
    layout_server.broadcaster.set_client_info(client_queue, "client-1", "everything", "desktop")
    try:
        result = _run_layout_script(["open", "app:nowhere", "--view", "Everything"], layout_server, _sandbox(tmp_path))
        assert result.returncode != 0
        assert "nowhere" in result.stderr
        _assert_no_layout_op(client_queue)
    finally:
        layout_server.broadcaster.unregister(client_queue)


def test_rename_and_delete_reach_the_app_through_the_relay(layout_server: PipelineHarness, tmp_path: Path) -> None:
    """``rename`` retitles the instance in its app, ``delete`` removes it there, and ``list`` follows."""
    sandbox = _sandbox(tmp_path)
    layout_server.stub_source.records.append(instance_record("stub-1", title="Stub 1"))
    address = f"app:{_STUB_APP_NAME}?instance=stub-1"
    _nudge(layout_server, _STUB_APP_NAME)
    _wait_for_instance_listed(layout_server, sandbox, _STUB_APP_NAME, address)

    rename = _run_layout_script(["rename", address, "Design notes"], layout_server, sandbox)
    assert rename.returncode == 0, f"stderr={rename.stderr!r}"
    assert [str(record.title) for record in layout_server.stub_source.records] == ["Design notes"]
    wait_for(
        lambda: {instance["title"] for instance in _listing(layout_server, sandbox)[_STUB_APP_NAME]["instances"]}
        == {"Design notes"},
        timeout=10.0,
        poll_interval=0.2,
        error_message="the listing never showed the new title",
    )

    delete = _run_layout_script(["delete", address], layout_server, sandbox)
    assert delete.returncode == 0, f"stderr={delete.stderr!r}"
    assert layout_server.stub_source.records == []
    wait_for(
        lambda: _listing(layout_server, sandbox)[_STUB_APP_NAME]["instances"] == [],
        timeout=10.0,
        poll_interval=0.2,
        error_message="the listing kept the deleted instance",
    )


def test_relay_verbs_need_an_instance_address(layout_server: PipelineHarness, tmp_path: Path) -> None:
    """``rename app:docs`` names the app, not an instance, and says so."""
    result = _run_layout_script(["rename", f"app:{_STUB_APP_NAME}", "Nope"], layout_server, _sandbox(tmp_path))
    assert result.returncode != 0
    assert "instance address" in result.stderr


def test_shortcuts_are_set_and_removed_on_a_project(layout_server: PipelineHarness, tmp_path: Path) -> None:
    """``shortcut set`` pins an app's action to a project's rail and ``shortcut remove`` takes it off."""
    sandbox = _sandbox(tmp_path)
    create = urllib.request.Request(
        f"{layout_server.base_url}/api/projects",
        data=json.dumps({"name": "Project 1", "color": "#3B82F6", "glyph": 1}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(create, timeout=5) as response:
        assert response.status == 201

    set_result = _run_layout_script(
        ["shortcut", "set", _STUB_APP_NAME, "new", "--mode", "focus", "--view", "Project 1"], layout_server, sandbox
    )
    assert set_result.returncode == 0, f"stderr={set_result.stderr!r}"
    listed = _run_layout_script(["shortcuts", "--view", "Project 1", "--json"], layout_server, sandbox)
    assert listed.returncode == 0, f"stderr={listed.stderr!r}"
    rows = {(row["app"], row["action"]): row["mode"] for row in json.loads(listed.stdout)["shortcuts"]}
    assert rows[(_STUB_APP_NAME, "new")] == "focus"
    # The chat's default shortcut was seeded when the project was created.
    assert rows[("chat", "new")] == "new"

    remove_result = _run_layout_script(
        ["shortcut", "remove", _STUB_APP_NAME, "new", "--view", "Project 1"], layout_server, sandbox
    )
    assert remove_result.returncode == 0, f"stderr={remove_result.stderr!r}"
    listed_after = _run_layout_script(["shortcuts", "--view", "Project 1", "--json"], layout_server, sandbox)
    assert (_STUB_APP_NAME, "new") not in {
        (row["app"], row["action"]) for row in json.loads(listed_after.stdout)["shortcuts"]
    }


def test_script_runs_without_a_registry_file_for_read_ops(layout_server: PipelineHarness, tmp_path: Path) -> None:
    """``views --json`` needs no registry on disk: the shell answers it."""
    sandbox = _sandbox(tmp_path)
    result = subprocess.run(
        [sys.executable, str(_LAYOUT_SCRIPT), "views", "--json"],
        capture_output=True,
        text=True,
        cwd=str(sandbox),
        env={
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": "",
            "MINDS_WORKSPACE_SERVER_URL": layout_server.base_url,
            "MINDS_APPS_FILE": str(tmp_path / "absent.toml"),
        },
        timeout=15,
    )
    assert result.returncode == 0, f"stderr={result.stderr!r}"
    views = json.loads(result.stdout)
    assert any(view["id"] == "everything" for view in views)
