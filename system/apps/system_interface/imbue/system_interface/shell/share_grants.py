"""The two grants documents the minds desktop and the share gateway write while the workspace is shared, read here
for the roster (who may be here) and for the no-window rule (which apps a visitor may reach on their own origin), and
the identities sidecar the desktop writes beside the people's grants.

``data/.secrets/share_grants.toml`` names the people: one ``[workspace]`` table and one ``[services.<name>]`` table
per app the owner shares on its own, each with ``users``, ``emails``, and ``email_domains``; the gateway admits a
visitor holding only a per-app grant to that app's origin and nothing else, so no window of the app exists anywhere
while they use it, and the no-window rule (the stop-when-no-windows spec, section 6.1) treats such an app as one that
has windows, unless the app is internal. ``data/.secrets/agent_access/grants.toml`` names the visiting agents. Both
are parsed by the ``share_grants`` library the gateway parses them with, so a document the gateway would refuse
admits nobody there and grants nothing here.

``data/.secrets/share_identities.json`` is what the desktop knew of each account it granted when it last saved the
grants (``{"version": 1, "identities": {"<user_id>": {"email", "display_name"}}}``): the grants file holds ids alone,
and a granted account that has never arrived has no presence record and no arrival record to be named from, so the
roster falls back to this. The desktop is its only writer; the shell never rewrites it.
"""

from collections.abc import Callable
from collections.abc import Set as AbstractSet
from datetime import datetime
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr
from pydantic import ValidationError
from share_grants.agent_grants import AGENT_GRANTS_FILE
from share_grants.agent_grants import AgentGrant
from share_grants.agent_grants import AgentGrantsError
from share_grants.agent_grants import load_active_agent_grants
from share_grants.grants import Grants
from share_grants.grants import GrantsError
from share_grants.grants import parse_grants

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.system_interface.shell.data_types import GrantedIdentity
from imbue.system_interface.shell.state_files import read_json_object

# Beside ``DEFAULT_SHARE_ENV_PATH`` (``profiles.py``): relative to the workspace root the supervised process runs from.
DEFAULT_SHARE_GRANTS_PATH: Final[Path] = Path("data/.secrets/share_grants.toml")
DEFAULT_SHARE_IDENTITIES_PATH: Final[Path] = Path("data/.secrets/share_identities.json")
DEFAULT_AGENT_GRANTS_PATH: Final[Path] = AGENT_GRANTS_FILE
SHARE_IDENTITIES_FILE_VERSION: Final[int] = 1

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

    def document_version(self) -> int | None:
        """What version of the document is on disk (its mtime), so a reader can say something about it once per
        version; None while there is none."""
        return _file_version(self.share_grants_path)

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

    def document_version(self) -> int | None:
        """What version of the document is on disk (its mtime); None while there is none."""
        return _file_version(self.agent_grants_path)

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


class _ShareIdentitiesDocument(FrozenModel):
    """The whole of ``share_identities.json`` as the desktop writes it."""

    model_config = ConfigDict(extra="ignore")

    version: int = Field(description="The file format version")
    identities: dict[str, GrantedIdentity] = Field(description="What the desktop knew of each granted account, by id")


class ShareIdentitiesReader(MutableModel):
    """Reads the identities sidecar the minds desktop writes beside the grants, warning once per version of a file
    it cannot read."""

    model_config = {"extra": "forbid", "frozen": False}

    share_identities_path: Path = Field(frozen=True, description="The sidecar beside the people's grants")

    _warned_version: int | None = PrivateAttr(default=None)

    def document_version(self) -> int | None:
        """What version of the file is on disk (its mtime); None while there is none."""
        return _file_version(self.share_identities_path)

    def read_identities(self) -> dict[str, GrantedIdentity]:
        """What the desktop recorded of each granted account, by user id; nothing while the file is absent, or
        while it is one this shell cannot read, which is warned about once per version of the file."""
        version = self.document_version()
        if version is None:
            return {}
        raw = read_json_object(self.share_identities_path)
        if raw is None:
            return {}
        try:
            document = _ShareIdentitiesDocument.model_validate(raw)
        except ValidationError as e:
            self._warn_once(version, f"Ignored the share identities at {self.share_identities_path}: {e}")
            return {}
        if document.version != SHARE_IDENTITIES_FILE_VERSION:
            self._warn_once(
                version,
                f"Ignored the share identities at {self.share_identities_path}: version {document.version} is not "
                f"{SHARE_IDENTITIES_FILE_VERSION}",
            )
            return {}
        self._warned_version = None
        return dict(document.identities)

    def _warn_once(self, version: int, message: str) -> None:
        if self._warned_version == version:
            return
        self._warned_version = version
        logger.warning(
            "{}; granted accounts that have not arrived are named by their id until it is rewritten", message
        )


def _file_version(path: Path) -> int | None:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None
