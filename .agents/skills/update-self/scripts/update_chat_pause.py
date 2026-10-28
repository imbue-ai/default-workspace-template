"""Pausing the workspace's other chats and background workers for an update's apply, and
bringing them back afterwards (``run-status stop-chats`` / ``resume-chats``).

What is paused: every chat (``user_created=true``) and background worker
(``agent_created=true``) that is mid-turn (mngr's ``RUNNING``), or that has ended its turn
(``WAITING``) while a command it started through ``run_in_background.py`` still runs -- that
command's report would start the agent's next turn in the middle of the apply. Left alone: the
lead running the update, every member of its chat, every worker it launched (the update's own
``update-self`` worker), and the ``is_primary=true`` services agent. A lead may also name agents
to pause for memory earlier in the run; those are paused whatever their state.

Pausing a busy-waiting agent stops its background runners first, so their reports cannot revive
it mid-apply; the commands they ran go into the record and the resume note, so the agent knows
to run them again. A pause is ``mngr stop``; a resume is ``mngr start`` followed by the note
(``update_resume_note.py``) through the workspace's chat messenger. A worker does not pick its
work back up when it is started, so it is named in its lead chat's note instead of being told
anything itself.

Agents are listed with a ``--format`` template scoped to the local provider: inside a workspace
the template path answers from local state where ``--format json`` can fail, and every agent
here is the local provider's. Background runners are read off ``/proc``, where each one's
command line names its chat and the command it runs; a host without it (macOS) has none to find.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from run_in_background import (
    EXIT_CHAT_GONE,
    EXIT_DELIVERED,
    EXIT_DELIVERED_BUT_BLOCKED,
    messenger_argv,
)
from update_apply_contract import StoppedAgent
from update_resume_note import compose_resume_note

# The update's own worker, which the lead stops and keeps itself.
UPDATE_WORKER_NAME = "update-self"

STATE_RUNNING = "RUNNING"

STATE_WAITING = "WAITING"

PRIMARY_LABEL = "is_primary"

CHAT_LABEL = "user_created"

WORKER_LABEL = "agent_created"

LEAD_AGENT_LABEL = "lead_agent"

CHAT_ID_LABEL = "chat_id"

MNGR_LIST_FIELDS = (
    "id",
    "name",
    "state",
    f"labels.{PRIMARY_LABEL}",
    f"labels.{CHAT_LABEL}",
    f"labels.{WORKER_LABEL}",
    f"labels.{LEAD_AGENT_LABEL}",
    f"labels.{CHAT_ID_LABEL}",
)

MNGR_LIST_FIELD_SEPARATOR = "|"

MNGR_LIST_ARGV = (
    "mngr",
    "list",
    "--provider",
    "local",
    "--on-error",
    "continue",
    "--format",
    MNGR_LIST_FIELD_SEPARATOR.join(f"{{{field}}}" for field in MNGR_LIST_FIELDS),
)

MNGR_LIST_TIMEOUT_SECONDS = 60.0

MNGR_STOP_START_TIMEOUT_SECONDS = 120.0

RUNNER_SCRIPT_NAME = "run_in_background.py"

AGENT_ID_ENV = "MNGR_AGENT_ID"

AGENT_NAME_ENV = "MNGR_AGENT_NAME"

CHAT_ID_ENV = "MINDS_CHAT_ID"

CommandRunner = Callable[[Sequence[str], float], subprocess.CompletedProcess[str]]


class AgentListingError(Exception):
    """``mngr list`` could not say which agents exist, so nothing was paused."""


class UnknownAgentError(Exception):
    """A name the lead asked to pause is not a chat or worker this workspace runs."""


@dataclass(frozen=True)
class ListedAgent:
    """One agent row of the listing, with the labels the pause rules read."""

    id: str
    name: str
    state: str
    is_primary: bool
    is_chat: bool
    is_worker: bool
    lead_agent_id: str
    chat_label: str

    @property
    def chat_id(self) -> str:
        """The chat the agent belongs to: its ``chat_id`` label, else its own id (a chat's
        first agent is its own chat, and so is a worker)."""
        return self.chat_label or self.id

    @property
    def is_live(self) -> bool:
        return self.state in (STATE_RUNNING, STATE_WAITING)


@dataclass(frozen=True)
class BackgroundRunner:
    """A live ``run_in_background.py`` runner: whose chat it reports to, and what it runs."""

    pid: int
    chat_id: str
    description: str


@dataclass(frozen=True)
class LeadIdentity:
    """The lead running the update, read from its environment."""

    agent_id: str
    agent_name: str
    chat_id: str

    @classmethod
    def from_environ(cls, environ: Mapping[str, str]) -> "LeadIdentity":
        agent_id = environ.get(AGENT_ID_ENV, "")
        return cls(
            agent_id=agent_id,
            agent_name=environ.get(AGENT_NAME_ENV, ""),
            chat_id=environ.get(CHAT_ID_ENV, "") or agent_id,
        )


@dataclass(frozen=True)
class PauseTarget:
    """One agent to pause, with the background runners to stop first."""

    agent: ListedAgent
    runners: tuple[BackgroundRunner, ...]
    lead_chat_id: str


def run_command(
    argv: Sequence[str], timeout: float
) -> subprocess.CompletedProcess[str]:
    """Run ``argv`` and capture its output; a missing program or a timeout reads as a failure."""
    try:
        return subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(list(argv), 1, "", str(exc))


def parse_agent_listing(stdout: str) -> list[ListedAgent]:
    """The agents in a ``MNGR_LIST_ARGV`` listing; a line that is not a full row is skipped."""
    agents: list[ListedAgent] = []
    for line in stdout.splitlines():
        columns = line.split(MNGR_LIST_FIELD_SEPARATOR, len(MNGR_LIST_FIELDS) - 1)
        if len(columns) != len(MNGR_LIST_FIELDS):
            continue
        values = dict(zip(MNGR_LIST_FIELDS, (column.strip() for column in columns)))
        if not values["id"] or not values["name"]:
            continue
        agents.append(
            ListedAgent(
                id=values["id"],
                name=values["name"],
                state=values["state"],
                is_primary=values[f"labels.{PRIMARY_LABEL}"] == "true",
                is_chat=values[f"labels.{CHAT_LABEL}"] == "true",
                is_worker=values[f"labels.{WORKER_LABEL}"] == "true",
                lead_agent_id=values[f"labels.{LEAD_AGENT_LABEL}"],
                chat_label=values[f"labels.{CHAT_ID_LABEL}"],
            )
        )
    return agents


def list_agents(run: CommandRunner) -> list[ListedAgent]:
    """The agents ``mngr list`` rendered. With ``--on-error continue`` it lists every agent it
    could and exits non-zero for the rest, so a non-zero exit is an error only when it listed
    none, as is output none of whose lines is an agent."""
    completed = run(MNGR_LIST_ARGV, MNGR_LIST_TIMEOUT_SECONDS)
    agents = parse_agent_listing(completed.stdout)
    failure = (
        f"`mngr list` exited {completed.returncode}: {(completed.stderr or '').strip()}"
    )
    if not agents and (completed.returncode != 0 or completed.stdout.strip()):
        raise AgentListingError(
            failure if completed.returncode != 0 else "`mngr list` printed no agent"
        )
    if completed.returncode != 0:
        print(f"warning: {failure}; pausing from what it listed", file=sys.stderr)
    return agents


def parse_runner_cmdline(argv: Sequence[str]) -> tuple[str, str] | None:
    """The ``(chat id, description)`` of a detached runner's argv, or ``None`` for any other process.

    Only the detached copy (``--foreground``) runs the command; the caller's copy has already
    exited. Its options all come before the ``--`` that starts the command.
    """
    script_index = next(
        (index for index, word in enumerate(argv) if word.endswith(RUNNER_SCRIPT_NAME)),
        None,
    )
    if script_index is None:
        return None
    options = list(argv[script_index + 1 :])
    if "--" in options:
        options = options[: options.index("--")]
    if "--foreground" not in options:
        return None
    values: dict[str, str] = {}
    for index, word in enumerate(options[:-1]):
        if word in ("--chat-id", "--description"):
            values[word] = options[index + 1]
    chat_id = values.get("--chat-id", "")
    if not chat_id:
        return None
    return chat_id, values.get("--description", "")


def read_background_runners(proc_dir: Path) -> list[BackgroundRunner]:
    """Every live background runner on this host, read off ``proc_dir`` (``/proc``)."""
    runners: list[BackgroundRunner] = []
    try:
        entries = list(proc_dir.iterdir())
    except OSError:
        return runners
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        argv = [part.decode(errors="replace") for part in raw.split(b"\0") if part]
        parsed = parse_runner_cmdline(argv)
        if parsed is not None:
            runners.append(
                BackgroundRunner(
                    pid=int(entry.name), chat_id=parsed[0], description=parsed[1]
                )
            )
    return runners


def _is_the_leads_own(
    agent: ListedAgent, lead: LeadIdentity, by_id: Mapping[str, ListedAgent]
) -> bool:
    """Whether ``agent`` is the lead, a member of its chat, or anywhere in the dispatch tree under it."""
    if agent.id == lead.agent_id or (lead.agent_name and agent.name == lead.agent_name):
        return True
    if lead.chat_id and agent.chat_id == lead.chat_id:
        return True
    if agent.name == UPDATE_WORKER_NAME:
        return True
    seen: set[str] = set()
    current = agent
    while (
        current.is_worker
        and current.lead_agent_id
        and current.lead_agent_id not in seen
    ):
        if current.lead_agent_id == lead.agent_id:
            return True
        seen.add(current.lead_agent_id)
        parent = by_id.get(current.lead_agent_id)
        if parent is None:
            return False
        current = parent
    return False


def _lead_chat_id(worker: ListedAgent, agents: Sequence[ListedAgent]) -> str:
    """The chat a worker's lead runs in: the lead's own chat, else the lead's id."""
    lead = next((agent for agent in agents if agent.id == worker.lead_agent_id), None)
    return lead.chat_id if lead is not None else worker.lead_agent_id


