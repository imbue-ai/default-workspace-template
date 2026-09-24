"""Tests for run_in_background.py: the detached run, and the report it delivers to the caller's chat.

The end-to-end tests start the real script as a subprocess, the way an agent's tool call does,
and watch the ``fake_chat_app`` fixture for the report; the script's messenger is the real
``message_chat.py`` beside it, which reads the fixture's registry. Its ``mngr message`` backoff
reaches the ``fake_mngr`` fixture, never the real ``mngr``.
"""

from __future__ import annotations

import contextlib
import os
import secrets
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from mngr_cli_contract.contract import assert_mngr_argv_valid
from oom_priority import bands

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


def _kill_processes_tagged_with_agent_id(agent_id: str) -> None:
    """What ``mngr stop`` does to an agent's processes: kill every one whose environment
    carries ``MNGR_AGENT_ID=<agent_id>``, wherever it was reparented to."""
    marker = f"MNGR_AGENT_ID={agent_id}".encode()
    for environ_path in Path("/proc").glob("[0-9]*/environ"):
        pid = int(environ_path.parent.name)
        if pid == os.getpid():
            continue
        try:
            records = environ_path.read_bytes().split(b"\0")
        except OSError:
            # A process that exited mid-scan, or one of another user's.
            continue
        if marker in records:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)


@pytest.mark.skipif(
    not Path("/proc/self/environ").exists(),
    reason="needs Linux's /proc/<pid>/environ, which mngr's stop scans",
)
@pytest.mark.usefixtures("fake_mngr")
def test_a_stop_of_the_callers_agent_ends_the_command_but_not_its_report(
    fake_chat_app: Any, tmp_path: Path
) -> None:
    """A handoff retiring the caller's agent, or a restart after a shed, stops it with mngr, which
    kills every process tagged with the agent's id. The command is the agent's work and ends
    with it; the runner holds the report and must survive to deliver how the command ended."""
    command = _python_command(
        "import os, time; print('agent', os.environ.get('MNGR_AGENT_ID'), flush=True); time.sleep(60)"
    )
    # An id of its own, so the kill below reaches no other run's processes.
    agent_id = f"agent-{secrets.token_hex(16)}"
    started = _start_runner(
        tmp_path, _agent_env(MNGR_AGENT_ID=agent_id), "Outlive a stop", *command
    )
    assert started.returncode == 0, started.stderr
    output_log = _task_dir_from(started.stdout, tmp_path) / "output.log"
    deadline = time.monotonic() + _DELIVERY_DEADLINE_SECONDS
    while "agent" not in (output_log.read_text() if output_log.exists() else ""):
        assert time.monotonic() < deadline, "the command never started"
        time.sleep(0.1)

    _kill_processes_tagged_with_agent_id(agent_id)

    [(_, body)] = _wait_for_posts(fake_chat_app, 1)
    assert "<summary>Outlive a stop (killed by signal 9)</summary>" in body["message"]
    assert f"agent {agent_id}" in body["message"]


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


