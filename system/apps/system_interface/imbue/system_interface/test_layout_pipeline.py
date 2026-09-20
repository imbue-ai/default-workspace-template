"""Acceptance test for the agent-driven desktop pipeline.

Exercises the full backend path the agent-facing helper depends on:
``system/scripts/layout.py`` (subprocess) -> ``POST /api/layout/broadcast``
(loopback Flask route) -> the desktop and placement files -> the broadcaster's
``desktops_updated`` and ``placements_updated`` messages. The WS-to-DOM step is
``test_e2e.py``'s.

The machine the script sees is a registry with two rows, both stand-in apps
served over loopback so the liveness probe finds them running. Broadcaster
output is observed via the broadcaster's own queue-registration API rather than
a live WebSocket.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from typing import Generator

import pytest
from flask import Flask
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.utils.polling import wait_for
from imbue.system_interface.config import Config
from imbue.system_interface.server import create_application
from imbue.system_interface.shell.testing import drain_messages
from imbue.system_interface.shell.testing import registry_row_toml
from imbue.system_interface.shell.testing import write_registry
from imbue.system_interface.testing import build_test_state
from imbue.system_interface.testing import serve_app
from imbue.system_interface.ws_broadcaster import WebSocketBroadcaster
from imbue.system_interface.wsgi import make_threaded_server

pytestmark = pytest.mark.acceptance

_PORT = 18766
_BASE_URL = f"http://127.0.0.1:{_PORT}"
# The app that declares launch paths, and the marker of the requester's own page of it.
_SEEDED_APP_NAME = "chat"
_SEEDED_KEY = "stub-1"
_SEEDED_TITLE = "alice"
# The requesting agent, which the script resolves ``self`` and its attribution against.
_AGENT_ID = "agent-test-alice"
_STUB_APP_NAME = "docs"
# The one connected client of most tests, on the default desktop.
_CLIENT_ID = "client-1"
_DEFAULT_DESKTOP_ID = "home"

_REPO_ROOT = Path(__file__).resolve().parents[5]
_LAYOUT_SCRIPT = _REPO_ROOT / "system" / "scripts" / "layout.py"


def _server_is_up(url: str) -> bool:
    """Any HTTP answer from ``/api/desktops`` means the app is serving."""
    try:
        urllib.request.urlopen(f"{url}/api/desktops", timeout=0.5)
        return True
    except urllib.error.HTTPError:
        return True
    except OSError:
        return False


class PipelineHarness(FrozenModel):
    """What one test gets: the shell's URL, its broadcaster, and the registry file."""

    model_config = {"arbitrary_types_allowed": True}

    base_url: str = Field(description="The shell's loopback URL")
    broadcaster: WebSocketBroadcaster = Field(description="The shell's broadcaster, for fake clients")
    registry_path: Path = Field(description="The registry file the shell and the script read")


def _stand_in_app() -> Flask:
    """An app that answers every path, so the liveness probe finds it running."""
    app = Flask("stand-in")
    app.add_url_rule("/", view_func=lambda: "ok", endpoint="root")
    app.add_url_rule("/<path:path>", view_func=lambda path: "ok", endpoint="page")
    return app


@pytest.fixture
def layout_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[PipelineHarness, None, None]:
    """A workspace server over a registry of two stand-in apps, one declaring launch paths, and a started shell."""
    registry_path = tmp_path / "apps.toml"
    monkeypatch.setenv("MINDS_APPS_FILE", str(registry_path))
    monkeypatch.setenv("MINDS_WORKSPACE_SERVER_URL", _BASE_URL)

    with serve_app(_stand_in_app()) as seeded, serve_app(_stand_in_app()) as stub:
        write_registry(
            registry_path,
            registry_row_toml(
                _SEEDED_APP_NAME,
                seeded.http_url,
                is_critical=True,
                default_shortcut=("new", "new"),
                launch_paths=(("new", "New Chat", "/new"), ("subagent", "Open subagent", "/subagent")),
            ),
            registry_row_toml(_STUB_APP_NAME, stub.http_url),
        )

        broadcaster = WebSocketBroadcaster()
        config = Config(system_interface_host="127.0.0.1", system_interface_port=_PORT)
        state = build_test_state(config=config, broadcaster=broadcaster, shell_state_directory=tmp_path / "shell")
        app = create_application(state)

        server = make_threaded_server("127.0.0.1", _PORT, app)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            wait_for(
                lambda: _server_is_up(_BASE_URL),
                timeout=5.0,
                poll_interval=0.05,
                error_message=f"workspace server did not come up at {_BASE_URL}",
            )
            state.shell.start()
            try:
                yield PipelineHarness(base_url=_BASE_URL, broadcaster=broadcaster, registry_path=registry_path)
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
        },
        timeout=15,
    )


