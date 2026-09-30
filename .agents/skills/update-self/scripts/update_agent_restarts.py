"""Putting the workspace's running agents onto what an update installed.

An agent's harness process loads its binary, its extensions and some of its config once, when
it starts, so an update that changes one of them reaches a running agent only when that agent
is restarted. A restart keeps the conversation: each harness resumes its own session.

``restart_idle_agents`` asks the chat app to restart every chat it lists, other than the
pass's own chat and its worker, if that chat has ended its turn, and reports the ones the chat
app left running because they were busy. ``start_self_restart`` restarts the pass's own chat
once its last turn ends, from a detached helper, and sends it a note to confirm the restart to
the user (or, when the restart does not happen, a note saying so).

Every restart is the chat app's interrupt route with ``only_if_idle``: the chat app knows which
agent a chat runs on and what it is doing, decides at the moment of the restart, and refuses
one during a handoff. The route is posted to directly, never through ``mngr``, which would
check none of that. These run only after a successful apply, so the chat app is the release's.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from run_in_background import wrap_background_task_report
from update_probes import registry_app_url
from update_runtime import HttpClient, Runner

CHAT_APP_NAME = "chat"
CHATS_PATH = "/api/chats"
MESSAGE_CHAT_REL = Path("system") / "scripts" / "message_chat.py"
# The staged copy of the flow the pass ran from, which it leaves in place.
STAGED_UPDATE_SELF_SCRIPT_REL = Path(
    "data/.tasks/update-self/skill-at-target/.agents/skills/update-self/scripts/update_self.py"
)

# The flow's worker, which the pass stops itself once it has consumed the worker's report.
UPDATE_WORKER_NAME = "update-self"

SELF_RESTART_DIR_REL = Path("data") / ".tasks" / "update-self" / "self-restart"
SELF_RESTART_NOTE_NAME = "note.md"
SELF_RESTART_FAILURE_NOTE_NAME = "failure-note.md"
SELF_RESTART_LOG_NAME = "helper.log"

# The variable mngr tags an agent's processes with and kills them by on a stop.
AGENT_ID_ENV = "MNGR_AGENT_ID"

STOPPED_CHAT_STATUS = "stopped"
# What the chat app's 409 for a chat mid-handoff carries instead of ``busy_with``.
HANDOFF_PHASE_FIELD = "phase"

# How long the chat list is retried while the chat app is unreachable or not ready: the apply
# has just restarted it.
CHAT_LIST_RETRY_WINDOW_SECONDS = 60.0
CHAT_LIST_RETRY_INTERVAL_SECONDS = 2.0
CHAT_LIST_REQUEST_TIMEOUT_SECONDS = 10.0

# The interrupt route answers once its ``mngr start --restart`` has finished.
INTERRUPT_REQUEST_TIMEOUT_SECONDS = 120.0
MESSAGE_CHAT_TIMEOUT_SECONDS = 180.0

# How long the self-restart helper waits for the pass's last turn to end.
SELF_RESTART_DEADLINE_SECONDS = 30 * 60.0
SELF_RESTART_POLL_SECONDS = 5.0


class ChatListUnavailableError(Exception):
    """The chat app could not give its chat list within the retry window."""


@dataclass(frozen=True)
class ListedChat:
    """One chat from the chat app's list, as much of it as choosing what to restart reads."""

    chat_id: str
    name: str
    title: str
    # The chat app's status for the chat (its ``ChatStatus`` wire value).
    status: str


class InterruptVerdict(Enum):
    RESTARTED = "restarted"
    BUSY = "busy"
    GONE = "gone"
    FAILED = "failed"


@dataclass(frozen=True)
class InterruptOutcome:
    verdict: InterruptVerdict
    # What a busy chat is doing, or why the restart failed; empty otherwise.
    detail: str


def parse_chat_list(answer_text: str) -> list[ListedChat]:
    """The chats in a ``GET /api/chats`` answer; raises ``ValueError`` for one of another shape."""
    body = json.loads(answer_text)
    raw_chats = body.get("chats") if isinstance(body, dict) else None
    if not isinstance(raw_chats, list):
        raise ValueError("the chat list answer carries no `chats` list")
    chats: list[ListedChat] = []
    for raw in raw_chats:
        chat_id = raw.get("chat_id") if isinstance(raw, dict) else None
        if not isinstance(chat_id, str) or not chat_id:
            raise ValueError(f"a chat in the list has no id: {raw!r}")
        chats.append(
            ListedChat(
                chat_id=chat_id,
                name=str(raw.get("name") or ""),
                title=str(raw.get("title") or raw.get("name") or chat_id),
                status=str(raw.get("status") or ""),
            )
        )
    return chats


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
                    return parse_chat_list(page.body)
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


