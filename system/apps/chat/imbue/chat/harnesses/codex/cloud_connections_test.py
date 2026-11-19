"""Codex's cloud connections: the ``app/list`` exchange over a scripted transport, the probe's failure
against a stub ``codex`` on PATH, and the ``features.apps`` flag in an agent's own config."""

import json
import os
from collections import deque
from pathlib import Path
from typing import Any

import pytest
import tomlkit

from imbue.chat.harnesses.cloud_connections import CloudConnectionsError
from imbue.chat.harnesses.codex.cloud_connections import CodexCloudConnections
from imbue.chat.harnesses.codex.cloud_connections import list_connected_apps
from imbue.mngr_codex.app_server_client import CodexAppServerClient
from imbue.mngr_codex.app_server_client import CodexAppServerError
from imbue.mngr_codex.codex_config import get_codex_config_path
from imbue.mngr_codex.codex_config import get_codex_home


class _AppListTransport:
    """Answers ``app/list`` with what a test scripts, after the notifications codex sends while it loads."""

    def __init__(self, answer: dict[str, Any] | None) -> None:
        self._answer = answer
        self._inbound: deque[str] = deque()
        self.requests: list[dict[str, Any]] = []

    def send(self, message: str) -> None:
        request = json.loads(message)
        self.requests.append(request)
        self._inbound.append(json.dumps({"jsonrpc": "2.0", "method": "app/list/updated", "params": {"data": []}}))
        self._inbound.append("not json")
        self._inbound.append(json.dumps({"jsonrpc": "2.0", "id": 99, "result": {"data": []}}))
        if self._answer is not None:
            self._inbound.append(json.dumps({"jsonrpc": "2.0", "id": request["id"], **self._answer}))

    def receive(self, timeout: float | None) -> str:
        if not self._inbound:
            raise TimeoutError
        return self._inbound.popleft()

    def close(self) -> None:
        pass


def _app(name: str, is_accessible: bool) -> dict[str, Any]:
    return {"id": name.lower(), "name": name, "isAccessible": is_accessible, "isEnabled": True}


def _agent_config(tmp_path: Path, text: str) -> Path:
    codex_home = get_codex_home(tmp_path)
    codex_home.mkdir(parents=True)
    get_codex_config_path(codex_home).write_text(text)
    return tmp_path


def test_only_the_apps_connected_on_the_account_are_named(tmp_path: Path) -> None:
    """The list is the whole directory; the account's own apps are the ones codex marks accessible."""
    apps = [_app("Gmail", True), _app("Slack", True), _app("Gmail", True), _app("Zillow", False)]
    transport = _AppListTransport({"result": {"data": apps, "nextCursor": "200"}})

    names = list_connected_apps(CodexAppServerClient(transport=transport))

    assert names == ("Gmail", "Slack")
    (request,) = transport.requests
    assert request["method"] == "app/list"
    assert request["params"]["limit"] == 200


def test_an_error_answer_and_a_malformed_one_are_both_failures() -> None:
    refused = _AppListTransport({"error": {"code": -32603, "message": "failed to load app lists"}})
    with pytest.raises(CodexAppServerError, match="failed to load app lists"):
        list_connected_apps(CodexAppServerClient(transport=refused))
    malformed = _AppListTransport({"result": {"apps": []}})
    with pytest.raises(CodexAppServerError, match="no list of apps"):
        list_connected_apps(CodexAppServerClient(transport=malformed))


def test_a_codex_that_never_answers_is_a_timeout() -> None:
    with pytest.raises(CodexAppServerError, match="timed out"):
        list_connected_apps(CodexAppServerClient(transport=_AppListTransport(None)))


def test_a_codex_that_does_not_come_up_is_reported_as_no_list(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The daemon is asked with apps on for that one launch, whatever the account folder's config says."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    seen = tmp_path / "arguments.txt"
    stub = bin_dir / "codex"
    stub.write_text(f'#!/bin/sh\necho "$@" > "{seen}"\necho "not signed in" >&2\nexit 1\n')
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    with pytest.raises(CloudConnectionsError, match="not signed in"):
        CodexCloudConnections().list_connected(tmp_path)

    assert seen.read_text().startswith("-c features.apps=true app-server --listen unix://")


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        ("[features]\napps = false\nmemories = false\n", False),
        ("[features]\napps = true\n", True),
        ("[features]\nmemories = false\n", True),
        ('model = "gpt-6.1-sol"\n', True),
    ],
)
def test_an_agent_loads_the_apps_unless_its_config_turns_them_off(tmp_path: Path, config: str, expected: bool) -> None:
    assert CodexCloudConnections().is_enabled(_agent_config(tmp_path, config)) is expected


def test_enabling_flips_the_one_flag_and_keeps_the_rest_of_the_config(tmp_path: Path) -> None:
    agent_state_dir = _agent_config(
        tmp_path, 'model = "gpt-6.1-sol"\n\n[features]\nmemories = false\napps = false\nunified_exec_tty = false\n'
    )
    connections = CodexCloudConnections()

    connections.enable(agent_state_dir)

    assert connections.is_enabled(agent_state_dir)
    config = tomlkit.parse(get_codex_config_path(get_codex_home(agent_state_dir)).read_text()).unwrap()
    assert config == {
        "model": "gpt-6.1-sol",
        "features": {"memories": False, "apps": True, "unified_exec_tty": False},
    }


def test_enabling_adds_the_flag_to_a_config_with_no_features_table(tmp_path: Path) -> None:
    agent_state_dir = _agent_config(tmp_path, 'model = "gpt-6.1-sol"\n')

    CodexCloudConnections().enable(agent_state_dir)

    config = tomlkit.parse(get_codex_config_path(get_codex_home(agent_state_dir)).read_text()).unwrap()
    assert config == {"model": "gpt-6.1-sol", "features": {"apps": True}}


def test_an_agent_with_no_config_cannot_be_read_or_switched(tmp_path: Path) -> None:
    connections = CodexCloudConnections()
    with pytest.raises(CloudConnectionsError, match="unreadable"):
        connections.is_enabled(tmp_path)
    with pytest.raises(CloudConnectionsError, match="unreadable"):
        connections.enable(tmp_path)