def _listing(harness: PipelineHarness, cwd: Path) -> dict[str, dict[str, Any]]:
    """``layout.py list --json`` as ``{app name: entry}``."""
    result = _run_layout_script(["list", "--json"], harness, cwd)
    assert result.returncode == 0, f"stderr={result.stderr!r}"
    return {entry["name"]: entry for entry in json.loads(result.stdout)["apps"]}


def _windows(harness: PipelineHarness, cwd: Path) -> list[dict[str, Any]]:
    """Every window on every desktop, as ``layout.py desktops --json`` lists them."""
    result = _run_layout_script(["desktops", "--json"], harness, cwd)
    assert result.returncode == 0, f"stderr={result.stderr!r}"
    return [window for desktop in json.loads(result.stdout)["desktops"] for window in desktop["windows"]]


def _sandbox(tmp_path: Path) -> Path:
    sandbox = tmp_path / "cwd"
    sandbox.mkdir(exist_ok=True)
    return sandbox


@pytest.fixture
def connected_client(layout_server: PipelineHarness) -> Generator["queue.Queue[str | None]", None, None]:
    """One connected browser client on the default desktop: the client every op with no ``--client`` targets."""
    client_queue = layout_server.broadcaster.register()
    layout_server.broadcaster.set_client_info(client_queue, _CLIENT_ID, _DEFAULT_DESKTOP_ID
    )
    try:
        yield client_queue
    finally:
        layout_server.broadcaster.unregister(client_queue)


def test_context_and_desktops_round_trip_through_script_and_endpoint(
    layout_server: PipelineHarness, tmp_path: Path
) -> None:
    """``context --json`` is empty for a machine nobody has opened and lists a client as soon as it connects,
    before it has messaged; ``desktops --json`` lists the default desktop with no windows."""
    sandbox = _sandbox(tmp_path)

    context = _run_layout_script(["context", "--json"], layout_server, sandbox)
    assert context.returncode == 0, f"stderr={context.stderr!r}"
    assert json.loads(context.stdout) == []

    client_queue = layout_server.broadcaster.register()
    layout_server.broadcaster.set_client_info(client_queue, "client-silent", "home")
    try:
        connected = _run_layout_script(["context", "--json"], layout_server, sandbox)
        assert connected.returncode == 0, f"stderr={connected.stderr!r}"
        (entry,) = json.loads(connected.stdout)
        assert entry["client_id"] == "client-silent"
        assert entry["is_connected"] is True and entry["active_desktop"] == "home"
    finally:
        layout_server.broadcaster.unregister(client_queue)

    desktops = _run_layout_script(["desktops", "--json"], layout_server, sandbox)
    assert desktops.returncode == 0, f"stderr={desktops.stderr!r}"
    listed = json.loads(desktops.stdout)
    assert [(desktop["id"], desktop["windows"]) for desktop in listed["desktops"]] == [(_DEFAULT_DESKTOP_ID, [])]


def test_an_op_with_no_client_to_target_fails_with_412(layout_server: PipelineHarness, tmp_path: Path) -> None:
    """With no client connected or recorded, there is nobody's placements to edit, and the script says so."""
    result = _run_layout_script(["open", _STUB_APP_NAME], layout_server, _sandbox(tmp_path))

    assert result.returncode == 1
    assert "Could not tell which client" in result.stderr and "--client" in result.stderr


def test_list_shows_every_app_with_its_launch_paths(layout_server: PipelineHarness, tmp_path: Path) -> None:
    """``list --json`` is the inventory: every app a user can open, its launch paths, and whether it runs."""
    listing = _listing(layout_server, _sandbox(tmp_path))

    assert set(listing) == {_SEEDED_APP_NAME, _STUB_APP_NAME}
    # An app declaring no launch path offers the synthesized ``open`` at its root.
    assert [(launch["id"], launch["path"]) for launch in listing[_STUB_APP_NAME]["launch_paths"]] == [("open", "/")]
    assert [launch["id"] for launch in listing[_SEEDED_APP_NAME]["launch_paths"]] == ["new", "subagent"]
    assert listing[_STUB_APP_NAME]["is_running"] is True
    assert listing[_STUB_APP_NAME]["windows"] == []


