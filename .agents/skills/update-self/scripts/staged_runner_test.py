"""update-self's copy of ``run_in_background.py``, run from where update-self stages it.

The lead waits for the update's worker through the staged skill's copy of the runner (SKILL.md
Step 3), in a workspace that may be as old as the oldest release the app updates from
(``minds-v0.3.17``). The runner delivers through whichever messenger that workspace's own
tree has: its ``system/scripts/message_chat.py`` (from ``minds-v0.6.1``), else ``mngr
message``. What it asks of each is held here to the oldest release that has it, as
``launcher_contract_test.py`` holds the launcher.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from messenger_testing import RecordingMessengers

_AGENT_ID = "agent-0123456789abcdef0123456789abcdef"
_RUNNER = Path(__file__).with_name("run_in_background.py")
# Where ``bootstrap-skill`` lays the target's skill down, relative to the workspace.
_STAGED_SCRIPTS_REL = (
    Path("data")
    / ".tasks"
    / "update-self"
    / "skill-at-target"
    / ".agents"
    / "skills"
    / "update-self"
    / "scripts"
)
_DELIVERY_DEADLINE_SECONDS = 8.0

# The options each messenger accepts at the oldest release that has it, besides the
# positional chat or agent id.
_MESSENGER_OPTIONS_AT_FLOOR: dict[str, frozenset[str]] = {
    # system/scripts/message_chat.py at minds-v0.6.1, the first release that ships it.
    "message_chat.py": frozenset({"-m", "--message", "--message-file", "--system"}),
    # `mngr message` at minds-v0.3.17 (the mngr vendored there).
    "mngr": frozenset(
        {
            "--agent",
            "-m",
            "--message",
            "--message-file",
            "--on-error",
            "--start",
            "--no-start",
        }
    ),
}


def _stage_runner(workspace: Path) -> Path:
    staged = workspace / _STAGED_SCRIPTS_REL / _RUNNER.name
    staged.parent.mkdir(parents=True)
    shutil.copy2(_RUNNER, staged)
    return staged


@pytest.mark.parametrize(
    "is_message_chat_in_tree, messenger",
    [(True, "message_chat.py"), (False, "mngr")],
)
def test_the_staged_runner_delivers_through_the_messenger_of_the_workspace_it_runs_in(
    tmp_path: Path,
    recording_messengers: RecordingMessengers,
    is_message_chat_in_tree: bool,
    messenger: str,
) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "system" / "scripts").mkdir(parents=True)
    if is_message_chat_in_tree:
        recording_messengers.install_message_chat(workspace)
    staged = _stage_runner(workspace)
    env = {key: value for key, value in os.environ.items() if key != "MINDS_CHAT_ID"}
    env["MNGR_AGENT_ID"] = _AGENT_ID

    started = subprocess.run(
        [
            sys.executable,
            str(staged),
            "--description",
            "Wait for the update's background agent",
            "--",
            sys.executable,
            "-c",
            "print('the worker reported')",
        ],
        cwd=workspace,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert started.returncode == 0, started.stderr

    deadline = time.monotonic() + _DELIVERY_DEADLINE_SECONDS
    while not recording_messengers.calls():
        assert time.monotonic() < deadline, "the staged runner never sent its report"
        time.sleep(0.1)
    [call] = recording_messengers.calls()
    assert call["messenger"] == messenger
    argv = call["argv"]
    file_index = argv.index("--message-file")
    words = argv[: file_index + 1] + argv[file_index + 2 :]
    positionals = [word for word in words if not word.startswith("-")]
    assert positionals == (
        ["message", _AGENT_ID] if messenger == "mngr" else [_AGENT_ID]
    )
    options = {word for word in words if word.startswith("-")}
    unsupported = sorted(options - _MESSENGER_OPTIONS_AT_FLOOR[messenger])
    assert not unsupported, (
        f"the runner passes {messenger} {unsupported}, which its oldest release does not accept"
    )
    assert (
        "<summary>Wait for the update's background agent (finished)</summary>"
        in call["text"]
    )
    assert "the worker reported" in call["text"]
