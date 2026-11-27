"""A workspace staged on disk as far as this library reads it, with stand-in tools, for the library's tests."""

import json
import os
import stat
import subprocess
import threading
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Final, NamedTuple
from uuid import uuid4

import pytest

from workspace_update_control import control
from workspace_update_control.agents import agent_events_path
from workspace_update_control.channel import (
    ChannelFileFetcher,
    ChannelFileUnreadableError,
)
from workspace_update_control.dispatcher import (
    AGENT_PIDS_DIR,
    AgentEnvironment,
    list_agents,
    run_tick,
)
from workspace_update_control.protocol import (
    BACKUP_CREDENTIALS_FILE_PATH,
    UPDATE_STATE_DIR,
)
from workspace_update_control.settings import OWN_SETTING_NAMES
from workspace_update_control.stub_programs_testing import (
    STUB_MNGR_SOURCE,
    STUB_TOOL_SOURCE,
)

SERVICES_AGENT_ID: Final[str] = f"agent-{uuid4().hex}"
_SERVICES_AGENT: Final[dict[str, str]] = {
    "id": SERVICES_AGENT_ID,
    "name": "system-services",
    "state": "RUNNING",
    "is_primary": "true",
}

# What the staged chat messenger answers a create with, until a test scripts otherwise.
STAGED_CREATED_CHAT: Final[dict[str, str]] = {
    "chat_id": f"agent-{uuid4().hex}",
    "name": "Update-to-v090",
    "display_name": "Update to v0.9.0",
}


# Where the staged update settings say the release channels' files are published.
STAGED_FEED_BASE_URL: Final[str] = "https://feed.example/updates"


class RecordingFeed:
    """A stand-in for the release feed: names ``template_ref`` for every channel (or cannot be read, when it is
    None), and remembers each address it was asked for."""

    def __init__(self, template_ref: str | None) -> None:
        self.template_ref = template_ref
        self.asked: list[str] = []

    def __call__(self, url: str) -> str:
        self.asked.append(url)
        if self.template_ref is None:
            raise ChannelFileUnreadableError("the feed is down")
        return json.dumps({"templateRef": self.template_ref})


# The channels the local feed publishes no readable file for: one whose request it takes and never answers,
# one whose file it sends a byte at a time for as long as the feed is up, and one whose file is larger than any
# channel's file.
SILENT_FEED_CHANNEL: Final[str] = "silent"
DRIPPING_FEED_CHANNEL: Final[str] = "dripping"
OVERSIZED_FEED_CHANNEL: Final[str] = "oversized"
_DRIP_INTERVAL_SECONDS: Final[float] = 0.02
_OVERSIZED_FILE_BYTES: Final[int] = 128 * 1024


class LocalFeedServer(ThreadingHTTPServer):
    """The local feed's server, which tells its handlers when the feed is taken down, so that one still holding
    a request ends with it."""

    def __init__(self, address: tuple[str, int]) -> None:
        super().__init__(address, LocalFeedHandler)
        self.is_taken_down = threading.Event()


