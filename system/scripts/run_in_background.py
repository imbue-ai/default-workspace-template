#!/usr/bin/env python3
"""Run a command in the background and deliver how it ended to your own chat.

Usage, from the repo root (every skill's cwd)::

    python3 system/scripts/run_in_background.py --description "Wait for the worker" -- <command> [args...]

The script returns at once: the command runs in a detached process of its own, so the
caller's tool call ends and the agent can end its turn. When the command exits, its exit
code and output are sent to the caller's chat as one message (through ``message_chat.py``),
and that message starts the agent's next turn. This is the way to be woken by a finished
command that works on every harness; most harnesses' own background tools do not start a
turn when their command finishes.

The message is a ``<background-task-report>`` whose ``<summary>`` line is all the chat
shows the user (the chat app renders it as a one-line notice; a test there pins its copy of
the tag to this one). The rest is for the agent: the command, its exit code, its output (cut in
the middle when long), and the file holding all of it.

A run leaves everything in ``data/.tasks/run-in-background/<task-id>/`` under the caller's
cwd: ``output.log`` (the command's stdout and stderr, interleaved), ``exit_code`` once the
command has exited, and ``runner.log``, the detached process's own record of the delivery.
A caller that needs the result mid-turn, before the message can reach it, reads the first
two.

The detached process starts a session of its own, so it outlives the caller's tool call,
its process group, and a stop of the caller's agent (the chat app revives a stopped agent
to take the message). A send that fails is retried with backoff for hours, since the
chat may be mid-handoff or its agent shed for memory until its lead restarts it; only a
chat that no longer exists ends the retries at once. The command keeps the caller's OOM
band, so a command shed for memory is reported like any other exit; the detached process
then moves itself down to the user-service band, below every agent, because it holds
next to no memory and shedding it would lose the report.

Standard library only: ``update-self`` stages this file from its target release and runs it
in a workspace that may predate it, so the messenger is looked up in the tree the script
sits in, falling back to ``mngr message`` for a tree older than ``message_chat.py``.
"""

from __future__ import annotations

import argparse
import datetime
import os
import secrets
import shlex
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

# Cross-layer contract: the chat app's ``harnesses/message_display.py`` keeps a copy
# (``BACKGROUND_TASK_REPORT_TAG``) and its test pins the two equal.
BACKGROUND_TASK_REPORT_TAG = "background-task-report"

TASKS_DIR = Path("data") / ".tasks" / "run-in-background"
OUTPUT_FILE_NAME = "output.log"
EXIT_CODE_FILE_NAME = "exit_code"
MESSAGE_FILE_NAME = "message.md"
RUNNER_LOG_FILE_NAME = "runner.log"

MESSAGE_CHAT_REL = Path("system") / "scripts" / "message_chat.py"

# ``mngr message``'s exit codes, which ``message_chat.py`` passes through.
EXIT_DELIVERED = 0
EXIT_DELIVERED_BUT_BLOCKED = 7
# ``message_chat.py``'s code for a chat that no longer exists. An older tree's copy never
# returns it, so there every failed send is retried.
EXIT_CHAT_GONE = 8
# This script's own exit status when there is no chat to deliver the result to.
EXIT_USAGE = 2
# What a shell reports for a command it could not start.
EXIT_COMMAND_NOT_RUNNABLE = 127

# The command's result exists only in this message, so a failed send is retried for as long
# as a shed agent might plausibly wait for its lead to restart it.
DELIVERY_RETRY_INITIAL_SECONDS = 20.0
DELIVERY_RETRY_MAX_INTERVAL_SECONDS = 600.0
DELIVERY_GIVE_UP_AFTER_SECONDS = 4 * 3600.0

# oom_priority's ``USER_SERVICE`` band, copied because this script is standard library only;
# a test pins it to ``bands.py``.
RUNNER_OOM_SCORE_ADJ = 200
OWN_OOM_SCORE_ADJ_PATH = Path("/proc/self/oom_score_adj")

MAX_INLINE_OUTPUT_CHARS = 20_000
# How much of a long output's start is kept: enough for a report's frontmatter, which a
# reader parses before anything else.
MAX_INLINE_HEAD_CHARS = 2_000


class RepoRootNotFoundError(Exception):
    """No ancestor of this script holds ``system/scripts``, so there is no tree to find the messenger in."""


def own_chat_id(environ: Mapping[str, str]) -> str:
    """The caller's chat: ``MINDS_CHAT_ID`` from the chat app that created the agent, else the agent's
    own id (an agent that is its own chat), else empty outside an agent."""
    return environ.get("MINDS_CHAT_ID", "") or environ.get("MNGR_AGENT_ID", "")


def script_repo_root() -> Path:
    """The repo this copy of the script belongs to: the nearest ancestor holding ``system/scripts``.

    Walked up rather than counted, because update-self runs a copy staged under ``data/``.
    """
    for ancestor in Path(__file__).resolve().parents:
        if (ancestor / "system" / "scripts").is_dir():
            return ancestor
    raise RepoRootNotFoundError(
        f"no ancestor of {Path(__file__).resolve()} holds system/scripts"
    )


