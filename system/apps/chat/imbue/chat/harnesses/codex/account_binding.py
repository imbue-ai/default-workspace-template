"""Codex's account binding: `CODEX_HOME`, and the `auth.json` link in the agent's codex home."""

from pathlib import Path
from typing import Final

from imbue.chat.harnesses.account_binding import CredentialLinkAccountBinding
from imbue.mngr_codex.codex_config import get_codex_auth_path
from imbue.mngr_codex.codex_config import get_codex_home

# codex keys its secret by a hash of the canonical CODEX_HOME unless the credential store is
# pinned to `file`. Without this pin a sign-in against an account dir can land in an OS
# keyring instead: auth.json is never written, the bind symlink dangles, the chat runs signed
# out -- and `codex login status` scoped to that same dir still reports success, so nothing
# downstream notices. `file` is codex's current default, but `auto` exists and prefers a
# keyring when one is present, so it is pinned explicitly.
_CODEX_CONFIG_TOML: Final = 'cli_auth_credentials_store = "file"\n'


class CodexAccountBinding(CredentialLinkAccountBinding):
    """Binds codex by repointing the agent's `auth.json` at the account's."""

    def account_env(self, account_dir: Path) -> dict[str, str]:
        return {"CODEX_HOME": str(account_dir)}

    def account_credential_path(self, account_dir: Path) -> Path:
        return get_codex_auth_path(account_dir)

    def agent_credential_path(self, agent_state_dir: Path) -> Path:
        return get_codex_auth_path(get_codex_home(agent_state_dir))

    def seed_account(self, account_dir: Path, work_dir: Path) -> None:
        super().seed_account(account_dir, work_dir)
        config = account_dir / "config.toml"
        if not config.exists():
            config.write_text(_CODEX_CONFIG_TOML)
