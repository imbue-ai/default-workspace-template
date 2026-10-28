"""Tests for ``launch_custom_agent.py``.

Run via: ``uv run pytest .agents/skills/launch-custom-agent/scripts/launch_custom_agent_test.py``

A recording ``Runner`` is injected so no real ``mngr`` processes are spawned; the
built argvs are pinned against the live mngr CLI via ``mngr_cli_contract``. Account
resolution runs against a real index file under a temp ``MINDS_ACCOUNTS_ROOT``.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import Any
from typing import Sequence

import pytest
from imbue.chat import accounts
from imbue.chat.harnesses.harness_type import HarnessType
from imbue.chat.harnesses.lanes import LANES
from imbue.mngr.config.loader import _parse_mngr_env_overrides
from mngr_cli_contract.contract import assert_mngr_argv_valid

_SCRIPT = Path(__file__).parent / "launch_custom_agent.py"
_spec = importlib.util.spec_from_file_location("launch_custom_agent", _SCRIPT)
assert _spec is not None and _spec.loader is not None
launch_custom_agent = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(launch_custom_agent)

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
class _RecordingRunner:
    """Answers `mngr list` with canned records and records every other call."""

    listing_records: list[dict[str, Any]] = field(default_factory=list)
    calls: list[_RecordedCall] = field(default_factory=list)

    def run(self, argv: Sequence[str], **kwargs: Any) -> _StubResult:
        call = _RecordedCall(argv=list(argv), kwargs=kwargs)
        self.calls.append(call)
        if list(argv[:2]) == ["mngr", "list"]:
            return _StubResult(stdout="".join(json.dumps(record) + "\n" for record in self.listing_records))
        return _StubResult()

    def create_calls(self) -> list[_RecordedCall]:
        return [call for call in self.calls if call.argv[:2] == ["mngr", "create"]]


def _lane_id_for(harness: HarnessType) -> str:
    return next(lane.id for lane in LANES if lane.harness is harness)


def _write_account_index(accounts_root: Path, index: accounts.AccountIndex) -> None:
    accounts_root.mkdir(parents=True, exist_ok=True)
    (accounts_root / "index.json").write_text(json.dumps(index.model_dump()))


def _account(account_id: str, harness: HarnessType) -> accounts.Account:
    return accounts.Account(id=account_id, lane=_lane_id_for(harness), seq=1, display=str(harness))


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
        accounts_root, accounts.AccountIndex(accounts=(_account("acct-claude", HarnessType.CLAUDE),))
    )
    return tmp_path


def _git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "their-repo"
    repo.mkdir()
    subprocess.run(["git", "init", "--quiet", str(repo)], check=True)
    return repo


def test_launch_builds_a_valid_mngr_create_for_worktree_mode(workspace_root: Path) -> None:
    repo = _git_repo(workspace_root)
    runner = _RecordingRunner()

    assert (
        launch_custom_agent.launch(
            name="my-task",
            source_path=repo,
            mode="worktree",
            account_id="",
            harness_name="",
            branch="main",
            display_name="My task",
            message="do the thing",
            runner=runner,
        )
        == 0
    )

    (create,) = runner.create_calls()
    assert_mngr_argv_valid(create.argv)
    assert create.argv[create.argv.index("--from") + 1] == f":{repo}"
    assert create.argv[create.argv.index("--transfer") + 1] == "git-worktree"
    # The workspace's create defaults set ensure_clean=false, which would otherwise copy
    # the source repo's uncommitted files into the worktree against this mode's promise.
    assert "--exclude-unclean" in create.argv
    assert create.argv[create.argv.index("--type") + 1] == "claude"
    assert "custom" in create.argv
    assert "user_created=true" in create.argv
    assert f"custom_source={repo}" in create.argv
    assert "account=acct-claude" in create.argv
    # Claude binds by the account env var riding the create.
    env_flag_values = [create.argv[idx + 1] for idx, arg in enumerate(create.argv) if arg == "--env"]
    assert any(value.startswith("CLAUDE_CONFIG_DIR=") for value in env_flag_values)
    assert create.argv[create.argv.index("--branch") + 1] == "main"
    assert create.argv[create.argv.index("--message") + 1] == "do the thing"
    # The custom template's aggregate replacements need the narrowing guard relaxed.
    assert create.kwargs["env"]["MNGR__ALLOW_SETTINGS_KEY_ASSIGNMENT_NARROWING"] == "true"


def test_the_narrowing_env_var_is_accepted_by_mngrs_env_override_layer() -> None:
    """mngr's `MNGR__*` override pattern is uppercase-only and silently skips any
    other casing, so the exact key the launcher sets must parse to the config flag --
    otherwise every `-t custom` create fails on the narrowing guard."""
    parsed = _parse_mngr_env_overrides(launch_custom_agent._NARROWING_ENV)
    assert parsed == {"allow_settings_key_assignment_narrowing": True}


def test_in_place_mode_runs_in_the_directory_itself(workspace_root: Path) -> None:
    source = workspace_root / "plain-dir"
    source.mkdir()
    runner = _RecordingRunner()

    launch_custom_agent.launch(
        name="my-task",
        source_path=source,
        mode="in-place",
        account_id="",
        harness_name="",
        branch="",
        display_name="",
        message="",
        runner=runner,
    )

    (create,) = runner.create_calls()
    assert_mngr_argv_valid(create.argv)
    assert create.argv[create.argv.index("--transfer") + 1] == "none"
    assert "--exclude-unclean" not in create.argv
    assert "--branch" not in create.argv
    assert "--message" not in create.argv
    # The display name falls back to the agent name.
    assert "display_name=my-task" in create.argv


def test_worktree_mode_refuses_a_non_git_directory(workspace_root: Path) -> None:
    source = workspace_root / "plain-dir"
    source.mkdir()

    with pytest.raises(SystemExit):
        launch_custom_agent.launch(
            name="my-task",
            source_path=source,
            mode="worktree",
            account_id="",
            harness_name="",
            branch="",
            display_name="",
            message="",
            runner=_RecordingRunner(),
        )


def test_a_taken_agent_name_is_refused_before_the_create(workspace_root: Path) -> None:
    repo = _git_repo(workspace_root)
    runner = _RecordingRunner(listing_records=[{"id": _AGENT_ID, "name": "my-task"}])

    with pytest.raises(SystemExit):
        launch_custom_agent.launch(
            name="my-task",
            source_path=repo,
            mode="worktree",
            account_id="",
            harness_name="",
            branch="",
            display_name="",
            message="",
            runner=runner,
        )
    assert runner.create_calls() == []


def test_an_explicit_harness_picks_an_account_on_that_harness(workspace_root: Path) -> None:
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

    launch_custom_agent.launch(
        name="my-task",
        source_path=repo,
        mode="worktree",
        account_id="",
        harness_name="codex",
        branch="",
        display_name="",
        message="",
        runner=runner,
    )

    (create,) = runner.create_calls()
    assert create.argv[create.argv.index("--type") + 1] == "codex"
    assert "account=acct-codex" in create.argv
    # The link-bound harnesses provision the credential over the new agent's state dir.
    assert "--extra-provision-command" in create.argv


def test_a_conflicting_account_and_harness_pair_is_refused(workspace_root: Path) -> None:
    repo = _git_repo(workspace_root)

    with pytest.raises(SystemExit):
        launch_custom_agent.launch(
            name="my-task",
            source_path=repo,
            mode="worktree",
            account_id="acct-claude",
            harness_name="codex",
            branch="",
            display_name="",
            message="",
            runner=_RecordingRunner(),
        )


def test_no_signed_in_account_is_refused_with_the_sign_in_message(
    workspace_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_account_index(workspace_root / "accounts", accounts.AccountIndex())
    repo = _git_repo(workspace_root)

    with pytest.raises(SystemExit):
        launch_custom_agent.launch(
            name="my-task",
            source_path=repo,
            mode="worktree",
            account_id="",
            harness_name="",
            branch="",
            display_name="",
            message="",
            runner=_RecordingRunner(),
        )
    assert "No provider account is signed in" in capsys.readouterr().err
