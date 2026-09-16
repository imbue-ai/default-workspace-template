"""Tests for binding an agent to an account.

The paths here are contracts with mngr's provisioning, so most of these assert the exact
shape rather than a property -- a path that drifts binds nothing and fails silently, with
the agent quietly running on the shared credential instead.
"""

import os
from pathlib import Path

import pytest

from imbue.chat.accounts import AccountError
from imbue.chat.accounts import commit_account
from imbue.chat.accounts import harness_for
from imbue.chat.accounts import mint_account_dir
from imbue.chat.accounts import resolve_account
from imbue.chat.accounts import set_default_account
from imbue.chat.accounts import set_mru
from imbue.chat.harnesses.account_binding import BindingError
from imbue.chat.harnesses.account_binding import CredentialLinkAccountBinding
from imbue.chat.harnesses.binding import REBIND_VERIFIED_HARNESSES
from imbue.chat.harnesses.binding import is_rebind_supported
from imbue.chat.harnesses.binding import resolve_binding
from imbue.chat.harnesses.harness_type import HarnessType
from imbue.chat.harnesses.registry import build_account_binding
from imbue.mngr_claude.claude_config import check_claude_dialogs_dismissed

_BOUND_HARNESSES = (
    HarnessType.CLAUDE,
    HarnessType.CODEX,
    HarnessType.ANTIGRAVITY,
    HarnessType.PI_CODING,
)


def _link_binding(harness: HarnessType) -> CredentialLinkAccountBinding:
    """The binding of a harness bound by a credential link, which is what names both ends of the link."""
    binding = build_account_binding(harness)
    assert isinstance(binding, CredentialLinkAccountBinding)
    return binding


def test_each_harness_scopes_through_exactly_one_variable(tmp_path: Path) -> None:
    """One variable per harness is the entire multi-account mechanism."""
    assert build_account_binding(HarnessType.CLAUDE).account_env(tmp_path) == {"CLAUDE_CONFIG_DIR": str(tmp_path)}
    assert build_account_binding(HarnessType.CODEX).account_env(tmp_path) == {"CODEX_HOME": str(tmp_path)}
    # agy has no config-dir override at all; relocating HOME is the only scope it offers.
    assert build_account_binding(HarnessType.ANTIGRAVITY).account_env(tmp_path) == {"HOME": str(tmp_path)}
    assert build_account_binding(HarnessType.PI_CODING).account_env(tmp_path) == {"PI_CODING_AGENT_DIR": str(tmp_path)}


def test_a_harness_with_no_scoping_raises_rather_than_binding_nothing(tmp_path: Path) -> None:
    with pytest.raises(BindingError):
        build_account_binding(HarnessType.OPENCODE).account_env(tmp_path)


def test_credential_paths_match_what_mngr_provisions(tmp_path: Path) -> None:
    state = tmp_path / "state"
    assert _link_binding(HarnessType.CODEX).agent_credential_path(state) == state / "plugin/codex/home/auth.json"
    assert _link_binding(HarnessType.PI_CODING).agent_credential_path(state) == state / "plugin/pi_coding/auth.json"
    assert _link_binding(HarnessType.ANTIGRAVITY).agent_credential_path(state) == (
        state / "plugin/antigravity/home/.gemini/antigravity-cli/antigravity-oauth-token"
    )
    # claude binds by environment, so it has no path to repoint.
    assert not isinstance(build_account_binding(HarnessType.CLAUDE), CredentialLinkAccountBinding)


def test_the_account_side_of_each_link_mirrors_the_agent_side(tmp_path: Path) -> None:
    """Source and destination must be the same shape or the symlink points at nothing."""
    for harness in (HarnessType.CODEX, HarnessType.ANTIGRAVITY, HarnessType.PI_CODING):
        source = _link_binding(harness).account_credential_path(tmp_path)
        agent_side = _link_binding(harness).agent_credential_path(tmp_path / "state")
        assert source.name == agent_side.name


def test_claude_binds_through_the_env_file(tmp_path: Path) -> None:
    """--env lands in <state>/env before provisioning, which is early enough; a post-create
    repoint would arrive after the first turn had already run."""
    args = build_account_binding(HarnessType.CLAUDE).create_args(tmp_path, tmp_path / "state")
    assert args == ["--env", f"CLAUDE_CONFIG_DIR={tmp_path}"]


def test_the_others_bind_by_replacing_the_provisioned_symlink(tmp_path: Path) -> None:
    for harness in (HarnessType.CODEX, HarnessType.ANTIGRAVITY, HarnessType.PI_CODING):
        flag, command = _link_binding(harness).create_args(tmp_path, tmp_path / "state")
        assert flag == "--extra-provision-command"
        # `ln -sfn` replaces whatever provisioning linked -- the same operation mngr used.
        assert "ln -sfn" in command
        assert str(_link_binding(harness).account_credential_path(tmp_path)) in command
        assert str(_link_binding(harness).agent_credential_path(tmp_path / "state")) in command


