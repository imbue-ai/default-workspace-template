"""Putting the workspace's running agents onto what an update installed.

An agent's harness process loads its binary, extensions and launch config once, when it
starts, so an update that changes one of them reaches a running agent only when that agent
is restarted. A restart keeps the conversation: each harness resumes its own session.

``restart_idle_agents`` restarts every agent the chat app lists that has ended its turn, other
than the pass's own chat and its worker, and reports the ones it left running because they
were busy.``start_self_restart`` restarts the pass's own chat once its last turn ends,
from a detached helper, and sends it a note to confirm the restart to the user (or, when
the restart does not happen, a note saying so).

Every restart goes through ``system/scripts/message_chat.py --interrupt``, the chat app's
interrupt route: the chat app knows which agent a chat runs on and refuses a restart during
a handoff. These run only after a successful apply, so the tree is the release's and has it.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from run_in_background import wrap_background_task_report
from update_probes import registry_app_url
from update_runtime import HttpClient, Runner

CHAT_APP_NAME = "chat"
CHATS_PATH = "/api/chats"
MESSAGE_CHAT_REL = Path("system") / "scripts" / "message_chat.py"

# The flow's worker, which the pass stops itself once it has consumed the worker's report.
UPDATE_WORKER_NAME = "update-self"

# ``restart-agents`` leaves its report here for ``restart-self`` to carry into the note.
AGENT_RESTARTS_REPORT_REL = (
    Path("data") / ".tasks" / "update-self" / "agent-restarts.json"
)
SELF_RESTART_DIR_REL = Path("data") / ".tasks" / "update-self" / "self-restart"
SELF_RESTART_NOTE_NAME = "note.md"
SELF_RESTART_FAILURE_NOTE_NAME = "failure-note.md"
SELF_RESTART_LOG_NAME = "helper.log"

# The variable mngr tags an agent's processes with and kills them by on a stop.
AGENT_ID_ENV = "MNGR_AGENT_ID"

# The detached runner ``run_in_background.py`` leaves while a chat's background command runs. It
# gives the command the chat's agent id, so a restart of the chat kills the command.
RUN_IN_BACKGROUND_SCRIPT_NAME = "run_in_background.py"
BACKGROUND_RUNNER_FLAG = "--foreground"
BACKGROUND_RUNNER_CHAT_ID_FLAG = "--chat-id"
PROC_DIR = Path("/proc")

# The chat app's status for a chat whose agent has ended its turn, and the mngr lifecycle
# state its Stop hook leaves behind. A transcript-derived "idle" alone can show for a moment
# between an assistant message and the tool call after it.
IDLE_CHAT_STATUS = "idle"
STOPPED_CHAT_STATUS = "stopped"
ATTENTION_CHAT_STATUS = "attention"
ERROR_CHAT_STATUS = "error"
WAITING_LIFECYCLE_STATE = "WAITING"

# How long the chat list is retried while the chat app is unreachable or not ready: the apply
# has just restarted it.
CHAT_LIST_RETRY_WINDOW_SECONDS = 60.0
CHAT_LIST_RETRY_INTERVAL_SECONDS = 2.0
CHAT_LIST_REQUEST_TIMEOUT_SECONDS = 10.0

RESTART_TIMEOUT_SECONDS = 180.0

# How long the self-restart helper waits for the pass's last turn to end.
SELF_RESTART_DEADLINE_SECONDS = 30 * 60.0
SELF_RESTART_POLL_SECONDS = 5.0


class ChatListUnavailableError(Exception):
    """The chat app could not give its chat list within the retry window."""


@dataclass(frozen=True)
class ListedChat:
    """One chat from the chat app's list, as much of it as a restart decision reads."""

    chat_id: str
    name: str
    title: str
    # The chat app's status for the chat (its ``ChatStatus`` wire value).
    status: str
    # The mngr lifecycle state of the agent the chat runs on.
    lifecycle_state: str
    is_converging: bool
    # A send is in flight, waiting for the agent's harness to come up; a restart would land
    # underneath it.
    is_receiving_message: bool

    @property
    def is_idle(self) -> bool:
        return (
            self.status == IDLE_CHAT_STATUS
            and self.lifecycle_state == WAITING_LIFECYCLE_STATE
            and not self.is_converging
            and not self.is_receiving_message
        )

    @property
    def busy_with(self) -> str:
        """What a chat that is not idle is doing, in the words the results message uses."""
        if self.is_converging:
            return "switching to another agent"
        if self.status == ATTENTION_CHAT_STATUS:
            return "waiting on a dialog"
        if self.status == ERROR_CHAT_STATUS:
            return "in an error state"
        if self.is_receiving_message:
            return "receiving a message"
        return "working"


