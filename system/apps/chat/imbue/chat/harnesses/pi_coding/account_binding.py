"""Pi's account binding: `PI_CODING_AGENT_DIR`, and the `auth.json` and `models.json` links in the agent's config dir."""

from pathlib import Path
from typing import Final

from imbue.chat.harnesses.account_binding import CredentialLinkAccountBinding
from imbue.chat.harnesses.pi_coding.local_model import MODELS_FILENAME
from imbue.chat.harnesses.pi_coding.model import PI_CONFIG_DIR_RELPATH

_AUTH_FILENAME: Final = "auth.json"


class PiAccountBinding(CredentialLinkAccountBinding):
    """Binds pi by repointing the agent's `auth.json` at the account's, and its `models.json` too.

    Nothing is seeded: pi has no onboarding dialog to dismiss, and writes its whole credential
    file itself on a successful sign-in.

    `models.json` is linked for every account, though only one on a local model server has the
    file. pi reads a dangling link as no file (measured on 0.87.1), and linking it always is what
    makes a rebind off such an account drop its provider instead of leaving the old link behind.
    """

    def account_env(self, account_dir: Path) -> dict[str, str]:
        return {"PI_CODING_AGENT_DIR": str(account_dir)}

    def account_credential_path(self, account_dir: Path) -> Path:
        return account_dir / _AUTH_FILENAME

    def agent_credential_path(self, agent_state_dir: Path) -> Path:
        return agent_state_dir / PI_CONFIG_DIR_RELPATH / _AUTH_FILENAME

    def linked_paths(self, account_dir: Path, agent_state_dir: Path) -> tuple[tuple[Path, Path], ...]:
        config_dir = agent_state_dir / PI_CONFIG_DIR_RELPATH
        return (
            *super().linked_paths(account_dir, agent_state_dir),
            (account_dir / MODELS_FILENAME, config_dir / MODELS_FILENAME),
        )
