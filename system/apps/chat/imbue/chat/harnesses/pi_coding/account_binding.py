"""Pi's account binding: `PI_CODING_AGENT_DIR`, and the `auth.json` link in the agent's pi config dir."""

from pathlib import Path
from typing import Final

from imbue.chat.harnesses.account_binding import CredentialLinkAccountBinding
from imbue.chat.harnesses.pi_coding.model import PI_CONFIG_DIR_RELPATH

_AUTH_FILENAME: Final = "auth.json"


class PiAccountBinding(CredentialLinkAccountBinding):
    """Binds pi by repointing the agent's `auth.json` at the account's.

    Nothing is seeded: pi has no onboarding dialog to dismiss, and writes its whole credential
    file itself on a successful sign-in.
    """

    def account_env(self, account_dir: Path) -> dict[str, str]:
        return {"PI_CODING_AGENT_DIR": str(account_dir)}

    def account_credential_path(self, account_dir: Path) -> Path:
        return account_dir / _AUTH_FILENAME

    def agent_credential_path(self, agent_state_dir: Path) -> Path:
        return agent_state_dir / PI_CONFIG_DIR_RELPATH / _AUTH_FILENAME
