"""Exercise native plugin discovery before the first turn, without model/API calls.

Requires Codex 0.154.0+ and the released guardian marketplace. For development,
CODE_GUARDIAN_TEST_REF can select an unreleased code-guardian branch.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import tomllib
from pathlib import Path
from typing import Any

import pytest
import tomlkit
from imbue.mngr_codex.codex_config import get_codex_home

_ROOT = Path(__file__).resolve().parents[2]
_PLUGIN = "imbue-code-guardian@imbue-code-guardian"


def _rpc(
    process: subprocess.Popen[str], request_id: int, method: str, params: dict[str, Any]
) -> dict[str, Any]:
    assert process.stdin is not None and process.stdout is not None
    process.stdin.write(
        json.dumps({"id": request_id, "method": method, "params": params}) + "\n"
    )
    process.stdin.flush()
    for line in process.stdout:
        response = json.loads(line)
        if response.get("id") == request_id:
            assert "error" not in response, response
            return response["result"]
    pytest.fail(f"Codex exited before answering {method}")


@pytest.mark.acceptance
@pytest.mark.timeout(120)
def test_first_session_and_reprovision_load_native_reviews(tmp_path: Path) -> None:
    if shutil.which("codex") is None:
        pytest.skip("Codex CLI is not installed")
    state = tmp_path / "agent state"
    home = get_codex_home(state)
    home.mkdir(parents=True)
    shared = tmp_path / "account home"
    shared.mkdir()
    (shared / "config.toml").write_text("# account config\n")
    env = {**os.environ, "MNGR_AGENT_STATE_DIR": str(state), "CODEX_HOME": str(shared)}
    agent_config = tomllib.loads((_ROOT / ".mngr/settings.toml").read_text())[
        "agent_types"
    ]["codex"]
    provision_commands = agent_config["extra_provision_command__extend"]
    installer = next(
        command for command in provision_commands if "codex_update_plugin.sh" in command
    )
    for _ in range(2):
        # Model mngr's rewrite from the actual agent-type settings, retaining
        # caches. An inherited account home must never be modified.
        overrides = tomllib.loads((_ROOT / ".mngr/settings.toml").read_text())[
            "agent_types"
        ]["codex"]["config_overrides"]
        if test_ref := os.environ.get("CODE_GUARDIAN_TEST_REF"):
            overrides["marketplaces"]["imbue-code-guardian"]["ref"] = test_ref
        (home / "config.toml").write_text(tomlkit.dumps(overrides))
        result = subprocess.run(
            [*shlex.split(installer), "--strict"],
            cwd=_ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
            timeout=45,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert (shared / "config.toml").read_text() == "# account config\n"
        assert not (shared / "plugins").exists()
        with (
            (tmp_path / "server.log").open("w") as log,
            subprocess.Popen(
                # Same hook flags as mngr's consent-gated daemon launch.
                [
                    "codex",
                    "--enable",
                    "hooks",
                    "--dangerously-bypass-hook-trust",
                    "app-server",
                ],
                cwd=tmp_path,
                env={**env, "CODEX_HOME": str(home)},
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=log,
                text=True,
            ) as process,
        ):
            try:
                _rpc(
                    process,
                    1,
                    "initialize",
                    {
                        "clientInfo": {"name": "dwt-plugin-test", "version": "1"},
                        "capabilities": {"experimentalApi": True},
                    },
                )
                assert process.stdin is not None
                process.stdin.write('{"method":"initialized"}\n')
                process.stdin.flush()
                skills = _rpc(process, 2, "skills/list", {"cwds": [str(tmp_path)]})[
                    "data"
                ][0]
                assert not skills["errors"]
                guardian_skills = [
                    skill
                    for skill in skills["skills"]
                    if "imbue-code-guardian" in skill["path"]
                ]
                assert {
                    f"imbue-code-guardian:{name}"
                    for name in (
                        "autofix",
                        "verify-architecture",
                        "verify-conversation",
                    )
                } <= {skill["name"] for skill in guardian_skills}
                hooks = _rpc(process, 3, "hooks/list", {"cwds": [str(tmp_path)]})[
                    "data"
                ][0]
                assert not hooks["errors"]
                guardian_hooks = [
                    hook for hook in hooks["hooks"] if hook.get("pluginId") == _PLUGIN
                ]
                assert len(guardian_hooks) == 1
                assert guardian_hooks[0]["eventName"] == "stop"
                assert guardian_hooks[0]["enabled"]
                assert "stop_hook_entrypoint.sh" in guardian_hooks[0]["command"]
            finally:
                process.terminate()
