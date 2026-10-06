"""Tests for the revival-notice hook, run as claude runs it: a bare ``python3``
subprocess in the agent's environment."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from oom_priority.ledger import append_shed_record, has_pending_shed

_SCRIPT = Path(__file__).parent / "claude_shed_notice_hook.py"
_NOTICE_FRAGMENT = "memory-pressure"


def _write_agent_labels(host_dir: Path, name: str, labels: dict[str, str]) -> None:
    agent_dir = host_dir / "agents" / "agent-id"
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "data.json").write_text(json.dumps({"name": name, "labels": labels}))


def _run_hook(tmp_path: Path, agent_name: str) -> str:
    env = dict(
        os.environ,
        MNGR_AGENT_NAME=agent_name,
        MNGR_HOST_DIR=str(tmp_path / "host"),
        OOM_PRIORITY_RUNTIME_DIR=str(tmp_path / "runtime"),
    )
    completed = subprocess.run(
        [sys.executable, str(_SCRIPT)],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout


@pytest.fixture
def runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("OOM_PRIORITY_RUNTIME_DIR", str(tmp_path / "runtime"))
    return tmp_path / "runtime"


def test_a_revived_chat_is_told_once_that_it_was_shed(
    tmp_path: Path, runtime: Path
) -> None:
    _write_agent_labels(tmp_path / "host", "Chat-2", {"user_created": "true"})
    append_shed_record(pid=4242, comm="claude", agent_name="Chat-2", is_worker=False)

    assert _NOTICE_FRAGMENT in _run_hook(tmp_path, "Chat-2")
    assert _run_hook(tmp_path, "Chat-2") == ""


def test_a_spare_started_under_a_shed_agents_name_is_not_told(
    tmp_path: Path, runtime: Path
) -> None:
    """The chat app replaces a shed spare with a new one that often reuses its
    name; the record is the old agent's, so it is acknowledged unsaid. Once a
    chat takes the spare and relabels it, a shed of its own is reported."""
    labels = {"user_created": "true", "chat_spare": "true"}
    _write_agent_labels(tmp_path / "host", "Chat-3", labels)
    append_shed_record(pid=4243, comm="claude", agent_name="Chat-3", is_worker=False)

    assert _run_hook(tmp_path, "Chat-3") == ""
    assert not has_pending_shed("Chat-3")

    _write_agent_labels(tmp_path / "host", "Chat-3", {**labels, "chat_spare": "false"})
    append_shed_record(pid=4244, comm="claude", agent_name="Chat-3", is_worker=False)

    assert _NOTICE_FRAGMENT in _run_hook(tmp_path, "Chat-3")