def interrupt_if_idle(
    repo_root: Path, chat_id: str, http: HttpClient
) -> InterruptOutcome:
    """Ask the chat app to restart ``chat_id`` only if it has ended its turn, and report what it did."""
    base_url = registry_app_url(repo_root, CHAT_APP_NAME)
    if base_url is None:
        return InterruptOutcome(
            InterruptVerdict.FAILED, "the chat app has no row in the app registry"
        )
    page = http.post_json(
        f"{base_url.rstrip('/')}{CHATS_PATH}/{chat_id}/interrupt",
        {"only_if_idle": True},
        INTERRUPT_REQUEST_TIMEOUT_SECONDS,
    )
    if page is None:
        return InterruptOutcome(
            InterruptVerdict.FAILED, f"the chat app at {base_url} did not answer"
        )
    if 200 <= page.status < 300:
        return InterruptOutcome(InterruptVerdict.RESTARTED, "")
    if page.status == 404:
        return InterruptOutcome(InterruptVerdict.GONE, "")
    try:
        body = json.loads(page.body)
    except ValueError:
        body = None
    body = body if isinstance(body, dict) else {}
    if page.status == 409:
        if HANDOFF_PHASE_FIELD in body:
            return InterruptOutcome(InterruptVerdict.BUSY, "switching to another agent")
        return InterruptOutcome(
            InterruptVerdict.BUSY, str(body.get("busy_with") or "working")
        )
    return InterruptOutcome(
        InterruptVerdict.FAILED,
        str(body.get("detail") or f"the chat app answered HTTP {page.status}"),
    )


def restart_idle_agents(
    repo_root: Path,
    own_chat_id: str,
    http: HttpClient,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, list[dict[str, str]]]:
    """Restart every idle chat but the pass's own and its worker's; the report of what happened.

    A stopped chat needs nothing, since its next start loads what the update installed; one
    deleted since the list was read is skipped. Raises ``ChatListUnavailableError`` when there
    is no chat list to act on.
    """
    report: dict[str, list[dict[str, str]]] = {
        "restarted": [],
        "left_running": [],
        "failed": [],
    }
    for chat in fetch_chat_list(repo_root, http, monotonic, sleep):
        if chat.chat_id == own_chat_id or chat.name == UPDATE_WORKER_NAME:
            continue
        if chat.status == STOPPED_CHAT_STATUS:
            continue
        outcome = interrupt_if_idle(repo_root, chat.chat_id, http)
        entry = {"chat_id": chat.chat_id, "title": chat.title}
        match outcome.verdict:
            case InterruptVerdict.RESTARTED:
                report["restarted"].append(entry)
            case InterruptVerdict.BUSY:
                report["left_running"].append({**entry, "busy_with": outcome.detail})
            case InterruptVerdict.FAILED:
                report["failed"].append({**entry, "detail": outcome.detail})
            case InterruptVerdict.GONE:
                pass
    return report


def read_restart_report(path: Path) -> Mapping[str, list[dict[str, str]]] | None:
    """What ``restart-agents`` printed into ``path``; None when it printed no report (it failed)."""
    try:
        report = json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        return None
    return report if isinstance(report, dict) else None


