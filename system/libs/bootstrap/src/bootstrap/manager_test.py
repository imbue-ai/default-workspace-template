"""Unit tests for the bootstrap first-boot setup helpers."""

from __future__ import annotations

import json
import os
import subprocess
from contextlib import redirect_stdout
from pathlib import Path

import pytest
from imbue.mngr.api.address_parsers import parse_new_agent_location
from imbue.mngr.cli.output_helpers import write_json_line
from mngr_cli_contract.contract import assert_mngr_argv_valid

from bootstrap.manager import (
    TimezoneFetchError,
    _apply_container_timezone,
    _configure_git_global,
    _ensure_git_identity,
    _fetch_user_timezone,
    _initialize_workspace_main_branch,
    _install_runtime_cron_entries,
    _parse_timezone_response,
    _read_host_name,
)

# --- _configure_git_global ---


def test_configure_git_global_sets_insteadof_but_not_hookspath(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Isolate the global git config to a tmp file so the test does not touch the
    # developer's real ~/.gitconfig. _configure_git_global should set both
    # insteadOf rewrites (git@ and ssh://). core.hooksPath must NOT be set:
    # the post-commit auto-push hook only becomes active when the opt-in
    # github-sync skill wires it up.
    gitconfig = tmp_path / ".gitconfig"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))

    _configure_git_global()

    insteadof = subprocess.run(
        ["git", "config", "--global", "--get-all", "url.https://github.com/.insteadOf"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.split()
    assert "git@github.com:" in insteadof
    assert "ssh://git@github.com/" in insteadof

    hooks_path = subprocess.run(
        ["git", "config", "--global", "core.hooksPath"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    assert hooks_path == ""


# --- _read_host_name ---


def test_read_host_name_returns_value_from_data_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    (tmp_path / "data.json").write_text(json.dumps({"host_name": "my-workspace"}))
    assert _read_host_name() == "my-workspace"


def test_read_host_name_returns_none_when_data_json_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    assert _read_host_name() is None


def test_read_host_name_returns_none_when_host_dir_env_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MNGR_HOST_DIR", raising=False)
    assert _read_host_name() is None


def test_read_host_name_returns_none_when_field_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    (tmp_path / "data.json").write_text(json.dumps({"other": "value"}))
    assert _read_host_name() is None


class _StubSubprocess:
    """Capture-and-replay double for subprocess.run used by the chat-create call."""

    def __init__(self, returncode: int = 0, stdout: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.calls: list[list[str]] = []

    def run(
        self,
        cmd: list[str],
        capture_output: bool = False,
        text: bool = False,
        check: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del capture_output, text, check  # keyword-only signature mirrors stdlib.
        self.calls.append(cmd)
        return subprocess.CompletedProcess(
            args=cmd, returncode=self.returncode, stdout=self.stdout, stderr=""
        )


@pytest.fixture


# --- _initialize_workspace_main_branch ---


def _git_in(work_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Helper for tests: run a real git command inside `work_dir`."""
    return subprocess.run(
        ["git", *args], cwd=work_dir, capture_output=True, text=True, check=False
    )


def test_initialize_workspace_main_branch_commits_and_renames(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """End-to-end: a real git repo on `mngr/foo` with uncommitted changes ends
    up on `main` with the working tree committed."""
    monkeypatch.chdir(tmp_path)
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    _git_in(work_dir, "init", "--initial-branch=main", "-q")
    _git_in(work_dir, "config", "user.email", "seed@test.local")
    _git_in(work_dir, "config", "user.name", "seed")
    (work_dir / "README.md").write_text("seed\n")
    _git_in(work_dir, "add", "-A")
    _git_in(work_dir, "commit", "-qm", "seed")
    # Branch the way agent_creator.py:447 does: `:mngr/<host_name>` makes a
    # new branch off current. Then add some uncommitted content (simulating
    # the desktop client's _rsync_worktree_over_clone).
    _git_in(work_dir, "checkout", "-q", "-b", "mngr/foo")
    (work_dir / "rsynced.txt").write_text("uncommitted from rsync\n")

    monkeypatch.setenv("MNGR_AGENT_WORK_DIR", str(work_dir))
    _initialize_workspace_main_branch()

    branch = _git_in(work_dir, "branch", "--show-current").stdout.strip()
    status = _git_in(work_dir, "status", "--porcelain").stdout.strip()
    head_msg = _git_in(work_dir, "log", "-1", "--format=%s").stdout.strip()
    assert branch == "main"
    assert status == ""  # all the uncommitted rsync content was captured
    assert head_msg == "Initial workspace commit"


def test_initialize_workspace_main_branch_skips_when_work_dir_unset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """If MNGR_AGENT_WORK_DIR isn't set, no git invocations happen."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MNGR_AGENT_WORK_DIR", raising=False)
    stub = _StubSubprocess(returncode=0)
    monkeypatch.setattr("bootstrap.manager.subprocess.run", stub.run)
    _initialize_workspace_main_branch()
    assert stub.calls == []


def test_initialize_workspace_main_branch_is_idempotent_on_clean_main(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Second invocation on an already-clean `main` branch is a no-op for
    the user (we make an empty allow-empty commit, but it's harmless)."""
    monkeypatch.chdir(tmp_path)
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    _git_in(work_dir, "init", "--initial-branch=main", "-q")
    _git_in(work_dir, "config", "user.email", "seed@test.local")
    _git_in(work_dir, "config", "user.name", "seed")
    (work_dir / "README.md").write_text("seed\n")
    _git_in(work_dir, "add", "-A")
    _git_in(work_dir, "commit", "-qm", "seed")
    monkeypatch.setenv("MNGR_AGENT_WORK_DIR", str(work_dir))
    _initialize_workspace_main_branch()
    branch = _git_in(work_dir, "branch", "--show-current").stdout.strip()
    assert branch == "main"


def test_initialize_workspace_main_branch_runs_once_per_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`git add -A` + commit is a once-ever operation: on a later boot it would sweep up
    whatever the user happened to have in flight. Its own signal, separate from the chat's."""
    monkeypatch.chdir(tmp_path)
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    _git_in(work_dir, "init", "--initial-branch=main", "-q")
    _git_in(work_dir, "config", "user.email", "seed@test.local")
    _git_in(work_dir, "config", "user.name", "seed")
    (work_dir / "README.md").write_text("seed\n")
    _git_in(work_dir, "add", "-A")
    _git_in(work_dir, "commit", "-qm", "seed")
    monkeypatch.setenv("MNGR_AGENT_WORK_DIR", str(work_dir))
    _initialize_workspace_main_branch()
    assert (tmp_path / "data" / ".state" / "workspace_main_branch_initialized").exists()

    # The user's work-in-progress, on a later boot.
    (work_dir / "wip.txt").write_text("half-finished\n")
    _initialize_workspace_main_branch()

    assert _git_in(work_dir, "status", "--porcelain").stdout.strip() != "", (
        "a second boot committed the user's working tree"
    )


# --- _ensure_git_identity ---


def test_ensure_git_identity_sets_one_when_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The workspace's only committer identity. `pool_bake` unsets it on finalize expecting
    bootstrap to put it back, so this runs every boot rather than once."""
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    _git_in(work_dir, "init", "--initial-branch=main", "-q")
    monkeypatch.setenv("MNGR_AGENT_WORK_DIR", str(work_dir))

    _ensure_git_identity()

    assert _git_in(work_dir, "config", "user.email").stdout.strip() == "bootstrap@minds.local"


def test_ensure_git_identity_never_overwrites_the_users_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    _git_in(work_dir, "init", "--initial-branch=main", "-q")
    _git_in(work_dir, "config", "user.email", "me@example.com")
    monkeypatch.setenv("MNGR_AGENT_WORK_DIR", str(work_dir))

    _ensure_git_identity()

    assert _git_in(work_dir, "config", "user.email").stdout.strip() == "me@example.com"


def test_initialize_workspace_main_branch_no_longer_sets_an_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It moved to `_ensure_git_identity`. Leaving it here tied the workspace's only identity
    to a one-shot signal, which is what broke an adopted pool workspace."""
    monkeypatch.chdir(tmp_path)
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    _git_in(work_dir, "init", "--initial-branch=main", "-q")
    monkeypatch.setenv("MNGR_AGENT_WORK_DIR", str(work_dir))

    _initialize_workspace_main_branch()

    assert _git_in(work_dir, "config", "user.email").returncode != 0


# --- _install_runtime_cron_entries ---


def test_install_runtime_cron_entries_copies_files_with_0644(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "data" / ".state" / "cron.d"
    source.mkdir(parents=True)
    (source / "minds-caretaker").write_text("* * * * * root true\n")
    target = tmp_path / "etc-cron-d"
    target.mkdir()

    _install_runtime_cron_entries(target_dir=target)

    installed = target / "minds-caretaker"
    assert installed.read_text() == "* * * * * root true\n"
    assert (installed.stat().st_mode & 0o777) == 0o644


def test_install_runtime_cron_entries_skips_names_cron_would_ignore(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "data" / ".state" / "cron.d"
    source.mkdir(parents=True)
    (source / "bad.name").write_text("* * * * * root true\n")
    (source / "good-name").write_text("* * * * * root true\n")
    target = tmp_path / "etc-cron-d"
    target.mkdir()

    _install_runtime_cron_entries(target_dir=target)

    assert not (target / "bad.name").exists()
    assert (target / "good-name").exists()


def test_install_runtime_cron_entries_no_ops_without_source_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "etc-cron-d"
    target.mkdir()
    _install_runtime_cron_entries(target_dir=target)
    assert list(target.iterdir()) == []


def test_install_runtime_cron_entries_tolerates_unwritable_target(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "data" / ".state" / "cron.d"
    source.mkdir(parents=True)
    (source / "minds-caretaker").write_text("* * * * * root true\n")
    # Target dir does not exist: the per-file OSError is logged, not raised.
    _install_runtime_cron_entries(target_dir=tmp_path / "missing")


# --- _apply_container_timezone ---


def _make_zoneinfo_tree(tmp_path: Path) -> Path:
    """Build a fake zoneinfo dir with a single America/New_York zone file."""
    zoneinfo = tmp_path / "zoneinfo"
    (zoneinfo / "America").mkdir(parents=True)
    (zoneinfo / "America" / "New_York").write_bytes(b"TZif-fake")
    return zoneinfo


def test_apply_container_timezone_symlinks_and_writes_name(tmp_path: Path) -> None:
    zoneinfo = _make_zoneinfo_tree(tmp_path)
    etc = tmp_path / "etc"
    etc.mkdir()
    localtime = etc / "localtime"
    timezone_file = etc / "timezone"

    assert _apply_container_timezone(
        "America/New_York",
        zoneinfo_dir=zoneinfo,
        localtime_path=localtime,
        timezone_path=timezone_file,
    )

    assert localtime.is_symlink()
    assert Path(os.readlink(localtime)) == zoneinfo / "America" / "New_York"
    assert timezone_file.read_text() == "America/New_York\n"


def test_apply_container_timezone_replaces_existing_localtime(tmp_path: Path) -> None:
    """The common container case: /etc/localtime already exists (a regular file
    baked into the image) and must be atomically replaced by the symlink."""
    zoneinfo = _make_zoneinfo_tree(tmp_path)
    etc = tmp_path / "etc"
    etc.mkdir()
    localtime = etc / "localtime"
    localtime.write_bytes(b"stale UTC zone data")
    timezone_file = etc / "timezone"

    assert _apply_container_timezone(
        "America/New_York",
        zoneinfo_dir=zoneinfo,
        localtime_path=localtime,
        timezone_path=timezone_file,
    )
    assert localtime.is_symlink()
    assert Path(os.readlink(localtime)) == zoneinfo / "America" / "New_York"


@pytest.mark.parametrize(
    "bad_name",
    [
        "",
        "../../etc",
        "America/../../etc/passwd",
        "America/New York",
        "UTC;rm -rf /",
        "/America/New_York",
        "America/",
    ],
)
def test_apply_container_timezone_rejects_malformed_names(
    tmp_path: Path, bad_name: str
) -> None:
    zoneinfo = _make_zoneinfo_tree(tmp_path)
    etc = tmp_path / "etc"
    etc.mkdir()
    localtime = etc / "localtime"

    assert not _apply_container_timezone(
        bad_name,
        zoneinfo_dir=zoneinfo,
        localtime_path=localtime,
        timezone_path=etc / "timezone",
    )
    assert not localtime.exists()


def test_apply_container_timezone_rejects_unknown_zone(tmp_path: Path) -> None:
    """A well-formed name whose zoneinfo file does not exist is rejected."""
    zoneinfo = _make_zoneinfo_tree(tmp_path)
    etc = tmp_path / "etc"
    etc.mkdir()
    localtime = etc / "localtime"

    assert not _apply_container_timezone(
        "Mars/Olympus_Mons",
        zoneinfo_dir=zoneinfo,
        localtime_path=localtime,
        timezone_path=etc / "timezone",
    )
    assert not localtime.exists()


def test_apply_container_timezone_tolerates_oserror(tmp_path: Path) -> None:
    """A failing filesystem write (here: parent dir absent) returns False
    instead of raising -- bootstrap must never die on the timezone step."""
    zoneinfo = _make_zoneinfo_tree(tmp_path)
    missing_dir = tmp_path / "does-not-exist"

    assert not _apply_container_timezone(
        "America/New_York",
        zoneinfo_dir=zoneinfo,
        localtime_path=missing_dir / "localtime",
        timezone_path=missing_dir / "timezone",
    )


# --- _fetch_user_timezone ---


def test_fetch_user_timezone_returns_empty_when_gateway_env_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LATCHKEY_GATEWAY", raising=False)
    monkeypatch.delenv("LATCHKEY_GATEWAY_PASSWORD", raising=False)
    assert _fetch_user_timezone() == ""


# --- _parse_timezone_response ---


def test_parse_timezone_response_returns_the_zone_name() -> None:
    assert (
        _parse_timezone_response(b'{"timezone": "America/New_York"}')
        == "America/New_York"
    )


def test_parse_timezone_response_accepts_the_documented_unknown_answer() -> None:
    """{"timezone": ""} is the desktop client's valid "unknown" answer -- it must
    come back as "" (fall back to UTC), not raise (which would be retried)."""
    assert _parse_timezone_response(b'{"timezone": ""}') == ""


@pytest.mark.parametrize(
    "body",
    [
        b"[]",
        b'"America/New_York"',
        b"{}",
        b'{"timezone": null}',
        b'{"timezone": 42}',
    ],
)
def test_parse_timezone_response_rejects_wrong_shapes(body: bytes) -> None:
    with pytest.raises(TimezoneFetchError):
        _parse_timezone_response(body)


def test_parse_timezone_response_rejects_a_non_json_body() -> None:
    with pytest.raises(ValueError):
        _parse_timezone_response(b"<html>bad gateway</html>")
