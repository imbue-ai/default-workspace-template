"""Where an account's credential lives, per harness: the one variable or file that scopes a CLI.

    claude   CLAUDE_CONFIG_DIR
    codex    CODEX_HOME
    agy      HOME               (it has no config-dir override -- the home IS the scope)
    pi       PI_CODING_AGENT_DIR

claude is scoped by its environment alone. The other three read a credential file that
mngr links into the agent's state directory at provisioning, so scoping one of them means
repointing that link at the account's copy, and these tables name both ends of the link.

Nothing here reads the account store, which is what lets the store's own derived output
(``create_defaults``) use these tables without an import cycle.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from imbue.chat.harnesses.harness_type import HarnessType
from imbue.chat.harnesses.pi_coding.model import PI_CONFIG_DIR_RELPATH
from imbue.mngr_antigravity.antigravity_config import get_antigravity_oauth_token_path
from imbue.mngr_codex.codex_config import get_codex_auth_path
from imbue.mngr_codex.codex_config import get_codex_home


class ScopeError(RuntimeError):
    """A harness has no account scoping."""


# Kept in sync with `_AGY_HOME_RELATIVE_PATH` in mngr_antigravity's plugin.py, which is
# private there. agy relocates the whole HOME rather than exposing a config-dir override.
_AGY_HOME_RELATIVE_PATH: Final[tuple[str, ...]] = ("plugin", "antigravity", "home")

_AUTH_FILENAME: Final = "auth.json"


def account_env(harness: HarnessType, account_dir: Path) -> dict[str, str]:
    """The environment that scopes a CLI to one account.

    Only the scoping variable -- callers layer this over `os.environ` themselves, because
    both `pexpect.spawn` and `Popen` REPLACE the environment rather than merging into it, and
    a child without `PATH` never starts.
    """
    if harness is HarnessType.CLAUDE:
        return {"CLAUDE_CONFIG_DIR": str(account_dir)}
    if harness is HarnessType.CODEX:
        return {"CODEX_HOME": str(account_dir)}
    if harness is HarnessType.ANTIGRAVITY:
        return {"HOME": str(account_dir)}
    if harness is HarnessType.PI_CODING:
        return {"PI_CODING_AGENT_DIR": str(account_dir)}
    raise ScopeError(f"{harness} has no account scoping")


def account_credential_path(harness: HarnessType, account_dir: Path) -> Path | None:
    """Where the credential lives inside an account folder, for the harnesses that link it.

    None for claude: its credential is the `env` block of the account's settings.json plus
    whatever the CLI writes beside it, and it binds by environment rather than by symlink.
    """
    if harness is HarnessType.CODEX:
        return get_codex_auth_path(account_dir)
    if harness is HarnessType.ANTIGRAVITY:
        return get_antigravity_oauth_token_path(account_dir)
    if harness is HarnessType.PI_CODING:
        return account_dir / _AUTH_FILENAME
    return None


def agent_credential_path(harness: HarnessType, agent_state_dir: Path) -> Path | None:
    """The per-agent path provisioning writes, and that binding then repoints."""
    if harness is HarnessType.CODEX:
        return get_codex_auth_path(get_codex_home(agent_state_dir))
    if harness is HarnessType.ANTIGRAVITY:
        return get_antigravity_oauth_token_path(agent_state_dir.joinpath(*_AGY_HOME_RELATIVE_PATH))
    if harness is HarnessType.PI_CODING:
        return agent_state_dir / PI_CONFIG_DIR_RELPATH / _AUTH_FILENAME
    return None


def agent_credential_relative_path(harness: HarnessType) -> Path | None:
    """`agent_credential_path` relative to the state directory, for a binding written before the agent has one."""
    return agent_credential_path(harness, Path())