def select_pause_targets(
    agents: Sequence[ListedAgent],
    runners: Sequence[BackgroundRunner],
    lead: LeadIdentity,
    requested_names: Sequence[str],
) -> list[PauseTarget]:
    """What to pause: the busy chats and workers, or exactly the ones the lead named.

    A named agent that is not a live chat or worker of this workspace (or is the lead's own) is
    refused rather than skipped, so a typo cannot read as a pause that happened.
    """
    by_id = {agent.id: agent for agent in agents}
    candidates = [
        agent
        for agent in agents
        if (agent.is_chat or agent.is_worker)
        and not agent.is_primary
        and not _is_the_leads_own(agent, lead, by_id)
    ]
    runners_by_chat: dict[str, list[BackgroundRunner]] = {}
    for runner in runners:
        runners_by_chat.setdefault(runner.chat_id, []).append(runner)
    if requested_names:
        by_name = {agent.name: agent for agent in candidates}
        unknown = sorted(name for name in set(requested_names) if name not in by_name)
        if unknown:
            raise UnknownAgentError(
                f"not a chat or background agent this run may pause: {', '.join(unknown)}"
            )
        chosen = [
            by_name[name]
            for name in dict.fromkeys(requested_names)
            if by_name[name].is_live
        ]
    else:
        chosen = [
            agent
            for agent in candidates
            if agent.state == STATE_RUNNING
            or (agent.state == STATE_WAITING and agent.chat_id in runners_by_chat)
        ]
    return [
        PauseTarget(
            agent=agent,
            runners=tuple(runners_by_chat.get(agent.chat_id, ())),
            lead_chat_id=_lead_chat_id(agent, agents) if agent.is_worker else "",
        )
        for agent in chosen
    ]