class _SleepClock:
    """A clock that moves only when the code under test sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def _deliver(exit_codes: list[int]) -> tuple[bool, int, _SleepClock]:
    """Run ``deliver_report`` against a messenger answering ``exit_codes`` in turn (the last one
    repeating); whether it delivered, how many sends it made, and the clock it slept on."""
    clock = _SleepClock()
    attempts: list[list[str]] = []

    def run(argv: list[str]) -> int:
        attempts.append(argv)
        return exit_codes[min(len(attempts), len(exit_codes)) - 1]

    is_delivered = run_in_background.deliver_report(
        ["messenger"], run=run, sleep=clock.sleep, clock=clock
    )
    return is_delivered, len(attempts), clock


@pytest.mark.parametrize(
    "exit_code",
    [
        run_in_background.EXIT_DELIVERED,
        # Delivered, but the agent's input is blocked on a dialog: the text is in, so no resend.
        run_in_background.EXIT_DELIVERED_BUT_BLOCKED,
    ],
)
def test_a_landed_send_is_never_repeated(exit_code: int) -> None:
    is_delivered, attempts, clock = _deliver([exit_code])

    assert is_delivered
    assert attempts == 1
    assert clock.slept == []


def test_a_failed_send_is_retried_with_a_growing_wait_until_it_lands() -> None:
    initial = run_in_background.DELIVERY_RETRY_INITIAL_SECONDS

    is_delivered, attempts, clock = _deliver(
        [1, 1, 1, run_in_background.EXIT_DELIVERED]
    )

    assert is_delivered
    assert attempts == 4
    assert clock.slept == [initial, initial * 2, initial * 4]


def test_a_gone_chat_ends_the_retries_at_once() -> None:
    is_delivered, attempts, clock = _deliver([1, run_in_background.EXIT_CHAT_GONE])

    assert not is_delivered
    assert attempts == 2
    assert clock.slept == [run_in_background.DELIVERY_RETRY_INITIAL_SECONDS]


def test_a_send_that_keeps_failing_is_retried_for_hours_at_a_capped_interval_then_given_up() -> (
    None
):
    """A worker shed for memory refuses its reports until its lead restarts it, which can take
    far longer than a mid-handoff refusal."""
    is_delivered, attempts, clock = _deliver([1])

    assert not is_delivered
    assert max(clock.slept) == run_in_background.DELIVERY_RETRY_MAX_INTERVAL_SECONDS
    # It gave up because one more wait would cross the budget, not before.
    assert clock.now <= run_in_background.DELIVERY_GIVE_UP_AFTER_SECONDS
    assert (
        clock.now + run_in_background.DELIVERY_RETRY_MAX_INTERVAL_SECONDS
        > run_in_background.DELIVERY_GIVE_UP_AFTER_SECONDS
    )
    assert attempts == len(clock.slept) + 1


@pytest.mark.usefixtures("fake_mngr")
def test_a_report_for_a_chat_that_no_longer_exists_is_given_up_on_the_first_send(
    fake_chat_app: Any, tmp_path: Path
) -> None:
    """The chat app does not know the chat and mngr has no agent by its id (the fake mngr sends
    nothing and exits 0), so the real messenger answers "gone" and the runner stops."""
    fake_chat_app.answers = [(404, {"detail": "Chat not found"})]

    started = _start_runner(
        tmp_path,
        _agent_env(MNGR_AGENT_ID=_CHAT_ID),
        "Report to nobody",
        *_python_command("print('done')"),
    )

    assert started.returncode == 0, started.stderr
    runner_log = _task_dir_from(started.stdout, tmp_path) / "runner.log"
    # The messenger's own 404 window runs first, in real time.
    deadline = time.monotonic() + _DELIVERY_DEADLINE_SECONDS + 10
    while "Gave up delivering" not in (
        runner_log.read_text() if runner_log.exists() else ""
    ):
        assert time.monotonic() < deadline, "the runner never gave up"
        time.sleep(0.2)
    log = runner_log.read_text()
    assert "Delivery attempt 1: the chat no longer exists" in log
    assert "retrying" not in log


def test_the_runners_band_is_oom_prioritys_user_service_band() -> None:
    assert run_in_background.RUNNER_OOM_SCORE_ADJ == bands.USER_SERVICE


@pytest.mark.parametrize(
    ("inherited", "expected"),
    [
        (bands.AGENT_SUBPROCESS, run_in_background.RUNNER_OOM_SCORE_ADJ),
        # Started from something more protected than a user service (a built-in service, say),
        # the runner keeps that protection rather than giving it up.
        (25, 25),
    ],
)
def test_the_runner_only_ever_lowers_its_own_band(
    tmp_path: Path, inherited: int, expected: int
) -> None:
    oom_score_adj = tmp_path / "oom_score_adj"
    oom_score_adj.write_text(f"{inherited}\n")

    run_in_background.move_to_runner_oom_band(oom_score_adj)

    assert int(oom_score_adj.read_text()) == expected


@pytest.mark.skipif(
    not run_in_background.OWN_OOM_SCORE_ADJ_PATH.exists(),
    reason="needs Linux's /proc/<pid>/oom_score_adj",
)
@pytest.mark.usefixtures("fake_mngr")
def test_the_runner_moves_below_the_agents_while_its_command_keeps_the_callers_band(
    fake_chat_app: Any, tmp_path: Path
) -> None:
    """The caller is an agent's Bash tool call, tagged most-expendable; the command it started
    stays there, and only the runner, which the report depends on, moves down."""
    # Reads its own band and, once the runner has moved, its parent's (the runner's).
    command = _python_command(
        "import os, time\n"
        "parent = f'/proc/{os.getppid()}/oom_score_adj'\n"
        "deadline = time.monotonic() + 5\n"
        f"while int(open(parent).read()) == {bands.AGENT_SUBPROCESS} and time.monotonic() < deadline:\n"
        "    time.sleep(0.05)\n"
        "print('command', open('/proc/self/oom_score_adj').read().strip())\n"
        "print('runner', open(parent).read().strip())\n"
    )
    caller_argv = [
        "sh",
        "-c",
        f"echo {bands.AGENT_SUBPROCESS} > /proc/self/oom_score_adj && exec {shlex.join(_runner_argv('Check the bands', *command))}",
    ]

    started = subprocess.run(
        caller_argv,
        cwd=tmp_path,
        env=_agent_env(MNGR_AGENT_ID=_CHAT_ID),
        capture_output=True,
        text=True,
        check=False,
    )

    assert started.returncode == 0, started.stderr
    [(_, body)] = _wait_for_posts(fake_chat_app, 1)
    assert f"command {bands.AGENT_SUBPROCESS}" in body["message"]
    assert f"runner {run_in_background.RUNNER_OOM_SCORE_ADJ}" in body["message"]


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
