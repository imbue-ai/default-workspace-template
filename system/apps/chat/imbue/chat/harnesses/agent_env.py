"""The env files mngr sources into an agent's tmux session on every start: the host's, then the agent's.

mngr writes each once (the host's when the host is created, the agent's at its create) and never
rewrites either, so an edit here holds from the next start on: of that agent for its own file, of
every agent on the host for the host's.

Which file wins for a variable both set depends on the shell that reads them. A claude agent is
launched from the interactive shell its pane ends in, whose ``~/.bashrc`` sources the host's file
again (``system/scripts/seed_home_skeleton.sh``), so the host's value wins there. The codex daemon
is launched from a shell that sources the two and nothing else, so the agent's wins there.
"""

import os
from pathlib import Path
from typing import Final

from imbue.mngr.utils.env_utils import parse_env_file

# mngr's per-agent env file, and the host's beside the agents.
AGENT_ENV_FILENAME: Final = "env"
_HOST_ENV_FILENAME: Final = "env"
# mngr writes the host dir into every agent's env file, which is how the host's file is found from the agent's.
_HOST_DIR_ENV_VAR: Final = "MNGR_HOST_DIR"


def read_env_file(env_path: Path) -> dict[str, str]:
    """The variables an env file sets; none for a file that does not exist."""
    if not env_path.exists():
        return {}
    return parse_env_file(env_path.read_text())


def host_env_path_of(agent_state_dir: Path) -> Path | None:
    """The env file of the host the agent runs on, or None for an agent whose env file names no host."""
    host_dir = read_env_file(agent_state_dir / AGENT_ENV_FILENAME).get(_HOST_DIR_ENV_VAR)
    return Path(host_dir) / _HOST_ENV_FILENAME if host_dir else None


def rewrite_env_var(env_path: Path, key: str, value: str) -> None:
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


def rewrite_agent_env_var(agent_state_dir: Path, key: str, value: str) -> None:
    """Set one ``KEY=VALUE`` line in the agent's own env file."""
    rewrite_env_var(agent_state_dir / AGENT_ENV_FILENAME, key, value)
