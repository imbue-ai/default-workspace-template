"""Antigravity's account binding: `HOME`, and the OAuth token link in the agent's relocated home."""

from pathlib import Path
from typing import Final

from imbue.chat.harnesses.account_binding import CredentialLinkAccountBinding
from imbue.mngr_antigravity.antigravity_config import get_antigravity_oauth_token_path

# Kept in sync with `_AGY_HOME_RELATIVE_PATH` in mngr_antigravity's plugin.py, which is
# private there. agy relocates the whole HOME rather than exposing a config-dir override.
_AGY_HOME_RELATIVE_PATH: Final[tuple[str, ...]] = ("plugin", "antigravity", "home")


class AntigravityAccountBinding(CredentialLinkAccountBinding):
    """Binds agy by repointing the agent's OAuth token at the account's.

    Nothing is seeded: agy has no onboarding dialog to dismiss, and writes its whole credential
    file itself on a successful sign-in.
    """

    def account_env(self, account_dir: Path) -> dict[str, str]:
        # agy has no config-dir override; the home IS the scope.
        return {"HOME": str(account_dir)}

    def account_credential_path(self, account_dir: Path) -> Path:
        return get_antigravity_oauth_token_path(account_dir)

    def agent_credential_path(self, agent_state_dir: Path) -> Path:
        return get_antigravity_oauth_token_path(agent_state_dir.joinpath(*_AGY_HOME_RELATIVE_PATH))
