"""Tests for :mod:`launch_custom_agent_skill.launcher`.

Run via: ``uv run --no-sync pytest .agents/skills/launch-custom-agent/python``

A recording ``Runner`` is injected so no real ``mngr`` processes are spawned; the
built argvs are pinned against the live mngr CLI via ``mngr_cli_contract``. Account
resolution runs against a real index file under a temp ``MINDS_ACCOUNTS_ROOT``.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

import pytest
from imbue.chat import accounts
from imbue.chat.harnesses.harness_type import HarnessType
from imbue.chat.harnesses.lanes import LANES
from imbue.mngr.config.loader import _parse_mngr_env_overrides
from mngr_cli_contract.contract import assert_mngr_argv_valid

from launch_custom_agent_skill import launcher

_AGENT_ID = "agent-00000000000000000000000000abcdef"


@dataclass
class _RecordedCall:
    argv: list[str]
    kwargs: dict[str, Any]


@dataclass
class _StubResult:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


@dataclass
class _RecordingRunner(launcher.Runner):
    """Answers `mngr list` with canned records and records every other call."""

    listing_records: list[dict[str, Any]] = field(default_factory=list)
    listing_returncode: int = 0
    calls: list[_RecordedCall] = field(default_factory=list)

    def run(self, argv: Sequence[str], **kwargs: Any):
        call = _RecordedCall(argv=list(argv), kwargs=kwargs)
        self.calls.append(call)
        if list(argv[:2]) == ["mngr", "list"]:
            return _StubResult(
                returncode=self.listing_returncode,
                stdout="".join(
                    json.dumps(record) + "\n" for record in self.listing_records
                ),
            )
        return _StubResult()

    def create_calls(self) -> list[_RecordedCall]:
        return [call for call in self.calls if call.argv[:2] == ["mngr", "create"]]


def _lane_id_for(harness: HarnessType) -> str:
    return next(lane.id for lane in LANES if lane.harness is harness)


def _write_account_index(accounts_root: Path, index: accounts.AccountIndex) -> None:
    accounts_root.mkdir(parents=True, exist_ok=True)
    (accounts_root / "index.json").write_text(json.dumps(index.model_dump()))
    # A signed-in account always has its folder (sign-in creates it), and the
    # launcher's claude trust seeding writes into it.
    for account in index.accounts:
        (accounts_root / account.id).mkdir(exist_ok=True)


def _account(account_id: str, harness: HarnessType) -> accounts.Account:
    return accounts.Account(
        id=account_id, lane=_lane_id_for(harness), seq=1, display=str(harness)
    )


@pytest.fixture
def workspace_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A cwd that passes the workspace-root check, with one claude account signed in."""
    (tmp_path / ".mngr").mkdir()
    (tmp_path / ".mngr" / "settings.toml").write_text("")
    monkeypatch.chdir(tmp_path)
    accounts_root = tmp_path / "accounts"
    monkeypatch.setenv("MINDS_ACCOUNTS_ROOT", str(accounts_root))
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path / "host"))
    _write_account_index(
        accounts_root,
        accounts.AccountIndex(accounts=(_account("acct-claude", HarnessType.CLAUDE),)),
    )
    return tmp_path


def _git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "their-repo"
    repo.mkdir()
    subprocess.run(["git", "init", "--quiet", str(repo)], check=True)
    return repo


def _launch(
    source_path: Path,
    mode: str,
    runner: _RecordingRunner | None = None,
    **overrides: Any,
) -> int:
    """Call ``launch()`` with innocuous defaults, so each test states only what it varies."""
    kwargs: dict[str, Any] = {
        "name": "my-task",
        "account_id": "",
        "harness_name": "",
        "branch": "",
        "display_name": "",
        "message": "",
    }
    kwargs.update(overrides)
    return launcher.launch(
        source_path=source_path,
        mode=mode,
        runner=runner if runner is not None else _RecordingRunner(),
        **kwargs,
    )


