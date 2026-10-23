"""Grok's account binding: the `GROK_AUTH_PATH` line of the agent's env file, naming the account's `auth.json`."""

import json
from pathlib import Path
from typing import Final

from loguru import logger as _loguru_logger
from pydantic import SecretStr

from imbue.chat.harnesses.account_binding import AGENT_ENV_FILENAME
from imbue.chat.harnesses.account_binding import AccountBinding
from imbue.chat.harnesses.account_binding import DefaultCreateBinding
from imbue.chat.harnesses.account_binding import rewrite_env_var
from imbue.mngr_grok.api import GROK_AUTH_PATH_ENV_VAR
from imbue.mngr_grok.api import build_pinned_grok_environment
from imbue.mngr_grok.api import render_pinned_grok_home

logger = _loguru_logger

_GROK_HOME_ENV_VAR: Final = "GROK_HOME"
AUTH_FILENAME: Final = "auth.json"
# The auth.json scope grok keeps a stored API key under; every other scope is a session login.
API_KEY_SCOPE: Final = "xai::api_key"
# Grok cannot read an auth.json whose scope lacks one of these, and then takes it as holding no credential at all.
_REQUIRED_SCOPE_FIELDS: Final = ("key", "auth_mode", "create_time", "user_id")


def _auth_path(account_dir: Path) -> Path:
    return account_dir / AUTH_FILENAME


class GrokAccountBinding(AccountBinding):
    """Binds grok by the credential file it reads: every agent on an account shares one file and its refresh lock.

    Only `GROK_AUTH_PATH` reaches an agent. Its `GROK_HOME` stays the per-agent home mngr provisions, which is also
    where mngr resolves the agent's trust store and consent answers from.
    """

    def account_env(self, account_dir: Path) -> dict[str, str]:
        return {
            **build_pinned_grok_environment(),
            _GROK_HOME_ENV_VAR: str(account_dir),
            GROK_AUTH_PATH_ENV_VAR: str(_auth_path(account_dir)),
        }

    def credential_paths(self, account_dir: Path) -> tuple[Path, ...]:
        return (_auth_path(account_dir),)

    def seed_account(self, account_dir: Path, work_dir: Path) -> None:
        super().seed_account(account_dir, work_dir)
        # Keeps the `[consent.answers.*]` a grok run in the account folder recorded, which mngr merges into each
        # agent's home at provision.
        render_pinned_grok_home(account_dir)

    def create_args(self, account_dir: Path, agent_state_dir: Path) -> list[str]:
        return ["--env", f"{GROK_AUTH_PATH_ENV_VAR}={_auth_path(account_dir)}"]

    def default_create_binding(self, account_dir: Path) -> DefaultCreateBinding:
        return DefaultCreateBinding(
            env=(f"{GROK_AUTH_PATH_ENV_VAR}={_auth_path(account_dir)}",), provision_commands=()
        )

    def rebind_agent(self, account_dir: Path, agent_state_dir: Path) -> None:
        rewrite_env_var(agent_state_dir / AGENT_ENV_FILENAME, GROK_AUTH_PATH_ENV_VAR, str(_auth_path(account_dir)))


def read_sole_api_key(account_dir: Path) -> SecretStr | None:
    """The API key in an account's `auth.json` when a readable `xai::api_key` scope is its only credential, or None."""
    path = _auth_path(account_dir)
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None
    except OSError as e:
        logger.warning("Could not read grok's {}: {}", path, e)
        return None
    try:
        scopes = json.loads(text)
    except json.JSONDecodeError as e:
        logger.warning("Could not parse grok's {}: {}", path, e)
        return None
    if not isinstance(scopes, dict) or set(scopes) != {API_KEY_SCOPE}:
        return None
    scope = scopes[API_KEY_SCOPE]
    if not isinstance(scope, dict) or not all(isinstance(scope.get(field), str) for field in _REQUIRED_SCOPE_FIELDS):
        logger.warning("Grok's {} has an {} scope grok cannot read", path, API_KEY_SCOPE)
        return None
    return SecretStr(scope["key"])
