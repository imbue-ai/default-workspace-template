"""Claude's account binding: the `CLAUDE_CONFIG_DIR` line of the agent's env file, and the sessions filed under it."""

import os
from collections.abc import Callable
from pathlib import Path
from typing import Final

from loguru import logger as _loguru_logger

from imbue.chat.agent_discovery import AgentInfo
from imbue.chat.harnesses.account_binding import AccountBinding
from imbue.chat.harnesses.account_binding import DefaultCreateBinding
from imbue.chat.harnesses.claude.session_files import claude_session_ids
from imbue.chat.harnesses.claude.session_files import move_claude_sessions
from imbue.mngr_claude.claude_config import auto_dismiss_claude_dialogs
from imbue.mngr_claude.claude_config import ensure_chat_cancel_tap_keybinding

logger = _loguru_logger

_CONFIG_DIR_ENV_VAR: Final = "CLAUDE_CONFIG_DIR"

# mngr's per-agent env file, sourced into the agent's tmux session on every start.
AGENT_ENV_FILENAME: Final = "env"


class ClaudeAccountBinding(AccountBinding):
    """Binds claude by the config dir it runs with, which is also where it files the chat's sessions."""

    def account_env(self, account_dir: Path) -> dict[str, str]:
        return {_CONFIG_DIR_ENV_VAR: str(account_dir)}

    def account_credential_path(self, account_dir: Path) -> None:
        # Its credential is the `env` block of the account's settings.json plus whatever the CLI
        # writes beside it, and it binds by environment rather than by symlink.
        return None

    def seed_account(self, account_dir: Path, work_dir: Path) -> None:
        super().seed_account(account_dir, work_dir)
        # With CLAUDE_CONFIG_DIR set, claude reads its global config from INSIDE the dir
        # rather than from ~/.claude.json beside it, so a fresh account folder starts with
        # no onboarding state at all and boots into the theme/trust dialogs.
        auto_dismiss_claude_dialogs(account_dir / ".claude.json", work_dir)
        # Same story for the meta+q interrupt chord: mngr writes it into the shared dir, so
        # a pinned agent would silently lose its native stop and fall back to a kill.
        ensure_chat_cancel_tap_keybinding(account_dir / "keybindings.json")

    def create_args(self, account_dir: Path, agent_state_dir: Path) -> list[str]:
        # This export is load-bearing beyond the agent itself. mngr sources an agent's env
        # file into every process in its tmux session, and propagates CLAUDE_CONFIG_DIR to a
        # child agent when the spawning shell already has it -- so a worker created from
        # inside this chat (`/launch-task`) runs on this same account, and so does any skill
        # script that shells claude. Binding claude some other way would silently sign every
        # worker out. See `binding_test.py`.
        return ["--env", f"{_CONFIG_DIR_ENV_VAR}={account_dir}"]

    def default_create_binding(self, account_dir: Path) -> DefaultCreateBinding:
        return DefaultCreateBinding(
            env=tuple(f"{name}={value}" for name, value in self.account_env(account_dir).items()),
            provision_commands=(),
        )

    def rebind_agent(self, account_dir: Path, agent_state_dir: Path) -> None:
        # mngr sources the env file on every start and never rewrites it.
        _rewrite_env_var(agent_state_dir / AGENT_ENV_FILENAME, _CONFIG_DIR_ENV_VAR, str(account_dir))

    def move_sessions(
        self,
        agent_info: AgentInfo,
        recorded_sessions_dir: Path | None,
        target_account_dir: Path,
        record_sessions_dir: Callable[[Path], None],
    ) -> None:
        """Carry the agent's sessions to the new account's tree (``session_files``), recording where they are.

        The files are looked for under the dir recorded for the rebind (the one the agent ran
        under before, read off the env file before the env line first changes) and under the
        env file's current dir: a resume can find the env rewritten before the move ran, and a
        retry on another account finds the files under the account the failed attempt moved them
        to. The target is recorded once they have moved, so the next attempt looks there.
        """
        recorded = recorded_sessions_dir
        if recorded is None:
            recorded = agent_info.claude_config_dir
            record_sessions_dir(recorded)
        session_ids = claude_session_ids(agent_info.agent_state_dir, agent_info.id)
        for source_dir in dict.fromkeys((recorded, agent_info.claude_config_dir)):
            moved = move_claude_sessions(session_ids, source_dir, target_account_dir)
            if moved:
                logger.info(
                    "Moved {} session file(s) of agent {} to {}", len(moved), agent_info.id, target_account_dir
                )
        if recorded != target_account_dir:
            record_sessions_dir(target_account_dir)


def _rewrite_env_var(env_path: Path, key: str, value: str) -> None:
    """Set one ``KEY=VALUE`` line in an env file mngr wrote, keeping every other line as it is."""
    lines = env_path.read_text().splitlines() if env_path.exists() else []
    kept = [line for line in lines if not line.startswith(f"{key}=")]
    # mngr's own quoting rule for the values it writes.
    is_quoted = any(character in value for character in (" ", '"', "'", "\n"))
    written = '"' + value.replace('"', '\\"') + '"' if is_quoted else value
    temp_path = env_path.with_name(f"{env_path.name}.rebind-tmp")
    temp_path.write_text("\n".join([*kept, f"{key}={written}"]) + "\n")
    if env_path.exists():
        temp_path.chmod(env_path.stat().st_mode & 0o777)
    os.replace(temp_path, env_path)