class LocalFeedHandler(BaseHTTPRequestHandler):
    """A feed that publishes the stable channel's file, naming ``minds-v0.10.5``, misbehaves for the channels
    named above, and publishes no other."""

    def do_GET(self) -> None:
        if self.path == "/stable-web.json":
            self._send_file(b'{"templateRef": "minds-v0.10.5"}')
            return
        if self.path == f"/{SILENT_FEED_CHANNEL}-web.json":
            self._is_taken_down().wait()
            return
        if self.path == f"/{DRIPPING_FEED_CHANNEL}-web.json":
            self._drip_file()
            return
        if self.path == f"/{OVERSIZED_FEED_CHANNEL}-web.json":
            self._send_file(b" " * _OVERSIZED_FILE_BYTES)
            return
        self.send_error(404)

    def _send_file(self, body: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _drip_file(self) -> None:
        self.send_response(200)
        self.send_header("Content-Length", str(_OVERSIZED_FILE_BYTES))
        self.end_headers()
        is_taken_down = self._is_taken_down()
        try:
            while not is_taken_down.wait(_DRIP_INTERVAL_SECONDS):
                self.wfile.write(b" ")
                self.wfile.flush()
        except OSError:
            # The reader went away.
            pass

    def _is_taken_down(self) -> threading.Event:
        server = self.server
        assert isinstance(server, LocalFeedServer)
        return server.is_taken_down

    def log_message(self, format: str, *args: object) -> None:
        pass


def update_settings_document(**fields: Any) -> dict[str, Any]:
    """Update settings as a client that names every one sends them: the stable channel, arming on, a window of
    02:00 to 05:00, nothing of the workspace's own, with ``fields`` over them."""
    return {
        "channel": "stable",
        "feed_base_url": STAGED_FEED_BASE_URL,
        "is_auto_update_enabled": True,
        "is_auto_update_enabled_here": None,
        "window_start_hour": 2,
        "window_end_hour": 5,
        "window_start_hour_here": None,
        "window_end_hour_here": None,
        **fields,
    }


def the_accounts_settings(**fields: Any) -> dict[str, Any]:
    """Update settings as a client sends them when it writes the account's: none of the workspace's own."""
    return {
        name: value
        for name, value in update_settings_document(**fields).items()
        if name not in OWN_SETTING_NAMES
    }


def epoch_at(iso: str) -> float:
    """The moment the ISO 8601 text ``iso`` names, in seconds since the epoch."""
    return datetime.fromisoformat(iso).timestamp()


class ScriptedToolAnswer(NamedTuple):
    """One answer a stubbed in-workspace tool gives, in order."""

    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""


def _write_executable(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _write_scripted_tool(path: Path, answers: Sequence[ScriptedToolAnswer]) -> None:
    _write_executable(path, STUB_TOOL_SOURCE)
    path.with_name(path.name + ".answers.json").write_text(
        json.dumps([answer._asdict() for answer in answers])
    )


class StagedUpdateWorkspace(NamedTuple):
    """A workspace root on disk, laid out as far as the update library reads it, with stubbed tools on PATH."""

    # The workspace root, which is the working directory.
    root: Path
    # Holds the stub mngr, tk and cron.
    bin_dir: Path
    # One JSON argv per line, for every stub mngr and tk call.
    calls_log: Path
    # What the stub ``mngr list`` answers.
    agents_file: Path
    # Stands in for /etc/cron.d.
    live_cron_dir: Path
    # Stands in for /proc.
    proc_root: Path
    # The template's agent-pid registry, at its place in the workspace.
    agent_pids_dir: Path
    # The events file ``mngr observe`` would write, under the staged host dir.
    events_path: Path
    # The id the stub ``mngr create`` gives the agent it makes.
    bare_created_agent_id: str

    def set_agents(
        self,
        agents: Sequence[Mapping[str, str]],
        *,
        is_services_agent_listed: bool = True,
    ) -> None:
        """What the stub ``mngr list`` answers: ``agents`` (``id``, ``name``, ``state``, optionally ``chat_id``),
        beside the workspace's services agent unless told not to."""
        listed = ([_SERVICES_AGENT] if is_services_agent_listed else []) + [
            dict(agent) for agent in agents
        ]
        self.agents_file.write_text(json.dumps(listed))

    def read_listed_agents(self) -> list[dict[str, str]] | None:
        """The workspace's agents as the dispatcher's own listing reads them, through the stub ``mngr list``."""
        return list_agents(AgentEnvironment())

    def tick(self, now: float) -> None:
        """One minute of the dispatcher against this workspace, at ``now``."""
        run_tick(now, str(self.proc_root), str(self.agent_pids_dir), AgentEnvironment())

    def script_tool(
        self, relative_path: str, answers: Sequence[ScriptedToolAnswer]
    ) -> None:
        """Put a stand-in for the template script at ``relative_path`` that answers ``answers`` in order."""
        path = self.root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_scripted_tool(path, answers)

    def script_program_on_path(
        self, name: str, answers: Sequence[ScriptedToolAnswer]
    ) -> None:
        """Put a stand-in for the program ``name`` on the stub PATH that answers ``answers`` in order."""
        _write_scripted_tool(self.bin_dir / name, answers)

    def recorded_calls(self) -> list[list[str]]:
        if not self.calls_log.exists():
            return []
        return [
            json.loads(line)
            for line in self.calls_log.read_text().splitlines()
            if line.strip()
        ]

    def tool_calls(self, script_name: str) -> list[list[str]]:
        """The recorded argv of every call to the stubbed template script named ``script_name``."""
        return [call[1:] for call in self.recorded_calls() if call[:1] == [script_name]]

    def chat_creates(self) -> list[list[str]]:
        """Every chat the stubbed messenger was asked to create; the message text is the last item of each."""
        return [
            call
            for call in self.tool_calls("message_chat.py")
            if call[:1] == ["--create"]
        ]

    def bare_creates(self) -> list[list[str]]:
        """Every ``mngr create`` the stub mngr was asked for, as argv from ``create`` on."""
        return [call for call in self.recorded_calls() if call[:1] == ["create"]]

    def state_path(self, name: str) -> Path:
        return self.root / UPDATE_STATE_DIR / name

    def write_state(self, name: str, content: Mapping[str, Any]) -> None:
        path = self.state_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(dict(content)))

    def read_state(self, name: str) -> dict[str, Any] | None:
        path = self.state_path(name)
        return json.loads(path.read_text()) if path.exists() else None

    def write_backup_credentials(self) -> None:
        """Leave the workspace holding backup credentials, which is how it tells that it has backups."""
        path = self.root / BACKUP_CREDENTIALS_FILE_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("RESTIC_REPOSITORY=x\n")

    def write_failed_run(self, target_ref: str, dispatched_at: float) -> float:
        """Leave the records of a run that went out for ``target_ref`` at ``dispatched_at`` and ended stuck;
        when that run began."""
        started_at = dispatched_at + 60
        self.write_state(
            "last-dispatch.json",
            {
                "chat_agent_name": "update-that-failed",
                "chat_id": "",
                "target_ref": target_ref,
                "dispatched_at": dispatched_at,
            },
        )
        self.write_state(
            "run.json",
            {
                "chat_agent_name": "update-that-failed",
                "started_at": started_at,
                "verdict": "STUCK",
            },
        )
        return started_at

    def dispatcher_log_lines(self) -> list[str]:
        log_path = self.state_path("dispatcher.log")
        return log_path.read_text().splitlines() if log_path.exists() else []

    def write_lease(self, lease_id: str, text_after_title: str = "") -> Path:
        """An open ``updating workspace`` lease as tk writes one; ``text_after_title`` is the rest of its file."""
        lease = self.root / "data" / ".tickets" / f"{lease_id}.md"
        lease.parent.mkdir(parents=True, exist_ok=True)
        lease.write_text(
            f"---\nid: {lease_id}\nstatus: in_progress\n---\n# updating workspace\n{text_after_title}"
        )
        return lease

    def register_agent_process(
        self,
        pid: int,
        agent_name: str,
        *,
        is_worker: bool,
        agent_id: str | None,
        oom_score_adj: int,
        environ_agent_name: str | None = None,
        tgid: int | None = None,
    ) -> None:
        """A live agent main process as the template's launch wrapper leaves it: its registry entry (with no
        ``agent_id`` key when ``agent_id`` is None) and its ``/proc`` files, whose agent name and thread group
        default to the entry's own."""
        self.agent_pids_dir.mkdir(parents=True, exist_ok=True)
        entry: dict[str, object] = {"agent_name": agent_name, "is_worker": is_worker}
        if agent_id is not None:
            entry["agent_id"] = agent_id
        (self.agent_pids_dir / f"{pid}.json").write_text(json.dumps(entry))
        process_dir = self.proc_root / str(pid)
        process_dir.mkdir(parents=True, exist_ok=True)
        (process_dir / "status").write_text(
            f"Name:\tclaude\nTgid:\t{pid if tgid is None else tgid}\nPid:\t{pid}\n"
        )
        environ = (
            f"HOME=/home/user\0MNGR_AGENT_NAME={environ_agent_name or agent_name}\0"
        )
        (process_dir / "environ").write_bytes(environ.encode())
        (process_dir / "oom_score_adj").write_text(f"{oom_score_adj}\n")

    def registry_entry(self, pid: int) -> dict[str, Any]:
        return json.loads((self.agent_pids_dir / f"{pid}.json").read_text())

    def oom_score_adj(self, pid: int) -> int:
        return int((self.proc_root / str(pid) / "oom_score_adj").read_text())


def stage_update_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    has_skill: bool = True,
    is_signed_in: bool = True,
    has_cron: bool = True,
    has_message_chat: bool = True,
) -> StagedUpdateWorkspace:
    """Lay out a workspace under ``tmp_path``, cd into it, and put stub tools first on PATH.

    The environment reads as the shell's (``MNGR_HOST_DIR`` set), so the library uses it as is. The template's
    chat messenger (unless ``has_message_chat`` is False) creates every chat it is asked for, and its
    update-self recorder records every verdict, until a test scripts otherwise.
    """
    root = tmp_path / "workspace"
    (root / ".mngr").mkdir(parents=True)
    (root / "system" / "scripts").mkdir(parents=True)
    (root / "system" / "scripts" / "require_create_account.py").write_text("")
    if has_skill:
        (root / ".agents" / "skills" / "update-self").mkdir(parents=True)
        (root / ".agents" / "skills" / "update-self" / "SKILL.md").write_text(
            "# update-self\n"
        )
    if is_signed_in:
        (root / ".mngr" / "settings.local.toml").write_text(
            '[commands.create]\ntype = "claude"\n'
        )
    bin_dir = tmp_path / "stub-bin"
    bin_dir.mkdir()
    _write_executable(bin_dir / "mngr", STUB_MNGR_SOURCE)
    _write_executable(bin_dir / "tk", STUB_MNGR_SOURCE)
    if has_cron:
        _write_executable(bin_dir / "cron", "#!/bin/sh\nexit 0\n")
    live_cron_dir = tmp_path / "etc-cron.d"
    # A workspace with no cron has no drop-in directory for one either.
    if has_cron:
        live_cron_dir.mkdir()
    host_dir = tmp_path / "host-dir"
    staged = StagedUpdateWorkspace(
        root=root,
        bin_dir=bin_dir,
        calls_log=tmp_path / "stub-calls.jsonl",
        agents_file=tmp_path / "stub-agents.json",
        live_cron_dir=live_cron_dir,
        proc_root=tmp_path / "proc",
        agent_pids_dir=root / AGENT_PIDS_DIR,
        events_path=Path(agent_events_path({"MNGR_HOST_DIR": str(host_dir)})),
        bare_created_agent_id=f"agent-{uuid4().hex}",
    )
    staged.set_agents([])
    if has_message_chat:
        staged.script_tool(
            "system/scripts/message_chat.py",
            [ScriptedToolAnswer(stdout=json.dumps(STAGED_CREATED_CHAT) + "\n")],
        )
    if has_skill:
        staged.script_tool(
            ".agents/skills/update-self/scripts/update_self.py", [ScriptedToolAnswer()]
        )
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("MNGR_HOST_DIR", str(host_dir))
    monkeypatch.setenv("TICKETS_DIR", str(root / "data" / ".tickets"))
    monkeypatch.setenv("STUB_CALLS_LOG", str(staged.calls_log))
    monkeypatch.setenv("STUB_AGENTS_FILE", str(staged.agents_file))
    monkeypatch.setenv("STUB_CREATED_AGENT_ID", staged.bare_created_agent_id)
    monkeypatch.chdir(root)
    return staged


