"""The `mngr create` defaults the workspace keeps for its default provider account.

Every `mngr create` in the workspace that names no harness and no account -- the chats the
Minds app starts from outside, workers, automations, the caretaker -- resolves through
`.mngr/settings.local.toml`, mngr's git-ignored local config layer, which sits above the
committed `.mngr/settings.toml` and below the CLI. Its managed `[commands.create]` keys name
the default account's harness as `type`, its binding (the variable claude is scoped by, or
for the other harnesses the credential symlink over `$MNGR_AGENT_STATE_DIR`, which needs no
agent id), and the `account=<id>` label a re-auth restarts agents by.

The account store owns the file: it is rewritten from the index on every index write and at
the chat app's boot, and is derived output only -- the pin and the most recently used account
stay in `index.json`. The list keys use mngr's `__extend` operator so the local layer only
ever adds to what the committed file sets; a CLI flag for the same key appends after both, so
an explicit `--type` or `--env` still wins. Keys outside the managed ones survive a rewrite.
"""

from __future__ import annotations

import os
import shlex
import tomllib
from pathlib import Path
from typing import Any
from typing import Final

import tomlkit
from loguru import logger as _loguru_logger
from pydantic import Field
from tomlkit.exceptions import ParseError
from tomlkit.items import Table

from imbue.chat.harnesses.account_scope import account_credential_path
from imbue.chat.harnesses.account_scope import account_env
from imbue.chat.harnesses.account_scope import agent_credential_relative_path
from imbue.chat.harnesses.harness_type import HarnessType
from imbue.imbue_common.frozen_model import FrozenModel

logger = _loguru_logger

# mngr's own override for where the project's settings files live; honored here so the file
# is written where the mngr that reads it will look, and so tests never touch a real one.
PROJECT_CONFIG_DIR_ENV_VAR: Final = "MNGR_PROJECT_CONFIG_DIR"
_DEFAULT_PROJECT_CONFIG_DIR: Final = Path(".mngr")
LOCAL_SETTINGS_FILENAME: Final = "settings.local.toml"

_COMMANDS_KEY: Final = "commands"
_CREATE_KEY: Final = "create"
TYPE_KEY: Final = "type"
# `__extend` rather than a bare assignment: mngr refuses a local-layer assignment that would
# drop entries the committed file sets, and extending never can.
ENV_KEY: Final = "env__extend"
PROVISION_COMMAND_KEY: Final = "extra_provision_command__extend"
LABEL_KEY: Final = "label__extend"
MANAGED_KEYS: Final[tuple[str, ...]] = (
    TYPE_KEY,
    ENV_KEY,
    PROVISION_COMMAND_KEY,
    LABEL_KEY,
)

# The state directory of the agent being created, as `extra_provision_command` sees it: mngr
# sources the agent's env before running the command.
_STATE_DIR_SHELL_VAR: Final = '"$MNGR_AGENT_STATE_DIR"'

_HEADER: Final = (
    "Written by the chat app from the provider account store: the account a `mngr create` in this "
    "workspace runs on when it names none. The keys under [commands.create] that the chat app "
    "manages are rewritten on every account change; anything else here is left alone."
)


class CreateDefaults(FrozenModel):
    """What every unqualified `mngr create` in the workspace resolves to: one account, and its harness."""

    harness: HarnessType = Field(description="The agent type the account's lane runs on")
    account_id: str = Field(description="The account's id, which its folder is named by")
    account_dir: Path = Field(description="The account's folder, where its credential lives")


def create_defaults_path() -> Path:
    """Where the local settings file lives, relative to the repo root every service runs from."""
    override = os.environ.get(PROJECT_CONFIG_DIR_ENV_VAR, "").strip()
    config_dir = Path(override) if override else _DEFAULT_PROJECT_CONFIG_DIR
    return config_dir / LOCAL_SETTINGS_FILENAME


