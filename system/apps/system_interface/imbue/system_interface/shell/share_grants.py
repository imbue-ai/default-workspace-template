"""The two grants documents the minds desktop and the share gateway write while the workspace is shared, read here
for the roster (who may be here) and for the no-window rule (which apps a visitor may reach on their own origin).

``data/.secrets/share_grants.toml`` names the people: one ``[workspace]`` table and one ``[services.<name>]`` table
per app the owner shares on its own, each with ``users``, ``emails``, and ``email_domains``; the gateway admits a
visitor holding only a per-app grant to that app's origin and nothing else, so no window of the app exists anywhere
while they use it, and the no-window rule (the stop-when-no-windows spec, section 6.1) treats such an app as one that
has windows, unless the app is internal. ``data/.secrets/agent_access/grants.toml`` names the visiting agents. Both
are parsed by the ``share_grants`` library the gateway parses them with, so a document the gateway would refuse
admits nobody there and grants nothing here.
"""

from collections.abc import Callable
from collections.abc import Set as AbstractSet
from datetime import datetime
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr
from share_grants.agent_grants import AGENT_GRANTS_FILE
from share_grants.agent_grants import AgentGrant
from share_grants.agent_grants import AgentGrantsError
from share_grants.agent_grants import load_active_agent_grants
from share_grants.grants import Grants
from share_grants.grants import GrantsError
from share_grants.grants import parse_grants

from imbue.imbue_common.mutable_model import MutableModel

# Beside ``DEFAULT_SHARE_ENV_PATH`` (``profiles.py``): relative to the workspace root the supervised process runs from.
DEFAULT_SHARE_GRANTS_PATH: Final[Path] = Path("data/.secrets/share_grants.toml")
DEFAULT_AGENT_GRANTS_PATH: Final[Path] = AGENT_GRANTS_FILE

# The names of every app with a per-app grant, read once per sweep pass.
GrantedAppsReader = Callable[[], AbstractSet[str]]


class ShareGrantsReader(MutableModel):
    """Reads the people's grants document the minds desktop writes while sharing, warning once per version of a
    document the gateway would refuse."""

    model_config = {"extra": "forbid", "frozen": False}

    share_env_path: Path = Field(
        frozen=True, description="The share materials; absent while the workspace is not shared"
    )
    share_grants_path: Path = Field(frozen=True, description="The grants document beside them")

    _warned_grants_mtime_ns: int | None = PrivateAttr(default=None)

    def read_grants(self) -> Grants | None:
        """The document, or None while the workspace is not shared, while the document is missing (the desktop
        writes it before the materials), or while it is one the gateway refuses, which is warned about once per
        version of the file."""
        if not self.share_env_path.exists():
            return None
        try:
            grants_mtime_ns = self.share_grants_path.stat().st_mtime_ns
        except FileNotFoundError:
            return None
        except OSError as e:
            logger.warning("Could not stat the share grants at {}: {}", self.share_grants_path, e)
            return None
        try:
            grants_text = self.share_grants_path.read_text(encoding="utf-8")
        except OSError as e:
            self._warn_once(grants_mtime_ns, f"Could not read the share grants at {self.share_grants_path}: {e}")
            return None
        try:
            grants = parse_grants(grants_text)
        except GrantsError as e:
            self._warn_once(grants_mtime_ns, f"Ignored the share grants at {self.share_grants_path}: {e}")
            return None
        self._warned_grants_mtime_ns = None
        return grants

    def granted_app_names(self) -> AbstractSet[str]:
        """The apps with a per-app grant; none while the document cannot be read."""
        grants = self.read_grants()
        return grants.granted_app_names() if grants is not None else set()

    def _warn_once(self, grants_mtime_ns: int, message: str) -> None:
        """Warn about a version of the document once, not on every read until the owner rewrites it."""
        if self._warned_grants_mtime_ns == grants_mtime_ns:
            return
        self._warned_grants_mtime_ns = grants_mtime_ns
        logger.warning("{}; nobody counts as granted until the file is rewritten", message)


class AgentGrantsReader(MutableModel):
    """Reads the visiting agents' grants the share gateway keeps, warning once per version of an unreadable file."""

    model_config = {"extra": "forbid", "frozen": False}

    agent_grants_path: Path = Field(frozen=True, description="The gateway's agent grants document")

    _warned_grants_mtime_ns: int | None = PrivateAttr(default=None)

    def read_active_grants(self, now: datetime) -> list[AgentGrant]:
        """Every unexpired grant, in file order; none while the file is absent or one the gateway refuses."""
        try:
            grants_mtime_ns = self.agent_grants_path.stat().st_mtime_ns
        except FileNotFoundError:
            return []
        except OSError as e:
            logger.warning("Could not stat the agent grants at {}: {}", self.agent_grants_path, e)
            return []
        try:
            grants = load_active_agent_grants(self.agent_grants_path, now)
        except AgentGrantsError as e:
            if self._warned_grants_mtime_ns != grants_mtime_ns:
                self._warned_grants_mtime_ns = grants_mtime_ns
                logger.warning("Ignored the agent grants at {}: {}", self.agent_grants_path, e)
            return []
        self._warned_grants_mtime_ns = None
        return grants