@dataclass(frozen=True)
class LeftRunningChat:
    chat: ListedChat
    # What the chat is doing, in the words the results message uses.
    busy_with: str


@dataclass(frozen=True)
class AgentRestartPlan:
    to_restart: tuple[ListedChat, ...]
    left_running: tuple[LeftRunningChat, ...]


def parse_chat_list(body: object) -> list[ListedChat]:
    """The chats in a ``GET /api/chats`` answer; raises ``ValueError`` for one of another shape."""
    if not isinstance(body, dict) or not isinstance(body.get("chats"), list):
        raise ValueError("the chat list answer carries no `chats` list")
    chats: list[ListedChat] = []
    for raw in body["chats"]:
        active_agent = raw.get("active_agent") if isinstance(raw, dict) else None
        if not isinstance(active_agent, dict):
            raise ValueError(f"a chat in the list has no active agent: {raw!r}")
        chat_id = raw.get("chat_id")
        if not isinstance(chat_id, str) or not chat_id:
            raise ValueError(f"a chat in the list has no id: {raw!r}")
        chats.append(
            ListedChat(
                chat_id=chat_id,
                name=str(raw.get("name") or ""),
                title=str(raw.get("title") or raw.get("name") or chat_id),
                status=str(raw.get("status") or ""),
                lifecycle_state=str(active_agent.get("state") or ""),
                is_converging=raw.get("handoff") is not None,
                is_receiving_message=active_agent.get("is_connecting") is True,
            )
        )
    return chats


def read_process_argvs(proc_dir: Path = PROC_DIR) -> list[list[str]]:
    """Every running process's argv, from ``/proc``; none on a host without it (not a workspace)."""
    argvs: list[list[str]] = []
    for cmdline_path in proc_dir.glob("[0-9]*/cmdline"):
        try:
            raw = cmdline_path.read_bytes()
        except (FileNotFoundError, ProcessLookupError):
            # The process exited between the listing and the read.
            continue
        if raw:
            argvs.append(
                raw.rstrip(b"\0").decode("utf-8", errors="replace").split("\0")
            )
    return argvs


def chats_running_background_commands(
    process_argvs: Iterable[Sequence[str]],
) -> frozenset[str]:
    """The chats a live ``run_in_background.py`` runner holds a command (or its report) for."""
    chat_ids: set[str] = set()
    for argv in process_argvs:
        runner_options = list(argv[: argv.index("--")] if "--" in argv else argv)
        if not any(
            Path(arg).name == RUN_IN_BACKGROUND_SCRIPT_NAME for arg in runner_options
        ):
            continue
        if (
            BACKGROUND_RUNNER_FLAG not in runner_options
            or BACKGROUND_RUNNER_CHAT_ID_FLAG not in runner_options
        ):
            continue
        value_index = runner_options.index(BACKGROUND_RUNNER_CHAT_ID_FLAG) + 1
        if value_index < len(runner_options):
            chat_ids.add(runner_options[value_index])
    return frozenset(chat_ids)