def compose_self_restart_note(
    reason: str, restart_report: Mapping[str, Sequence[Mapping[str, str]]] | None
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
    restart_command = "`python3 system/scripts/message_chat.py <chat-id> --interrupt`"
    if restart_report is None:
        lines.append(
            "Restarting the workspace's other chats did not run, so they may all still be on the "
            "previous version. Tell the user, and offer to restart the idle ones now (`python3 "
            f"{STAGED_UPDATE_SELF_SCRIPT_REL} restart-agents`); do not run it without a yes."
        )
    else:
        not_restarted = [
            *restart_report.get("left_running", []),
            *restart_report.get("failed", []),
        ]
        if not_restarted:
            names = ", ".join(
                f'"{chat["title"]}" ({chat["chat_id"]})' for chat in not_restarted
            )
            lines.append(
                f"These were not restarted and are still running the previous version: {names}. "
                f"Ask the user whether to interrupt and restart them now ({restart_command}); "
                "do not restart them without a yes."
            )
    return wrap_background_task_report(
        "Restarted to finish the update", "\n\n".join(lines)
    )


def compose_self_restart_failure_note(problem: str) -> str:
    """The note the pass's own chat gets when its restart did not happen, so the user is not
    left believing the results message's promise that it would."""
    body = "\n\n".join(
        [
            "This chat was to restart after the update of this workspace, but it was not "
            f"restarted: {problem}.",
            "Tell the user in one or two plain sentences that the update is complete but this chat "
            "is still running the previous version, and that choosing Stop agent in this chat's "
            "model menu, then sending it any message, brings it back on the new one (its "
            "conversation carries over).",
        ]
    )
    return wrap_background_task_report(
        "The restart after the update did not happen", body
    )


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


def send_note_argv(repo_root: Path, chat_id: str, note_path: Path) -> list[str]:
    """The ``message_chat.py`` call that sends a chat the note at ``note_path``."""
    return [
        sys.executable,
        str(repo_root / MESSAGE_CHAT_REL),
        chat_id,
        "--message-file",
        str(note_path),
    ]


def _send_note(
    repo_root: Path, chat_id: str, note_path: Path, runner: Runner
) -> str | None:
    """Send one note through ``message_chat.py``; None when it was sent, else what went wrong."""
    try:
        result = runner.run(
            send_note_argv(repo_root, chat_id, note_path),
            cwd=str(repo_root),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
            timeout=MESSAGE_CHAT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return f"it did not finish within {MESSAGE_CHAT_TIMEOUT_SECONDS:g}s"
    except OSError as exc:
        return f"it could not be run: {exc}"
    if result.returncode != 0:
        return (result.stderr or "").strip() or f"exit code {result.returncode}"
    return None


def restart_when_idle(
    repo_root: Path,
    chat_id: str,
    http: HttpClient,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> str | None:
    """Keep asking the chat app to restart ``chat_id`` if idle until it does; None once it has,
    else why it never did.

    A busy chat and a chat app that cannot answer right now are both asked again; only a chat
    that no longer exists, or the deadline, ends the wait.
    """
    started_at = monotonic()
    while True:
        outcome = interrupt_if_idle(repo_root, chat_id, http)
        match outcome.verdict:
            case InterruptVerdict.RESTARTED:
                return None
            case InterruptVerdict.GONE:
                return "the chat no longer exists"
            case InterruptVerdict.BUSY | InterruptVerdict.FAILED:
                pass
        if monotonic() - started_at >= SELF_RESTART_DEADLINE_SECONDS:
            return (
                f"it was still {outcome.detail} after {SELF_RESTART_DEADLINE_SECONDS / 60:g} minutes"
                if outcome.verdict is InterruptVerdict.BUSY
                else f"the restart kept failing ({outcome.detail})"
            )
        sleep(SELF_RESTART_POLL_SECONDS)


def restart_self_when_idle(
    repo_root: Path,
    chat_id: str,
    http: HttpClient,
    runner: Runner,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """The detached helper's work: restart the chat once its turn has ended, then send it the note.

    A restart that does not happen is told to the chat instead, in a note of its own.
    """
    _log(f"Waiting for chat {chat_id} to end its turn")
    work_dir = repo_root / SELF_RESTART_DIR_REL
    problem = restart_when_idle(repo_root, chat_id, http, monotonic, sleep)
    if problem is None:
        note_problem = _send_note(
            repo_root, chat_id, work_dir / SELF_RESTART_NOTE_NAME, runner
        )
        if note_problem is not None:
            _log(
                f"Restarted chat {chat_id}, but sending it the note failed: {note_problem}"
            )
            return 1
        _log(f"Restarted chat {chat_id} and sent it the note")
        return 0
    _log(f"Chat {chat_id} was not restarted: {problem}")
    failure_note_path = work_dir / SELF_RESTART_FAILURE_NOTE_NAME
    work_dir.mkdir(parents=True, exist_ok=True)
    failure_note_path.write_text(compose_self_restart_failure_note(problem))
    send_problem = _send_note(repo_root, chat_id, failure_note_path, runner)
    if send_problem is not None:
        _log(
            f"Telling chat {chat_id} its restart did not happen failed too: {send_problem}"
        )
    return 1


def _log(message: str) -> None:
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    print(f"{stamp} {message}", flush=True)
