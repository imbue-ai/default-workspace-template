"""Tests for antigravity's policy-guard bash shim.

The shim sits on the path of EVERY command the agent runs, so these cover two things in equal
measure: that the guards actually fire, and that the shim is otherwise indistinguishable from
bash. A guard that blocks correctly but corrupts argv, exit codes or stdin is a worse bug than
a guard that never fires.
"""

import os
import subprocess
from pathlib import Path

import pytest

_SHIM = Path(__file__).resolve().parent / "bash"
_WORK_DIR = (
    _SHIM.parent.parent.parent.parent
)  # repo root: system/scripts/agy_shim/ -> up 3


def _run(
    *args: str, stdin: str = "", env: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    full_env = {**os.environ, "MNGR_AGENT_WORK_DIR": str(_WORK_DIR)}
    full_env.pop("MNGR_AGY_BASH_SHIM", None)
    full_env.pop("MNGR_AGY_SHIM_OFF", None)
    if env:
        full_env.update(env)
    return subprocess.run(
        [str(_SHIM), *args],
        input=stdin,
        capture_output=True,
        text=True,
        env=full_env,
        timeout=60,
    )


# --- the shebang, which is a fork bomb if it is wrong -------------------------------------


def test_the_shebang_is_absolute() -> None:
    """`#!/usr/bin/env bash` -- which every other script here uses -- would make `env` resolve
    `bash` back to this shim, forever, killing every command the agent runs."""
    assert _SHIM.read_text().splitlines()[0] == "#!/bin/bash"


# --- the guards fire ----------------------------------------------------------------------


def test_it_blocks_a_git_history_rewrite() -> None:
    result = _run("-c", "git rebase -i HEAD~2")
    assert result.returncode == 2
    assert "git rebase" in result.stderr


def test_it_blocks_a_pipe_into_head() -> None:
    result = _run("-c", "ls | head -5")
    assert result.returncode == 2
    assert "tail or head" in result.stderr


def test_a_quote_in_the_command_does_not_defeat_a_guard() -> None:
    """THE bypass. Building the guard payload by string interpolation makes any command
    containing a quote invalid JSON; the guards run under `set -e`, so jq's parse error exits
    non-zero, and fail-open runs the command. Appending `# "` would defeat every guard."""
    result = _run("-c", 'git rebase -i HEAD~2 # "')
    assert result.returncode == 2, "a trailing quote must not turn a block into a pass"


def test_a_newline_in_the_command_does_not_break_the_payload() -> None:
    """A newline must not corrupt the guard payload. It is asserted with the rebase on the
    FIRST line because the guard anchors on `^git rebase` -- a second-line rebase is unblocked
    on claude too (same script, same regex), so matching it here would be a divergence, not a
    fix."""
    result = _run("-c", "git rebase -i HEAD~2\necho after")
    assert result.returncode == 2


def test_a_benign_command_runs() -> None:
    result = _run("-c", "echo benign-ok")
    assert result.returncode == 0
    assert "benign-ok" in result.stdout


def test_a_guard_that_writes_to_stderr_but_exits_zero_stays_silent(
    tmp_path: Path,
) -> None:
    """The commit-rewrite guard writes 'No command found in input' and exits 0. Only exit 2
    speaks to the model, or that note would ride the result of a legitimate command."""
    result = _run("-c", "echo quiet-ok")
    assert result.returncode == 0
    assert "No command found" not in result.stderr


# --- the rewrite --------------------------------------------------------------------------


def test_the_oom_tag_is_applied() -> None:
    result = _run("-c", "cat /proc/self/oom_score_adj")
    assert result.returncode == 0
    assert result.stdout.strip() == "900"


# --- transparency: it must be indistinguishable from bash ----------------------------------


def test_it_preserves_the_exit_code() -> None:
    assert _run("-c", "exit 42").returncode == 42


def test_it_preserves_stdin() -> None:
    result = _run("-c", "read x; echo got=$x", stdin="from-stdin\n")
    assert "got=from-stdin" in result.stdout


def test_it_passes_argv_after_the_command() -> None:
    """`bash -c CMD name arg...` sets $0 and the positional parameters."""
    result = _run("-c", 'echo "arg0=$0 first=$1"', "myname", "myarg")
    assert "arg0=myname first=myarg" in result.stdout


def test_a_command_with_a_trailing_newline_is_not_stripped() -> None:
    """Routing the executed command through JSON would strip it (and mangle non-UTF-8)."""
    result = _run("-c", 'printf "%s" "$(echo -n keep)"')
    assert result.returncode == 0
    assert "keep" in result.stdout


def test_a_non_dash_c_invocation_passes_straight_through() -> None:
    result = _run("--version")
    assert result.returncode == 0
    assert "GNU bash" in result.stdout


# --- scoping and the kill switch -----------------------------------------------------------


def test_a_nested_invocation_is_not_guarded_again() -> None:
    """Policing the whole process tree would block third-party code the agent did not write
    (a build whose package script pipes into head), and re-apply the prefix at every level."""
    result = _run("-c", '/bin/bash -c "echo nested-ok"; echo depth=$MNGR_AGY_BASH_SHIM')
    assert "nested-ok" in result.stdout
    assert "depth=1" in result.stdout


def test_the_kill_switch_disables_every_guard() -> None:
    result = _run("-c", "git rebase -i HEAD~2", env={"MNGR_AGY_SHIM_OFF": "1"})
    assert result.returncode != 2, "the kill switch must work without a redeploy"


# --- fail open ------------------------------------------------------------------------------


def test_it_runs_the_command_when_the_guards_are_missing(tmp_path: Path) -> None:
    """A shim that dies takes the agent with it, which is worse than a skipped guard."""
    result = _run("-c", "echo still-runs", env={"MNGR_AGENT_WORK_DIR": str(tmp_path)})
    assert result.returncode == 0
    assert "still-runs" in result.stdout


@pytest.mark.parametrize(
    "command", ["echo $(echo nested)", "echo 'single'", 'echo "double"', "echo a\\\\b"]
)
def test_quoting_forms_survive_the_round_trip(command: str) -> None:
    assert _run("-c", command).returncode == 0
