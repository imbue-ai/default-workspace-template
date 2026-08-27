"""Binding an agent to an account, and running a CLI scoped to one.

"Which account" is one environment variable per harness. That is the whole mechanism:

    claude   CLAUDE_CONFIG_DIR
    codex    CODEX_HOME
    agy      HOME               (it has no config-dir override -- the home IS the scope)
    pi       PI_CODING_AGENT_DIR

mngr already sets all four per-agent; today it points them at one shared credential file so
every agent shares a login. Binding changes what they point at, and nothing else.

The binding has to happen INSIDE `mngr create`, not after it. `mngr create` writes the agent
env file, provisions, starts the agent, waits for readiness -- destroying the agent if that
times out -- and delivers the first message, all before it returns. A repoint afterwards
would land after the first turn had already run on the wrong credential. Two flags already
land at the right moments:

    --env KEY=VALUE              written to <state>/env BEFORE provisioning
    --extra-provision-command    run AFTER provisioning, BEFORE start

So claude binds through the env file (its launch command carries no inline `env`, so the
sourced value wins), and the other three bind by replacing the credential symlink that
provisioning just created -- the same `ln -sfn` mngr itself used, one step later.
"""

from __future__ import annotations

import shlex
import shutil
from pathlib import Path
from typing import Final

from loguru import logger as _loguru_logger

from imbue.mngr_antigravity.antigravity_config import get_antigravity_oauth_token_path
from imbue.mngr_claude.claude_config import auto_dismiss_claude_dialogs
from imbue.mngr_claude.claude_config import ensure_chat_cancel_tap_keybinding
from imbue.mngr_codex.codex_config import get_codex_auth_path
from imbue.mngr_codex.codex_config import get_codex_home
from imbue.system_interface import accounts
from imbue.system_interface.accounts import Account
from imbue.system_interface.harnesses.harness_type import HarnessType
from imbue.system_interface.harnesses.lanes import LaneNotFoundError
from imbue.system_interface.harnesses.lanes import get_lane
from imbue.system_interface.harnesses.pi_coding.model import PI_CONFIG_DIR_RELPATH

logger = _loguru_logger


class BindingError(RuntimeError):
    """An agent could not be bound to an account."""


# Kept in sync with `_AGY_HOME_RELATIVE_PATH` in mngr_antigravity's plugin.py, which is
# private there. agy relocates the whole HOME rather than exposing a config-dir override.
_AGY_HOME_RELATIVE_PATH: Final[tuple[str, ...]] = ("plugin", "antigravity", "home")

_AUTH_FILENAME: Final = "auth.json"

# codex keys its secret by a hash of the canonical CODEX_HOME unless the credential store is
# pinned to `file`. Without this pin a sign-in against an account dir can land in an OS
# keyring instead: auth.json is never written, the bind symlink dangles, the chat runs signed
# out -- and `codex login status` scoped to that same dir still reports success, so nothing
# downstream notices. `file` is codex's current default, but `auto` exists and prefers a
# keyring when one is present, so it is pinned explicitly.
_CODEX_CONFIG_TOML: Final = 'cli_auth_credentials_store = "file"\n'


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
    raise BindingError(f"{harness} has no account scoping")


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


def seed_account(harness: HarnessType, account_dir: Path, work_dir: Path) -> None:
    """Write the per-account files a harness needs before it will run unattended.

    Provisioning does this for a per-agent config dir; an account folder is ours, so nothing
    else will. Skipping it does not fail loudly -- it fails by parking the CLI on an
    interactive dialog, which reads downstream as a readiness timeout and gets the agent
    destroyed.
    """
    account_dir.mkdir(parents=True, exist_ok=True)
    if harness is HarnessType.CLAUDE:
        # With CLAUDE_CONFIG_DIR set, claude reads its global config from INSIDE the dir
        # rather than from ~/.claude.json beside it, so a fresh account folder starts with
        # no onboarding state at all and boots into the theme/trust dialogs.
        auto_dismiss_claude_dialogs(account_dir / ".claude.json", work_dir)
        # Same story for the meta+q interrupt chord: mngr writes it into the shared dir, so
        # a pinned agent would silently lose its native stop and fall back to a kill.
        ensure_chat_cancel_tap_keybinding(account_dir / "keybindings.json")
    elif harness is HarnessType.CODEX:
        config = account_dir / "config.toml"
        if not config.exists():
            config.write_text(_CODEX_CONFIG_TOML)
    else:
        # agy and pi need nothing seeded: neither has an onboarding dialog to dismiss, and
        # both write their whole credential file themselves on a successful sign-in.
        pass


