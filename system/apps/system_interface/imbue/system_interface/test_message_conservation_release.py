"""One live message-conservation check (invariant U1) against a REAL claude agent.

The storm tests (``harnesses/conservation_storm_test.py``) enforce the ledger property at scale
against scripted worlds; this release test proves the same property against reality, small: a
real ``mngr create``'d claude agent processes a handful of sends interleaved with the REAL stop
executor (``drain-to-composer``'s dispatch) and the REAL restart-based flush (``/flush-queue``'s
``restart_drain`` + resend), each of which restarts the live process -- then conservation is read
straight off the on-disk session records:

- every delivered message appears as a user turn EXACTLY once (no ghost duplicates),
- every stop-retracted message rides the returned composer block and NEVER appears as a user
  turn (interrupted, handed back, not run),
- a fresh watcher's full replay after the restart derives an EMPTY queue (no ghost re-queue of
  the retracted messages -- the process-epoch scoping, U6).

The agent lives in an isolated ``MNGR_HOST_DIR`` (its own profile opted into pytest, docker and
modal providers disabled) with an isolated tmux server, so nothing touches the workspace's real
agents. The work repo must sit under a directory the real claude config trusts (mngr refuses an
untrusted ``--no-connect`` create), so it is created inside this project's gitignored
``.test_output/``; the shared credentials reach the agent through mngr's normal per-agent
config-dir symlink. Skipped cleanly when the environment cannot run a live claude agent.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from collections import Counter
from pathlib import Path

import pytest

from imbue.mngr.utils.polling import wait_for
from imbue.system_interface.agent_discovery import AgentInfo
from imbue.system_interface.agent_discovery import read_claude_config_dir_from_env_file
from imbue.system_interface.harnesses.claude.watcher import ClaudeSessionWatcher
from imbue.system_interface.harnesses.harness_type import HarnessType
from imbue.system_interface.harnesses.interrupt import restart_drain
from imbue.system_interface.harnesses.registry import build_interrupt_to_composer

pytestmark = pytest.mark.release

_PROJECT_ROOT = Path(__file__).parents[2]

# The whole flow shells out to mngr ~10 times (each invocation pays the CLI startup tax) and
# restarts the agent twice, so the budgets are generous but bounded.
_CREATE_TIMEOUT_SECONDS = 300.0
_MNGR_COMMAND_TIMEOUT_SECONDS = 120.0
_TURN_START_TIMEOUT_SECONDS = 90.0
_QUEUE_PARK_TIMEOUT_SECONDS = 90.0
_DELIVERY_TIMEOUT_SECONDS = 180.0

# The kick-off turns run a long sleep so the mid-turn sends reliably park while the turn is
# still open (each interleaved mngr invocation costs several seconds of startup tax).
_LONG_TURN_PROMPT = "Use the Bash tool to run exactly: sleep 240 && echo done. Then reply with one word: done."


def _is_ancestor_trusted_by_claude(path: Path) -> bool:
    """Whether ``path`` (or an ancestor) is trusted in the REAL ~/.claude.json.

    mngr's ``--no-connect`` create refuses an untrusted source repo, so without this the test
    cannot run. Mirrors mngr's own ancestor-walking trust check, read-only.
    """
    config_file = Path.home() / ".claude.json"
    if not config_file.is_file():
        return False
    try:
        config = json.loads(config_file.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    projects = config.get("projects", {})
    if not isinstance(projects, dict):
        return False
    resolved = path.resolve()
    for candidate in (resolved, *resolved.parents):
        project = projects.get(str(candidate))
        if isinstance(project, dict) and project.get("hasTrustDialogAccepted") is True:
            return True
    return False


def _skip_unless_live_claude_agent_possible(work_repo_parent: Path) -> None:
    if shutil.which("mngr") is None:
        pytest.skip("mngr CLI not on PATH")
    if shutil.which("claude") is None:
        pytest.skip("claude binary not on PATH")
    if not (Path.home() / ".claude" / ".credentials.json").is_file():
        pytest.skip("no claude credentials at ~/.claude/.credentials.json; a live turn cannot run")
    if not _is_ancestor_trusted_by_claude(work_repo_parent):
        pytest.skip("no claude-trusted ancestor for the work repo; mngr create --no-connect would refuse")


def _prepare_isolated_host_dir(host_dir: Path) -> None:
    """An isolated mngr host dir with its own profile, opted into pytest, local provider only."""
    profile_dir = host_dir / "profiles" / "conservation"
    profile_dir.mkdir(parents=True)
    (host_dir / "config.toml").write_text('profile = "conservation"\n')
    (profile_dir / "settings.toml").write_text(
        "is_allowed_in_pytest = true\n\n[providers.modal]\nis_enabled = false\n\n[providers.docker]\nis_enabled = false\n"
    )
    (profile_dir / "tmux_onboarding_shown").write_text("")


def _mngr_env(host_dir: Path, tmux_dir: Path) -> dict[str, str]:
    """The subprocess env: the caller's env minus its own agent identity, re-homed to the
    isolated host dir and tmux server (and outside any enclosing tmux client)."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("MNGR_") and key != "TMUX"}
    env["MNGR_HOST_DIR"] = str(host_dir)
    env["TMUX_TMPDIR"] = str(tmux_dir)
    return env


