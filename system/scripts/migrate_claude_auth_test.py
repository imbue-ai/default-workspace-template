"""Tests for the host-env -> account Claude auth migration script.

Exercised via importlib, the same pattern as claude_oom_launch_test.py. There is no
restart phase to cover any more: an account is read when a chat is created, not frozen
into a running process's environment, so nothing has to be torn down to see it.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from imbue.system_interface.accounts import account_dir
from imbue.system_interface.accounts import read_index

_SCRIPT = Path(__file__).parent / "migrate_claude_auth.py"
_spec = importlib.util.spec_from_file_location("migrate_claude_auth", _SCRIPT)
assert _spec is not None and _spec.loader is not None
migration = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(migration)


@pytest.fixture
def host_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A workspace whose host env file and accounts store are both throwaway."""
    host = tmp_path / "host"
    host.mkdir()
    monkeypatch.setenv("MNGR_HOST_DIR", str(host))
    monkeypatch.setenv("MINDS_ACCOUNTS_ROOT", str(tmp_path / "accounts"))
    return host


def _only_account_env() -> dict[str, str]:
    accounts = read_index().accounts
    assert len(accounts) == 1, f"expected exactly one migrated account, got {accounts}"
    settings = json.loads((account_dir(accounts[0].id) / "settings.json").read_text())
    return dict(settings["env"])


def test_migrate_moves_keys_into_an_account_and_scrubs_host_env(host_dir: Path) -> None:
    (host_dir / "env").write_text(
        "CLAUDE_CONFIG_DIR=/home/user/.mngr/claude\n"
        "ANTHROPIC_API_KEY=sk-old-key\n"
        "ANTHROPIC_BASE_URL=https://litellm.example\n"
    )

    assert migration.migrate() is True

    # Both keys: the base URL is the whole point of a proxied setup, and dropping it
    # would silently route the migrated account to Anthropic instead.
    assert _only_account_env() == {
        "ANTHROPIC_API_KEY": "sk-old-key",
        "ANTHROPIC_BASE_URL": "https://litellm.example",
    }
    host_env_text = (host_dir / "env").read_text()
    assert "ANTHROPIC_API_KEY" not in host_env_text
    assert "ANTHROPIC_BASE_URL" not in host_env_text
    # Non-managed host env keys survive the scrub.
    assert "CLAUDE_CONFIG_DIR=/home/user/.mngr/claude" in host_env_text


def test_migrate_is_noop_when_host_env_holds_no_auth_keys(host_dir: Path) -> None:
    (host_dir / "env").write_text("CLAUDE_CONFIG_DIR=/home/user/.mngr/claude\n")

    assert migration.migrate() is False
    assert read_index().accounts == ()


def test_migrate_rerun_after_success_mints_only_one_account(host_dir: Path) -> None:
    """The scrub is what makes it idempotent -- a second run finds nothing to move."""
    (host_dir / "env").write_text("ANTHROPIC_API_KEY=sk-old-key\n")

    assert migration.migrate() is True
    assert migration.migrate() is False
    assert len(read_index().accounts) == 1


def test_migrate_carries_a_token_rather_than_a_key(host_dir: Path) -> None:
    """A subscription workspace that was given a long-lived token still migrates."""
    (host_dir / "env").write_text("CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-example\n")

    assert migration.migrate() is True

    assert _only_account_env() == {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-example"}
