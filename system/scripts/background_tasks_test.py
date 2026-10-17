"""Tests for background_tasks.py: the CLI that reads the markers through the chat app or, failing
that, the files."""

from __future__ import annotations

import json
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from chat_background_tasks import markers
from chat_background_tasks.listing import AGENTS_PATH
from chat_background_tasks.testing import runner_task

_CHAT_ID = "agent-0123456789abcdef0123456789abcdef"
_OTHER_CHAT_ID = "agent-fedcba9876543210fedcba9876543210"
_SCRIPT = Path(__file__).parent / "background_tasks.py"


def _run_cli(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_SCRIPT), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def _agents_answer(*agents: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    return 200, {"agents": list(agents)}


def test_list_answers_from_the_chat_app_with_agent_names(
    fake_chat_app: Any, background_task_markers: Path, tmp_path: Path
) -> None:
    marker = runner_task("from-app", "2026-10-08T10:00:00+00:00").to_json()
    fake_chat_app.get_answers[AGENTS_PATH] = _agents_answer(
        {
            "id": "agent-active",
            "name": "lead",
            "state": "WAITING",
            "chat_id": _CHAT_ID,
            "is_busy": True,
            "background_tasks": [marker],
        },
        {
            "id": "agent-idle",
            "name": "other",
            "state": "WAITING",
            "chat_id": "agent-idle",
            "is_busy": False,
            "background_tasks": [],
        },
    )
    # The files disagree; the chat app is the authority while it answers.
    markers.write_marker(
        background_task_markers,
        _OTHER_CHAT_ID,
        runner_task("on-disk", "2026-10-08T10:00:00+00:00"),
    )

    listed = _run_cli("list", "--format", "tsv", cwd=tmp_path)

    assert listed.returncode == 0, listed.stderr
    assert listed.stdout.splitlines() == [f"{_CHAT_ID}\tagent-active\tlead\ttrue\t1"]


def test_is_busy_counts_a_turn_in_flight_the_chat_app_reports(
    fake_chat_app: Any, tmp_path: Path
) -> None:
    fake_chat_app.get_answers[AGENTS_PATH] = _agents_answer(
        {
            "id": _CHAT_ID,
            "name": "lead",
            "state": "RUNNING",
            "chat_id": _CHAT_ID,
            "is_busy": True,
            "background_tasks": [],
        },
    )

    assert _run_cli("is-busy", _CHAT_ID, cwd=tmp_path).returncode == 0
    assert _run_cli("is-busy", _OTHER_CHAT_ID, cwd=tmp_path).returncode == 1


def test_a_chat_app_from_before_the_busy_fields_falls_back_to_the_files(
    fake_chat_app: Any, background_task_markers: Path, tmp_path: Path
) -> None:
    fake_chat_app.get_answers[AGENTS_PATH] = _agents_answer(
        {"id": _CHAT_ID, "name": "lead", "state": "WAITING"}
    )
    markers.write_marker(
        background_task_markers,
        _CHAT_ID,
        runner_task("on-disk", "2026-10-08T10:00:00+00:00"),
    )

    listed = _run_cli("list", cwd=tmp_path)

    assert listed.returncode == 0, listed.stderr
    answer = json.loads(listed.stdout)
    assert answer["source"] == "files"
    [chat] = answer["chats"]
    assert (chat["chat_id"], chat["active_agent_id"], chat["active_agent_name"]) == (
        _CHAT_ID,
        _CHAT_ID,
        "",
    )
    assert [task["id"] for task in chat["background_tasks"]] == ["on-disk"]
    assert _run_cli("is-busy", _CHAT_ID, cwd=tmp_path).returncode == 0


def test_an_unreachable_chat_app_falls_back_to_the_files(
    background_task_markers: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = tmp_path / "apps.toml"
    # A port nothing listens on: bind one, then let it go.
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    registry.write_text(f'[[apps]]\nname = "chat"\nurl = "http://127.0.0.1:{port}"\n')
    monkeypatch.setenv("MINDS_APPS_FILE", str(registry))
    markers.write_marker(
        background_task_markers,
        _CHAT_ID,
        runner_task("on-disk", "2026-10-08T10:00:00+00:00"),
    )

    listed = _run_cli("list", "--chat", _CHAT_ID, "--format", "tsv", cwd=tmp_path)

    assert listed.stdout.splitlines() == [f"{_CHAT_ID}\t{_CHAT_ID}\t\ttrue\t1"]
    assert _run_cli("is-busy", _OTHER_CHAT_ID, cwd=tmp_path).returncode == 1