def test_the_provision_command_quotes_paths(tmp_path: Path) -> None:
    """It is shell-evaluated on the host, unlike the argv around it."""
    spaced = tmp_path / "a dir with spaces"
    _, command = build_account_binding(HarnessType.CODEX).create_args(spaced, tmp_path / "state")
    assert "'" in command


def test_seeding_claude_dismisses_the_dialogs_that_would_block_readiness(tmp_path: Path) -> None:
    """A fresh account folder has no onboarding state, because CLAUDE_CONFIG_DIR moves
    .claude.json INSIDE the dir. Unseeded, claude boots into the theme/trust dialogs, never
    signals readiness, and mngr destroys the agent."""
    work_dir = tmp_path / "workspace"
    work_dir.mkdir()
    account = tmp_path / "acct"

    build_account_binding(HarnessType.CLAUDE).seed_account(account, work_dir)

    # mngr's own verifier for the same condition -- it raises if anything is undismissed.
    check_claude_dialogs_dismissed(account / ".claude.json", work_dir)
    assert (account / "keybindings.json").exists()


def test_seeding_codex_pins_the_file_credential_store(tmp_path: Path) -> None:
    """Without the pin, codex can key its secret by a hash of CODEX_HOME and store it in an
    OS keyring: auth.json is never written, the bind symlink dangles, the chat runs signed
    out -- and `codex login status` against that dir still reports success."""
    account = tmp_path / "acct"
    build_account_binding(HarnessType.CODEX).seed_account(account, tmp_path)
    assert 'cli_auth_credentials_store = "file"' in (account / "config.toml").read_text()


def test_seeding_does_not_clobber_an_existing_codex_config(tmp_path: Path) -> None:
    account = tmp_path / "acct"
    account.mkdir()
    (account / "config.toml").write_text("model = 'gpt-5'\n")
    build_account_binding(HarnessType.CODEX).seed_account(account, tmp_path)
    assert (account / "config.toml").read_text() == "model = 'gpt-5'\n"


def test_seeding_is_idempotent(tmp_path: Path) -> None:
    work_dir = tmp_path / "workspace"
    work_dir.mkdir()
    for harness in _BOUND_HARNESSES:
        account = tmp_path / f"acct-{harness.value}"
        build_account_binding(harness).seed_account(account, work_dir)
        build_account_binding(harness).seed_account(account, work_dir)
        assert account.is_dir()


def _bound_id(account_id: str = "", home: Path | None = None) -> str | None:
    account = resolve_binding(account_id, home)
    return None if account is None else account.id


def _account(home: Path, lane: str, display: str) -> str:
    """Mint and commit an account, returning its id."""
    account_id, _ = mint_account_dir(home)
    commit_account(account_id, lane, display, home)
    return account_id


def test_no_accounts_is_refused_rather_than_bound_to_nothing(tmp_path: Path) -> None:
    """There is no shared login to fall back to. Returning None here let a caller create an
    agent anyway -- one that cannot take a turn, and says nothing about why."""
    with pytest.raises(AccountError):
        resolve_binding(home=tmp_path)


def test_the_most_recently_used_account_wins(tmp_path: Path) -> None:
    first = _account(tmp_path, "anthropic", "Anthropic")
    second = _account(tmp_path, "anthropic", "Anthropic")

    assert _bound_id(home=tmp_path) == second
    set_mru(first, tmp_path)
    assert _bound_id(home=tmp_path) == first


def test_a_pinned_default_beats_the_most_recently_used_account(tmp_path: Path) -> None:
    """The pin is what lets the user say which harness an unnamed launch opens on; the mru
    keeps moving under it with every launch and sign-in."""
    pinned = _account(tmp_path, "anthropic", "Anthropic")
    recent = _account(tmp_path, "google", "Google")
    set_default_account(pinned, True, tmp_path)

    assert _bound_id(home=tmp_path) == pinned
    set_mru(recent, tmp_path)
    assert _bound_id(home=tmp_path) == pinned


def test_a_pinned_default_on_a_lane_this_build_lacks_falls_back_to_the_mru(tmp_path: Path) -> None:
    stale = _account(tmp_path, "a-lane-from-the-future", "Mystery")
    usable = _account(tmp_path, "anthropic", "Anthropic")
    set_default_account(stale, True, tmp_path)

    assert _bound_id(home=tmp_path) == usable


def test_the_account_decides_the_harness(tmp_path: Path) -> None:
    """The caller never names one, so a chat cannot claim a harness its credential is not."""
    agy = _account(tmp_path, "google", "Google")
    codex = _account(tmp_path, "openai", "OpenAI")

    assert harness_for(resolve_account(agy, tmp_path)) is HarnessType.ANTIGRAVITY
    assert harness_for(resolve_account(codex, tmp_path)) is HarnessType.CODEX


