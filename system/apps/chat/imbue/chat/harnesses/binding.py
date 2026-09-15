"""Binding an agent to an account, and running a CLI scoped to one.

"Which account" is one environment variable per harness (see `account_scope`). mngr already
sets all four per-agent; today it points them at one shared credential file so every agent
shares a login. Binding changes what they point at, and nothing else.

The binding has to happen INSIDE `mngr create`, not after it. `mngr create` writes the agent
env file, provisions, starts the agent, waits for readiness -- destroying the agent if that
times out -- and delivers the first message, all before it returns. A repoint afterwards
would land after the first turn had already run on the wrong credential. Four of mngr's own
flags land at the right moments:

    --env KEY=VALUE              written to <state>/env BEFORE provisioning
    --env-file PATH              read on this host, merged into <state>/env, same moment
    --setting KEY=VALUE          a config override, so it reaches provisioning too
    --extra-provision-command    run AFTER provisioning, BEFORE start

So claude binds through the env file (its launch command carries no inline `env`, so the
sourced value wins), and the other three bind by replacing the credential symlink that
provisioning just created -- the same `ln -sfn` mngr itself used, one step later.

An agy account signed in with a pasted Gemini key is the one binding that is a COPY rather
than a pointer: mngr flattens `--env-file` into the agent's own env file at create, and writes
the per-agent settings.json once, at provision. So re-keying such an account reaches chats
created afterwards; the chats already on it keep the key they were created with, even across
the `mngr start --restart` a re-auth runs, until they are recreated.

A create that names no account gets the same binding from the workspace's own mngr config:
the account store writes the default account's harness and binding into
`.mngr/settings.local.toml` (see `create_defaults`), so the arguments here are what the
chat app adds on top of that default when it binds a chat to a chosen account.
"""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Final

from imbue.chat import accounts
from imbue.chat.accounts import Account
from imbue.chat.accounts import choose_default_account
from imbue.chat.accounts import harness_for
from imbue.chat.harnesses.account_scope import account_credential_path
from imbue.chat.harnesses.account_scope import agent_credential_path
from imbue.chat.harnesses.antigravity.auth import GEMINI_API_KEY_ENV_VAR
from imbue.chat.harnesses.antigravity.auth import GEMINI_MODE_SETTING
from imbue.chat.harnesses.antigravity.auth import gemini_env_path
from imbue.chat.harnesses.antigravity.auth import has_gemini_api_key
from imbue.chat.harnesses.harness_type import HarnessType
from imbue.mngr_claude.claude_config import auto_dismiss_claude_dialogs
from imbue.mngr_claude.claude_config import ensure_chat_cancel_tap_keybinding


class BindingError(RuntimeError):
    """An agent could not be bound to an account."""