def test_open_lands_a_window_the_window_verbs_arrange_and_close_takes_away(
    layout_server: PipelineHarness, connected_client: "queue.Queue[str | None]", tmp_path: Path
) -> None:
    """``open docs`` opens a window at the app's launch path on the connected client's desktop and prints its
    id; ``focus``, ``place``, and ``minimize`` edit that client's placements; ``close`` removes the window for
    everyone; the writes are announced to the client."""
    sandbox = _sandbox(tmp_path)

    opened = _run_layout_script(["open", _STUB_APP_NAME], layout_server, sandbox)
    assert opened.returncode == 0, f"stderr={opened.stderr!r}"
    window_id = opened.stdout.strip()
    assert window_id.startswith("win-")
    assert f"opened window {window_id} ({_STUB_APP_NAME} at /)" in opened.stderr
    (window,) = _windows(layout_server, sandbox)
    assert (window["id"], window["app"], window["path"], window["is_settling"]) == (window_id, _STUB_APP_NAME, "/", True)
    assert [entry["id"] for entry in _listing(layout_server, sandbox)[_STUB_APP_NAME]["windows"]] == [window_id]

    # Opening the app again focuses the window already at its launch path rather than opening another.
    focused = _run_layout_script(["open", _STUB_APP_NAME], layout_server, sandbox)
    assert focused.returncode == 0, f"stderr={focused.stderr!r}"
    assert focused.stdout.strip() == window_id
    assert len(_windows(layout_server, sandbox)) == 1

    def placement_states() -> list[tuple[str, str, bool]]:
        stored = _get_json(layout_server, f"/api/placements/{_DEFAULT_DESKTOP_ID}?client={_CLIENT_ID}")
        return [(row["window_id"], row["state"], row["is_minimized"]) for row in stored["placements"]]

    for verb, extra, expected in (
        ("focus", [], (window_id, "NORMAL", False)),
        ("place", ["--zone", "left"], (window_id, "SNAPPED_LEFT", False)),
        ("minimize", [], (window_id, "SNAPPED_LEFT", True)),
        ("restore", [], (window_id, "NORMAL", False)),
        ("maximize", [], (window_id, "MAXIMIZED", False)),
        ("place", ["--frame", "0.1,0.1,0.5,0.5"], (window_id, "NORMAL", False)),
    ):
        result = _run_layout_script([verb, window_id, *extra], layout_server, sandbox)
        assert result.returncode == 0, f"{verb}: stderr={result.stderr!r}"
        assert placement_states() == [expected], verb

    closed = _run_layout_script(["close", window_id], layout_server, sandbox)
    assert closed.returncode == 0, f"stderr={closed.stderr!r}"
    assert _windows(layout_server, sandbox) == []
    types = [message["type"] for message in drain_messages(connected_client)]
    assert "desktops_updated" in types and "placements_updated" in types
    # Focusing what is gone is a 404 with the window named.
    missing = _run_layout_script(["focus", window_id], layout_server, sandbox)
    assert missing.returncode == 1 and window_id in missing.stderr