def pause(
    targets: Sequence[PauseTarget],
    run: CommandRunner,
    kill: Callable[[int], None],
    record: Callable[[StoppedAgent], None],
) -> list[str]:
    """Pause each target -- its runners first, then the agent -- recording each as it goes.

    Returns the problems, one line each; an agent ``mngr stop`` could not stop is left out of
    the record, since there is nothing for the resume to bring back.
    """
    problems: list[str] = []
    for target in targets:
        for runner in target.runners:
            kill(runner.pid)
        completed = run(
            ["mngr", "stop", target.agent.id], MNGR_STOP_START_TIMEOUT_SECONDS
        )
        tasks = [
            runner.description or "a background command" for runner in target.runners
        ]
        if completed.returncode != 0:
            interrupted = (
                f" (its background commands run on, but their reports were stopped, so it will not"
                f" hear how they end: {'; '.join(tasks)})"
                if tasks
                else ""
            )
            problems.append(
                f"could not pause {target.agent.name}: `mngr stop` exited {completed.returncode}: "
                f"{(completed.stderr or '').strip()}{interrupted}"
            )
            continue
        record(
            StoppedAgent(
                id=target.agent.id,
                name=target.agent.name,
                chat_id=target.agent.chat_id,
                is_worker=target.agent.is_worker,
                lead_chat_id=target.lead_chat_id,
                interrupted_tasks=tasks,
            )
        )
        kind = "background agent" if target.agent.is_worker else "chat"
        print(f"Paused {kind} {target.agent.name} ({target.agent.id}).")
    return problems


def stop_runner(pid: int) -> None:
    """Stop one background runner; one that has already exited needs nothing."""
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass


