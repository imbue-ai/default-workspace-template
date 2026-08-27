"""Tests for binding an agent to an account.

The paths here are contracts with mngr's provisioning, so most of these assert the exact
shape rather than a property -- a path that drifts binds nothing and fails silently, with
the agent quietly running on the shared credential instead.
"""

from pathlib import Path

import pytest

from imbue.mngr_claude.claude_config import check_claude_dialogs_dismissed
from imbue.system_interface.accounts import commit_account
from imbue.system_interface.accounts import mint_account_dir
from imbue.system_interface.accounts import set_mru
from imbue.system_interface.harnesses.binding import BindingError
from imbue.system_interface.harnesses.binding import account_credential_path
from imbue.system_interface.harnesses.binding import account_env
from imbue.system_interface.harnesses.binding import agent_credential_path
from imbue.system_interface.harnesses.binding import create_args
from imbue.system_interface.harnesses.binding import resolve_binding
from imbue.system_interface.harnesses.binding import seed_account
from imbue.system_interface.harnesses.harness_type import HarnessType

_BOUND_HARNESSES = (
    HarnessType.CLAUDE,
    HarnessType.CODEX,
    HarnessType.ANTIGRAVITY,
    HarnessType.PI_CODING,
)


def test_each_harness_scopes_through_exactly_one_variable(tmp_path: Path) -> None:
    """One variable per harness is the entire multi-account mechanism."""
    assert account_env(HarnessType.CLAUDE, tmp_path) == {"CLAUDE_CONFIG_DIR": str(tmp_path)}
    assert account_env(HarnessType.CODEX, tmp_path) == {"CODEX_HOME": str(tmp_path)}
    # agy has no config-dir override at all; relocating HOME is the only scope it offers.
    assert account_env(HarnessType.ANTIGRAVITY, tmp_path) == {"HOME": str(tmp_path)}
    assert account_env(HarnessType.PI_CODING, tmp_path) == {"PI_CODING_AGENT_DIR": str(tmp_path)}


def test_a_harness_with_no_scoping_raises_rather_than_binding_nothing(tmp_path: Path) -> None:
    with pytest.raises(BindingError):
        account_env(HarnessType.OPENCODE, tmp_path)


def test_credential_paths_match_what_mngr_provisions(tmp_path: Path) -> None:
    state = tmp_path / "state"
    assert agent_credential_path(HarnessType.CODEX, state) == state / "plugin/codex/home/auth.json"
    assert agent_credential_path(HarnessType.PI_CODING, state) == state / "plugin/pi_coding/auth.json"
    assert agent_credential_path(HarnessType.ANTIGRAVITY, state) == (
        state / "plugin/antigravity/home/.gemini/antigravity-cli/antigravity-oauth-token"
    )
    # claude binds by environment, so it has no path to repoint.
    assert agent_credential_path(HarnessType.CLAUDE, state) is None


def test_the_account_side_of_each_link_mirrors_the_agent_side(tmp_path: Path) -> None:
    """Source and destination must be the same shape or the symlink points at nothing."""
    for harness in (HarnessType.CODEX, HarnessType.ANTIGRAVITY, HarnessType.PI_CODING):
        source = account_credential_path(harness, tmp_path)
        agent_side = agent_credential_path(harness, tmp_path / "state")
        assert source is not None and agent_side is not None
        assert source.name == agent_side.name


def test_claude_binds_through_the_env_file(tmp_path: Path) -> None:
    """--env lands in <state>/env before provisioning, which is early enough; a post-create
    repoint would arrive after the first turn had already run."""
    args = create_args(HarnessType.CLAUDE, tmp_path, tmp_path / "state")
    assert args == ["--env", f"CLAUDE_CONFIG_DIR={tmp_path}"]


def test_the_others_bind_by_replacing_the_provisioned_symlink(tmp_path: Path) -> None:
    for harness in (HarnessType.CODEX, HarnessType.ANTIGRAVITY, HarnessType.PI_CODING):
        flag, command = create_args(harness, tmp_path, tmp_path / "state")
        assert flag == "--extra-provision-command"
        # `ln -sfn` replaces whatever provisioning linked -- the same operation mngr used.
        assert "ln -sfn" in command
        assert str(account_credential_path(harness, tmp_path)) in command
        assert str(agent_credential_path(harness, tmp_path / "state")) in command