def test_open_at_a_path_names_the_page_and_self_names_the_callers_window(
    layout_server: PipelineHarness, connected_client: "queue.Queue[str | None]", tmp_path: Path
) -> None:
    """``open chat --path /?chat=<id>`` opens the page itself (no launch path, not settling); ``self`` resolves
    to the window of the requester's chat; ``navigate`` points a window elsewhere; a second window of the app
    is reached by the app's name."""
    sandbox = _sandbox(tmp_path)
    own_path = f"/?chat={_AGENT_ID}"

    opened = _run_layout_script(["open", _SEEDED_APP_NAME, "--path", own_path], layout_server, sandbox)
    assert opened.returncode == 0, f"stderr={opened.stderr!r}"
    own_window_id = opened.stdout.strip()
    (window,) = _windows(layout_server, sandbox)
    assert (window["path"], window["is_settling"]) == (own_path, False)

    focused = _run_layout_script(["focus", "self"], layout_server, sandbox)
    assert focused.returncode == 0, f"stderr={focused.stderr!r}"
    assert f"focused window {own_window_id}" in focused.stderr

    navigated = _run_layout_script(["navigate", "self", "/?chat=other"], layout_server, sandbox)
    assert navigated.returncode == 0, f"stderr={navigated.stderr!r}"
    assert [window["path"] for window in _windows(layout_server, sandbox)] == ["/?chat=other"]
    # With its marker gone from every path, ``self`` names nothing.
    gone = _run_layout_script(["focus", "self"], layout_server, sandbox)
    assert gone.returncode == 1 and "self" in gone.stderr

    by_app = _run_layout_script(["maximize", _SEEDED_APP_NAME], layout_server, sandbox)
    assert by_app.returncode == 0, f"stderr={by_app.stderr!r}"
    assert f"maximized window {own_window_id}" in by_app.stderr


@pytest.mark.parametrize(
    ("argv", "expected_hint"),
    [
        (["open", f"app:{_STUB_APP_NAME}"], "give an app name and a path"),
        (["open", f"chat:{_SEEDED_TITLE}"], "give an app name and a path"),
        (["focus", f"app:{_STUB_APP_NAME}?instance=x"], "give an app name and a path"),
        (["split", _STUB_APP_NAME, "--relative-to", "self"], "not a desktop verb"),
        (["rename", f"app:{_STUB_APP_NAME}?instance=x", "Docs"], "not a desktop verb"),
    ],
)
def test_retired_spellings_and_verbs_are_refused_before_they_reach_the_shell(
    layout_server: PipelineHarness,
    connected_client: "queue.Queue[str | None]",
    tmp_path: Path,
    argv: list[str],
    expected_hint: str,
) -> None:
    """A retired form fails at the script, naming what to use instead, and opens nothing."""
    result = _run_layout_script(argv, layout_server, _sandbox(tmp_path))

    assert result.returncode != 0
    assert expected_hint in result.stderr
    assert _windows(layout_server, _sandbox(tmp_path)) == []


def test_a_url_needs_the_browser_app(
    layout_server: PipelineHarness, connected_client: "queue.Queue[str | None]", tmp_path: Path
) -> None:
    """``open https://...`` is the browser's ``new`` launch path, so with no browser registered it fails naming it."""
    result = _run_layout_script(["open", "https://example.com/"], layout_server, _sandbox(tmp_path))

    assert result.returncode != 0 and "browser" in result.stderr
    assert _windows(layout_server, _sandbox(tmp_path)) == []


def test_unknown_app_is_refused_by_name(
    layout_server: PipelineHarness, connected_client: "queue.Queue[str | None]", tmp_path: Path
) -> None:
    """``open nowhere`` names the missing registration and opens nothing."""
    result = _run_layout_script(["open", "nowhere"], layout_server, _sandbox(tmp_path))

    assert result.returncode != 0
    assert "nowhere" in result.stderr
    assert _windows(layout_server, _sandbox(tmp_path)) == []


def test_shortcuts_are_set_moved_and_removed_on_a_desktop(
    layout_server: PipelineHarness, connected_client: "queue.Queue[str | None]", tmp_path: Path
) -> None:
    """``shortcut set`` pins an app's launch path to the desktop's backdrop, ``shortcut move`` puts it in another
    cell, and ``shortcut remove`` takes it off."""
    sandbox = _sandbox(tmp_path)

    set_result = _run_layout_script(
        ["shortcut", "set", _STUB_APP_NAME, "open", "--mode", "new", "--cell", "1,0"], layout_server, sandbox
    )
    assert set_result.returncode == 0, f"stderr={set_result.stderr!r}"
    listed = _run_layout_script(["shortcuts", "--json"], layout_server, sandbox)
    assert listed.returncode == 0, f"stderr={listed.stderr!r}"
    shortcuts = json.loads(listed.stdout)
    assert shortcuts["desktop"] == _DEFAULT_DESKTOP_ID
    rows = {(row["target"]["app"], row["target"]["launch"]): row for row in shortcuts["shortcuts"]}
    assert rows[(_STUB_APP_NAME, "open")]["mode"] == "new"
    assert rows[(_STUB_APP_NAME, "open")]["cell"] == {"column": 1, "row": 0}

    moved = _run_layout_script(["shortcut", "move", _STUB_APP_NAME, "open", "--cell", "2,1"], layout_server, sandbox)
    assert moved.returncode == 0, f"stderr={moved.stderr!r}"
    desktops = _get_json(layout_server, "/api/desktops")["desktops"]
    (shortcut,) = [
        shortcut for shortcut in desktops[0]["shortcuts"] if shortcut["target"]["app"] == _STUB_APP_NAME
    ]
    assert shortcut["cell"] == {"column": 2, "row": 1}

    removed = _run_layout_script(["shortcut", "remove", _STUB_APP_NAME, "open"], layout_server, sandbox)
    assert removed.returncode == 0, f"stderr={removed.stderr!r}"
    listed_after = _run_layout_script(["shortcuts", "--json"], layout_server, sandbox)
    assert (_STUB_APP_NAME, "open") not in {
        (row["target"]["app"], row["target"]["launch"]) for row in json.loads(listed_after.stdout)["shortcuts"]
    }