def _run_mngr(
    args: list[str], env: dict[str, str], cwd: Path, timeout: float = _MNGR_COMMAND_TIMEOUT_SECONDS
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(["mngr", *args], env=env, cwd=str(cwd), capture_output=True, text=True, timeout=timeout)
    assert result.returncode == 0, f"mngr {args} failed (rc={result.returncode}):\n{result.stderr}\n{result.stdout}"
    return result


def _user_turn_contents(watcher: ClaudeSessionWatcher) -> list[str]:
    """Every delivered user turn on disk, in order (both plain user records and the
    queued-command attachments a delivered queued message is recorded as)."""
    events = watcher.get_all_events()
    return [
        event["content"]
        for event in events
        if event.get("type") == "user_message" and not event.get("is_meta") and not event.get("is_compact_summary")
    ]


def _queued_contents(watcher: ClaudeSessionWatcher) -> list[str]:
    watcher.get_all_events()
    return [entry["content"] for entry in watcher.get_queued_messages()]


@pytest.mark.tmux
@pytest.mark.rsync
@pytest.mark.timeout(900, func_only=False)
def test_live_claude_stop_flush_and_restart_conserve_every_message(tmp_path: Path) -> None:
    work_repo_parent = _PROJECT_ROOT / ".test_output"
    work_repo_parent.mkdir(exist_ok=True)
    _skip_unless_live_claude_agent_possible(work_repo_parent)

    host_dir = tmp_path / "host"
    _prepare_isolated_host_dir(host_dir)
    tmux_dir = tmp_path / "tmux"
    tmux_dir.mkdir()
    env = _mngr_env(host_dir, tmux_dir)

    suffix = uuid.uuid4().hex
    agent_name = f"conserve-live-{suffix}"
    work_repo = work_repo_parent / f"conservation-live-repo-{suffix}"
    work_repo.mkdir()
    subprocess.run(["git", "init", "-q", str(work_repo)], check=True, timeout=30)
    subprocess.run(
        [
            "git",
            "-C",
            str(work_repo),
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "init",
        ],
        check=True,
        timeout=30,
    )

    message_1 = f"conserve-m1-{suffix}: {_LONG_TURN_PROMPT}"
    message_2 = f"conserve-m2-{suffix} parked alpha"
    message_3 = f"conserve-m3-{suffix} parked beta"
    message_4 = f"conserve-m4-{suffix}: {_LONG_TURN_PROMPT}"
    message_5 = f"conserve-m5-{suffix} parked gamma"

    try:
        _run_mngr(
            ["create", agent_name, "--no-connect", "--type", "claude"], env, work_repo, timeout=_CREATE_TIMEOUT_SECONDS
        )
        agent_dirs = sorted((host_dir / "agents").iterdir())
        assert len(agent_dirs) == 1, f"expected exactly one created agent, found {agent_dirs}"
        agent_state_dir = agent_dirs[0]
        agent_info = AgentInfo(
            id=agent_state_dir.name,
            name=agent_name,
            state="RUNNING",
            agent_state_dir=agent_state_dir,
            claude_config_dir=read_claude_config_dir_from_env_file(agent_state_dir),
            harness=HarnessType.CLAUDE,
        )
        watcher = ClaudeSessionWatcher.build(agent_info, on_events=lambda _agent_id, _events: None)
        active_marker = agent_state_dir / "active"

        def restart_process() -> tuple[bool, str]:
            result = subprocess.run(
                ["mngr", "start", agent_name, "--restart", "--no-resume"],
                env=env,
                cwd=str(work_repo),
                capture_output=True,
                text=True,
                timeout=_MNGR_COMMAND_TIMEOUT_SECONDS,
            )
            return (result.returncode == 0, result.stdout if result.returncode == 0 else result.stderr)

        # --- Turn 1: deliver m1, park m2+m3 mid-turn, then STOP (interrupt-and-retract). ------
        _run_mngr(["message", agent_name, "-m", message_1], env, work_repo)
        wait_for(
            lambda: active_marker.exists(),
            timeout=_TURN_START_TIMEOUT_SECONDS,
            error_message="the first turn never started (no active marker)",
        )
        _run_mngr(["message", agent_name, "-m", message_2], env, work_repo)
        _run_mngr(["message", agent_name, "-m", message_3], env, work_repo)
        wait_for(
            lambda: {message_2, message_3} <= set(_queued_contents(watcher)),
            timeout=_QUEUE_PARK_TIMEOUT_SECONDS,
            error_message="the mid-turn sends never showed up as parked in the queue mirror",
        )

        interrupter = build_interrupt_to_composer(agent_info)
        process_marker = agent_state_dir / "claude_process_started"
        marker_mtime_before_stop = process_marker.stat().st_mtime
        # press_chord is wired inert: this stop exercises the NONEMPTY branch (the bounded-lock
        # restart-drain); were the mirror to read empty, an inert chord still falls back to the
        # base restart, so the stop always stops.
        block = interrupter.drain_to_composer(watcher, restart_process, lambda: None, lambda: False)
        assert message_2 in block and message_3 in block, f"retracted messages must ride the block: {block!r}"
        assert block.index(message_2) < block.index(message_3), f"the block must keep queue order: {block!r}"

        # Ghost check (U6): once the relaunched process has announced its epoch, a FRESH
        # watcher's full replay -- the backend-restart shape -- must derive an EMPTY queue: the
        # retracted enqueues are stamped before the new ``claude_process_started`` mtime, so the
        # process-epoch scoping excludes them. The settle wait first: ``mngr start --restart``
        # returns several seconds before the relaunched claude's SessionStart hook touches the
        # marker (claude resumes the SAME session file, so until then the on-disk ledger is
        # indistinguishable from the dead process's, and a replay inside that boot window
        # transiently re-derives the retracted messages -- in production the agent manager's
        # idle sweep owns that window; this watcher-only harness has no manager, so it asserts
        # the post-settle claim, which is the one the epoch-scoping fix makes).
        wait_for(
            lambda: process_marker.stat().st_mtime > marker_mtime_before_stop,
            timeout=_TURN_START_TIMEOUT_SECONDS,
            error_message="the relaunched claude never announced its new process epoch",
        )
        fresh_watcher = ClaudeSessionWatcher.build(agent_info, on_events=lambda _agent_id, _events: None)
        assert _queued_contents(fresh_watcher) == [], "retracted messages re-derived as ghost queue entries"

        # --- Turn 2: deliver m4, park m5, then FLUSH (restart-drain + resend, the endpoint's
        # exact recipe) so m5 is delivered exactly once. --------------------------------------
        _run_mngr(["message", agent_name, "-m", message_4], env, work_repo)
        wait_for(
            lambda: message_4 in _user_turn_contents(watcher) and active_marker.exists(),
            timeout=_DELIVERY_TIMEOUT_SECONDS,
            error_message="the second turn never started",
        )
        _run_mngr(["message", agent_name, "-m", message_5], env, work_repo)
        wait_for(
            lambda: message_5 in _queued_contents(watcher),
            timeout=_QUEUE_PARK_TIMEOUT_SECONDS,
            error_message="the third mid-turn send never parked",
        )
        marker_mtime_before_flush = process_marker.stat().st_mtime
        flush_block = restart_drain(agent_info, watcher, restart_process, lambda: None)
        assert message_5 in flush_block, f"the flush must capture the parked message: {flush_block!r}"
        assert message_2 not in flush_block and message_3 not in flush_block, "retracted messages resurfaced"
        # Let the relaunch settle before the resend, so the flushed block opens a fresh turn.
        wait_for(
            lambda: process_marker.stat().st_mtime > marker_mtime_before_flush,
            timeout=_TURN_START_TIMEOUT_SECONDS,
            error_message="the relaunched claude never announced its epoch after the flush restart",
        )
        _run_mngr(["message", agent_name, "-m", flush_block], env, work_repo)
        wait_for(
            lambda: message_5 in _user_turn_contents(watcher),
            timeout=_DELIVERY_TIMEOUT_SECONDS,
            error_message="the flushed message never landed as a user turn",
        )

        # --- Conservation, read straight off the on-disk session records. --------------------
        turn_counts = Counter(_user_turn_contents(watcher))
        for delivered in (message_1, message_4, message_5):
            assert turn_counts[delivered] == 1, f"{delivered!r} must be a user turn exactly once: {turn_counts}"
        for returned in (message_2, message_3):
            assert turn_counts[returned] == 0, f"{returned!r} was returned to the composer yet ran: {turn_counts}"
        assert _queued_contents(watcher) == [], "nothing should remain queued at the end"
    finally:
        subprocess.run(
            ["mngr", "destroy", agent_name, "--force"],
            env=env,
            cwd=str(work_repo),
            capture_output=True,
            text=True,
            timeout=240,
        )
        socket_path = tmux_dir / f"tmux-{os.getuid()}" / "default"
        subprocess.run(
            ["tmux", "-S", str(socket_path), "kill-server"], capture_output=True, text=True, timeout=30, check=False
        )
        shutil.rmtree(work_repo, ignore_errors=True)