# codex keys its secret by a hash of the canonical CODEX_HOME unless the credential store is
# pinned to `file`. Without this pin a sign-in against an account dir can land in an OS
# keyring instead: auth.json is never written, the bind symlink dangles, the chat runs signed
# out -- and `codex login status` scoped to that same dir still reports success, so nothing
# downstream notices. `file` is codex's current default, but `auto` exists and prefers a
# keyring when one is present, so it is pinned explicitly.
_CODEX_CONFIG_TOML: Final = 'cli_auth_credentials_store = "file"\n'


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
        # This export is load-bearing beyond the agent itself. mngr sources an agent's env
        # file into every process in its tmux session, and propagates CLAUDE_CONFIG_DIR to a
        # child agent when the spawning shell already has it -- so a worker created from
        # inside this chat (`/launch-task`) runs on this same account, and so does any skill
        # script that shells claude. Binding claude some other way would silently sign every
        # worker out. See `binding_test.py`.
        return ["--env", f"CLAUDE_CONFIG_DIR={account_dir}"]

    if harness is HarnessType.ANTIGRAVITY and has_gemini_api_key(account_dir):
        # agy's key mode is an environment variable plus a flag in its own settings.json, so
        # there is no credential file to link. `--env-file` is read on the host running mngr
        # and its contents are merged into the agent's env file before provisioning; the
        # setting reaches the per-agent settings.json mngr writes at the same moment.
        #
        # The oauth-token symlink mngr provisions anyway is left dangling, which is what it
        # already is for any account that has never completed a browser sign-in: in key mode
        # agy never reads it.
        return ["--env-file", str(gemini_env_path(account_dir)), "--setting", GEMINI_MODE_SETTING]

    if harness is HarnessType.ANTIGRAVITY:
        # The workspace's create defaults carry the key-mode binding whenever the DEFAULT account
        # is on a pasted key, and `--env-file` accumulates rather than being overridden -- so
        # without this an agy chat bound to a browser account inherits that account's key and runs
        # on it. An explicit `--env` outranks every env file, and an empty value is what agy sees
        # when nobody has pasted a key at all.
        #
        # This only takes the key away. The mode flag rides `--setting`, which accumulates too, so
        # such a chat starts in key mode with no key and agy stops at once saying so -- loud, and
        # attributable, where before it quietly spent another account's key. Binding the mode
        # through a channel a per-chat create can override is the real fix.
        return ["--env", f"{GEMINI_API_KEY_ENV_VAR}=", *_credential_link_args(harness, account_dir, agent_state_dir)]

    return _credential_link_args(harness, account_dir, agent_state_dir)


def _credential_link_args(harness: HarnessType, account_dir: Path, agent_state_dir: Path) -> list[str]:
    """The provision command that repoints an agent's credential symlink at the account's copy."""
    source = account_credential_path(harness, account_dir)
    dest = agent_credential_path(harness, agent_state_dir)
    if source is None or dest is None:
        return []
    # `ln -sfn` replaces whatever provisioning just linked, which is exactly what mngr's own
    # helper does -- the same operation, one step later.
    link = f"mkdir -p {shlex.quote(str(dest.parent))} && ln -sfn {shlex.quote(str(source))} {shlex.quote(str(dest))}"
    return ["--extra-provision-command", link]


def resolve_binding(account_id: str = "", home: Path | None = None) -> Account:
    """The account a new agent should run under.

    An explicit id wins; otherwise the account the user pinned as the default; otherwise the
    most recently used account, which is bumped on every launch -- so signing in and then
    starting a chat "just works" without the caller having to name what it just created,
    while a pinned default keeps every unnamed launch on the harness the user chose. That
    rule is `accounts.choose_default_account`, shared with the workspace's create defaults.

    The account decides the harness (see `harness_for`), not the other way round: asking the
    caller for both invites a chat that names codex while running on an agy credential, and
    there is no way to notice that until its first turn fails.

    Raises when there are none. There is no shared login to fall back to (`~/.claude` is
    left alone), so an agent created without an account is simply unauthenticated. The
    instances API makes that unreachable (with nothing signed in it mints a chat that waits
    for an account, whose page offers the chooser), and this is the backstop for anything
    that does not.
    """
    if account_id:
        account = accounts.resolve_account(account_id, home)
        if harness_for(account) is None:
            raise BindingError(f"account {account_id} is on a lane this build does not have")
        return account

    chosen = choose_default_account(accounts.read_index(home))
    if chosen is None:
        raise accounts.AccountError("no provider accounts exist yet")
    # Back through `resolve_account` for the folder check. The explicit-id path above has
    # always had it; this one did not, so a row whose folder had gone bound an agent to a
    # directory that is not there -- which surfaces as an empty model bar, not as an error.
    return accounts.resolve_account(chosen.id, home)


def has_usable_account(home: Path | None = None) -> bool:
    """Whether any signed-in account is on a lane this build runs: what ``resolve_binding("")`` needs."""
    return any(harness_for(account) is not None for account in accounts.read_index(home).accounts)