def messenger_argv(repo_root: Path, chat_id: str, message_file: Path) -> list[str]:
    """The command that sends ``message_file`` to the chat: the chat messenger, else ``mngr message``."""
    messenger = repo_root / MESSAGE_CHAT_REL
    if messenger.is_file():
        return [
            sys.executable,
            str(messenger),
            chat_id,
            "--message-file",
            str(message_file),
        ]
    return ["mngr", "message", chat_id, "--start", "--message-file", str(message_file)]


def describe_exit(returncode: int) -> str:
    if returncode == 0:
        return "finished"
    if returncode < 0:
        return f"killed by signal {-returncode}"
    return f"exit code {returncode}"


def wrap_background_task_report(summary: str, body: str) -> str:
    """The message the chat shows as a notice reading ``summary``; all of it reaches the agent."""
    return f"<{BACKGROUND_TASK_REPORT_TAG}>\n<summary>{summary}</summary>\n{body}\n</{BACKGROUND_TASK_REPORT_TAG}>"


def compose_report(
    description: str,
    command: Sequence[str],
    returncode: int,
    output: str,
    output_path: Path,
) -> str:
    """The message for one finished command; long output keeps its start and, mostly, its end,
    where a result usually is."""
    if len(output) > MAX_INLINE_OUTPUT_CHARS:
        tail_chars = MAX_INLINE_OUTPUT_CHARS - MAX_INLINE_HEAD_CHARS
        omitted_count = len(output) - MAX_INLINE_OUTPUT_CHARS
        shown_output = (
            output[:MAX_INLINE_HEAD_CHARS]
            + f"\n[{omitted_count} characters omitted; the whole output is in {output_path}]\n"
            + output[-tail_chars:]
        )
    else:
        shown_output = output
    body = "\n".join(
        [
            "A command you started with system/scripts/run_in_background.py has exited.",
            f"Command: {shlex.join(command)}",
            f"Exit code: {returncode}",
            f"Output file: {output_path}",
            "",
            "<output>",
            shown_output.rstrip("\n"),
            "</output>",
        ]
    )
    # The summary is one line of the chat's notice, whatever the caller typed.
    summary = f"{' '.join(description.split())} ({describe_exit(returncode)})"
    return wrap_background_task_report(summary, body)


def deliver_report(
    messenger: Sequence[str],
    run: Callable[[list[str]], int],
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> bool:
    """Send the report, retrying a failed send with backoff until it lands, the chat is gone, or
    the retry budget runs out; whether it was delivered."""
    started_at = clock()
    interval = DELIVERY_RETRY_INITIAL_SECONDS
    attempt = 0
    while True:
        attempt += 1
        returncode = run(list(messenger))
        if returncode in (EXIT_DELIVERED, EXIT_DELIVERED_BUT_BLOCKED):
            return True
        if returncode == EXIT_CHAT_GONE:
            _log(f"Delivery attempt {attempt}: the chat no longer exists")
            return False
        if clock() - started_at + interval > DELIVERY_GIVE_UP_AFTER_SECONDS:
            _log(
                f"Delivery attempt {attempt} failed with exit code {returncode}, "
                f"and the {DELIVERY_GIVE_UP_AFTER_SECONDS:.0f}s retry budget is spent"
            )
            return False
        _log(
            f"Delivery attempt {attempt} failed with exit code {returncode}; retrying in {interval:.0f}s"
        )
        sleep(interval)
        interval = min(interval * 2, DELIVERY_RETRY_MAX_INTERVAL_SECONDS)


def move_to_runner_oom_band(oom_score_adj_path: Path) -> None:
    """Lower this process's ``oom_score_adj`` to the runner's band; never raises it.

    Best-effort: a host without the file (macOS) or a refused write leaves the inherited band,
    noted in ``runner.log``.
    """
    try:
        current = int(oom_score_adj_path.read_text())
        if current > RUNNER_OOM_SCORE_ADJ:
            oom_score_adj_path.write_text(f"{RUNNER_OOM_SCORE_ADJ}\n")
    except (OSError, ValueError) as exc:
        _log(f"Kept the inherited OOM band: {exc}")


def _log(message: str) -> None:
    """One line of ``runner.log`` (the detached process's stderr), stamped so a silent gap is visible."""
    stamp = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    print(f"{stamp} {message}", file=sys.stderr, flush=True)


def _run_messenger(argv: list[str]) -> int:
    try:
        return subprocess.run(argv, stdin=subprocess.DEVNULL, check=False).returncode
    except FileNotFoundError as exc:
        _log(f"Could not run the messenger: {exc}")
        return 1


def _run_command(command: Sequence[str], output_path: Path) -> int:
    with output_path.open("wb") as output:
        try:
            process: subprocess.Popen[bytes] | None = subprocess.Popen(
                list(command),
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
            )
        except OSError as exc:
            output.write(f"Could not start the command: {exc}\n".encode())
            process = None
        # After the start, so the command keeps the band it inherited.
        move_to_runner_oom_band(OWN_OOM_SCORE_ADJ_PATH)
        if process is None:
            return EXIT_COMMAND_NOT_RUNNABLE
        return process.wait()


def run_and_deliver(
    task_dir: Path, description: str, chat_id: str, command: Sequence[str]
) -> int:
    """Run the command to completion here, then send its report to the chat."""
    output_path = task_dir / OUTPUT_FILE_NAME
    _log(f"Running {shlex.join(command)}")
    started_at = time.monotonic()
    returncode = _run_command(command, output_path)
    (task_dir / EXIT_CODE_FILE_NAME).write_text(f"{returncode}\n")
    _log(
        f"Command exited with code {returncode} after {time.monotonic() - started_at:.0f}s"
    )

    message_file = task_dir / MESSAGE_FILE_NAME
    message_file.write_text(
        compose_report(
            description=description,
            command=command,
            returncode=returncode,
            output=output_path.read_text(encoding="utf-8", errors="replace"),
            output_path=output_path,
        ),
        encoding="utf-8",
    )
    messenger = messenger_argv(script_repo_root(), chat_id, message_file)
    if deliver_report(
        messenger, run=_run_messenger, sleep=time.sleep, clock=time.monotonic
    ):
        _log(f"Delivered the report to chat {chat_id}")
        return 0
    _log(f"Gave up delivering the report to chat {chat_id}; it is in {message_file}")
    return 1


def _start_detached(
    task_dir: Path, description: str, chat_id: str, command: Sequence[str]
) -> None:
    # A session of its own, and no stdio shared with the caller, so nothing the caller's harness
    # does to the tool call's processes reaches it and nothing waits on it.
    with (task_dir / RUNNER_LOG_FILE_NAME).open("ab") as runner_log:
        subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--foreground",
                "--task-dir",
                str(task_dir),
                "--chat-id",
                chat_id,
                "--description",
                description,
                "--",
                *command,
            ],
            stdin=subprocess.DEVNULL,
            stdout=runner_log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )


