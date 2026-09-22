"""Binding an agent to an account, per harness: the scope a CLI runs under, and how an agent is pointed at it.

"Which account" is one environment variable per harness:

    claude   CLAUDE_CONFIG_DIR
    codex    CODEX_HOME
    agy      HOME               (it has no config-dir override -- the home IS the scope)
    pi       PI_CODING_AGENT_DIR

mngr already sets all four per-agent; binding changes what they point at, and nothing else. Each
harness registers an ``AccountBinding`` on its ``HarnessSpec`` (``registry.build_account_binding``),
so nothing outside the harness modules branches on which harness it binds.

The binding has to happen INSIDE `mngr create`, not after it. `mngr create` writes the agent
env file, provisions, starts the agent, waits for readiness -- destroying the agent if that
times out -- and delivers the first message, all before it returns. A repoint afterwards
would land after the first turn had already run on the wrong credential. Two flags already
land at the right moments:

    --env KEY=VALUE              written to <state>/env BEFORE provisioning
    --extra-provision-command    run AFTER provisioning, BEFORE start

So claude binds through the env file (its launch command carries no inline `env`, so the
sourced value wins), and the other three bind by replacing the credential symlink that
provisioning just created -- the same `ln -sfn` mngr itself used, one step later
(``CredentialLinkAccountBinding``).

A create that names no account gets the same binding from the workspace's own mngr config:
the account store writes the default account's harness and binding into
`.mngr/settings.local.toml` (see `create_defaults`), so ``create_args`` is what the chat app
adds on top of that default when it binds a chat to a chosen account.
"""

import os
import shlex
from abc import ABC
from abc import abstractmethod
from collections.abc import Callable
from pathlib import Path
from typing import Final

from pydantic import Field

from imbue.chat.agent_discovery import AgentInfo
from imbue.imbue_common.frozen_model import FrozenModel

# The state directory of the agent being created, as `extra_provision_command` sees it: mngr
# sources the agent's env before running the command.
_STATE_DIR_SHELL_VAR: Final = '"$MNGR_AGENT_STATE_DIR"'


class BindingError(RuntimeError):
    """An agent could not be bound to an account."""


class DefaultCreateBinding(FrozenModel):
    """What the workspace's local create defaults carry to bind an agent that does not exist yet."""

    env: tuple[str, ...] = Field(description="``KEY=VALUE`` lines for the new agent's env file")
    provision_commands: tuple[str, ...] = Field(
        description="Shell run after provisioning and before start, with ``$MNGR_AGENT_STATE_DIR`` naming the agent"
    )


class AccountBinding(ABC):
    """How one harness's agents run on a signed-in account: the CLI's scope, the create's arguments, the
    rebind's edit, and the session files a rebind carries along."""

    @abstractmethod
    def account_env(self, account_dir: Path) -> dict[str, str]:
        """The environment that scopes the harness's CLI to one account.

        Only the scoping variable -- callers layer this over `os.environ` themselves, because
        both `pexpect.spawn` and `Popen` REPLACE the environment rather than merging into it, and
        a child without `PATH` never starts.
        """

    @abstractmethod
    def credential_paths(self, account_dir: Path) -> tuple[Path, ...]:
        """Every file in an account folder that says the account is signed in, whoever wrote it."""

    def seed_account(self, account_dir: Path, work_dir: Path) -> None:
        """Write the per-account files the harness needs before it will run unattended.

        Provisioning does this for a per-agent config dir; an account folder is ours, so nothing
        else will. Skipping it does not fail loudly -- it fails by parking the CLI on an
        interactive dialog, which reads downstream as a readiness timeout and gets the agent
        destroyed. The folder itself is all a harness with nothing to seed needs.
        """
        account_dir.mkdir(parents=True, exist_ok=True)

    @abstractmethod
    def create_args(self, account_dir: Path, agent_state_dir: Path) -> list[str]:
        """The `mngr create` arguments that bind a new agent to an account.

        Returns argv fragments, not a shell string: dwt runs mngr as an argv list, so nothing
        quotes these on the way.
        """

    @abstractmethod
    def default_create_binding(self, account_dir: Path) -> DefaultCreateBinding:
        """The binding the workspace's local create defaults carry, for an agent whose state dir is not known yet."""

    @abstractmethod
    def rebind_agent(self, account_dir: Path, agent_state_dir: Path) -> None:
        """Repoint an existing, stopped agent's binding at ``account_dir``: the rebind's edit (spec 6).

        The same mechanism ``create_args`` uses, applied to the agent's own files. Idempotent,
        and each write lands whole (a temp file or link renamed into place), so a resume that
        runs it again changes nothing.
        """

    def move_sessions(
        self,
        agent_info: AgentInfo,
        # Where the files were found by an earlier attempt of this rebind, or None on its first.
        recorded_sessions_dir: Path | None,
        target_account_dir: Path,
        # Persists where the files are, before the binding is rewritten and once they have moved.
        record_sessions_dir: Callable[[Path], None],
    ) -> None:
        """Carry the agent's session files to the target account before its binding is rewritten.

        A no-op for a harness that keeps its sessions in the agent's own state dir, which a
        rebind leaves where it is; only the credential moves.
        """


