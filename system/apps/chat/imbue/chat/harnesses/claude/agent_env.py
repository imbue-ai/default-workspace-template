"""The env files mngr sources into a claude agent's tmux session on every start: the host's, then the agent's.

mngr writes each once (the host's when the host is created, the agent's at its create) and never
rewrites either, so an edit here holds from the agent's next start on, and the agent's file wins
over the host's for a variable both set.
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


def _read_env_file(env_path: Path) -> dict[str, str]:
    """The variables an env file sets; none for a file that does not exist."""
    if not env_path.exists():
        return {}
    return parse_env_file(env_path.read_text())


def read_agent_env_var(agent_state_dir: Path, key: str) -> str | None:
    """What a variable is when the agent starts: the agent's own value, else the host's, else None."""
    agent_env = _read_env_file(agent_state_dir / AGENT_ENV_FILENAME)
    if key in agent_env:
        return agent_env[key]
    host_dir = agent_env.get(_HOST_DIR_ENV_VAR)
    if host_dir is None:
        return None
    return _read_env_file(Path(host_dir) / _HOST_ENV_FILENAME).get(key)


def rewrite_agent_env_var(agent_state_dir: Path, key: str, value: str) -> None:
    """Set one ``KEY=VALUE`` line in the agent's env file, keeping every other line as it is."""
    env_path = agent_state_dir / AGENT_ENV_FILENAME
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
