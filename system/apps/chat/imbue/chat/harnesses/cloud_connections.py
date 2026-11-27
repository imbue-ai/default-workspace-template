"""A provider account's cloud connections: the third-party connectors kept on the provider's own side.

Claude (claude.ai connectors) and Codex (ChatGPT apps) can each load the connectors a user set up
on their provider account, as MCP servers the provider hosts. The workspace turns that off for
every agent, because those connections reach third-party services without going through the
latchkey gateway and the permissions the user grants there.

One line of the host's env file is the switch, for every agent of both harnesses:
``ENABLE_CLAUDEAI_MCP_SERVERS``, which ``.mngr/settings.toml`` writes as ``false`` when the
workspace is created. Claude Code reads the variable itself; codex is started with
``-c features.apps=${ENABLE_CLAUDEAI_MCP_SERVERS:-false}`` (``cli_args`` of ``[agent_types.codex]``).
mngr sources the host's env file at every agent start and never rewrites it, so a value written
here reaches each agent when its process next starts: new chats, restarted ones, and background
agents alike. An agent that was already running keeps what it started with, which is why whether
one agent loads the connectors is read off the switch and the time the agent's process started.

A harness with such connectors registers a ``CloudConnections`` on its ``HarnessSpec``, which also
answers which connectors an account has connected (asked once, when the account signs in): each
by its display name, and by the address of the MCP server behind it where the provider tells it.
"""

from abc import ABC
from abc import abstractmethod
from pathlib import Path
from typing import ClassVar
from typing import Final

from pydantic import Field

from imbue.chat.harnesses.agent_env import host_env_path_of
from imbue.chat.harnesses.agent_env import read_env_file
from imbue.chat.harnesses.agent_env import rewrite_env_var
from imbue.imbue_common.frozen_model import FrozenModel

# Claude Code's own name for it; codex is handed the same variable through its command line.
CLOUD_CONNECTIONS_ENV_VAR: Final = "ENABLE_CLAUDEAI_MCP_SERVERS"
# The values Claude Code reads as "off".
_DISABLING_VALUES: Final = frozenset(("0", "false", "no", "off"))
_ENABLING_VALUE: Final = "true"


class CloudConnector(FrozenModel):
    """One connector an account has connected on its provider's side."""

    name: str = Field(description="The connector's display name, as the provider spells it")
    url: str | None = Field(
        default=None,
        description="The address of the MCP server behind the connector; None when the provider does not tell it",
    )


class CloudConnectionsError(RuntimeError):
    """An account's connectors could not be listed, or the workspace's switch could not be read or set."""


class CloudConnections(ABC):
    """One harness's cloud connections: the account's connector list, and the workspace's switch as its agents see it."""

    # The file the harness's mngr plugin touches in the agent's state dir each time the agent's process starts.
    process_started_marker_filename: ClassVar[str]
    # What the harness does when the host's env file does not set the variable at all.
    is_on_when_unset: ClassVar[bool]

    @abstractmethod
    def list_connected(self, account_dir: Path) -> tuple[CloudConnector, ...]:
        """The connectors the account has connected, one per display name. Raises ``CloudConnectionsError``."""

    def is_enabled(self, agent_state_dir: Path) -> bool:
        """Whether the agent's running process loaded its account's connectors. Raises ``CloudConnectionsError``.

        False for an agent that started before the switch was last written, which a restart brings up to date,
        and for one that has not started at all.
        """
        host_env_path = _host_env_path(agent_state_dir)
        try:
            value = read_env_file(host_env_path).get(CLOUD_CONNECTIONS_ENV_VAR)
            if value is None:
                return self.is_on_when_unset
            if value.strip().lower() in _DISABLING_VALUES:
                return False
            switched_at = host_env_path.stat().st_mtime
            marker_path = agent_state_dir / self.process_started_marker_filename
            return marker_path.exists() and marker_path.stat().st_mtime >= switched_at
        except OSError as e:
            raise CloudConnectionsError(f"the switch in {host_env_path} could not be read: {e}") from e

    def enable(self, agent_state_dir: Path) -> None:
        """Turn the workspace's switch on, for every agent's next start. Raises ``CloudConnectionsError``.

        A switch that is already on is left untouched, so the agents that started since it was turned on keep
        counting as loading the connectors.
        """
        host_env_path = _host_env_path(agent_state_dir)
        try:
            value = read_env_file(host_env_path).get(CLOUD_CONNECTIONS_ENV_VAR)
            if value is not None and value.strip().lower() not in _DISABLING_VALUES:
                return
            rewrite_env_var(host_env_path, CLOUD_CONNECTIONS_ENV_VAR, _ENABLING_VALUE)
        except OSError as e:
            raise CloudConnectionsError(f"the switch in {host_env_path} could not be written: {e}") from e


def _host_env_path(agent_state_dir: Path) -> Path:
    try:
        host_env_path = host_env_path_of(agent_state_dir)
    except OSError as e:
        raise CloudConnectionsError(f"the env file of the agent at {agent_state_dir} is unreadable: {e}") from e
    if host_env_path is None:
        raise CloudConnectionsError(f"the env file of the agent at {agent_state_dir} names no host")
    return host_env_path