def test_launch_builds_a_valid_mngr_create_for_worktree_mode(
    workspace_root: Path,
) -> None:
    repo = _git_repo(workspace_root)
    runner = _RecordingRunner()

    assert (
        _launch(
            repo,
            "worktree",
            runner,
            branch="main",
            display_name="My task",
            message="do the thing",
        )
        == 0
    )

    (create,) = runner.create_calls()
    assert_mngr_argv_valid(create.argv)
    assert create.argv[create.argv.index("--from") + 1] == f":{repo}"
    assert create.argv[create.argv.index("--transfer") + 1] == "git-worktree"
    # The worktree lands at an explicit path, known before the create (the trust
    # seeding below needs it); mngr would otherwise generate one.
    assert (
        create.argv[create.argv.index("--target-path") + 1]
        == "/home/user/worktrees/my-task"
    )
    # The workspace's create defaults set ensure_clean=false, which would otherwise copy
    # the source repo's uncommitted files into the worktree against this mode's promise.
    assert "--exclude-unclean" in create.argv
    assert create.argv[create.argv.index("--type") + 1] == "claude"
    assert create.argv[create.argv.index("--template") + 1] == "custom"
    assert "user_created=true" in create.argv
    assert f"custom_source={repo}" in create.argv
    assert "account=acct-claude" in create.argv
    # Claude binds by the account env var riding the create.
    env_flag_values = [
        create.argv[idx + 1] for idx, arg in enumerate(create.argv) if arg == "--env"
    ]
    assert any(value.startswith("CLAUDE_CONFIG_DIR=") for value in env_flag_values)
    assert create.argv[create.argv.index("--branch") + 1] == "main"
    assert create.argv[create.argv.index("--message") + 1] == "do the thing"
    # The custom template's aggregate replacements need the narrowing guard relaxed.
    assert (
        create.kwargs["env"]["MNGR__ALLOW_SETTINGS_KEY_ASSIGNMENT_NARROWING"] == "true"
    )
    # Claude's folder-trust dialog is pre-dismissed for the new work dir, the way the
    # account seeding pre-trusts the workspace checkout; without it the create's
    # first-message send refuses while the dialog is up. Claude matches a folder's
    # trust against the realpath of its cwd, and /home/user is a symlink in an
    # imbue_cloud workspace, so the key is the resolved path.
    trust_config = json.loads(
        (workspace_root / "accounts" / "acct-claude" / ".claude.json").read_text()
    )
    assert os.path.realpath("/home/user/worktrees/my-task") in trust_config.get(
        "projects", {}
    )


def test_the_narrowing_env_var_is_accepted_by_mngrs_env_override_layer() -> None:
    """mngr's `MNGR__*` override pattern is uppercase-only and silently skips any
    other casing, so the exact key the launcher sets must parse to the config flag --
    otherwise every `-t custom` create fails on the narrowing guard."""
    parsed = _parse_mngr_env_overrides(launcher._NARROWING_ENV)
    assert parsed == {"allow_settings_key_assignment_narrowing": True}


def test_in_place_mode_runs_in_the_directory_itself(workspace_root: Path) -> None:
    source = workspace_root / "plain-dir"
    source.mkdir()
    runner = _RecordingRunner()

    _launch(source, "in-place", runner)

    (create,) = runner.create_calls()
    assert_mngr_argv_valid(create.argv)
    assert create.argv[create.argv.index("--transfer") + 1] == "none"
    assert "--target-path" not in create.argv
    assert "--exclude-unclean" not in create.argv
    assert "--branch" not in create.argv
    assert "--message" not in create.argv
    # The display name falls back to the agent name.
    assert "display_name=my-task" in create.argv


def test_worktree_mode_refuses_a_non_git_directory(workspace_root: Path) -> None:
    source = workspace_root / "plain-dir"
    source.mkdir()

    with pytest.raises(SystemExit):
        _launch(source, "worktree")