class CredentialLinkAccountBinding(AccountBinding, ABC):
    """A harness bound by a credential file mngr links into the agent's state dir at provisioning: binding
    repoints that link at the account's copy, and the sessions stay in the agent's state dir."""

    @abstractmethod
    def account_credential_path(self, account_dir: Path) -> Path:
        """Where the credential lives inside an account folder."""

    def credential_paths(self, account_dir: Path) -> tuple[Path, ...]:
        return (self.account_credential_path(account_dir),)

    @abstractmethod
    def agent_credential_path(self, agent_state_dir: Path) -> Path:
        """The per-agent path provisioning writes, and that binding then repoints."""

    def create_args(self, account_dir: Path, agent_state_dir: Path) -> list[str]:
        # The extra-provision command IS shell-evaluated on the host, which is why its paths
        # are quoted. `ln -sfn` replaces whatever provisioning just linked, which is exactly what
        # mngr's own helper does -- the same operation, one step later.
        source = self.account_credential_path(account_dir)
        dest = self.agent_credential_path(agent_state_dir)
        link = (
            f"mkdir -p {shlex.quote(str(dest.parent))} && ln -sfn {shlex.quote(str(source))} {shlex.quote(str(dest))}"
        )
        return ["--extra-provision-command", link]

    def default_create_binding(self, account_dir: Path) -> DefaultCreateBinding:
        # The same `mkdir -p` and `ln -sfn` as `create_args`, with the agent side written over
        # `$MNGR_AGENT_STATE_DIR` -- unquoted so the shell expands it -- since the agent does not exist yet.
        source = self.account_credential_path(account_dir)
        relative = self.agent_credential_path(Path())
        link = (
            f"mkdir -p {_STATE_DIR_SHELL_VAR}/{shlex.quote(str(relative.parent))}"
            f" && ln -sfn {shlex.quote(str(source))} {_STATE_DIR_SHELL_VAR}/{shlex.quote(str(relative))}"
        )
        return DefaultCreateBinding(env=(), provision_commands=(link,))

    def rebind_agent(self, account_dir: Path, agent_state_dir: Path) -> None:
        dest = self.agent_credential_path(agent_state_dir)
        dest.parent.mkdir(parents=True, exist_ok=True)
        _replace_symlink(self.account_credential_path(account_dir), dest)


def _replace_symlink(source: Path, dest: Path) -> None:
    """Point ``dest`` at ``source`` in one rename, whatever ``dest`` was before (a link, a copy, or nothing)."""
    temp_path = dest.with_name(f"{dest.name}.rebind-tmp")
    temp_path.unlink(missing_ok=True)
    os.symlink(source, temp_path)
    os.replace(temp_path, dest)