def plan_agent_restarts(
    chats: Sequence[ListedChat],
    own_chat_id: str,
    chats_with_background_commands: frozenset[str],
) -> AgentRestartPlan:
    """Which chats to restart now: every idle one but the pass's own and its worker's.

    A stopped chat needs nothing, since its next start loads what the update installed; any
    other is mid-turn, waiting on a dialog, mid-handoff, receiving a message, or has ended
    its turn while a background command it started runs (which the restart would kill), and
    is left running for the user to decide about.
    """
    to_restart: list[ListedChat] = []
    left_running: list[LeftRunningChat] = []
    for chat in chats:
        if chat.chat_id == own_chat_id or chat.name == UPDATE_WORKER_NAME:
            continue
        if chat.status == STOPPED_CHAT_STATUS:
            continue
        if not chat.is_idle:
            left_running.append(LeftRunningChat(chat, chat.busy_with))
        elif chat.chat_id in chats_with_background_commands:
            left_running.append(LeftRunningChat(chat, "running a background command"))
        else:
            to_restart.append(chat)
    return AgentRestartPlan(
        to_restart=tuple(to_restart), left_running=tuple(left_running)
    )


def fetch_chat_list(
    repo_root: Path,
    http: HttpClient,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> list[ListedChat]:
    """The chat app's chat list, retried while the app is unregistered, unreachable or not ready."""
    started_at = monotonic()
    last_problem = "the chat app has no row in the app registry"
    while True:
        base_url = registry_app_url(repo_root, CHAT_APP_NAME)
        if base_url is not None:
            page = http.get_page(
                f"{base_url.rstrip('/')}{CHATS_PATH}", CHAT_LIST_REQUEST_TIMEOUT_SECONDS
            )
            if page is not None and page.status == 200:
                try:
                    return parse_chat_list(json.loads(page.body))
                except ValueError as exc:
                    raise ChatListUnavailableError(
                        f"the chat app's chat list did not parse: {exc}"
                    ) from exc
            last_problem = (
                f"the chat app at {base_url} did not answer"
                if page is None
                else f"the chat app answered HTTP {page.status}"
            )
        if monotonic() - started_at >= CHAT_LIST_RETRY_WINDOW_SECONDS:
            raise ChatListUnavailableError(last_problem)
        sleep(CHAT_LIST_RETRY_INTERVAL_SECONDS)


def interrupt_argv(repo_root: Path, chat_id: str) -> list[str]:
    """The ``message_chat.py --interrupt`` call that restarts a chat."""
    return [sys.executable, str(repo_root / MESSAGE_CHAT_REL), chat_id, "--interrupt"]


def send_note_argv(repo_root: Path, chat_id: str, note_path: Path) -> list[str]:
    """The ``message_chat.py`` call that sends a chat the note at ``note_path``."""
    return [
        sys.executable,
        str(repo_root / MESSAGE_CHAT_REL),
        chat_id,
        "--message-file",
        str(note_path),
    ]


def _run_message_chat(
    argv: Sequence[str], repo_root: Path, runner: Runner
) -> str | None:
    """Run one ``message_chat.py`` call; None when it worked, else what went wrong."""
    try:
        result = runner.run(
            argv,
            cwd=str(repo_root),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
            timeout=RESTART_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return f"it did not finish within {RESTART_TIMEOUT_SECONDS:g}s"
    except OSError as exc:
        return f"it could not be run: {exc}"
    if result.returncode != 0:
        return (result.stderr or "").strip() or f"exit code {result.returncode}"
    return None


def restart_idle_agents(
    repo_root: Path,
    own_chat_id: str,
    http: HttpClient,
    runner: Runner,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    process_argvs: Callable[[], Iterable[Sequence[str]]] = read_process_argvs,
) -> dict[str, list[dict[str, str]]]:
    """Restart every idle chat but the pass's own and its worker's; the report of what happened.

    The report is also written to ``AGENT_RESTARTS_REPORT_REL``. Raises
    ``ChatListUnavailableError`` when there is no chat list to act on.
    """
    report_path = repo_root / AGENT_RESTARTS_REPORT_REL
    report_path.unlink(missing_ok=True)
    chats = fetch_chat_list(repo_root, http, monotonic, sleep)
    plan = plan_agent_restarts(
        chats, own_chat_id, chats_running_background_commands(process_argvs())
    )
    report: dict[str, list[dict[str, str]]] = {
        "restarted": [],
        "left_running": [
            {
                "chat_id": entry.chat.chat_id,
                "title": entry.chat.title,
                "busy_with": entry.busy_with,
            }
            for entry in plan.left_running
        ],
        "failed": [],
    }
    for chat in plan.to_restart:
        problem = _run_message_chat(
            interrupt_argv(repo_root, chat.chat_id), repo_root, runner
        )
        if problem is None:
            report["restarted"].append({"chat_id": chat.chat_id, "title": chat.title})
        else:
            report["failed"].append(
                {"chat_id": chat.chat_id, "title": chat.title, "detail": problem}
            )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2) + "\n")
    return report


