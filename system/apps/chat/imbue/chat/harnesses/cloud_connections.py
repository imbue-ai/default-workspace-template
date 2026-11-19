"""A provider account's cloud connections: the third-party connectors kept on the provider's own side.

Claude (claude.ai connectors) and Codex (ChatGPT apps) can each load the connectors a user set up
on their provider account, as MCP servers the provider hosts. The workspace turns that off for
every agent (``.mngr/settings.toml``), because those connections reach third-party services
without going through the latchkey gateway and the permissions the user grants there.

A harness with such connectors registers a ``CloudConnections`` on its ``HarnessSpec``, which
answers three things: which connectors an account has connected (asked once, when the account
signs in), whether one agent loads them, and how to make it load them. The switch is the agent's
own (its env file or its config), read as it stands and never mirrored anywhere, and takes effect
when the agent's process next starts.
"""

from abc import ABC
from abc import abstractmethod
from pathlib import Path


class CloudConnectionsError(RuntimeError):
    """An account's connectors could not be listed, or an agent's switch could not be read or set."""


class CloudConnections(ABC):
    """One harness's cloud connections: the account's connector list, and each agent's switch."""

    @abstractmethod
    def list_connected(self, account_dir: Path) -> tuple[str, ...]:
        """The display names of the connectors the account has connected. Raises ``CloudConnectionsError``."""

    @abstractmethod
    def is_enabled(self, agent_state_dir: Path) -> bool:
        """Whether the agent loads its account's connectors when it starts. Raises ``CloudConnectionsError``."""

    @abstractmethod
    def enable(self, agent_state_dir: Path) -> None:
        """Make the agent load its account's connectors from its next start. Raises ``CloudConnectionsError``."""
