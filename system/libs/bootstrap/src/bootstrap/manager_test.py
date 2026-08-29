"""Unit tests for the bootstrap first-boot setup helpers."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
from loguru import logger
from mngr_cli_contract.contract import assert_mngr_argv_valid

from bootstrap.manager import (
    _DRI_WAKE_TIMEOUT_SECONDS,
    _UPDATE_RECOVER_TIMEOUT_SECONDS,
    UPDATE_APPLY_MARKER,
    UPDATE_APPLY_SCRIPT,
    UPDATE_RECOVER_CRON_NAME,
    UPDATE_RECOVER_EXIT_EMERGENCY,
    WORKSPACE_ROOT_DIR,
    TimezoneFetchError,
    _apply_container_timezone,
    _configure_git_global,
    _ensure_git_identity,
    _fetch_user_timezone,
    _initialize_workspace_main_branch,
    _install_runtime_cron_entries,
    _parse_timezone_response,
    _read_host_name,
    _read_update_marker_dri_agent,
    _recover_interrupted_update,
    _wake_update_dri_agent,
    _write_update_recovery_cron_entry,
    main,
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


# --- the shared subprocess double (the recovery path, the DRI wake)


class _StubSubprocess:
    """Capture-and-replay double for ``subprocess.run``.

    ``calls`` and ``kwargs`` record each invocation. ``on_command`` (when set)
    runs for every argv, which is how the recovery tests model the script
    clearing the apply marker on a successful rollback. ``raise_on`` maps a
    token in the argv to the exception that call raises, for the "the
    executable is missing / hangs" paths.
    """

    def __init__(self, returncode: int = 0, stdout: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.calls: list[list[str]] = []
        self.kwargs: list[dict[str, object]] = []
        self.on_command: Callable[[list[str]], None] | None = None
        self.raise_on: dict[str, BaseException] = {}

    def run(self, cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.calls.append(cmd)
        self.kwargs.append(dict(kwargs))
        if self.on_command is not None:
            self.on_command(cmd)
        for token, error in self.raise_on.items():
            if token in cmd:
                raise error
        return subprocess.CompletedProcess(
            args=cmd, returncode=self.returncode, stdout=self.stdout, stderr=""
        )


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
    # Isolate the global git config so a developer machine's own identity does
    # not satisfy the only-if-unset check the test is about.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
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
    # Isolate the global git config so a developer machine's own identity does
    # not leak into the "no identity was set" assertion below.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
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


# --- _write_update_recovery_cron_entry ---


def test_update_recovery_cron_entry_is_rewritten_every_boot(tmp_path: Path) -> None:
    # The whole point of moving this off setup_system.sh: /etc/cron.d does not
    # survive container recreation, so the guard has to be laid down again at
    # each boot rather than once at provision time -- and an entry already
    # sitting there (a baked-in one, or a previous boot's) has to be replaced
    # rather than left alone, or a workspace stays pinned to whatever its image
    # shipped.
    target = tmp_path / "etc-cron-d"
    target.mkdir()
    installed = target / UPDATE_RECOVER_CRON_NAME
    installed.write_text("*/5 * * * * root true  # a stale entry\n")

    _write_update_recovery_cron_entry(target_dir=target)

    entry = installed.read_text()
    assert "stale entry" not in entry
    assert str(UPDATE_APPLY_SCRIPT) in entry
    assert (installed.stat().st_mode & 0o777) == 0o644


def test_update_recovery_cron_entry_can_run_from_cron(tmp_path: Path) -> None:
    # cron gives a drop-in its own compiled-in /usr/bin:/bin and its own cwd,
    # and `recover`'s live path shells out to mngr, uv and npm -- so an entry
    # without a PATH line or an absolute cd rolls the tree back and leaves the
    # live workspace broken, silently.
    target = tmp_path / "etc-cron-d"
    target.mkdir()
    _write_update_recovery_cron_entry(target_dir=target)
    entry = (target / UPDATE_RECOVER_CRON_NAME).read_text()

    assert entry.startswith("PATH=/root/.local/bin:")
    assert f"cd {WORKSPACE_ROOT_DIR} &&" in entry
    # Composed from the one constant that says where the script lives, so the
    # path cannot drift from the boot-time recovery's own invocation.
    assert str(UPDATE_APPLY_SCRIPT) in entry
    assert "flock -n" in entry


def test_update_recovery_cron_entry_tolerates_an_unwritable_target(
    tmp_path: Path,
) -> None:
    # This runs on the path to supervisord; a boot that reaches the services is
    # worth more than the guard.
    _write_update_recovery_cron_entry(target_dir=tmp_path / "missing")


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


# --- _recover_interrupted_update ---


def _clear_marker_on_recover(cmd: list[str]) -> None:
    """Model the recovery script clearing the marker on a successful rollback --
    the signal the bootstrap reads as "a rollback really happened", as opposed to
    the guard declining."""
    if "recover" in cmd:
        UPDATE_APPLY_MARKER.unlink()


def _write_apply_marker(dri_agent: str = "the-lead") -> None:
    UPDATE_APPLY_MARKER.parent.mkdir(parents=True, exist_ok=True)
    UPDATE_APPLY_MARKER.write_text(
        json.dumps({"dri_agent": dri_agent, "phase": "merged"})
    )


def test_read_update_marker_dri_agent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    assert _read_update_marker_dri_agent() == ""  # no marker
    _write_apply_marker("agent-omega")
    assert _read_update_marker_dri_agent() == "agent-omega"
    UPDATE_APPLY_MARKER.write_text("not json {")
    assert _read_update_marker_dri_agent() == ""  # corrupt marker degrades
    # A write torn mid-multibyte -- the failure mode an interrupted apply
    # actually produces -- must degrade too, not raise out of boot.
    UPDATE_APPLY_MARKER.write_bytes(b'{"dri_agent": "\xff\xfe')
    assert _read_update_marker_dri_agent() == ""
    # Well-formed JSON of the wrong shape degrades the same way.
    UPDATE_APPLY_MARKER.write_text(json.dumps([{"dri_agent": "agent-omega"}]))
    assert _read_update_marker_dri_agent() == ""
    UPDATE_APPLY_MARKER.write_text(json.dumps({"dri_agent": 7}))
    assert _read_update_marker_dri_agent() == ""
    # An apply driven outside an agent records no name; that is not corruption.
    UPDATE_APPLY_MARKER.write_text(json.dumps({"dri_agent": ""}))
    assert _read_update_marker_dri_agent() == ""


def test_recover_skips_entirely_without_a_marker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    stub = _StubSubprocess()
    monkeypatch.setattr("bootstrap.manager.subprocess.run", stub.run)

    assert _recover_interrupted_update() == ""

    assert stub.calls == []


def test_recover_rolls_back_and_names_the_dri_agent_to_wake(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_apply_marker("agent-omega")
    stub = _StubSubprocess()
    # A successful recovery clears the marker; that clearing is what tells the
    # bootstrap a rollback really happened (vs the guard's silent no-op).
    stub.on_command = _clear_marker_on_recover
    monkeypatch.setattr("bootstrap.manager.subprocess.run", stub.run)

    dri_agent = _recover_interrupted_update()

    recover_call = stub.calls[0]
    assert recover_call[:2] == ["python3", str(UPDATE_APPLY_SCRIPT)]
    assert "recover" in recover_call
    # The boot path: disk state only, with the script's own staleness guard.
    assert "--no-restart" in recover_call
    assert "--if-stale" in recover_call
    assert stub.kwargs[0].get("timeout") == _UPDATE_RECOVER_TIMEOUT_SECONDS
    # The agent is named back to main(), not started here: starting it would
    # race _sync_workspace_venv, which runs after this.
    assert dri_agent == "agent-omega"
    assert len(stub.calls) == 1


def test_wake_starts_the_agent_and_hands_it_the_finding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = _StubSubprocess()
    monkeypatch.setattr("bootstrap.manager.subprocess.run", stub.run)

    _wake_update_dri_agent("agent-omega")

    assert stub.calls[0] == ["mngr", "start", "agent-omega"]
    assert stub.calls[1][:3] == ["mngr", "message", "agent-omega"]
    for argv in stub.calls:
        assert_mngr_argv_valid(argv)
    # Both gate boot, so neither may hang forever.
    assert [kwargs.get("timeout") for kwargs in stub.kwargs] == [
        _DRI_WAKE_TIMEOUT_SECONDS,
        _DRI_WAKE_TIMEOUT_SECONDS,
    ]


def test_wake_survives_an_unrunnable_mngr_so_boot_continues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `mngr` missing is a live possibility on this path -- an apply interrupted
    # mid `uv tool install` of the vendored mngr is exactly why the recovery
    # runs. main() does not wrap this call, so an escaping FileNotFoundError
    # would kill bootstrap before supervisord starts and boot the container
    # with no services at all.
    stub = _StubSubprocess()
    stub.raise_on = {"start": FileNotFoundError("mngr")}
    monkeypatch.setattr("bootstrap.manager.subprocess.run", stub.run)

    _wake_update_dri_agent("agent-omega")

    assert stub.calls == [["mngr", "start", "agent-omega"]]


def test_wake_skips_the_message_when_the_start_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = _StubSubprocess(returncode=1)
    monkeypatch.setattr("bootstrap.manager.subprocess.run", stub.run)

    _wake_update_dri_agent("agent-omega")

    assert stub.calls == [["mngr", "start", "agent-omega"]]


def test_recover_names_nobody_when_the_guard_noops(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The marker surviving the recover call means the guard declined to act
    # (e.g. the apply is live again); nothing was rolled back, so nobody is
    # messaged about a rollback.
    monkeypatch.chdir(tmp_path)
    _write_apply_marker("agent-omega")
    stub = _StubSubprocess()
    monkeypatch.setattr("bootstrap.manager.subprocess.run", stub.run)

    assert _recover_interrupted_update() == ""

    assert len(stub.calls) == 1  # only the recover invocation, no mngr calls


def test_recover_names_nobody_when_the_rollback_itself_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_apply_marker("agent-omega")
    stub = _StubSubprocess()
    stub.raise_on = {"recover": OSError("no python3")}
    monkeypatch.setattr("bootstrap.manager.subprocess.run", stub.run)

    # Boot continues even though the recovery could not run at all.
    assert _recover_interrupted_update() == ""


def test_recover_failure_does_not_wake_or_raise(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_apply_marker("agent-omega")
    stub = _StubSubprocess(returncode=1)
    monkeypatch.setattr("bootstrap.manager.subprocess.run", stub.run)

    # Must not raise: boot continues regardless, and nobody is woken.
    assert _recover_interrupted_update() == ""

    assert len(stub.calls) == 1


def test_a_partial_restore_at_boot_is_an_error_that_still_wakes_the_dri_agent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Exit 3: the tree is rolled back, the marker is gone, but a snapshot
    # could not be put back and the services are about to boot over that
    # mismatch. That is the state most in need of a person, so it is logged
    # as an error (a clean rollback is a warning) and the agent is still named.
    monkeypatch.chdir(tmp_path)
    _write_apply_marker("agent-omega")
    stub = _StubSubprocess(returncode=UPDATE_RECOVER_EXIT_EMERGENCY)
    stub.on_command = _clear_marker_on_recover
    monkeypatch.setattr("bootstrap.manager.subprocess.run", stub.run)
    errors: list[str] = []
    sink = logger.add(lambda message: errors.append(str(message)), level="ERROR")
    try:
        dri_agent = _recover_interrupted_update()
    finally:
        logger.remove(sink)

    assert dri_agent == "agent-omega"
    assert len(stub.calls) == 1
    assert any("could not put the pre-apply state back" in line for line in errors)


def test_main_rolls_back_before_the_venv_sync_and_wakes_the_agent_after_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The two orderings the boot path is built around: the rollback must run
    # before the venv converge (which has to converge against the restored
    # tree, not the half-applied one), and the DRI agent must be woken only
    # after it (a live agent's `uv run` would race the venv rewrite).
    # chdir into tmp_path so the marker and signal files land somewhere
    # ephemeral; MNGR_AGENT_WORK_DIR is unset so the git-identity and
    # main-branch steps short-circuit.
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MNGR_AGENT_WORK_DIR", raising=False)
    _write_apply_marker("agent-omega")
    stub = _StubSubprocess()
    stub.on_command = _clear_marker_on_recover
    monkeypatch.setattr("bootstrap.manager.subprocess.run", stub.run)
    # The steps that touch the host or replace the process.
    for name in (
        "_migrate_legacy_claude_state_best_effort",
        "_write_update_recovery_cron_entry",
        "_ensure_supervisor_log_dir",
        "_exec_supervisord",
    ):
        monkeypatch.setattr(f"bootstrap.manager.{name}", lambda: None)
    monkeypatch.delenv("LATCHKEY_GATEWAY", raising=False)

    main()

    recover_index = next(
        index for index, argv in enumerate(stub.calls) if "recover" in argv
    )
    sync_index = stub.calls.index(["uv", "sync", "--all-packages", "--frozen"])
    wake_index = stub.calls.index(["mngr", "start", "agent-omega"])
    assert recover_index < sync_index < wake_index


def test_recover_names_nobody_when_the_marker_recorded_no_agent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # An apply driven outside an agent records dri_agent="". The rollback still
    # happens; there is simply nobody to hand the finding to.
    monkeypatch.chdir(tmp_path)
    _write_apply_marker("")
    stub = _StubSubprocess()
    stub.on_command = _clear_marker_on_recover
    monkeypatch.setattr("bootstrap.manager.subprocess.run", stub.run)

    assert _recover_interrupted_update() == ""

    assert len(stub.calls) == 1  # the recover invocation, and no mngr calls
    assert not UPDATE_APPLY_MARKER.exists()
