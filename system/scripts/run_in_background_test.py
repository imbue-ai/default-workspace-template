"""Tests for run_in_background.py: the detached run, and the report it delivers to the caller's chat.

The end-to-end tests start the real script as a subprocess, the way an agent's tool call does,
and watch the ``fake_chat_app`` fixture for the report; the script's messenger is the real
``message_chat.py`` beside it, which reads the fixture's registry. Its ``mngr message`` backoff
reaches the ``fake_mngr`` fixture, never the real ``mngr``.
"""

from __future__ import annotations

import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from mngr_cli_contract.contract import assert_mngr_argv_valid

from conftest import run_in_background

_CHAT_ID = "agent-0123456789abcdef0123456789abcdef"
_SCRIPT = Path(__file__).parent / "run_in_background.py"
_DELIVERY_DEADLINE_SECONDS = 8.0


def _runner_argv(
    description: str, *command: str, options: tuple[str, ...] = ()
) -> list[str]:
    return [
        sys.executable,
        str(_SCRIPT),
        "--description",
        description,
        *options,
        "--",
        *command,
    ]


def _start_runner(
    cwd: Path,
    env: dict[str, str],
    description: str,
    *command: str,
    options: tuple[str, ...] = (),
) -> subprocess.CompletedProcess[str]:
    """Run the script as an agent's tool call does; it returns once the command is detached."""
    return subprocess.run(
        _runner_argv(description, *command, options=options),
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _python_command(source: str) -> list[str]:
    return [sys.executable, "-c", source]


def _agent_env(**extra: str) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in ("MINDS_CHAT_ID", "MNGR_AGENT_ID")
    }
    env.update(extra)
    return env


def _wait_for_posts(fake_chat_app: Any, count: int) -> list[tuple[str, dict[str, Any]]]:
    deadline = time.monotonic() + _DELIVERY_DEADLINE_SECONDS
    while len(fake_chat_app.posted) < count:
        assert time.monotonic() < deadline, "the report never reached the chat app"
        time.sleep(0.1)
    return list(fake_chat_app.posted)


def _task_dir_from(stdout: str, cwd: Path) -> Path:
    """The task directory the script announced, from its ``Output:`` line."""
    [output_line] = [
        line for line in stdout.splitlines() if line.startswith("Output: ")
    ]
    return (cwd / output_line.removeprefix("Output: ")).parent


@pytest.mark.usefixtures("fake_mngr")
def test_the_command_runs_detached_and_its_result_is_posted_to_the_callers_chat(
    fake_chat_app: Any, tmp_path: Path
) -> None:
    command = _python_command(
        "import sys, time; time.sleep(1); print('the report'); "
        "print('a note', file=sys.stderr); sys.exit(3)"
    )

    started = _start_runner(
        tmp_path, _agent_env(MNGR_AGENT_ID=_CHAT_ID), "Wait for the worker", *command
    )

    # The caller gets its turn back while the command is still running.
    assert started.returncode == 0, started.stderr
    task_dir = _task_dir_from(started.stdout, tmp_path)
    assert not (task_dir / "exit_code").exists()

    [(path, body)] = _wait_for_posts(fake_chat_app, 1)
    assert path == f"/api/chats/{_CHAT_ID}/message"
    report = body["message"]
    assert report.startswith(f"<{run_in_background.BACKGROUND_TASK_REPORT_TAG}>")
    assert "<summary>Wait for the worker (exit code 3)</summary>" in report
    assert "Exit code: 3" in report
    assert "the report" in report
    assert "a note" in report
    assert (task_dir / "exit_code").read_text().strip() == "3"
    assert "the report" in (task_dir / "output.log").read_text()


@pytest.mark.usefixtures("fake_mngr")
def test_the_chat_app_stamped_chat_id_wins_over_the_agent_id(
    fake_chat_app: Any, tmp_path: Path
) -> None:
    chat_id = "agent-fedcba9876543210fedcba9876543210"

    started = _start_runner(
        tmp_path,
        _agent_env(MINDS_CHAT_ID=chat_id, MNGR_AGENT_ID=_CHAT_ID),
        "Say hello",
        *_python_command("print('hello')"),
    )

    assert started.returncode == 0, started.stderr
    [(path, body)] = _wait_for_posts(fake_chat_app, 1)
    assert path == f"/api/chats/{chat_id}/message"
    assert "<summary>Say hello (finished)</summary>" in body["message"]