def compose_self_restart_note(
    reason: str, left_running: Sequence[Mapping[str, str]]
) -> str:
    """The note the pass's own chat gets once it is back: all of it for the agent, one line for the user.

    It stands on its own, because a harness that cannot resume the old session starts a new
    one, and then the note is all the agent knows about the update.
    """
    lines = [
        "The update of this workspace has finished, and this chat was restarted so it runs on "
        f"what the update installed: {' '.join(reason.split())}.",
        "Check that the restart took, with the harness's own version command where it has one "
        "(for example `claude --version`), then tell the user in one or two plain sentences that "
        "the update is complete and this chat is running on the new version.",
        "If you do not remember the update, the earlier conversation could not be resumed: say "
        "so plainly, and do not guess at what was said before.",
    ]
    if left_running:
        names = ", ".join(f'"{chat["title"]}"' for chat in left_running)
        lines.append(
            f"These were busy and are still running the previous version: {names}. Ask the user "
            "whether to interrupt and restart them now (`python3 system/scripts/message_chat.py "
            "<chat-id> --interrupt`); their ids are in "
            f"`{AGENT_RESTARTS_REPORT_REL}`. Do not restart them without a yes."
        )
    return wrap_background_task_report(
        "Restarted to finish the update", "\n\n".join(lines)
    )


def compose_self_restart_failure_note(problem: str) -> str:
    """The note the pass's own chat gets when its restart did not happen, so the user is not
    left believing the results message's promise that it would."""
    body = "\n\n".join(
        [
            f"This chat was to restart after the update of this workspace, but it was not restarted: {problem}.",
            "Tell the user in one or two plain sentences that the update is complete but this chat "
            "is still running the previous version, and that pressing its stop button restarts it "
            "onto the new one (its conversation carries over).",
        ]
    )
    return wrap_background_task_report(
        "The restart after the update did not happen", body
    )


def read_left_running(repo_root: Path) -> list[dict[str, str]]:
    """The chats ``restart-agents`` left running this pass; none when it has not run or recorded none."""
    try:
        report = json.loads((repo_root / AGENT_RESTARTS_REPORT_REL).read_text())
    except (OSError, ValueError):
        return []
    left_running = report.get("left_running") if isinstance(report, dict) else None
    return list(left_running) if isinstance(left_running, list) else []