def merge_stopped(existing: list[StoppedAgent], stopped: StoppedAgent) -> None:
    """Add ``stopped`` to the record, or reopen its entry when an earlier round already paused it.

    The earlier round's interrupted commands ride along only until a resume has told the chat
    about them.
    """
    for index, entry in enumerate(existing):
        if entry.id == stopped.id:
            if not entry.is_told:
                stopped.interrupted_tasks = list(
                    dict.fromkeys(
                        [*entry.interrupted_tasks, *stopped.interrupted_tasks]
                    )
                )
            existing[index] = stopped
            return
    existing.append(stopped)


def _is_agent_listed(run: CommandRunner, agent_id: str) -> bool:
    """Whether ``mngr list`` still lists ``agent_id``; a listing that fails says nothing, so yes."""
    try:
        agents = list_agents(run)
    except AgentListingError as exc:
        print(
            f"warning: could not tell whether {agent_id} still exists: {exc}",
            file=sys.stderr,
        )
        return True
    return any(agent.id == agent_id for agent in agents)


def send_note(
    repo_root: Path, run: CommandRunner, chat_id: str, note: str
) -> subprocess.CompletedProcess[str]:
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", suffix=".md", delete=False
    ) as handle:
        handle.write(note)
        message_path = Path(handle.name)
    try:
        return run(
            messenger_argv(repo_root, chat_id, message_path),
            MNGR_STOP_START_TIMEOUT_SECONDS,
        )
    finally:
        message_path.unlink(missing_ok=True)


@dataclass
class _NoteRecipient:
    is_chat_paused: bool
    tasks: list[str]
    workers: list[str]
    entries: list[StoppedAgent]


def resume(
    stopped: Sequence[StoppedAgent],
    run: CommandRunner,
    deliver: Callable[[str, str], subprocess.CompletedProcess[str]],
    landed_ref: str | None,
    wip_commit: str,
    save: Callable[[], None],
) -> list[str]:
    """Start every paused agent again, then send each chat concerned one note, saving progress
    after each step so a re-run picks up only what is left. Returns the problems, one line each."""
    problems: list[str] = []
    for entry in stopped:
        if entry.is_restarted:
            continue
        completed = run(["mngr", "start", entry.id], MNGR_STOP_START_TIMEOUT_SECONDS)
        if completed.returncode != 0 and not _is_agent_listed(run, entry.id):
            # Deleted while it was paused: no later try could start or tell it.
            print(
                f"{entry.name} no longer exists, so nothing is left to start or tell."
            )
            entry.is_restarted = True
            entry.is_told = True
            save()
            continue
        if completed.returncode != 0:
            problems.append(
                f"could not start {entry.name} again: `mngr start` exited {completed.returncode}: "
                f"{(completed.stderr or '').strip()}"
            )
            continue
        entry.is_restarted = True
        save()
        print(f"Started {entry.name} ({entry.id}) again.")

    recipients: dict[str, _NoteRecipient] = {}
    for entry in stopped:
        if not entry.is_restarted or entry.is_told:
            continue
        chat_id = entry.lead_chat_id if entry.is_worker else entry.chat_id
        if not chat_id:
            # A worker from before the lead_agent label names no chat, so no re-run could tell one.
            print(f"Started {entry.name} again; it names no lead chat to tell.")
            entry.is_told = True
            save()
            continue
        recipient = recipients.setdefault(chat_id, _NoteRecipient(False, [], [], []))
        recipient.entries.append(entry)
        if entry.is_worker:
            recipient.workers.append(entry.name)
            recipient.tasks.extend(
                f"{task} (run by {entry.name})" for task in entry.interrupted_tasks
            )
        else:
            recipient.is_chat_paused = True
            recipient.tasks.extend(entry.interrupted_tasks)

    for chat_id, recipient in recipients.items():
        note = compose_resume_note(
            landed_ref=landed_ref,
            is_chat_paused=recipient.is_chat_paused,
            wip_commit=wip_commit,
            interrupted_tasks=recipient.tasks,
            restarted_workers=recipient.workers,
        )
        completed = deliver(chat_id, note)
        if completed.returncode == EXIT_CHAT_GONE:
            # No later try can deliver to a chat that no longer exists.
            print(f"Chat {chat_id} no longer exists, so nothing is left to tell it.")
            for entry in recipient.entries:
                entry.is_told = True
            save()
            continue
        if completed.returncode not in (EXIT_DELIVERED, EXIT_DELIVERED_BUT_BLOCKED):
            problems.append(
                f"could not send chat {chat_id} the resume note: exit {completed.returncode}: "
                f"{(completed.stderr or '').strip()}"
            )
            continue
        for entry in recipient.entries:
            entry.is_told = True
        save()
        print(f"Told chat {chat_id} it can carry on.")
    return problems