def test_the_provision_command_quotes_paths(tmp_path: Path) -> None:
    """It is shell-evaluated on the host, unlike the argv around it."""
    spaced = tmp_path / "a dir with spaces"
    _, command = create_args(HarnessType.CODEX, spaced, tmp_path / "state")
    assert "'" in command


def test_seeding_claude_dismisses_the_dialogs_that_would_block_readiness(tmp_path: Path) -> None:
    """A fresh account folder has no onboarding state, because CLAUDE_CONFIG_DIR moves
    .claude.json INSIDE the dir. Unseeded, claude boots into the theme/trust dialogs, never
    signals readiness, and mngr destroys the agent."""
    work_dir = tmp_path / "workspace"
    work_dir.mkdir()
    account = tmp_path / "acct"

    seed_account(HarnessType.CLAUDE, account, work_dir)

    # mngr's own verifier for the same condition -- it raises if anything is undismissed.
    check_claude_dialogs_dismissed(account / ".claude.json", work_dir)
    assert (account / "keybindings.json").exists()


def test_seeding_codex_pins_the_file_credential_store(tmp_path: Path) -> None:
    """Without the pin, codex can key its secret by a hash of CODEX_HOME and store it in an
    OS keyring: auth.json is never written, the bind symlink dangles, the chat runs signed
    out -- and `codex login status` against that dir still reports success."""
    account = tmp_path / "acct"
    seed_account(HarnessType.CODEX, account, tmp_path)
    assert 'cli_auth_credentials_store = "file"' in (account / "config.toml").read_text()


def test_seeding_does_not_clobber_an_existing_codex_config(tmp_path: Path) -> None:
    account = tmp_path / "acct"
    account.mkdir()
    (account / "config.toml").write_text("model = 'gpt-5'\n")
    seed_account(HarnessType.CODEX, account, tmp_path)
    assert (account / "config.toml").read_text() == "model = 'gpt-5'\n"


def test_seeding_is_idempotent(tmp_path: Path) -> None:
    work_dir = tmp_path / "workspace"
    work_dir.mkdir()
    for harness in _BOUND_HARNESSES:
        account = tmp_path / f"acct-{harness.value}"
        seed_account(harness, account, work_dir)
        seed_account(harness, account, work_dir)
        assert account.is_dir()


def _bound_id(harness: HarnessType, account_id: str = "", home: Path | None = None) -> str | None:
    account = resolve_binding(harness, account_id, home)
    return None if account is None else account.id


def _account(home: Path, lane: str, display: str) -> str:
    """Mint and commit an account, returning its id."""
    account_id, _ = mint_account_dir(home)
    commit_account(account_id, lane, display, home)
    return account_id


def test_no_accounts_leaves_the_agent_on_the_shared_login(tmp_path: Path) -> None:
    """None is the pre-accounts behaviour, which is what lets binding land before any UI."""
    assert resolve_binding(HarnessType.CLAUDE, home=tmp_path) is None


def test_the_most_recently_used_account_on_the_harness_wins(tmp_path: Path) -> None:
    first = _account(tmp_path, "anthropic", "Anthropic")
    second = _account(tmp_path, "anthropic", "Anthropic")

    assert _bound_id(HarnessType.CLAUDE, home=tmp_path) == second
    set_mru(first, tmp_path)
    assert _bound_id(HarnessType.CLAUDE, home=tmp_path) == first


def test_an_account_on_another_harness_is_not_picked(tmp_path: Path) -> None:
    """The mru is global, so a codex sign-in must not become an agy agent's account."""
    agy = _account(tmp_path, "google", "Google")
    _account(tmp_path, "openai", "OpenAI")

    assert _bound_id(HarnessType.ANTIGRAVITY, home=tmp_path) == agy
    assert resolve_binding(HarnessType.OPENCODE, home=tmp_path) is None


def test_binding_an_account_to_the_wrong_harness_is_refused(tmp_path: Path) -> None:
    """Silently allowing it would produce a chat that cannot take a turn."""
    codex = _account(tmp_path, "openai", "OpenAI")
    with pytest.raises(BindingError):
        resolve_binding(HarnessType.ANTIGRAVITY, codex, tmp_path)


def test_an_explicit_account_beats_the_most_recently_used_one(tmp_path: Path) -> None:
    wanted = _account(tmp_path, "anthropic", "Anthropic")
    _account(tmp_path, "anthropic", "Anthropic")

    assert _bound_id(HarnessType.CLAUDE, wanted, tmp_path) == wanted
