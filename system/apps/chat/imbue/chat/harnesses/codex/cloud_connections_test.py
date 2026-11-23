"""Codex's cloud connections: the ``app/list`` exchange over a scripted transport, the probe's failure
against a stub ``codex`` on PATH."""

import json
import os
from collections import deque
from pathlib import Path
from typing import Any

import pytest

from imbue.chat.harnesses.cloud_connections import CloudConnectionsError
from imbue.chat.harnesses.cloud_connections import CloudConnector
from imbue.chat.harnesses.codex.cloud_connections import CodexCloudConnections
from imbue.chat.harnesses.codex.cloud_connections import list_connected_apps
from imbue.mngr_codex.app_server_client import CodexAppServerClient
from imbue.mngr_codex.app_server_client import CodexAppServerError


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


def test_only_the_apps_connected_on_the_account_are_named(tmp_path: Path) -> None:
    """The list is the whole directory; the account's own apps are the ones codex marks accessible."""
    apps = [_app("Gmail", True), _app("Slack", True), _app("Gmail", True), _app("Zillow", False)]
    transport = _AppListTransport({"result": {"data": apps, "nextCursor": "200"}})

    connectors = list_connected_apps(CodexAppServerClient(transport=transport))

    assert connectors == (CloudConnector(name="Gmail"), CloudConnector(name="Slack"))
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
