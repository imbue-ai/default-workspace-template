"""Which apps a visitor may reach on their own origin: the per-app grants of the share gateway's grants document.

The minds desktop writes ``data/.secrets/share_grants.toml`` while the workspace is shared (``data/.secrets/share.env``
exists), one ``[services.<name>]`` table per app the owner shares on its own, and the gateway admits a visitor holding
only such a grant to that app's origin and nothing else: not the shell, so no window of the app exists anywhere while
they use it. The no-window rule (the stop-when-no-windows spec, section 6.1) therefore treats an app with a per-app
grant as one that has windows. The shell reads the document itself, with the gateway's shape but not its package: a
document the gateway would refuse admits nobody there, so here it grants no app.
"""

import tomllib
from collections.abc import Callable
from collections.abc import Sequence
from collections.abc import Set as AbstractSet
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.system_interface.shell.errors import ShareGrantsError

# Beside ``DEFAULT_SHARE_ENV_PATH`` (``profiles.py``): relative to the workspace root the supervised process runs from.
DEFAULT_SHARE_GRANTS_PATH: Final[Path] = Path("data/.secrets/share_grants.toml")
# The document's workspace-level table, its per-app tables, and the three allow-lists every table holds, as the
# gateway's ``grants.py`` names them.
_WORKSPACE_TABLE_KEY: Final[str] = "workspace"
_PER_APP_TABLE_KEY: Final[str] = "services"
_GRANT_LIST_KEYS: Final[tuple[str, ...]] = ("users", "emails", "email_domains")

# The names of every app with a per-app grant, read once per sweep pass.
GrantedAppsReader = Callable[[], AbstractSet[str]]


@pure
def _validated_string_list(raw_list: object, scope: str, key: str) -> list[str]:
    if not isinstance(raw_list, list):
        raise ShareGrantsError(f"grants scope {scope!r}: {key} must be a list of strings")
    entries: list[str] = []
    for entry in raw_list:
        if not isinstance(entry, str):
            raise ShareGrantsError(f"grants scope {scope!r}: {key} must be a list of strings")
        entries.append(entry)
    return entries


@pure
def _validated_grant_lists(raw_table: object, scope: str) -> list[list[str]]:
    """One scope's three allow-lists as the gateway's parser reads them (an absent list is empty). Raises
    ShareGrantsError for a table the gateway would refuse: every list is checked, whatever the others hold."""
    if not isinstance(raw_table, dict):
        raise ShareGrantsError(f"grants scope {scope!r} must be a table")
    # What TOML parsed: keys and values are anything until each list is checked.
    raw_list_by_key: dict[object, object] = {key: value for key, value in raw_table.items()}
    return [_validated_string_list(raw_list_by_key.get(key, []), scope, key) for key in _GRANT_LIST_KEYS]


@pure
def _is_anyone_listed(grant_lists: Sequence[Sequence[str]]) -> bool:
    """Whether any allow-list names anyone: the gateway ignores blank entries, so they grant no one here either."""
    return any(entry.strip() for entries in grant_lists for entry in entries)


@pure
def parse_granted_app_names(grants_text: str) -> set[str]:
    """The apps the document admits someone to on their own origin: each ``[services.<name>]`` table with a
    non-empty ``users``, ``emails``, or ``email_domains``. Raises ShareGrantsError for a document the gateway would
    refuse, its ``[workspace]`` table included (a workspace-level grant exempts no app, but a malformed one makes
    the gateway admit nobody)."""
    try:
        document = tomllib.loads(grants_text)
    except tomllib.TOMLDecodeError as e:
        raise ShareGrantsError(f"grants file is not valid TOML: {e}") from e
    # Checked for its shape only: whom the workspace table names does not matter here.
    _validated_grant_lists(document.get(_WORKSPACE_TABLE_KEY, {}), _WORKSPACE_TABLE_KEY)
    raw_tables = document.get(_PER_APP_TABLE_KEY, {})
    if not isinstance(raw_tables, dict):
        raise ShareGrantsError(f"grants [{_PER_APP_TABLE_KEY}] must be a table of per-app tables")
    granted: set[str] = set()
    for name, raw_table in raw_tables.items():
        if _is_anyone_listed(_validated_grant_lists(raw_table, f"{_PER_APP_TABLE_KEY}.{name}")):
            granted.add(name)
    return granted


class ShareGrantsReader(MutableModel):
    """Answers which apps carry a per-app share grant from the files the minds desktop writes while sharing."""

    model_config = {"extra": "forbid", "frozen": False}

    share_env_path: Path = Field(
        frozen=True, description="The share materials; absent while the workspace is not shared"
    )
    share_grants_path: Path = Field(frozen=True, description="The grants document beside them")

    _warned_grants_mtime_ns: int | None = PrivateAttr(default=None)

    def granted_app_names(self) -> set[str]:
        """The apps with a per-app grant; none while the workspace is not shared, while the document is missing (the
        desktop writes it before the materials), or while it is one the gateway refuses, which is warned about once
        per version of the file."""
        if not self.share_env_path.exists():
            return set()
        try:
            grants_mtime_ns = self.share_grants_path.stat().st_mtime_ns
        except FileNotFoundError:
            return set()
        except OSError as e:
            logger.warning("Could not stat the share grants at {}: {}", self.share_grants_path, e)
            return set()
        try:
            grants_text = self.share_grants_path.read_text(encoding="utf-8")
        except OSError as e:
            self._warn_once(grants_mtime_ns, f"Could not read the share grants at {self.share_grants_path}: {e}")
            return set()
        try:
            granted = parse_granted_app_names(grants_text)
        except ShareGrantsError as e:
            self._warn_once(grants_mtime_ns, f"Ignored the share grants at {self.share_grants_path}: {e}")
            return set()
        self._warned_grants_mtime_ns = None
        return granted

    def _warn_once(self, grants_mtime_ns: int, message: str) -> None:
        """Warn about a version of the document once, not on every sweep pass until the owner rewrites it."""
        if self._warned_grants_mtime_ns == grants_mtime_ns:
            return
        self._warned_grants_mtime_ns = grants_mtime_ns
        logger.warning("{}; no app counts as shared on its own until the file is rewritten", message)