def create_args(harness: HarnessType, account_dir: Path, agent_state_dir: Path) -> list[str]:
    """The `mngr create` arguments that bind a new agent to an account.

    Returns argv fragments, not a shell string: dwt runs mngr as an argv list, so nothing
    quotes these on the way. The extra-provision command IS shell-evaluated on the host,
    which is why its paths are quoted here.
    """
    if harness is HarnessType.CLAUDE:
        return ["--env", f"CLAUDE_CONFIG_DIR={account_dir}"]

    source = account_credential_path(harness, account_dir)
    dest = agent_credential_path(harness, agent_state_dir)
    if source is None or dest is None:
        return []
    # `ln -sfn` replaces whatever provisioning just linked, which is exactly what mngr's own
    # helper does -- the same operation, one step later.
    link = f"mkdir -p {shlex.quote(str(dest.parent))} && ln -sfn {shlex.quote(str(source))} {shlex.quote(str(dest))}"
    return ["--extra-provision-command", link]


def resolve_binding(account_id: str = "", home: Path | None = None) -> Account | None:
    """The account a new agent should run under, or None for the workspace's shared login.

    An explicit id wins; otherwise the most recently used account, which is bumped on every
    launch -- so signing in and then starting a chat "just works" without the caller having
    to name what it just created.

    The account decides the harness (see `harness_for`), not the other way round: asking the
    caller for both invites a chat that names codex while running on an agy credential, and
    there is no way to notice that until its first turn fails.

    None is not an error: a workspace with no accounts keeps today's behaviour exactly.
    """
    if account_id:
        account = accounts.resolve_account(account_id, home)
        if harness_for(account) is None:
            raise BindingError(f"account {account_id} is on a lane this build does not have")
        return account

    index = accounts.read_index(home)
    usable = [a for a in index.accounts if harness_for(a) is not None]
    if not usable:
        return None
    return next((a for a in usable if a.id == index.mru), usable[-1])


def harness_for(account: Account) -> HarnessType | None:
    """The harness an account's lane runs on, or None if this build no longer has that lane."""
    try:
        return get_lane(account.lane).harness
    except LaneNotFoundError:
        logger.warning("Account {} names unknown lane {}", account.id, account.lane)
        return None


# What claude reads when nothing pins CLAUDE_CONFIG_DIR: the workspace terminals, the
# supervisord services, and `claude_p.py` (which eight skills plus build-app and
# migrate-workspace call for scripted steps). None of those has an agent, so none can be
# bound -- they land here or nowhere.
_DEFAULT_CLAUDE_HOME: Final = ".claude"


def adopt_default_claude_home(account_path: Path, home: Path | None = None) -> None:
    """Point `~/.claude` at an account, carrying anything already there into it.

    The agentless callers above resolve `~/.claude` and cannot be given an account, so the
    first claude account becomes what they use. A plain `ln -sfn` would not do: onto an
    existing directory it creates a link INSIDE it, and forced it would drop `projects/`,
    which is where the transcript watcher and mngr's resume gate both look. So the existing
    tree is merged in first -- the account wins any collision, since its credential is the
    one being adopted -- and only then replaced by the link.
    """
    root = (home or Path.home()) / _DEFAULT_CLAUDE_HOME
    account_path.mkdir(parents=True, exist_ok=True)
    if root.is_symlink():
        root.unlink()
    elif root.is_dir():
        for existing in root.iterdir():
            destination = account_path / existing.name
            if not destination.exists():
                shutil.move(str(existing), str(destination))
        shutil.rmtree(root, ignore_errors=True)
    elif root.exists():
        root.unlink()
    root.parent.mkdir(parents=True, exist_ok=True)
    root.symlink_to(account_path, target_is_directory=True)
    logger.info("Pointed {} at account {}", root, account_path.name)