def arm_schedule_in(
    staged: StagedUpdateWorkspace,
    feed: ChannelFileFetcher,
    *,
    now: float,
    ceiling: str | None = None,
    override_ref: str = "",
    window: tuple[int, int] | None = None,
    is_backup_configured: bool | None = None,
    is_auto_armed: bool = False,
) -> dict[str, Any]:
    """Arm the staged workspace's schedule at ``now`` as a client does: ``window`` is the start minute in UTC
    and the length in minutes, and a value left out is left to the workspace."""
    return control.arm_schedule(
        ceiling=ceiling,
        override_ref=override_ref,
        window_start_minute_utc=window[0] if window is not None else None,
        window_length_minutes=window[1] if window is not None else None,
        is_backup_configured=is_backup_configured,
        is_auto_armed=is_auto_armed,
        now=now,
        live_cron_dir=str(staged.live_cron_dir),
        agent_environment=AgentEnvironment(),
        fetch_channel_file=feed,
    )


def stage_checkout_at(
    repo_root: Path, *commit_subjects_and_tags: tuple[str, str]
) -> None:
    """Make ``repo_root`` a checkout with one empty commit per ``(subject, tag)``, in order; a tag of '' tags
    nothing."""
    git = ["git", "-c", "user.name=test", "-c", "user.email=test@example.com"]
    subprocess.run(
        [*git, "init", "--quiet"], cwd=repo_root, check=True, capture_output=True
    )
    for subject, tag in commit_subjects_and_tags:
        subprocess.run(
            [*git, "commit", "--allow-empty", "--quiet", "-m", subject],
            cwd=repo_root,
            check=True,
            capture_output=True,
        )
        if tag:
            subprocess.run(
                [*git, "tag", tag], cwd=repo_root, check=True, capture_output=True
            )