@pytest.mark.usefixtures("fake_mngr")
def test_the_report_still_arrives_after_the_callers_whole_process_group_is_killed(
    fake_chat_app: Any, tmp_path: Path
) -> None:
    """A harness may kill everything a tool call started once the call is over; the run must
    not be part of that group."""
    command = _python_command("import time; time.sleep(1); print('survived')")
    caller = subprocess.Popen(
        [
            "sh",
            "-c",
            f"{shlex.join(_runner_argv('Outlive the caller', *command))}; sleep 60",
        ],
        cwd=tmp_path,
        env=_agent_env(MNGR_AGENT_ID=_CHAT_ID),
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    assert caller.stdout is not None
    assert caller.stdout.readline().startswith("Started ")

    os.killpg(caller.pid, signal.SIGKILL)
    caller.wait(timeout=10)
    caller.stdout.close()

    [(_, body)] = _wait_for_posts(fake_chat_app, 1)
    assert "survived" in body["message"]


@pytest.mark.usefixtures("fake_mngr")
def test_a_reused_task_dir_shows_no_exit_code_until_the_new_command_exits(
    fake_chat_app: Any, tmp_path: Path
) -> None:
    task_dir = tmp_path / "reused-task"
    task_dir.mkdir()
    (task_dir / "exit_code").write_text("0\n")

    started = _start_runner(
        tmp_path,
        _agent_env(MNGR_AGENT_ID=_CHAT_ID),
        "Run again",
        *_python_command("import sys, time; time.sleep(1); sys.exit(5)"),
        options=("--task-dir", str(task_dir)),
    )

    assert started.returncode == 0, started.stderr
    assert not (task_dir / "exit_code").exists()
    _wait_for_posts(fake_chat_app, 1)
    assert (task_dir / "exit_code").read_text().strip() == "5"


def test_a_caller_that_is_not_an_agent_is_refused_before_anything_starts(
    tmp_path: Path,
) -> None:
    started = _start_runner(
        tmp_path, _agent_env(), "Say hello", *_python_command("print('hello')")
    )

    assert started.returncode == run_in_background.EXIT_USAGE
    assert "MNGR_AGENT_ID" in started.stderr
    # No task directory: the refusal comes before anything is started.
    assert not (tmp_path / "data").exists()


def test_help_is_shown_without_a_command_while_a_missing_command_is_named(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as help_exit:
        run_in_background.main(["--help"])
    assert help_exit.value.code == 0
    assert "--description" in capsys.readouterr().out

    with pytest.raises(SystemExit) as usage_exit:
        run_in_background.main(["--description", "Say hello", "echo", "hello"])
    assert usage_exit.value.code == 2
    assert "put the command after a `--`" in capsys.readouterr().err


def test_a_tree_without_the_chat_messenger_delivers_through_mngr_message(
    tmp_path: Path,
) -> None:
    """A workspace older than message_chat.py (an update-self run stages this script into one)."""
    message_file = tmp_path / "message.md"

    argv = run_in_background.messenger_argv(tmp_path, _CHAT_ID, message_file)

    assert argv == [
        "mngr",
        "message",
        _CHAT_ID,
        "--start",
        "--message-file",
        str(message_file),
    ]
    assert_mngr_argv_valid(argv)


@pytest.mark.parametrize(
    ("exit_codes", "expected_attempts", "is_delivered"),
    [
        ((0,), 1, True),
        # Delivered, but the agent's input is blocked on a dialog: the text is in, so no resend.
        ((7,), 1, True),
        ((1, 1, 0), 3, True),
        ((1,) * 10, run_in_background.DELIVERY_ATTEMPTS, False),
    ],
)
def test_a_failed_delivery_is_retried_until_it_lands_or_the_attempts_run_out(
    exit_codes: tuple[int, ...], expected_attempts: int, is_delivered: bool
) -> None:
    remaining = list(exit_codes)
    attempts: list[list[str]] = []
    slept: list[float] = []

    def run(argv: list[str]) -> int:
        attempts.append(argv)
        return remaining.pop(0)

    result = run_in_background.deliver_report(
        ["messenger"], run=run, sleep=slept.append
    )

    assert result is is_delivered
    assert len(attempts) == expected_attempts
    assert slept == [run_in_background.DELIVERY_RETRY_SECONDS] * (expected_attempts - 1)


@pytest.mark.parametrize(
    ("returncode", "status"),
    [(0, "finished"), (76, "exit code 76"), (-9, "killed by signal 9")],
)
def test_the_summary_says_how_the_command_ended(returncode: int, status: str) -> None:
    report = run_in_background.compose_report(
        description="Wait for the worker",
        command=["uv", "run", "thing"],
        returncode=returncode,
        output="",
        output_path=Path("data/.tasks/run-in-background/x/output.log"),
    )

    assert f"<summary>Wait for the worker ({status})</summary>" in report
    assert f"Exit code: {returncode}" in report


def test_long_output_keeps_its_start_and_end_and_points_at_the_full_file() -> None:
    """A worker report's frontmatter is at the start of ``await``'s output, and its result at the end."""
    output = (
        "---\ntype: status\nname: done\n---\n"
        + "x" * run_in_background.MAX_INLINE_HEAD_CHARS
        + "middle line\n"
        + "x" * run_in_background.MAX_INLINE_OUTPUT_CHARS
        + "\nlast line\n"
    )
    output_path = Path("data/.tasks/run-in-background/x/output.log")

    report = run_in_background.compose_report(
        description="Build",
        command=["make"],
        returncode=0,
        output=output,
        output_path=output_path,
    )

    assert "<output>\n---\ntype: status\nname: done\n---\n" in report
    assert "last line" in report
    assert "middle line" not in report
    assert f"omitted; the whole output is in {output_path}" in report
    assert len(report) < run_in_background.MAX_INLINE_OUTPUT_CHARS + 2000