def test_an_account_on_a_lane_this_build_lacks_is_refused(tmp_path: Path) -> None:
    """Binding it would produce a chat with no way to know which harness to run."""
    stale = _account(tmp_path, "a-lane-from-the-future", "Mystery")
    with pytest.raises(BindingError):
        resolve_binding(stale, tmp_path)


def test_an_explicit_account_beats_the_most_recently_used_one(tmp_path: Path) -> None:
    wanted = _account(tmp_path, "anthropic", "Anthropic")
    _account(tmp_path, "anthropic", "Anthropic")

    assert _bound_id(wanted, tmp_path) == wanted


def test_claude_is_bound_by_an_export_that_children_inherit(tmp_path: Path) -> None:
    """A worker created from inside a bound chat has to run on that chat's account.

    mngr sources an agent's env file into every process in its tmux session and propagates
    CLAUDE_CONFIG_DIR to a child agent when the spawning shell already carries it, so
    `/launch-task` inherits for free -- but only while claude is bound by that export.
    Binding it any other way, or with any other variable, signs every worker out with no
    error anywhere. This test exists to make that a deliberate choice rather than an
    accident.
    """
    account = tmp_path / "acct"

    args = build_account_binding(HarnessType.CLAUDE).create_args(account, tmp_path / "state")

    assert args == ["--env", f"CLAUDE_CONFIG_DIR={account}"]
    assert build_account_binding(HarnessType.CLAUDE).account_env(account) == {"CLAUDE_CONFIG_DIR": str(account)}


def test_every_scoped_harness_can_be_rebound_and_the_unscoped_one_cannot() -> None:
    assert REBIND_VERIFIED_HARNESSES == frozenset(_BOUND_HARNESSES)
    assert all(is_rebind_supported(harness) for harness in _BOUND_HARNESSES)
    assert not is_rebind_supported(HarnessType.OPENCODE)


def test_rebinding_claude_rewrites_only_the_config_dir_line_and_lands_whole(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    env_path = state / "env"
    env_path.write_text(f"MNGR_AGENT_ID=agent-1\nCLAUDE_CONFIG_DIR={tmp_path / 'old'}\nMINDS_CHAT_ID=agent-1\n")
    env_path.chmod(0o600)

    build_account_binding(HarnessType.CLAUDE).rebind_agent(tmp_path / "new", state)

    assert (
        env_path.read_text() == f"MNGR_AGENT_ID=agent-1\nMINDS_CHAT_ID=agent-1\nCLAUDE_CONFIG_DIR={tmp_path / 'new'}\n"
    )
    assert oct(env_path.stat().st_mode & 0o777) == "0o600"
    assert not env_path.with_name("env.rebind-tmp").exists()
    # Again is the same file; a value with a space is quoted the way mngr quotes its own.
    build_account_binding(HarnessType.CLAUDE).rebind_agent(tmp_path / "new", state)
    assert env_path.read_text().count("CLAUDE_CONFIG_DIR=") == 1
    build_account_binding(HarnessType.CLAUDE).rebind_agent(tmp_path / "with space", state)
    assert env_path.read_text().endswith(f'CLAUDE_CONFIG_DIR="{tmp_path / "with space"}"\n')
    # An agent with no env file yet gets one holding just the line.
    bare = tmp_path / "bare"
    bare.mkdir()
    build_account_binding(HarnessType.CLAUDE).rebind_agent(tmp_path / "new", bare)
    assert (bare / "env").read_text() == f"CLAUDE_CONFIG_DIR={tmp_path / 'new'}\n"


@pytest.mark.parametrize("harness", [HarnessType.CODEX, HarnessType.PI_CODING, HarnessType.ANTIGRAVITY])
def test_rebinding_the_others_repoints_the_credential_link_whatever_was_there(
    tmp_path: Path, harness: HarnessType
) -> None:
    state = tmp_path / "state"
    dest = _link_binding(harness).agent_credential_path(state)
    source = _link_binding(harness).account_credential_path(tmp_path / "account")
    # Nothing there yet (a fresh state dir), then a copy, then an older link: each ends as the new link.
    for before in (None, "a copied credential", str(tmp_path / "elsewhere")):
        if before is None:
            pass
        elif before.startswith(str(tmp_path)):
            dest.unlink(missing_ok=True)
            os.symlink(before, dest)
        else:
            dest.unlink(missing_ok=True)
            dest.write_text(before)
        _link_binding(harness).rebind_agent(tmp_path / "account", state)
        assert dest.is_symlink() and os.readlink(dest) == str(source)
    assert not dest.with_name(f"{dest.name}.rebind-tmp").exists()


def test_a_harness_with_no_binding_cannot_be_rebound(tmp_path: Path) -> None:
    with pytest.raises(BindingError):
        build_account_binding(HarnessType.OPENCODE).rebind_agent(tmp_path / "account", tmp_path / "state")