def listed_agent(name: str, state: str, agent_id: str = "") -> dict[str, str]:
    """An agent as a test hands it to the stub ``mngr list``, or to a decision as the listing gives it."""
    return {"name": name, "state": state, "id": agent_id or f"agent-{uuid4().hex}"}


def armed_schedule(armed_at: float, **fields: Any) -> dict[str, Any]:
    """A schedule file armed at ``armed_at`` for a window of three hours from 09:00 UTC, with ``fields`` over it."""
    return {
        "armed_at": armed_at,
        "ceiling": "minds-v0.9.0",
        "window_start_minute_utc": 9 * 60,
        "window_length_minutes": 3 * 60,
        "override_ref": "",
        "marked_chat": "",
        "marked_at": None,
        "window_seen_open_at": None,
        "pending_skip_reason": "",
        **fields,
    }


def labels_of(create_argv: Sequence[str]) -> list[str]:
    """The ``--label`` values of one recorded create, in order."""
    return [
        create_argv[index + 1]
        for index, token in enumerate(create_argv)
        if token == "--label"
    ]


def run_in_place(work: Callable[[], None]) -> None:
    """A background runner that runs the work before it returns, so a test reads the outcome at once."""
    work()


def event_timestamp(epoch: float) -> str:
    """``epoch`` as the observer stamps an event: ISO 8601 UTC with nine fractional digits."""
    moment = datetime.fromtimestamp(epoch, timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond * 1000:09d}Z"


