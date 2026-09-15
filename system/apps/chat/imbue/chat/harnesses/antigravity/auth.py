"""agy's API-key mode: what an account holds when it signed in with a pasted Gemini key.

agy can be signed in two ways, and they share no file. A browser OAuth leaves a token where
`account_scope.account_credential_path` points, which is what mngr links per agent. An API key
is not a file agy reads at all: the key is an environment variable, and the mode is a flag in
agy's own settings.json. The two halves are each useless alone -- with `modelProvider` unset agy
ignores `GEMINI_API_KEY` and runs the browser flow, and with it set and no key in the
environment agy exits at once saying so.

So an API-key account carries both: the key in a dotenv file, which `mngr create --env-file`
reads and folds into every agent bound to the account, and the mode in the settings.json under
the account folder (the account folder IS agy's `$HOME`, see `account_scope`). That env file's
presence is also the marker for "this account is on a key, not on OAuth" -- binding, the
workspace's create defaults and the promote probe all branch on it.

Nothing here reads the account store, so the store's own derived output (`create_defaults`) can
use it without an import cycle.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from typing import Final

from imbue.chat.harnesses.harness_type import HarnessType
from imbue.mngr.utils.env_utils import parse_env_file
from imbue.mngr_antigravity.antigravity_config import get_antigravity_settings_path
from imbue.mngr_antigravity.antigravity_config import serialize_antigravity_settings


class AntigravitySettingsError(RuntimeError):
    """agy's settings.json holds something this cannot merge into."""


GEMINI_API_KEY_ENV_VAR: Final = "GEMINI_API_KEY"
# A dotenv file rather than a shell snippet: `mngr create --env-file` parses it with
# python-dotenv, and the same parser reads it back here.
GEMINI_ENV_FILENAME: Final = "gemini.env"
# The settings.json key that puts agy in headless API-key mode, and its one value. Supported
# since agy 1.1.13; the template pins 1.1.22.
MODEL_PROVIDER_KEY: Final = "modelProvider"
GEMINI_MODEL_PROVIDER: Final = "gemini"

# The `mngr create -S/--setting` override that puts a per-AGENT agy in the same mode. An agent
# gets its own `$HOME`, so the account's settings.json is not the one it reads: mngr builds that
# file at provision from the agent type's `settings_overrides`, a free-form dict it folds in
# whole, which is the only channel into it. The key is the mngr agent type, not the `agy` alias.
#
# Spelled with `__extend` and a JSON object, never as a bare dotted path: antigravity's
# `settings_overrides` is a plain dict on the mngr side (unlike claude's, which deep-merges), so
# a dotted assignment over the non-empty table `.mngr/settings.toml` already gives the type is
# refused by mngr's settings narrowing guard, and the create fails before any agent exists.
# Same shape as the `first` template's codex entry, for the same reason.
GEMINI_MODE_SETTING: Final = "agent_types.{}.settings_overrides__extend={}".format(
    HarnessType.ANTIGRAVITY.value, json.dumps({MODEL_PROVIDER_KEY: GEMINI_MODEL_PROVIDER}, separators=(",", ":"))
)


def gemini_env_path(account_dir: Path) -> Path:
    """The dotenv file holding an API-key account's key."""
    return account_dir / GEMINI_ENV_FILENAME


def has_gemini_api_key(account_dir: Path) -> bool:
    """Whether this account signed in with a pasted key rather than through a browser."""
    return gemini_env_path(account_dir).is_file()


def gemini_credential_paths(account_dir: Path) -> tuple[Path, ...]:
    """Every file the API-key sign-in writes, so a rejected key can be rolled back.

    The settings flag counts as a credential: left behind after the key is gone, it is worse
    than nothing, because agy then refuses to start instead of falling back to the browser
    flow.
    """
    return (gemini_env_path(account_dir), get_antigravity_settings_path(account_dir))


def read_gemini_api_key(account_dir: Path) -> str | None:
    """The key this account holds, or None when it has no key file or the file names none."""
    path = gemini_env_path(account_dir)
    if not path.is_file():
        return None
    return parse_env_file(path.read_text()).get(GEMINI_API_KEY_ENV_VAR)


def write_gemini_api_key(account_dir: Path, api_key: str) -> None:
    """Put `account_dir` into agy's API-key mode with `api_key`.

    Both files, because either alone leaves agy unable to run: the key with no mode is ignored,
    and the mode with no key makes agy exit before its first turn.
    """
    env_path = gemini_env_path(account_dir)
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text(f"{GEMINI_API_KEY_ENV_VAR}={api_key}\n")
    env_path.chmod(0o600)
    settings_path = get_antigravity_settings_path(account_dir)
    settings = _read_settings(settings_path)
    settings[MODEL_PROVIDER_KEY] = GEMINI_MODEL_PROVIDER
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(serialize_antigravity_settings(settings))


def _read_settings(settings_path: Path) -> dict[str, Any]:
    """agy's settings.json as it stands, so the mode is merged over it rather than replacing it.

    Read here rather than with `read_antigravity_settings`, whose signature takes a host: an
    account folder is always on this machine, and nothing in this app holds a host object.

    A file that does not parse is raised on rather than treated as empty. agy writes this file
    itself, so overwriting content that failed to read would discard whatever it put there.
    """
    if not settings_path.is_file():
        return {}
    content = settings_path.read_text()
    if not content.strip():
        return {}
    try:
        parsed: Any = json.loads(content)
    except json.JSONDecodeError as e:
        raise AntigravitySettingsError(f"{settings_path} holds malformed JSON ({e}); refusing to overwrite it") from e
    if not isinstance(parsed, dict):
        raise AntigravitySettingsError(f"{settings_path} is a {type(parsed).__name__}, not an object")
    return parsed