def _credential_link_command(harness: HarnessType, account_dir: Path) -> str | None:
    """The shell that repoints the agent's credential link at the account's copy, or None when the harness has none.

    The same `mkdir -p` and `ln -sfn` as `binding.create_args`, with the agent side written over
    `$MNGR_AGENT_STATE_DIR` -- unquoted so the shell expands it -- since the agent does not exist yet.
    """
    source = account_credential_path(harness, account_dir)
    relative = agent_credential_relative_path(harness)
    if source is None or relative is None:
        return None
    return (
        f"mkdir -p {_STATE_DIR_SHELL_VAR}/{shlex.quote(str(relative.parent))}"
        f" && ln -sfn {shlex.quote(str(source))} {_STATE_DIR_SHELL_VAR}/{shlex.quote(str(relative))}"
    )


def managed_create_settings(defaults: CreateDefaults) -> dict[str, Any]:
    """The managed `[commands.create]` keys for `defaults`."""
    settings: dict[str, Any] = {
        TYPE_KEY: defaults.harness.value,
        LABEL_KEY: [f"account={defaults.account_id}"],
    }
    if defaults.harness is HarnessType.CLAUDE:
        settings[ENV_KEY] = [
            f"{name}={value}" for name, value in account_env(defaults.harness, defaults.account_dir).items()
        ]
    else:
        link = _credential_link_command(defaults.harness, defaults.account_dir)
        if link is not None:
            settings[PROVISION_COMMAND_KEY] = [link]
    return settings


def _empty_document() -> tomlkit.TOMLDocument:
    document = tomlkit.document()
    document.add(tomlkit.comment(_HEADER))
    return document


def _load_document(path: Path) -> tomlkit.TOMLDocument:
    """The file as it stands, or a fresh document when there is none or it no longer parses.

    A malformed file is rebuilt rather than raised on: it is derived output, mngr refuses to
    load a malformed local layer anyway, and raising here would turn one bad hand edit into a
    failure of every account write and of the boot sweep that regenerates the file.
    """
    if not path.exists():
        return _empty_document()
    try:
        return tomlkit.parse(path.read_text())
    except ParseError as e:
        logger.warning("Rewriting {}, which no longer parses ({}); any hand-kept keys in it are lost", path, e)
        return _empty_document()


def _write_document(path: Path, document: tomlkit.TOMLDocument) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(tomlkit.dumps(document))
    os.replace(tmp, path)


def _child_table(parent: Table | tomlkit.TOMLDocument, key: str) -> Table:
    existing = parent.get(key)
    if isinstance(existing, Table):
        return existing
    table = tomlkit.table()
    parent[key] = table
    return table


def write_create_defaults(path: Path, defaults: CreateDefaults | None) -> None:
    """Rewrite the managed keys of `path` for `defaults`, or remove them when there are none.

    Everything else in the file survives: a hand edit outside the managed keys, another key
    under `[commands.create]`. A file left holding nothing else is removed.
    """
    document = _load_document(path)
    commands = _child_table(document, _COMMANDS_KEY)
    create = _child_table(commands, _CREATE_KEY)
    for key in MANAGED_KEYS:
        create.pop(key, None)
    if defaults is not None:
        for key, value in managed_create_settings(defaults).items():
            create[key] = value
    if not create:
        commands.pop(_CREATE_KEY, None)
    if not commands:
        document.pop(_COMMANDS_KEY, None)
    if not document:
        path.unlink(missing_ok=True)
        return
    _write_document(path, document)


def read_create_defaults_type(path: Path) -> str | None:
    """The `commands.create.type` the file names, or None when it names none or is absent."""
    if not path.exists():
        return None
    raw = tomllib.loads(path.read_text())
    create = raw.get(_COMMANDS_KEY, {}).get(_CREATE_KEY, {})
    agent_type = create.get(TYPE_KEY) if isinstance(create, dict) else None
    return agent_type if isinstance(agent_type, str) and agent_type else None