def observed_agent(
    agent_id: str, name: str, state: str, **labels: object
) -> dict[str, object]:
    """One agent's details as the observer writes them, as far as this library reads them."""
    return {"id": agent_id, "name": name, "state": state, "labels": dict(labels)}


def observed_services_agent() -> dict[str, object]:
    """The workspace's own services agent, which a file must list to vouch for the workspace's agents."""
    return observed_agent(
        SERVICES_AGENT_ID, "system-services", "RUNNING", is_primary="true"
    )


def full_state_line(agents: Sequence[object], at: float) -> str:
    """The observer's snapshot of every agent, taken at ``at``, as one line of its events file; ``agents`` are
    agents' details, or whatever else a test wants found in their place."""
    event = {
        "timestamp": event_timestamp(at),
        "type": "AGENTS_FULL_STATE",
        "event_id": f"evt-{uuid4().hex}",
        "source": "mngr/agents",
        "agents": list(agents),
    }
    return json.dumps(event, separators=(",", ":"))


def agent_state_line(agent: Mapping[str, object], at: float) -> str:
    """The observer's event for one agent's state, seen at ``at``."""
    event = {
        "timestamp": event_timestamp(at),
        "type": "AGENT_STATE",
        "event_id": f"evt-{uuid4().hex}",
        "source": "mngr/agents",
        "agent": dict(agent),
    }
    return json.dumps(event, separators=(",", ":"))


def agent_removed_line(agent_id: str, at: float) -> str:
    """The observer's event for an agent destroyed at ``at``."""
    event = {
        "timestamp": event_timestamp(at),
        "type": "AGENT_REMOVED",
        "event_id": f"evt-{uuid4().hex}",
        "source": "mngr/agents",
        "agent_id": agent_id,
    }
    return json.dumps(event, separators=(",", ":"))


def write_event_lines(events_path: Path, lines: Sequence[str]) -> None:
    """Write the observer's events file with ``lines``, each ended by a newline as an append leaves it."""
    events_path.parent.mkdir(parents=True, exist_ok=True)
    events_path.write_text("".join(line + "\n" for line in lines))