def test_a_branch_in_in_place_mode_is_refused(workspace_root: Path) -> None:
    """mngr ignores --branch outright for --transfer none, so the launcher refuses the
    combination rather than silently running on whatever --path has checked out."""
    source = workspace_root / "plain-dir"
    source.mkdir()
    runner = _RecordingRunner()

    with pytest.raises(SystemExit):
        _launch(source, "in-place", runner, branch="main")
    assert runner.create_calls() == []


def test_an_unknown_harness_is_refused_before_anything_runs(
    workspace_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _git_repo(workspace_root)
    runner = _RecordingRunner()

    with pytest.raises(SystemExit):
        _launch(repo, "worktree", runner, harness_name="cluade")
    assert runner.calls == []
    assert (
        "unknown harness 'cluade'; choose one of: claude, " in capsys.readouterr().err
    )


def test_a_failed_agent_listing_refuses_the_launch_before_the_create(
    workspace_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A failed `mngr list` parses to zero records, which would read as "no agents" and
    silently disable the taken-name guard -- so the launch is refused instead."""
    repo = _git_repo(workspace_root)
    runner = _RecordingRunner(listing_returncode=3)

    with pytest.raises(SystemExit):
        _launch(repo, "worktree", runner)
    assert runner.create_calls() == []
    assert "`mngr list` failed" in capsys.readouterr().err


def test_a_taken_agent_name_is_refused_before_the_create(workspace_root: Path) -> None:
    repo = _git_repo(workspace_root)
    runner = _RecordingRunner(listing_records=[{"id": _AGENT_ID, "name": "my-task"}])

    with pytest.raises(SystemExit):
        _launch(repo, "worktree", runner)
    assert runner.create_calls() == []


def test_an_explicit_harness_picks_an_account_on_that_harness(
    workspace_root: Path,
) -> None:
    accounts_root = workspace_root / "accounts"
    _write_account_index(
        accounts_root,
        accounts.AccountIndex(
            accounts=(
                _account("acct-claude", HarnessType.CLAUDE),
                _account("acct-codex", HarnessType.CODEX),
            ),
            mru="acct-claude",
        ),
    )
    repo = _git_repo(workspace_root)
    runner = _RecordingRunner()

    _launch(repo, "worktree", runner, harness_name="codex")

    (create,) = runner.create_calls()
    assert create.argv[create.argv.index("--type") + 1] == "codex"
    assert "account=acct-codex" in create.argv
    # The link-bound harnesses provision the credential over the new agent's state dir.
    assert "--extra-provision-command" in create.argv


def test_a_conflicting_account_and_harness_pair_is_refused(
    workspace_root: Path,
) -> None:
    repo = _git_repo(workspace_root)

    with pytest.raises(SystemExit):
        _launch(repo, "worktree", account_id="acct-claude", harness_name="codex")


def test_an_account_on_an_unknown_lane_is_reported_as_unsupported(
    workspace_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An account whose lane this build no longer has is reported as unsupported --
    even alongside a conflicting --harness, which would otherwise win with the
    misleading 'conflicting pair' message."""
    _write_account_index(
        workspace_root / "accounts",
        accounts.AccountIndex(
            accounts=(
                accounts.Account(id="acct-old", lane="gone-lane", seq=1, display="Old"),
            )
        ),
    )
    repo = _git_repo(workspace_root)

    with pytest.raises(SystemExit):
        _launch(repo, "worktree", account_id="acct-old", harness_name="codex")
    assert "lane this build does not support" in capsys.readouterr().err


def test_no_signed_in_account_is_refused_with_the_sign_in_message(
    workspace_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_account_index(workspace_root / "accounts", accounts.AccountIndex())
    repo = _git_repo(workspace_root)

    with pytest.raises(SystemExit):
        _launch(repo, "worktree")
    assert "No provider account is signed in" in capsys.readouterr().err