def start_self_restart(
    repo_root: Path,
    chat_id: str,
    note: str,
    environ: Mapping[str, str],
    helper_argv: Sequence[str],
) -> Path:
    """Write the note and detach the helper that restarts ``chat_id`` once its turn ends; the note's path.

    The helper leads a session of its own with no stdio shared with the caller, and has no
    agent id in its environment: the restart it performs kills every process carrying the
    chat's agent id, and the helper has to outlive that to send the note.
    """
    work_dir = repo_root / SELF_RESTART_DIR_REL
    work_dir.mkdir(parents=True, exist_ok=True)
    note_path = work_dir / SELF_RESTART_NOTE_NAME
    note_path.write_text(note)
    with (work_dir / SELF_RESTART_LOG_NAME).open("ab") as helper_log:
        subprocess.Popen(
            list(helper_argv),
            cwd=repo_root,
            env={key: value for key, value in environ.items() if key != AGENT_ID_ENV},
            stdin=subprocess.DEVNULL,
            stdout=helper_log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    return note_path


def wait_for_idle_chat(
    read_chat: Callable[[], ListedChat | None],
    deadline_seconds: float,
    poll_seconds: float,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Poll until the chat has ended its turn; False when the deadline passes first.

    A chat the list cannot show right now (the chat app restarting, a read that failed) is
    not idle, and is polled again.
    """
    started_at = monotonic()
    while True:
        chat = read_chat()
        if chat is not None and chat.is_idle:
            return True
        if monotonic() - started_at >= deadline_seconds:
            return False
        sleep(poll_seconds)


def read_listed_chat(
    repo_root: Path, chat_id: str, http: HttpClient
) -> ListedChat | None:
    """One read of ``chat_id`` from the chat list; None when the chat app cannot show it now."""
    base_url = registry_app_url(repo_root, CHAT_APP_NAME)
    if base_url is None:
        return None
    page = http.get_page(
        f"{base_url.rstrip('/')}{CHATS_PATH}", CHAT_LIST_REQUEST_TIMEOUT_SECONDS
    )
    if page is None or page.status != 200:
        return None
    try:
        chats = parse_chat_list(json.loads(page.body))
    except ValueError:
        return None
    return next((chat for chat in chats if chat.chat_id == chat_id), None)


def restart_self_when_idle(
    repo_root: Path,
    chat_id: str,
    http: HttpClient,
    runner: Runner,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """The detached helper's work: wait for the chat's turn to end, restart it, then send it the note.

    A restart that does not happen is told to the chat instead, in a note of its own.
    """
    _log(f"Waiting for chat {chat_id} to end its turn")
    work_dir = repo_root / SELF_RESTART_DIR_REL
    if not wait_for_idle_chat(
        lambda: read_listed_chat(repo_root, chat_id, http),
        deadline_seconds=SELF_RESTART_DEADLINE_SECONDS,
        poll_seconds=SELF_RESTART_POLL_SECONDS,
        monotonic=monotonic,
        sleep=sleep,
    ):
        problem = f"its turn did not end within {SELF_RESTART_DEADLINE_SECONDS / 60:g} minutes"
    else:
        restart_problem = _run_message_chat(
            interrupt_argv(repo_root, chat_id), repo_root, runner
        )
        if restart_problem is None:
            note_problem = _run_message_chat(
                send_note_argv(repo_root, chat_id, work_dir / SELF_RESTART_NOTE_NAME),
                repo_root,
                runner,
            )
            if note_problem is not None:
                _log(
                    f"Restarted chat {chat_id}, but sending it the note failed: {note_problem}"
                )
                return 1
            _log(f"Restarted chat {chat_id} and sent it the note")
            return 0
        problem = f"the restart failed ({restart_problem})"
    _log(f"Chat {chat_id} was not restarted: {problem}")
    failure_note_path = work_dir / SELF_RESTART_FAILURE_NOTE_NAME
    work_dir.mkdir(parents=True, exist_ok=True)
    failure_note_path.write_text(compose_self_restart_failure_note(problem))
    send_problem = _run_message_chat(
        send_note_argv(repo_root, chat_id, failure_note_path), repo_root, runner
    )
    if send_problem is not None:
        _log(
            f"Telling chat {chat_id} its restart did not happen failed too: {send_problem}"
        )
    return 1


def _log(message: str) -> None:
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    print(f"{stamp} {message}", flush=True)
