"""Unit tests for the bootstrap first-boot setup helpers."""

from __future__ import annotations

import io
import json
import os
import subprocess
from contextlib import redirect_stdout
from pathlib import Path

import pytest
from imbue.mngr.cli.output_helpers import write_json_line
from loguru import logger
from mngr_cli_contract.contract import assert_mngr_argv_valid

from bootstrap.manager import (
    FAST_MODE_DECISION_FILE,
    INITIAL_CHAT_AGENT_ID_FILENAME,
    TimezoneFetchError,
    _apply_container_timezone,
    _build_create_chat_command,
    _configure_git_global,
    _fetch_user_timezone,
    _initialize_workspace_main_branch,
    _install_runtime_cron_entries,
    _maybe_create_initial_chat,
    _parse_created_agent_id,
    _parse_timezone_response,
    _persist_initial_chat_agent_id,
    _read_host_name,
    _read_main_agent_labels,
    _read_workspace_fast_mode_enabled,
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


# --- _read_main_agent_labels ---


def test_read_main_agent_labels_returns_label_dict(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-1")
    agent_dir = tmp_path / "agents" / "agent-1"
    agent_dir.mkdir(parents=True)
    (agent_dir / "data.json").write_text(
        json.dumps({"labels": {"workspace": "my-ws", "is_primary": "true"}})
    )
    assert _read_main_agent_labels() == {"workspace": "my-ws", "is_primary": "true"}


def test_read_main_agent_labels_returns_empty_when_env_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MNGR_HOST_DIR", raising=False)
    monkeypatch.delenv("MNGR_AGENT_ID", raising=False)
    assert _read_main_agent_labels() == {}


def test_read_main_agent_labels_returns_empty_when_data_json_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-1")
    assert _read_main_agent_labels() == {}


def test_read_main_agent_labels_returns_empty_when_labels_field_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-1")
    agent_dir = tmp_path / "agents" / "agent-1"
    agent_dir.mkdir(parents=True)
    (agent_dir / "data.json").write_text(json.dumps({"other": "value"}))
    assert _read_main_agent_labels() == {}


# --- _build_create_chat_command ---


def test_build_create_chat_command_includes_welcome_and_template() -> None:
    cmd = _build_create_chat_command(
        "my-workspace", {"workspace": "my-workspace"}, True
    )
    assert cmd[:3] == ["mngr", "create", "my-workspace"]
    assert "--template" in cmd
    assert cmd[cmd.index("--template") + 1] == "chat"
    assert "--message" in cmd
    assert cmd[cmd.index("--message") + 1] == "/welcome"
    assert "--no-connect" in cmd


def test_build_create_chat_command_includes_transfer_none() -> None:
    """`--transfer none` makes mngr skip the per-agent worktree, so the chat
    agent reuses the services agent's work_dir. Without it, mngr collides
    with the services agent's existing `mngr/<host>` branch."""
    cmd = _build_create_chat_command(
        "my-workspace", {"workspace": "my-workspace"}, True
    )
    assert "--transfer" in cmd
    assert cmd[cmd.index("--transfer") + 1] == "none"


def test_build_create_chat_command_carries_no_workspace_label() -> None:
    cmd = _build_create_chat_command(
        "my-workspace", {"workspace": "my-workspace"}, True
    )
    # The chat agent belongs to its workspace by sharing the host; it carries no
    # workspace label (the label was removed from the naming model).
    labels = [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "--label"]
    assert all(not label.startswith("workspace=") for label in labels)


def test_build_create_chat_command_tags_user_created() -> None:
    """The initial chat agent is tagged ``user_created=true`` so the OOM
    agent-tagging hook places it in the protected user-agent band (shed only as a
    last resort)."""
    cmd = _build_create_chat_command(
        "my-workspace", {"workspace": "my-workspace"}, True
    )
    labels = [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "--label"]
    assert "user_created=true" in labels


def test_build_create_chat_command_passes_project_label_when_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cmd = _build_create_chat_command(
        "ws", {"workspace": "ws", "project": "my-project"}, True
    )
    labels = [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "--label"]
    assert "project=my-project" in labels


def test_build_create_chat_command_omits_project_label_when_missing() -> None:
    cmd = _build_create_chat_command("ws", {"workspace": "ws"}, True)
    labels = [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "--label"]
    assert all(not label.startswith("project=") for label in labels)


def test_build_create_chat_command_argv_accepted_by_live_cli() -> None:
    """Confront the emitted argv with the live ``imbue.mngr.main.cli`` tree, so
    a system/vendor/mngr rename of ``create``/its flags fails here at merge time rather
    than only at host boot. A ``workspace`` label is supplied so the builder's
    label resolution short-circuits without reading host files."""
    argv = _build_create_chat_command(
        "host-1", {"workspace": "ws", "project": "proj"}, True
    )
    assert_mngr_argv_valid(argv)


def test_build_create_chat_command_requests_json_output() -> None:
    """`--format json` lets the create step read back the new agent's id."""
    cmd = _build_create_chat_command("ws", {"workspace": "ws"}, True)
    assert "--format" in cmd
    assert cmd[cmd.index("--format") + 1] == "json"


def test_build_create_chat_command_carries_the_fast_mode_setting() -> None:
    """Chat is the only agent type that starts fast, so its create says which way."""
    enabled = _build_create_chat_command("ws", {"workspace": "ws"}, True)
    disabled = _build_create_chat_command("ws", {"workspace": "ws"}, False)
    assert "agent_types.claude.settings_overrides.fastMode=true" in enabled
    assert "agent_types.claude.settings_overrides.fastMode=false" in disabled
    # Both forms must resolve against the live mngr config model, not just parse
    # as CLI tokens: an unresolvable -S key path fails the create outright.
    assert_mngr_argv_valid(enabled)
    assert_mngr_argv_valid(disabled)


def test_build_create_chat_command_never_pins_claude_config_dir() -> None:
    """Every claude in the workspace must resolve claude's own default
    ~/.claude, so the create argv must not export CLAUDE_CONFIG_DIR (the old
    services-agent-owned shared dir was removed in the ~/.claude cutover)."""
    cmd = _build_create_chat_command("ws", {"workspace": "ws"}, True)
    assert all("CLAUDE_CONFIG_DIR" not in arg for arg in cmd)


# --- _read_workspace_fast_mode_enabled ---


def test_fast_mode_defaults_on_when_no_decision_recorded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """First boot has no decision yet, and the opening conversation should be fast."""
    monkeypatch.chdir(tmp_path)
    assert _read_workspace_fast_mode_enabled() is True


def test_fast_mode_follows_a_recorded_decision(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A workspace whose user turned fast mode off gets standard-speed chats."""
    monkeypatch.chdir(tmp_path)
    decision_path = tmp_path / "data" / ".state" / "fast_mode_decision.json"
    decision_path.parent.mkdir(parents=True)
    decision_path.write_text(
        json.dumps({"is_decided": True, "is_fast_mode_enabled": False})
    )
    assert _read_workspace_fast_mode_enabled() is False

    decision_path.write_text(
        json.dumps({"is_decided": True, "is_fast_mode_enabled": True})
    )
    assert _read_workspace_fast_mode_enabled() is True


def test_fast_mode_defaults_on_when_the_decision_is_unreadable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A corrupt or wrong-shaped decision must not silently strand new chats -- and
    must say so, since falling back turns on the setting that costs money."""
    monkeypatch.chdir(tmp_path)
    decision_path = tmp_path / "data" / ".state" / "fast_mode_decision.json"
    decision_path.parent.mkdir(parents=True)

    messages: list[str] = []
    sink_id = logger.add(lambda message: messages.append(message), level="WARNING")
    try:
        decision_path.write_text("{not valid json")
        assert _read_workspace_fast_mode_enabled() is True

        # Valid JSON the writer would never produce: the value decides nothing,
        # so this is a format skew rather than a fresh workspace.
        decision_path.write_text(json.dumps({"is_fast_mode_enabled": "no"}))
        assert _read_workspace_fast_mode_enabled() is True
    finally:
        logger.remove(sink_id)

    assert len(messages) == 2
    assert all(str(FAST_MODE_DECISION_FILE) in message for message in messages)


# --- _parse_created_agent_id ---


def test_parse_created_agent_id_reads_agent_id_from_json_object() -> None:
    stdout = '{"agent_id": "agent-abc", "host_id": "host-1", "host_name": "ws"}\n'
    assert _parse_created_agent_id(stdout) == "agent-abc"


def test_parse_created_agent_id_returns_none_when_absent() -> None:
    assert _parse_created_agent_id('{"host_id": "host-1"}') is None
    assert _parse_created_agent_id("not json at all") is None
    assert _parse_created_agent_id("") is None


def test_parse_created_agent_id_reads_live_mngr_json_output() -> None:
    """Confront the parser with mngr's real `--format json` serializer, so a
    system/vendor/mngr switch to pretty-printed or JSONL create output fails here at
    merge time rather than only at host boot. `write_json_line` is exactly what
    `mngr create`'s JSON branch calls (one compact object on stdout)."""
    result_data = {
        "agent_id": "agent-0123456789abcdef0123456789abcdef",
        "host_id": "host-1",
        "host_name": "my-workspace",
    }
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        write_json_line(result_data)
    assert _parse_created_agent_id(buffer.getvalue()) == result_data["agent_id"]


# --- _persist_initial_chat_agent_id ---


def test_persist_initial_chat_agent_id_writes_sidecar(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    _persist_initial_chat_agent_id("agent-abc")
    assert (tmp_path / INITIAL_CHAT_AGENT_ID_FILENAME).read_text() == "agent-abc"


def test_persist_initial_chat_agent_id_skips_when_host_dir_unset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("MNGR_HOST_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    _persist_initial_chat_agent_id("agent-abc")
    assert not (tmp_path / INITIAL_CHAT_AGENT_ID_FILENAME).exists()


# --- _maybe_create_initial_chat ---


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
def _bootstrap_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Common setup: MNGR_HOST_DIR rooted in tmp_path, a workspace in data.json,
    a chdir into tmp_path so the signal file lands somewhere ephemeral.

    Explicitly unsets MNGR_AGENT_WORK_DIR so `_initialize_workspace_main_branch`
    short-circuits in tests that don't care about the git initialization path;
    tests that DO want that path can monkeypatch MNGR_AGENT_WORK_DIR back in.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-1")
    monkeypatch.delenv("MNGR_AGENT_WORK_DIR", raising=False)
    (tmp_path / "data.json").write_text(json.dumps({"host_name": "my-workspace"}))
    agent_dir = tmp_path / "agents" / "agent-1"
    agent_dir.mkdir(parents=True)
    (agent_dir / "data.json").write_text(
        json.dumps({"labels": {"workspace": "my-workspace", "is_primary": "true"}})
    )
    return tmp_path


def test_maybe_create_initial_chat_creates_and_writes_signal(
    monkeypatch: pytest.MonkeyPatch, _bootstrap_env: Path
) -> None:
    stub = _StubSubprocess(returncode=0)
    monkeypatch.setattr("bootstrap.manager.subprocess.run", stub.run)
    _maybe_create_initial_chat()
    assert len(stub.calls) == 1
    assert (_bootstrap_env / "data" / ".state" / "initial_chat_created").exists()


def test_maybe_create_initial_chat_writes_no_host_env_file(
    monkeypatch: pytest.MonkeyPatch, _bootstrap_env: Path
) -> None:
    """The bootstrap must not touch $MNGR_HOST_DIR/env at all: since the
    ~/.claude cutover there is no CLAUDE_CONFIG_DIR (or anything else) for it
    to export, and a stray env write would silently pin every future agent."""
    stub = _StubSubprocess(returncode=0)
    monkeypatch.setattr("bootstrap.manager.subprocess.run", stub.run)
    _maybe_create_initial_chat()
    assert not (_bootstrap_env / "env").exists()


def test_maybe_create_initial_chat_persists_created_agent_id(
    monkeypatch: pytest.MonkeyPatch, _bootstrap_env: Path
) -> None:
    """A successful create writes the parsed agent id to the welcome-resend sidecar."""
    stub = _StubSubprocess(returncode=0, stdout='{"agent_id": "agent-created"}\n')
    monkeypatch.setattr("bootstrap.manager.subprocess.run", stub.run)
    _maybe_create_initial_chat()
    assert (
        _bootstrap_env / INITIAL_CHAT_AGENT_ID_FILENAME
    ).read_text() == "agent-created"


def test_maybe_create_initial_chat_skips_when_signal_present(
    monkeypatch: pytest.MonkeyPatch, _bootstrap_env: Path
) -> None:
    runtime = _bootstrap_env / "data" / ".state"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "initial_chat_created").write_text("")
    stub = _StubSubprocess(returncode=0)
    monkeypatch.setattr("bootstrap.manager.subprocess.run", stub.run)
    _maybe_create_initial_chat()
    assert stub.calls == []


def test_maybe_create_initial_chat_skips_signal_on_failure(
    monkeypatch: pytest.MonkeyPatch, _bootstrap_env: Path
) -> None:
    stub = _StubSubprocess(returncode=1)
    monkeypatch.setattr("bootstrap.manager.subprocess.run", stub.run)
    _maybe_create_initial_chat()
    assert len(stub.calls) == 1
    assert not (_bootstrap_env / "data" / ".state" / "initial_chat_created").exists()


def test_maybe_create_initial_chat_skips_when_host_name_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-1")
    monkeypatch.delenv("MNGR_AGENT_WORK_DIR", raising=False)
    # No data.json at all -> host_name resolution fails.
    stub = _StubSubprocess(returncode=0)
    monkeypatch.setattr("bootstrap.manager.subprocess.run", stub.run)
    _maybe_create_initial_chat()
    assert stub.calls == []
    assert not (tmp_path / "data" / ".state" / "initial_chat_created").exists()


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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If MNGR_AGENT_WORK_DIR isn't set, no git invocations happen."""
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
    monkeypatch.delenv("LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE", raising=False)
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
