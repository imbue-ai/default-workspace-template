"""Integration test: a room's real ``mngr create`` runs on no account, whatever the workspace's create defaults stamp.

The account store writes ``[commands.create] label__extend = ["account=<id>"]`` (and the account's environment) to
the workspace's ``settings.local.toml``, which mngr applies to every create; the room's argv and environment
(``ROOM_CREATE_SETTINGS``, ``create_command_env``) clear that layer for the one create, which only the real CLI's
config merge can prove.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from imbue.chat.agent_manager import _build_chat_create_command
from imbue.chat.chat_handoffs import create_command_env
from imbue.chat.harnesses.harness_type import HarnessType
from imbue.chat.primitives import ChatId
from imbue.chat.testing import prepare_isolated_mngr_host_dir

# The room agent type the test's project defines: ``room_agent.py`` needs a workspace, so a sleep stands in; the
# number is unique so nothing else on the machine runs the same command.
_ROOM_AGENT_TYPE_SETTINGS = (
    'is_allowed_in_pytest = true\n\n[agent_types.room]\nparent_type = "command"\ncommand = "sleep 73915"\n'
)
_CREATE_DEFAULTS = (
    'is_allowed_in_pytest = true\n\n[commands.create]\ntype = "claude"\nlabel__extend = ["account=acct-stamped-73915"]\n'
    'env__extend = ["CLAUDE_CONFIG_DIR=/nonexistent/accounts/acct-stamped-73915"]\n'
)


def _mngr_env(host_dir: Path, project_dir: Path) -> dict[str, str]:
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith(("MNGR_AGENT_", "MNGR_PREFIX"))
    }
    environment["MNGR_HOST_DIR"] = str(host_dir)
    environment["MNGR_PROJECT_CONFIG_DIR"] = str(project_dir / ".mngr")
    return environment


@pytest.mark.timeout(120)
def test_a_rooms_create_carries_no_account_label_or_environment_from_the_create_defaults(
    tmp_path: Path, isolated_tmux: None
) -> None:
    if shutil.which("mngr") is None:
        pytest.skip("mngr binary not on PATH")
    host_dir = tmp_path / "host"
    prepare_isolated_mngr_host_dir(host_dir)
    project_dir = tmp_path / "project"
    (project_dir / ".mngr").mkdir(parents=True)
    (project_dir / ".mngr" / "settings.toml").write_text(_ROOM_AGENT_TYPE_SETTINGS)
    (project_dir / ".mngr" / "settings.local.toml").write_text(_CREATE_DEFAULTS)
    subprocess.run(["git", "init", "-q", str(project_dir)], check=True, timeout=30)
    environment = _mngr_env(host_dir, project_dir)
    chat_id = ChatId("agent-00000000000000000000000000073915")
    argv = [
        *_build_chat_create_command("mngr", "Lounge", chat_id, str(chat_id), {}, HarnessType.ROOM),
        "--no-ensure-clean",
    ]

    created = subprocess.run(
        argv,
        cwd=project_dir,
        env=create_command_env(HarnessType.ROOM, environment),
        capture_output=True,
        text=True,
        timeout=90,
    )
    try:
        assert created.returncode == 0, created.stderr
        record = json.loads((host_dir / "agents" / str(chat_id) / "data.json").read_text())
        env_file = (host_dir / "agents" / str(chat_id) / "env").read_text()
    finally:
        subprocess.run(
            ["mngr", "destroy", str(chat_id), "--force"],
            cwd=project_dir,
            env=environment,
            capture_output=True,
            timeout=60,
        )

    assert "account" not in record["labels"]
    assert record["labels"]["display_name"] == "Lounge" and record["labels"]["user_created"] == "true"
    assert "CLAUDE_CONFIG_DIR" not in env_file
    assert f"MINDS_CHAT_ID={chat_id}" in env_file