def test_script_runs_without_a_registry_file_for_read_ops(layout_server: PipelineHarness, tmp_path: Path) -> None:
    """``desktops --json`` needs no registry on disk: the shell answers it."""
    sandbox = _sandbox(tmp_path)
    result = subprocess.run(
        [sys.executable, str(_LAYOUT_SCRIPT), "desktops", "--json"],
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
    assert [desktop["id"] for desktop in json.loads(result.stdout)["desktops"]] == [_DEFAULT_DESKTOP_ID]


def _post_op(
    harness: PipelineHarness, op: str, args: dict[str, Any], requester: dict[str, str]
) -> tuple[int, dict[str, Any]]:
    """Post one desktop verb to the op route the way the desktop interface's ``layout.py`` will."""
    body = json.dumps({"op": op, "args": args, "requester": requester}).encode()
    request = urllib.request.Request(
        f"{harness.base_url}/api/layout/broadcast",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _get_json(harness: PipelineHarness, path: str) -> dict[str, Any]:
    with urllib.request.urlopen(f"{harness.base_url}{path}", timeout=10) as response:
        return json.loads(response.read())


def test_desktop_verbs_open_and_close_a_window_in_the_clients_layout_with_no_browser_connected(
    layout_server: PipelineHarness,
) -> None:
    """The desktop vocabulary on the op route: ``open`` at the app's synthesized launch path lands a window on the
    default desktop and a placement in the target client's file, and ``close`` takes both away, whether or not a
    window is open; the writes are announced to that client."""
    requester = {"app": _SEEDED_APP_NAME, "marker": _SEEDED_KEY}
    client_queue = layout_server.broadcaster.register()
    layout_server.broadcaster.set_client_info(client_queue, "client-1", "home")
    try:
        status, listed = _post_op(layout_server, "desktops", {}, requester)
        assert status == 200 and [desktop["id"] for desktop in listed["desktops"]] == ["home"]

        status, opened = _post_op(layout_server, "open", {"app": _STUB_APP_NAME}, requester)
        assert status == 200, opened
        window_id = opened["window_id"]
        (window,) = opened["desktop"]["windows"]
        assert window["app"] == _STUB_APP_NAME and window["path"] == "/" and window["is_settling"] is True
        assert [placement["window_id"] for placement in opened["layout"]["placements"]] == [window_id]
        stored = _get_json(layout_server, "/api/placements/home?client=client-1")
        assert [placement["window_id"] for placement in stored["placements"]] == [window_id]
        assert [desktop["windows"][0]["id"] for desktop in _get_json(layout_server, "/api/desktops")["desktops"]] == [
            window_id
        ]

        status, closed = _post_op(layout_server, "close", {"window": window_id}, requester)
        assert status == 200 and closed["desktop"]["windows"] == [] and closed["layout"]["placements"] == []
        types = [message["type"] for message in drain_messages(client_queue)]
        assert "desktops_updated" in types and "placements_updated" in types

        status, refused = _post_op(layout_server, "focus", {"window": window_id}, requester)
        assert status == 404 and window_id in refused["detail"]
    finally:
        layout_server.broadcaster.unregister(client_queue)