def _new_task_dir() -> Path:
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return TASKS_DIR / f"{stamp}-{secrets.token_hex(3)}"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        usage="run_in_background.py --description TEXT [options] -- COMMAND [ARGS...]",
        description="Run a command in the background; when it exits, its exit code and output "
        "arrive in your chat as a message, which starts your next turn.",
    )
    parser.add_argument(
        "--description",
        required=True,
        help="What the command is for, in a few plain words; the chat shows it to the user.",
    )
    parser.add_argument(
        "--chat-id",
        default=None,
        help="The chat to deliver the result to (default: your own, $MINDS_CHAT_ID or $MNGR_AGENT_ID).",
    )
    parser.add_argument(
        "--task-dir",
        default=None,
        help="Keep the run's files here instead of a new directory under data/.tasks/run-in-background/.",
    )
    parser.add_argument(
        "--foreground",
        action="store_true",
        help="Run the command in this process and deliver its result when it exits, instead of "
        "detaching (what the detached copy runs).",
    )
    return parser


def main(
    argv: Sequence[str] | None = None, environ: Mapping[str, str] | None = None
) -> int:
    parser = _build_parser()
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if "--" not in raw_argv:
        # Let --help, and a missing --description, answer for themselves first.
        parser.parse_known_args(raw_argv)
        parser.error("put the command after a `--`")
    separator_index = raw_argv.index("--")
    args = parser.parse_args(raw_argv[:separator_index])
    command = raw_argv[separator_index + 1 :]
    if not command:
        parser.error("no command after `--`")

    chat_id = args.chat_id or own_chat_id(os.environ if environ is None else environ)
    if not chat_id:
        print(
            "run_in_background.py: neither MINDS_CHAT_ID nor MNGR_AGENT_ID is set, so there is no "
            "chat to deliver the result to. Run it as an agent, or name one with --chat-id.",
            file=sys.stderr,
        )
        return EXIT_USAGE

    task_dir = Path(args.task_dir) if args.task_dir else _new_task_dir()
    task_dir.mkdir(parents=True, exist_ok=True)
    # A reused directory's exit code belongs to the previous command, not this one.
    (task_dir / EXIT_CODE_FILE_NAME).unlink(missing_ok=True)
    if args.foreground:
        return run_and_deliver(task_dir, args.description, chat_id, command)
    _start_detached(task_dir, args.description, chat_id, command)
    print(
        f"Started in the background as task {task_dir.name}. When the command exits, its exit "
        "code and output arrive in this chat as a message, and that message starts your next "
        "turn: nothing needs to wait on it."
    )
    print(f"Output: {task_dir / OUTPUT_FILE_NAME}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
