"""Tests for the workspace memory command, against a fake ``/proc``, a fake ``mngr list`` and fake
Screen-service and shell answers (all injected through ``Sources``)."""

import json
import os
import re
import string
import subprocess
import sys
import urllib.error
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import workspace_memory
from chat_background_tasks import markers
from chat_background_tasks.testing import runner_task
from imbue.chat.config import DEFAULT_CHAT_DATA_DIR
from imbue.chat.main_chat import MAIN_CHAT_FILENAME, MainChatStore
from imbue.chat.primitives import ChatId
from imbue.mngr.cli.field_catalog import FieldContext, build_list_field_catalog
from mngr_cli_contract.contract import assert_mngr_argv_valid
from oom_priority.meminfo import HOST_MEMINFO_MAX_AGE_SECONDS
from oom_priority.testing import FAKE_BOOT_ID, write_fake_process
from update_self_skill import update_memory

_SCRIPT = Path(__file__).parent / "workspace_memory.py"

_NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
_SCREEN_URL = "http://screen.test"
_SHELL_URL = "http://shell.test"
_PROFILE_ROOT = "/home/user/.mngr/browser-profiles"


def _fake_process(
    proc_dir: Path,
    pid: int,
    anonymous_kib: int,
    ppid: int = 1,
    argv: Sequence[str] = ("python3",),
    oom_score_adj: int = 0,
    comm: str | None = None,
) -> None:
    """One process in the fake ``/proc``, started at tick ``pid`` by ``ppid``: its ``smaps`` (two
    mappings whose anonymous memory sums to ``anonymous_kib``), its status (a VmRSS three times
    that, as gVisor inflates it, and no RssAnon, as under gVisor), its band, its argv, and its
    name (the command's, unless ``comm`` gives the one the process set for itself)."""
    comm = comm or Path(argv[0]).name
    write_fake_process(proc_dir, pid, start_ticks=pid, comm=comm)
    entry = proc_dir / str(pid)
    (entry / "status").write_text(
        f"Name:\t{comm}\nTgid:\t{pid}\nPPid:\t{ppid}\nVmRSS:\t   {anonymous_kib * 3} kB\n"
        "VmPTE:\t   4 kB\nVmSwap:\t   0 kB\n"
    )
    first = anonymous_kib // 2
    (entry / "smaps").write_text(
        f"00400000-00500000 r-xp 00000000 00:00 0\nRss:   {anonymous_kib} kB\nAnonymous:   {first} kB\n"
        f"00600000-00700000 rw-p 00000000 00:00 0\nRss:   {anonymous_kib} kB\n"
        f"Anonymous:   {anonymous_kib - first} kB\n"
    )
    (entry / "cmdline").write_bytes("\0".join(argv).encode() + b"\0")
    (entry / "comm").write_text(f"{comm}\n")
    (entry / "oom_score_adj").write_text(f"{oom_score_adj}\n")


def _cells(text: str) -> list[list[str]]:
    """Each line of a printed table as its cells, which two or more spaces separate."""
    return [re.split(r" {2,}", line.strip()) for line in text.splitlines()]


def _write_meminfo(proc_dir: Path, fields: Mapping[str, int]) -> None:
    proc_dir.mkdir(parents=True, exist_ok=True)
    (proc_dir / "meminfo").write_text(
        "".join(f"{key}:{value:>12} kB\n" for key, value in fields.items())
    )


def _agent_record(
    name: str,
    state: str,
    labels: Mapping[str, str],
    last_activity: datetime,
    pid: int | None = None,
    provider: str = "local",
) -> dict[str, Any]:
    """One agent as mngr holds it (``AgentDetails``' shape, for the fields the template names)."""
    return {
        "id": f"agent-{uuid4().hex}",
        "name": name,
        "type": "claude",
        "state": state,
        "pid": pid,
        "start_time": last_activity - timedelta(hours=1),
        "user_activity_time": None,
        "agent_activity_time": last_activity,
        "idle_seconds": 1.0,
        "labels": dict(labels),
        "host": {"id": "host-1", "name": "localhost", "provider_name": provider},
    }


def _render_like_mngr(template: str, record: Mapping[str, Any]) -> str:
    """Render one agent through a ``mngr list --format`` template the way the pinned mngr does: a dotted
    field walks nested fields and label keys, and a null field or an absent label renders empty."""
    parts: list[str] = []
    for literal, field, _, _ in string.Formatter().parse(template):
        parts.append(literal)
        if field is None:
            continue
        value: Any = record
        for key in field.split("."):
            value = value.get(key) if isinstance(value, dict) else None
        parts.append("" if value is None else str(value))
    return "".join(parts)


def _register_pid(runtime_dir: Path, pid: int, agent_id: str) -> None:
    """Register ``pid`` for ``agent_id`` as started at tick ``pid`` in this boot, matching ``_fake_process``."""
    directory = runtime_dir / "agent_pids"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{pid}.json").write_text(
        json.dumps(
            {
                "agent_name": "x",
                "is_worker": False,
                "agent_id": agent_id,
                "start_ticks": pid,
                "boot_id": FAKE_BOOT_ID,
            }
        )
    )


class _FakeMngr:
    """Stands in for running ``mngr list``: renders each record through the ``--format`` template it is
    given, keeping only the ``--provider`` asked for; or raises the given error."""

    def __init__(
        self,
        records: Sequence[Mapping[str, Any]],
        returncode: int = 0,
        stderr: str = "",
        error: Exception | None = None,
        extra_lines: Sequence[str] = (),
    ) -> None:
        self.records = records
        self.returncode = returncode
        self.stderr = stderr
        self.error = error
        self.extra_lines = extra_lines

    def __call__(
        self, argv: Sequence[str], timeout_seconds: float
    ) -> "subprocess.CompletedProcess[str]":
        if self.error is not None:
            raise self.error
        options = dict(zip(argv[2::2], argv[3::2]))
        provider = options.get("--provider")
        listed = [
            r
            for r in self.records
            if provider is None or r["host"]["provider_name"] == provider
        ]
        rendered = [_render_like_mngr(options["--format"], record) for record in listed]
        stdout = "".join(f"{line}\n" for line in [*rendered, *self.extra_lines])
        return subprocess.CompletedProcess(
            list(argv), self.returncode, stdout=stdout, stderr=self.stderr
        )


class _FakeHttp:
    """Answers each URL with a JSON payload, or raises the exception given for it.

    A URL with no answer behaves as one nothing listens on, which is what a test that does not care
    about that service is saying.
    """

    def __init__(self, answers: Mapping[str, object]) -> None:
        self.answers = answers

    def __call__(self, url: str, timeout_seconds: float) -> object:
        answer = self.answers.get(
            url, urllib.error.URLError(ConnectionRefusedError(61, "Connection refused"))
        )
        if isinstance(answer, Exception):
            raise answer
        return answer


def _fleet(*browsers: Mapping[str, object]) -> dict[str, object]:
    return {
        "browsers": list(browsers),
        "can_create": True,
        "browser_count": len(browsers),
        "browser_max": 1,
    }


def _browser(
    name: str, lifecycle: str, tabs: Sequence[tuple[str, bool]] = ()
) -> dict[str, object]:
    return {
        "id": name,
        "lifecycle": lifecycle,
        "controller": None,
        "owner_agent_id": None,
        "owner_name": None,
        "human_pinned": False,
        "waiting": [],
        "crashed": False,
        "tabs": [
            {"index": i, "title": "", "url": url, "active": active}
            for i, (url, active) in enumerate(tabs)
        ],
    }


def _app(
    name: str,
    program: str = "",
    critical: bool = False,
    internal: bool = False,
    stop_when_no_windows: bool = False,
    display_name: str = "",
) -> dict[str, object]:
    """One app as the shell's inventory document carries it (``workspace_layout.answers.InventoryApp``)."""
    return {
        "name": name,
        "display_name": display_name or name,
        "program": program,
        "critical": critical,
        "internal": internal,
        "stop_when_no_windows": stop_when_no_windows,
        "is_running": True,
    }


def _inventory(
    *windows: Mapping[str, object], apps: Sequence[Mapping[str, object]] = ()
) -> dict[str, object]:
    return {
        "desktops": [{"id": "desktop-1", "windows": list(windows)}],
        "apps": list(apps),
        "clients": [],
    }


def _never_busy(chat_id: str) -> bool:
    return False


def _sources(
    proc_dir: Path,
    mngr: _FakeMngr,
    http: _FakeHttp,
    is_chat_busy: Callable[[str], bool] = _never_busy,
    supervised_pids: Mapping[str, int] | None = None,
    host_meminfo: Path | None = None,
    main_chat_file: Path | None = None,
    earlyoom_program_conf: Path | None = None,
    scratch_kib: int | None = None,
) -> workspace_memory.Sources:
    return workspace_memory.Sources(
        proc_dir=proc_dir,
        host_meminfo=host_meminfo if host_meminfo is not None else proc_dir / "absent",
        earlyoom_program_conf=earlyoom_program_conf
        if earlyoom_program_conf is not None
        else proc_dir / "absent-earlyoom.conf",
        run_command=mngr,
        fetch_json=http,
        read_supervised_pids=lambda: dict(supervised_pids or {}),
        read_scratch_kib=lambda: scratch_kib,
        screen_service_url=_SCREEN_URL,
        shell_url=_SHELL_URL,
        now=_NOW,
        is_chat_busy=is_chat_busy,
        main_chat_file=main_chat_file
        if main_chat_file is not None
        else proc_dir / "absent-main-chat",
    )


def _write_marker(root: Path, chat_id: str, pid: int) -> None:
    """A ``run_in_background`` marker for ``chat_id``, written by the library that owns the format."""
    markers.write_marker(
        root,
        chat_id,
        runner_task(f"task-{uuid4().hex[:8]}", "2026-09-24T10:00:00+00:00", pid=pid),
    )


@pytest.fixture
def runtime_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("OOM_PRIORITY_RUNTIME_DIR", str(runtime))
    return runtime


def test_lists_idle_chats_and_workers_with_last_activity_and_the_memory_of_their_process_trees(
    tmp_path: Path, runtime_dir: Path
) -> None:
    proc = tmp_path / "proc"
    _write_meminfo(
        proc,
        {
            "MemTotal": 4 * 1024 * 1024,
            "MemFree": 300 * 1024,
            "MemAvailable": 300 * 1024,
        },
    )
    chat_activity = _NOW - timedelta(hours=2, minutes=5)
    chat = _agent_record(
        "chat-a",
        "WAITING",
        {"user_created": "true", "display_name": "Trip plan"},
        chat_activity,
        100,
    )
    worker = _agent_record(
        "fix-login",
        "WAITING",
        {"agent_created": "true"},
        _NOW - timedelta(minutes=30),
        200,
    )
    # The chat's harness runs a second registered process (codex's app-server) mngr does not report.
    _fake_process(proc, 100, 1000)
    _fake_process(proc, 101, 500, ppid=100)
    _fake_process(proc, 150, 700)
    _register_pid(runtime_dir, 150, str(chat["id"]))
    _register_pid(runtime_dir, 151, str(chat["id"]))  # exited: no /proc entry
    _fake_process(proc, 200, 300)

    report = workspace_memory.collect_report(
        _sources(
            proc,
            _FakeMngr([chat, worker]),
            _FakeHttp({f"{_SCREEN_URL}/browsers": _fleet()}),
        )
    )

    agents = report.agents.candidates
    assert agents is not None
    assert [(a.kind, a.name, a.display_name) for a in agents] == [
        ("chat", "chat-a", "Trip plan"),
        ("worker", "fix-login", None),
    ]
    assert agents[0].last_activity == chat_activity
    assert agents[0].pids == (100, 150)
    assert agents[0].anonymous_kib == 2200
    assert agents[1].anonymous_kib == 300
    assert report.agents.notes == ()
    printed = workspace_memory.render_stoppable(report)
    assert printed.splitlines()[0] == (
        f"Free memory: 300 MiB (MemAvailable from {proc / 'meminfo'}; MemTotal 4096 MiB)"
    )
    assert ["chat", "chat-a", "Trip plan", "2 MiB", "idle 2h 05m"] in _cells(printed)


def test_lists_only_idle_local_chats_and_workers_never_infrastructure_or_active_agents(
    tmp_path: Path, runtime_dir: Path
) -> None:
    proc = tmp_path / "proc"
    _write_meminfo(proc, {"MemTotal": 4 * 1024 * 1024, "MemAvailable": 100 * 1024})
    # The never-kill infrastructure and a service, each holding plenty of memory.
    for pid, argv in (
        (1, ("/usr/sbin/sshd",)),
        (2, ("/usr/bin/python3", "/usr/bin/supervisord")),
        (3, ("earlyoom",)),
        (4, ("tmux",)),
        (5, ("python3", "-m", "imbue.chat.server")),
    ):
        _fake_process(proc, pid, 900_000, argv=argv)
    old = _NOW - timedelta(days=2)
    records = [
        _agent_record("busy-chat", "RUNNING", {"user_created": "true"}, old, 10),
        _agent_record(
            "just-answered",
            "WAITING",
            {"user_created": "true"},
            _NOW - timedelta(minutes=5),
            11,
        ),
        _agent_record(
            "services",
            "WAITING",
            {"is_primary": "true", "user_created": "true"},
            old,
            12,
        ),
        _agent_record("caretaker", "WAITING", {"automation": "caretaker"}, old, 13),
        _agent_record("stopped-chat", "STOPPED", {"user_created": "true"}, old),
        _agent_record("done-worker", "DONE", {"agent_created": "true"}, old),
        _agent_record(
            "remote-chat",
            "WAITING",
            {"user_created": "true"},
            old,
            14,
            provider="modal",
        ),
        # No activity time at all, so nothing says it has sat idle.
        {
            **_agent_record("no-times", "WAITING", {"user_created": "true"}, old, 15),
            "start_time": None,
            "agent_activity_time": None,
        },
        _agent_record(
            "background-chat", "RUNNING_BACKGROUND", {"user_created": "true"}, old, 16
        ),
        _agent_record(
            "unknown-type",
            "RUNNING_UNKNOWN_AGENT_TYPE",
            {"agent_created": "true"},
            old,
            17,
        ),
        _agent_record("replaced-chat", "REPLACED", {"user_created": "true"}, old, 18),
        _agent_record("unread-chat", "UNKNOWN", {"user_created": "true"}, old, 19),
    ]

    report = workspace_memory.collect_report(
        _sources(
            proc, _FakeMngr(records), _FakeHttp({f"{_SCREEN_URL}/browsers": _fleet()})
        )
    )

    assert report.agents.candidates == ()
    assert report.browsers.candidates == ()
    # The chats still holding memory are listed with why they are not candidates; infrastructure,
    # an automation, a stopped or done agent and another provider's agent are not.
    assert report.agents.others is not None
    assert [(e.name, e.why) for e in report.agents.others] == [
        ("busy-chat", "mid-turn"),
        ("just-answered", "in use: active 5m ago"),
        ("no-times", "no activity time to judge it by"),
        (
            "background-chat",
            "waiting on a background task, which will start its next turn",
        ),
        (
            "unknown-type",
            "running, but of an agent type mngr does not know, so whether it is mid-turn is unknown",
        ),
        ("replaced-chat", "another program runs in its window"),
        (
            "unread-chat",
            "its state could not be read, so whether it is mid-turn is unknown",
        ),
    ]
    # What nothing above started is counted by its command, so the list still adds up.
    assert {e.name: e.anonymous_kib for e in report.unowned} == {
        "SSH access": 900_000,
        "Internal system": 1_800_000,
        "Other": 1_800_000,
    }
    printed = workspace_memory.render_stoppable(report)
    assert "no window shows):\n  none\n" in printed
    assert "not enough):\n  none\n" in printed


def test_an_agent_whose_chat_waits_on_a_background_task_is_not_a_candidate(
    tmp_path: Path, runtime_dir: Path
) -> None:
    proc = tmp_path / "proc"
    _write_meminfo(proc, {"MemTotal": 4 * 1024 * 1024, "MemAvailable": 100 * 1024})
    old = _NOW - timedelta(hours=2)
    lead = _agent_record("lead", "WAITING", {"user_created": "true"}, old, 10)
    # A chat's successor carries the chat's id as a label; its markers are filed under that id, not its own.
    successor = _agent_record(
        "successor",
        "WAITING",
        {"user_created": "true", "chat_id": "agent-chat-1"},
        old,
        11,
    )
    worker = _agent_record("fix-login", "WAITING", {"agent_created": "true"}, old, 12)
    finished = _agent_record("finished", "WAITING", {"agent_created": "true"}, old, 13)
    for pid in (10, 11, 12, 13):
        _fake_process(proc, pid, 100)
    root = tmp_path / "background_tasks"
    _write_marker(root, str(lead["id"]), os.getpid())
    _write_marker(root, "agent-chat-1", os.getpid())
    _write_marker(root, str(worker["id"]), os.getpid())
    # A marker whose runner has exited is stale: that chat is no longer busy.
    exited = subprocess.Popen([sys.executable, "-c", "pass"])
    exited.wait()
    _write_marker(root, str(finished["id"]), exited.pid)
    is_chat_busy = workspace_memory.busy_chat_check(
        {markers.MARKER_ROOT_ENV: str(root)}
    )
    mngr = _FakeMngr([lead, successor, worker, finished])

    report = workspace_memory.collect_report(
        _sources(
            proc, mngr, _FakeHttp({f"{_SCREEN_URL}/browsers": _fleet()}), is_chat_busy
        )
    )

    assert report.agents.candidates is not None
    assert [a.name for a in report.agents.candidates] == ["finished"]
    assert report.agents.others is not None
    assert {(e.name, e.why, e.anonymous_kib) for e in report.agents.others} == {
        (name, "waiting on a background task, which will start its next turn", 100)
        for name in ("lead", "successor", "fix-login")
    }


def test_the_main_chat_is_never_a_candidate_because_the_chat_app_starts_it_again(
    tmp_path: Path, runtime_dir: Path
) -> None:
    proc = tmp_path / "proc"
    _write_meminfo(proc, {"MemTotal": 4 * 1024 * 1024, "MemAvailable": 100 * 1024})
    old = _NOW - timedelta(hours=2)
    # The main chat has handed off once, so its live agent carries the chat's id as a label.
    main_chat = _agent_record(
        "main-successor",
        "WAITING",
        {"user_created": "true", "chat_id": "agent-main-chat"},
        old,
        10,
    )
    other_chat = _agent_record(
        "trip-plan", "WAITING", {"user_created": "true"}, old, 11
    )
    for pid in (10, 11):
        _fake_process(proc, pid, 100)
    # Written by the chat app's own store, so the file's place and shape are the ones it keeps.
    MainChatStore(path=tmp_path / DEFAULT_CHAT_DATA_DIR / MAIN_CHAT_FILENAME).write(
        ChatId("agent-main-chat")
    )

    report = workspace_memory.collect_report(
        _sources(
            proc,
            _FakeMngr([main_chat, other_chat]),
            _FakeHttp({f"{_SCREEN_URL}/browsers": _fleet()}),
            main_chat_file=tmp_path / workspace_memory.MAIN_CHAT_FILE,
        )
    )

    assert report.agents.candidates is not None
    assert [a.name for a in report.agents.candidates] == ["trip-plan"]
    # Still counted, under what holds memory without being a candidate.
    assert report.agents.others is not None
    assert [(e.name, e.why, e.anonymous_kib) for e in report.agents.others] == [
        (
            "main-successor",
            "the main chat: the chat app starts it again as soon as it stops",
            100,
        )
    ]


def test_an_unreadable_main_chat_file_is_noted_and_the_agents_are_still_listed(
    tmp_path: Path, runtime_dir: Path
) -> None:
    proc = tmp_path / "proc"
    _write_meminfo(proc, {"MemTotal": 4 * 1024 * 1024, "MemAvailable": 100 * 1024})
    old = _NOW - timedelta(hours=2)
    _fake_process(proc, 10, 100)
    main_chat_file = tmp_path / "main_chat.json"
    main_chat_file.write_text("{not json", encoding="utf-8")

    report = workspace_memory.collect_report(
        _sources(
            proc,
            _FakeMngr(
                [
                    _agent_record(
                        "trip-plan", "WAITING", {"user_created": "true"}, old, 10
                    )
                ]
            ),
            _FakeHttp({f"{_SCREEN_URL}/browsers": _fleet()}),
            main_chat_file=main_chat_file,
        )
    )

    assert report.agents.candidates is not None
    assert [a.name for a in report.agents.candidates] == ["trip-plan"]
    assert [
        note
        for note in report.agents.notes
        if note.startswith(f"could not read the main chat from {main_chat_file}")
    ]


def test_lists_running_browsers_while_no_window_shows_the_screen_with_their_chromium_memory(
    tmp_path: Path, runtime_dir: Path
) -> None:
    proc = tmp_path / "proc"
    # browser-1's Chromium (a renderer beneath it); browser-10's must not leak into browser-1's by prefix.
    _fake_process(
        proc,
        300,
        400,
        argv=(
            "tilion",
            f"--user-data-dir={_PROFILE_ROOT}/browser-use-user-data-dir-browser-1",
        ),
    )
    _fake_process(proc, 301, 600, ppid=300, argv=("tilion", "--type=renderer"))
    _fake_process(
        proc,
        310,
        900,
        argv=(
            "tilion",
            f"--user-data-dir={_PROFILE_ROOT}/browser-use-user-data-dir-browser-10",
        ),
    )
    fleet = _fleet(
        _browser(
            "browser-1",
            "running",
            tabs=[("https://a.test/", False), ("https://b.test/", True)],
        ),
        _browser("browser-10", "running", tabs=[("https://c.test/", True)]),
        _browser("browser-2", "stopped"),
        _browser("browser-3", "init"),
    )
    # A chat window shows nothing of the Screen, so every running browser is a candidate.
    inventory = _inventory({"app": "chat", "path": "/?chat=chat-a"})

    report = workspace_memory.collect_report(
        _sources(
            proc,
            _FakeMngr([]),
            _FakeHttp(
                {
                    f"{_SCREEN_URL}/browsers": fleet,
                    f"{_SHELL_URL}/api/inventory": inventory,
                }
            ),
        )
    )

    browsers = report.browsers.candidates
    assert browsers is not None
    assert [(b.name, b.controller, b.tab_count, b.active_url) for b in browsers] == [
        ("browser-1", "free", 2, "https://b.test/"),
        ("browser-10", "free", 1, "https://c.test/"),
    ]
    assert browsers[0].pids == (300,)
    assert browsers[0].anonymous_kib == 1000
    assert browsers[1].pids == (310,)
    assert browsers[1].anonymous_kib == 900


def test_a_running_browser_is_not_a_candidate_while_a_window_shows_the_screen(
    tmp_path: Path, runtime_dir: Path
) -> None:
    proc = tmp_path / "proc"
    fleet = _fleet(_browser("browser-1", "running", tabs=[("https://a.test/", True)]))
    inventory = _inventory(
        {"app": "screen", "path": "/"},
        {"app": "chat", "path": "/?chat=chat-a"},
        apps=[_app("screen", program="screen")],
    )
    http = _FakeHttp(
        {
            f"{_SCREEN_URL}/browsers": fleet,
            f"{_SHELL_URL}/api/inventory": inventory,
        }
    )

    # The Screen's service launches the browser's Chromium (and an app on the Screen) as children.
    _fake_process(proc, 200, 30 * 1024)
    _fake_process(proc, 210, 10 * 1024, ppid=200)
    _fake_process(
        proc,
        300,
        500 * 1024,
        ppid=200,
        argv=(
            "tilion",
            f"--user-data-dir={_PROFILE_ROOT}/browser-use-user-data-dir-browser-1",
        ),
    )
    _fake_process(proc, 301, 200 * 1024, ppid=300, argv=("tilion", "--type=renderer"))

    report = workspace_memory.collect_report(
        _sources(proc, _FakeMngr([]), http, supervised_pids={"screen": 200})
    )

    assert report.browsers.candidates == ()
    # Not a candidate, but still the answer to "how much is the browser using".
    assert report.browsers.others == (
        workspace_memory.HeldEntry(
            kind="browser",
            name="browser-1",
            title="https://a.test/",
            why="a window shows the Screen it runs on",
            pids=(300,),
            process_pids=(300, 301),
            anonymous_kib=700 * 1024,
        ),
    )
    # The Screen's own row leaves its browser out, so the browser is not counted twice.
    assert report.apps.others is not None
    assert [(e.name, e.anonymous_kib) for e in report.apps.others] == [
        ("screen", 40 * 1024)
    ]
    cells = _cells(workspace_memory.render_everything(report))
    assert [
        "browser",
        "browser-1",
        "https://a.test/",
        "700 MiB",
        "no",
        "a window shows the Screen it runs on",
    ] in cells
    assert [
        "app",
        "screen",
        "screen",
        "40 MiB",
        "no",
        "the Screen's service: its browser is listed on its own",
    ] in cells
    assert workspace_memory.report_to_json(report)["browsers"] == {
        "candidates": [],
        "notes": [],
        "others": [
            {
                "kind": "browser",
                "name": "browser-1",
                "title": "https://a.test/",
                "why": "a window shows the Screen it runs on",
                "pids": [300],
                "anonymous_kib": 700 * 1024,
            }
        ],
    }


def test_lists_the_apps_the_workspace_may_stop_with_how_many_windows_show_them(
    tmp_path: Path, runtime_dir: Path
) -> None:
    proc = tmp_path / "proc"
    _fake_process(proc, 400, 1024)
    _fake_process(proc, 401, 2048, ppid=400)
    _fake_process(proc, 410, 512)
    _fake_process(proc, 420, 4096)
    _fake_process(proc, 430, 8192)
    _fake_process(proc, 450, 256)
    inventory = _inventory(
        {"app": "daily-digest", "path": "/"},
        {"app": "daily-digest", "path": "/archive"},
        {"app": "chat", "path": "/"},
        apps=[
            _app("daily-digest", program="daily-digest", display_name="Daily Digest"),
            _app("lineup-scout", program="lineup-scout", stop_when_no_windows=True),
            # Never offered: the workspace is reached and talked to through its critical apps, an
            # internal app has no window to lose and no user who asked for it, an app with no
            # program cannot be started again, and the Screen's browser has its own section.
            _app("chat", program="chat", critical=True),
            _app("system-monitor", program="system-monitor", internal=True),
            _app("docs", program=""),
            _app("screen", program="screen"),
            # Registered but not running, so stopping it frees nothing.
            _app("setlist-playlists", program="setlist-playlists"),
        ],
    )
    http = _FakeHttp(
        {
            f"{_SCREEN_URL}/browsers": _fleet(),
            f"{_SHELL_URL}/api/inventory": inventory,
        }
    )

    report = workspace_memory.collect_report(
        _sources(
            proc,
            _FakeMngr([]),
            http,
            supervised_pids={
                "daily-digest": 400,
                "lineup-scout": 410,
                "chat": 420,
                "system-monitor": 430,
                "screen": 440,
                "earlyoom": 450,
            },
        )
    )

    apps = report.apps.candidates
    assert apps is not None
    assert [
        (a.name, a.display_name, a.window_count, a.anonymous_kib) for a in apps
    ] == [
        ("daily-digest", "Daily Digest", 2, 3072),
        ("lineup-scout", "lineup-scout", 0, 512),
    ]
    assert [a.stops_when_no_windows for a in apps] == [False, True]
    # The running programs that are not candidates, with why: the critical and internal apps, the
    # Screen's service, and a workspace service no app names.
    assert report.apps.others is not None
    assert [(e.kind, e.name, e.title, e.why) for e in report.apps.others] == [
        ("app", "system-monitor", "system-monitor", "an internal app"),
        (
            "app",
            "chat",
            "chat",
            "a critical app: the workspace is reached and talked to through it",
        ),
        ("service", "earlyoom", None, "a workspace service, not the user's to stop"),
        (
            "app",
            "screen",
            "screen",
            "the Screen's service: its browser is listed on its own",
        ),
    ]
    # An app a window shows is offered apart from the rest, and last: stopping it takes something
    # off the user's screen. Each row says whether the shell stops the app by itself, since one it
    # does not may be doing work between requests.
    printed = workspace_memory.render_stoppable(report)
    first_group, on_screen = printed.split("On screen now")
    assert [
        "app",
        "lineup-scout",
        "lineup-scout",
        "0 MiB",
        "no window shows it; stops by itself once none does",
    ] in _cells(first_group)
    assert [
        "app",
        "daily-digest",
        "Daily Digest",
        "3 MiB",
        "2 windows show it; keeps running when none does",
    ] in _cells(on_screen)
    everything = _cells(workspace_memory.render_everything(report))
    assert [row[4] for row in everything if row[:2] == ["app", "daily-digest"]] == [
        "on screen"
    ]


def test_an_app_sharing_a_critical_apps_program_is_never_offered(
    tmp_path: Path, runtime_dir: Path
) -> None:
    """The shell's own `stoppable_program_of` refuses these, because `supervisorctl stop <program>`
    would take the critical app down with the row that merely shares its program."""
    proc = tmp_path / "proc"
    _fake_process(proc, 500, 4096)
    inventory = _inventory(
        apps=[
            _app("terminal", program="terminal", critical=True),
            _app("terminal-extras", program="terminal"),
        ],
    )
    http = _FakeHttp(
        {
            f"{_SCREEN_URL}/browsers": _fleet(),
            f"{_SHELL_URL}/api/inventory": inventory,
        }
    )

    report = workspace_memory.collect_report(
        _sources(proc, _FakeMngr([]), http, supervised_pids={"terminal": 500})
    )

    assert report.apps.candidates == ()


def test_a_program_several_apps_share_takes_its_most_binding_reason_whatever_their_order(
    tmp_path: Path,
) -> None:
    proc = tmp_path / "proc"
    _fake_process(proc, 600, 1024)
    _fake_process(proc, 610, 2048)
    apps = [
        _app("screen-tools", program="screen", internal=True),
        _app("screen", program="screen"),
        _app("terminal-extras", program="terminal", internal=True),
        _app("terminal", program="terminal", critical=True),
    ]

    programs = {"screen": 600, "terminal": 610}
    held = workspace_memory.held_programs(
        apps, (), programs, workspace_memory.read_process_table(proc, {}, programs)
    )

    assert [(e.name, e.title, e.why) for e in held] == [
        (
            "terminal",
            "terminal-extras, terminal",
            "a critical app: the workspace is reached and talked to through it",
        ),
        (
            "screen",
            "screen-tools, screen",
            "the Screen's service: its browser is listed on its own",
        ),
    ]


def test_apps_are_unknown_rather_than_none_when_supervisord_cannot_be_asked(
    tmp_path: Path, runtime_dir: Path
) -> None:
    proc = tmp_path / "proc"
    http = _FakeHttp(
        {
            f"{_SCREEN_URL}/browsers": _fleet(),
            f"{_SHELL_URL}/api/inventory": _inventory(
                apps=[_app("daily-digest", program="daily-digest")]
            ),
        }
    )

    report = workspace_memory.collect_report(
        _sources(proc, _FakeMngr([]), http, supervised_pids={})
    )

    assert report.apps.candidates is None
    assert report.apps.notes[0].startswith("could not ask supervisord")


def test_a_failed_mngr_list_reports_agents_as_unknown_and_still_lists_browsers(
    tmp_path: Path, runtime_dir: Path
) -> None:
    proc = tmp_path / "proc"
    fleet = _fleet(_browser("browser-1", "running"))
    http = _FakeHttp(
        {f"{_SCREEN_URL}/browsers": fleet, f"{_SHELL_URL}/api/inventory": _inventory()}
    )

    report = workspace_memory.collect_report(
        _sources(
            proc,
            _FakeMngr(
                [], error=FileNotFoundError(2, "No such file or directory", "mngr")
            ),
            http,
        )
    )

    assert report.agents.candidates is None
    assert report.agents.others == ()
    assert len(report.agents.notes) == 1
    assert report.agents.notes[0].startswith("could not run `mngr list`: ")
    assert report.browsers.candidates is not None
    assert [b.name for b in report.browsers.candidates] == ["browser-1"]
    printed = workspace_memory.render_stoppable(report)
    assert (
        "unknown: which chats and workers could be stopped (see the note below); unknown is not none\n"
        "unknown: which apps could be stopped (see the note below); unknown is not none\n"
        "note: could not run `mngr list`: " in printed
    )


def test_a_listing_error_keeps_the_agents_mngr_did_list_and_notes_the_error(
    tmp_path: Path, runtime_dir: Path
) -> None:
    proc = tmp_path / "proc"
    chat = _agent_record(
        "chat-a", "WAITING", {"user_created": "true"}, _NOW - timedelta(hours=1), 100
    )
    mngr = _FakeMngr(
        [chat], returncode=1, stderr="Errors while listing:\n  host-2: ssh timed out\n"
    )

    report = workspace_memory.collect_report(
        _sources(proc, mngr, _FakeHttp({f"{_SCREEN_URL}/browsers": _fleet()}))
    )

    assert report.agents.candidates is not None
    assert [a.name for a in report.agents.candidates] == ["chat-a"]
    assert report.agents.notes == (
        "mngr list exited 1: Errors while listing:; host-2: ssh timed out",
    )


def test_a_listing_that_fails_with_no_agents_is_unknown_not_empty(
    tmp_path: Path, runtime_dir: Path
) -> None:
    proc = tmp_path / "proc"
    mngr = _FakeMngr([], returncode=1, stderr="Error: cannot load the local provider\n")

    report = workspace_memory.collect_report(
        _sources(proc, mngr, _FakeHttp({f"{_SCREEN_URL}/browsers": _fleet()}))
    )

    assert report.agents.candidates is None
    assert report.agents.notes == (
        "mngr list exited 1: Error: cannot load the local provider",
    )


def test_a_listing_none_of_whose_lines_parse_is_unknown_not_empty(
    tmp_path: Path, runtime_dir: Path
) -> None:
    proc = tmp_path / "proc"
    mngr = _FakeMngr(
        [], extra_lines=["NAME  STATE  (an output shape this command does not read)"]
    )

    report = workspace_memory.collect_report(
        _sources(proc, mngr, _FakeHttp({f"{_SCREEN_URL}/browsers": _fleet()}))
    )

    assert report.agents.candidates is None
    assert len(report.agents.notes) == 1
    assert report.agents.notes[0].startswith(
        "1 line(s) of `mngr list` output did not parse as an agent"
    )


def test_the_listing_argv_and_every_template_field_exist_in_the_pinned_mngr() -> None:
    assert_mngr_argv_valid(workspace_memory.MNGR_LIST_ARGV)
    template_fields = {
        row.key.replace("$KEY", "")
        for row in build_list_field_catalog()
        if FieldContext.TEMPLATE in row.contexts
    }
    for field in workspace_memory.MNGR_LIST_FIELDS:
        label_prefix = "labels."
        assert (
            field if not field.startswith(label_prefix) else label_prefix
        ) in template_fields, field


def test_an_unreachable_browser_service_reports_browsers_as_unknown_and_still_lists_agents(
    tmp_path: Path, runtime_dir: Path
) -> None:
    proc = tmp_path / "proc"
    chat = _agent_record(
        "chat-a", "WAITING", {"user_created": "true"}, _NOW - timedelta(hours=1), 100
    )
    http = _FakeHttp(
        {
            f"{_SCREEN_URL}/browsers": urllib.error.URLError(
                ConnectionRefusedError(61, "Connection refused")
            )
        }
    )

    report = workspace_memory.collect_report(_sources(proc, _FakeMngr([chat]), http))

    assert report.browsers.candidates is None
    assert report.browsers.notes[0].startswith(
        f"could not reach the Screen service at {_SCREEN_URL}/browsers:"
    )
    assert report.agents.candidates is not None
    assert [a.name for a in report.agents.candidates] == ["chat-a"]


def test_an_unreadable_shell_leaves_browsers_unknown_rather_than_calling_them_unviewed(
    tmp_path: Path, runtime_dir: Path
) -> None:
    proc = tmp_path / "proc"
    for pid, name in ((300, "browser-1"), (310, "browser-2")):
        _fake_process(
            proc,
            pid,
            256,
            argv=(
                "tilion",
                f"--user-data-dir={_PROFILE_ROOT}/browser-use-user-data-dir-{name}",
            ),
        )
    fleet = _fleet(_browser("browser-1", "running"), _browser("browser-2", "stopped"))
    for inventory_answer in (
        urllib.error.URLError("timed out"),
        {"unexpected": "shape"},
    ):
        http = _FakeHttp(
            {
                f"{_SCREEN_URL}/browsers": fleet,
                f"{_SHELL_URL}/api/inventory": inventory_answer,
            }
        )

        report = workspace_memory.collect_report(_sources(proc, _FakeMngr([]), http))

        assert report.apps.candidates is None
        assert (
            "so which apps run and which windows show them is unknown"
            in report.apps.notes[0]
        )
        assert report.browsers.candidates is None
        assert (
            "so whether a window shows the Screen is unknown"
            in report.browsers.notes[0]
        )
        # Only the running browser waits on the windows; the other was never the service's to stop.
        assert {entry.name: entry.why for entry in report.browsers.others} == {
            "browser-1": "whether a window shows the Screen it runs on is unknown",
            "browser-2": "the Screen service does not list it as running",
        }


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        (
            {"MemTotal": 8192, "MemFree": 1024, "MemAvailable": 2048},
            ("MemAvailable", 2048, 8192),
        ),
        ({"MemTotal": 8192, "MemFree": 1024}, ("MemFree", 1024, 8192)),
    ],
)
def test_free_memory_names_the_meminfo_field_it_read(
    tmp_path: Path, fields: Mapping[str, int], expected: tuple[str, int, int]
) -> None:
    _write_meminfo(tmp_path, fields)
    free = workspace_memory.read_free_memory(
        tmp_path / "meminfo", tmp_path / "absent-host-meminfo", _NOW, shed_percent=None
    )
    assert free is not None
    assert (free.field, free.free_kib, free.total_kib) == expected
    assert free.source == str(tmp_path / "meminfo")


def test_free_memory_comes_from_the_headroom_file_the_memory_guard_acts_on(
    tmp_path: Path,
) -> None:
    """The container's own /proc/meminfo is several times more optimistic than the capped figure
    earlyoom acts on, and reading it is what makes a lead rerun a command that goes on being shed."""
    proc = tmp_path / "proc"
    _write_meminfo(proc, {"MemTotal": 8192, "MemAvailable": 6000})
    host = tmp_path / ".host-meminfo"
    host.write_text("MemTotal:         8192 kB\nMemAvailable:     1000 kB\n")
    os.utime(host, (_NOW.timestamp(), _NOW.timestamp()))

    fresh = workspace_memory.read_free_memory(
        proc / "meminfo", host, _NOW, shed_percent=None
    )
    assert fresh is not None
    assert (fresh.source, fresh.free_kib) == (str(host), 1000)

    # earlyoom ignores the file once it is stale, and so must this: a publisher that died would
    # otherwise freeze the reported headroom at whatever it last wrote.
    stale = _NOW + timedelta(seconds=HOST_MEMINFO_MAX_AGE_SECONDS + 1)
    fallen_back = workspace_memory.read_free_memory(
        proc / "meminfo", host, stale, shed_percent=None
    )
    assert fallen_back is not None
    assert (fallen_back.source, fallen_back.free_kib) == (str(proc / "meminfo"), 6000)


def _write_earlyoom_program(path: Path, command: str) -> Path:
    path.write_text(f"[program:earlyoom]\ncommand={command}\nautostart=true\n")
    return path


def test_free_memory_says_how_much_of_it_is_left_before_the_memory_guard_acts(
    tmp_path: Path,
) -> None:
    """The guard stops processes at a share of the total, not at zero: 1 GiB free on a 6.4 GiB
    workspace is about 400 MiB of room."""
    _write_meminfo(tmp_path, {"MemTotal": 6568960, "MemAvailable": 1076224})
    program = _write_earlyoom_program(
        tmp_path / "earlyoom.conf",
        "/home/user/workspace/run.sh /usr/local/bin/earlyoom -m 10,5 -s 100,100 -r 0",
    )

    free = workspace_memory.read_free_memory(
        tmp_path / "meminfo",
        tmp_path / "absent-host-meminfo",
        _NOW,
        shed_percent=workspace_memory.read_shed_percent(program),
    )

    assert workspace_memory.render_free_memory(free) == (
        f"Free memory: 1051 MiB (MemAvailable from {tmp_path / 'meminfo'}; MemTotal 6415 MiB); "
        "409 MiB of it before the memory guard starts stopping processes at 10% of MemTotal"
    )


@pytest.mark.parametrize(
    "command",
    [
        "/usr/local/bin/earlyoom -s 100,100 -r 0",
        "/usr/local/bin/earlyoom -m nope",
        '/usr/local/bin/earlyoom -m 10,5 --avoid "^(sshd',
    ],
    ids=["no-threshold", "unparseable-threshold", "unbalanced-quote"],
)
def test_free_memory_without_a_readable_guard_threshold_says_only_the_free_memory(
    tmp_path: Path, command: str
) -> None:
    _write_meminfo(tmp_path, {"MemTotal": 8192, "MemAvailable": 2048})
    program = _write_earlyoom_program(tmp_path / "earlyoom.conf", command)

    free = workspace_memory.read_free_memory(
        tmp_path / "meminfo",
        tmp_path / "absent-host-meminfo",
        _NOW,
        shed_percent=workspace_memory.read_shed_percent(program),
    )

    assert "memory guard" not in workspace_memory.render_free_memory(free)


def test_the_guard_threshold_is_read_off_the_workspaces_own_earlyoom_program() -> None:
    assert (
        workspace_memory.read_shed_percent(workspace_memory.EARLYOOM_PROGRAM_CONF)
        is not None
    )


def test_json_report_shape(tmp_path: Path, runtime_dir: Path) -> None:
    proc = tmp_path / "proc"
    _write_meminfo(proc, {"MemTotal": 4096, "MemAvailable": 1024})
    chat = _agent_record(
        "chat-a", "WAITING", {"user_created": "true"}, _NOW - timedelta(hours=1), 100
    )
    _fake_process(proc, 100, 2048)
    _fake_process(
        proc,
        300,
        512,
        argv=(
            "tilion",
            f"--user-data-dir={_PROFILE_ROOT}/browser-use-user-data-dir-browser-1",
        ),
    )
    browser = _browser("browser-1", "running", tabs=[("https://a.test/", True)])
    browser.update({"controller": "agent", "owner_name": "chat-b"})
    _fake_process(proc, 400, 256)
    http = _FakeHttp(
        {
            f"{_SCREEN_URL}/browsers": _fleet(browser),
            f"{_SHELL_URL}/api/inventory": _inventory(
                {"app": "daily-digest", "path": "/"},
                apps=[_app("daily-digest", program="daily-digest")],
            ),
        }
    )

    report = workspace_memory.collect_report(
        _sources(
            proc,
            _FakeMngr([chat]),
            http,
            supervised_pids={"daily-digest": 400},
            earlyoom_program_conf=_write_earlyoom_program(
                tmp_path / "earlyoom.conf", "/usr/local/bin/earlyoom -m 10,5"
            ),
        )
    )
    document = json.loads(json.dumps(workspace_memory.report_to_json(report)))

    assert document == {
        "generated_at": "2026-09-24T12:00:00Z",
        "idle_after_seconds": 900.0,
        "free_memory": {
            "field": "MemAvailable",
            "free_kib": 1024,
            "total_kib": 4096,
            "source": str(proc / "meminfo"),
            "shed_line_kib": 409,
            "room_kib": 615,
        },
        "agents": {
            "candidates": [
                {
                    "name": "chat-a",
                    "id": chat["id"],
                    "display_name": None,
                    "kind": "chat",
                    "state": "WAITING",
                    "last_activity": "2026-09-24T11:00:00Z",
                    "idle_seconds": 3600,
                    "pids": [100],
                    "anonymous_kib": 2048,
                }
            ],
            "notes": [],
            "others": [],
        },
        "apps": {
            "candidates": [
                {
                    "name": "daily-digest",
                    "display_name": "daily-digest",
                    "program": "daily-digest",
                    "window_count": 1,
                    "stop_when_no_windows": False,
                    "pids": [400],
                    "anonymous_kib": 256,
                }
            ],
            "notes": [],
            "others": [],
        },
        "browsers": {
            "candidates": [
                {
                    "name": "browser-1",
                    "controller": "agent chat-b",
                    "tab_count": 1,
                    "active_url": "https://a.test/",
                    "pids": [300],
                    "anonymous_kib": 512,
                }
            ],
            "notes": [],
            "others": [],
        },
        "unowned": [],
        "totals": {
            "in_use_kib": 3072,
            "shared_kib": 0,
            "temporary_files_kib": None,
            "system_kib": 256,
        },
    }


def test_the_script_runs_under_a_plain_python3_and_prints_json(tmp_path: Path) -> None:
    """End to end through the real wiring: a fake ``mngr`` on PATH printing two rendered agents, one of whose chats
    waits on a background task, and a Screen service nothing listens on."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    long_ago = datetime.now(timezone.utc) - timedelta(hours=3)
    record = _agent_record("chat-a", "WAITING", {"user_created": "true"}, long_ago)
    waiting_on_a_task = _agent_record(
        "chat-b", "WAITING", {"user_created": "true"}, long_ago
    )
    _write_marker(
        tmp_path / "background_tasks", str(waiting_on_a_task["id"]), os.getpid()
    )
    lines = "\n".join(
        _render_like_mngr(workspace_memory.MNGR_LIST_ARGV[-1], r)
        for r in (record, waiting_on_a_task)
    )
    fake_mngr = bindir / "mngr"
    fake_mngr.write_text(f"#!/bin/sh\ncat <<'EOF'\n{lines}\nEOF\n")
    fake_mngr.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        "OOM_PRIORITY_RUNTIME_DIR": str(tmp_path / "runtime"),
        "MINDS_SCREEN_SERVICE_URL": "http://127.0.0.1:9",
        markers.MARKER_ROOT_ENV: str(tmp_path / "background_tasks"),
    }

    result = subprocess.run(
        # -S keeps site-packages off the path, so a non-stdlib import fails here as it would under python3.
        [sys.executable, "-S", str(_SCRIPT), "list", "--json"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    document = json.loads(result.stdout)
    assert [(a["name"], a["kind"]) for a in document["agents"]["candidates"]] == [
        ("chat-a", "chat")
    ]
    assert document["browsers"]["candidates"] is None
    assert document["browsers"]["notes"][0].startswith(
        "could not reach the Screen service at http://127.0.0.1:9/browsers"
    )


def test_the_free_command_prints_only_the_free_memory_line() -> None:
    # The worker polls this while memory is short, so it must not run mngr or call the services.
    completed = subprocess.run(
        [sys.executable, str(_SCRIPT), "free"],
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "PATH": "/nonexistent"},
        check=True,
    )
    lines = completed.stdout.splitlines()
    assert len(lines) == 1
    assert lines[0].startswith("Free memory: ")


def _whole_workspace(
    tmp_path: Path, runtime_dir: Path
) -> tuple[Path, workspace_memory.Sources]:
    """A workspace with one of everything that holds memory: an idle chat running a test, a chat
    mid-turn, the services agent, an agent mngr does not list, an app a window shows and one none
    does, the chat app, the Screen's service with a browser nobody is looking at, and an SSH
    session."""
    proc = tmp_path / "proc"
    _write_meminfo(
        proc, {"MemTotal": 4000 * 1024, "MemAvailable": 1000 * 1024, "SwapTotal": 0}
    )
    old = _NOW - timedelta(hours=3)
    idle_chat = _agent_record(
        "trip-plan",
        "WAITING",
        {"user_created": "true", "display_name": "Trip plan"},
        old,
        100,
    )
    busy_chat = _agent_record("budget", "RUNNING", {"user_created": "true"}, old, 110)
    services = _agent_record(
        "system-services", "RUNNING", {"is_primary": "true"}, old, 120
    )
    _fake_process(proc, 1, 1 * 1024, ppid=0, argv=("tini",))
    _fake_process(proc, 2, 20 * 1024, argv=("supervisord",))
    _fake_process(proc, 100, 300 * 1024, argv=("claude",), oom_score_adj=560)
    _fake_process(proc, 101, 100 * 1024, ppid=100, argv=("pytest",), oom_score_adj=900)
    _fake_process(proc, 110, 250 * 1024, argv=("claude",), oom_score_adj=300)
    _fake_process(proc, 120, 40 * 1024, argv=("claude",))
    _fake_process(proc, 130, 60 * 1024, argv=("codex",), oom_score_adj=600)
    _register_pid(runtime_dir, 130, "agent-gone-from-the-listing")
    _fake_process(proc, 200, 150 * 1024, ppid=2, argv=("notes-app",), oom_score_adj=200)
    _fake_process(proc, 210, 90 * 1024, ppid=2, argv=("digest-app",), oom_score_adj=200)
    _fake_process(proc, 220, 200 * 1024, ppid=2, argv=("chat",), oom_score_adj=25)
    _fake_process(proc, 230, 10 * 1024, ppid=2, argv=("screen",), oom_score_adj=850)
    _fake_process(
        proc,
        231,
        400 * 1024,
        ppid=230,
        argv=(
            "tilion",
            f"--user-data-dir={_PROFILE_ROOT}/browser-use-user-data-dir-browser-1",
        ),
        oom_score_adj=910,
    )
    _fake_process(proc, 300, 5 * 1024, argv=("sshd: user@pts/0",), comm="sshd")
    inventory = _inventory(
        {"app": "notes", "path": "/"},
        {"app": "chat", "path": "/"},
        apps=[
            _app("notes", program="notes-app", display_name="Notes"),
            _app("digest", program="digest-app", display_name="Daily Digest"),
            _app("chat", program="chat", critical=True, display_name="Chat"),
            _app("screen", program="screen", display_name="Screen"),
        ],
    )
    return proc, _sources(
        proc,
        _FakeMngr([idle_chat, busy_chat, services]),
        _FakeHttp(
            {
                f"{_SCREEN_URL}/browsers": _fleet(
                    _browser("browser-1", "running", tabs=[("https://a.test/", True)])
                ),
                f"{_SHELL_URL}/api/inventory": inventory,
            }
        ),
        supervised_pids={
            "notes-app": 200,
            "digest-app": 210,
            "chat": 220,
            "screen": 230,
        },
        scratch_kib=50 * 1024,
        earlyoom_program_conf=_write_earlyoom_program(
            tmp_path / "earlyoom.conf",
            '/usr/local/bin/earlyoom -m 10,5 --avoid "^(sshd|supervisord|tini)$|^tmux"',
        ),
    )


def test_the_list_holds_everything_using_memory_and_adds_up_to_what_is_in_use(
    tmp_path: Path, runtime_dir: Path
) -> None:
    _, sources = _whole_workspace(tmp_path, runtime_dir)

    report = workspace_memory.collect_report(sources)

    rows = workspace_memory.report_rows(report)
    assert [
        (row.kind, row.name, row.anonymous_kib // 1024, row.can_stop.value)
        for row in rows
        if row.anonymous_kib is not None
    ] == [
        ("chat", "trip-plan", 400, "yes"),
        ("browser", "browser-1", 400, "yes"),
        ("chat", "budget", 250, "no"),
        ("app", "chat", 200, "no"),
        ("app", "notes-app", 150, "on screen"),
        ("app", "digest-app", 90, "yes"),
        ("agent", "agent-gone-from-the-listing", 60, "no"),
        ("services agent", "system-services", 40, "no"),
        ("unowned", "Internal system", 21, "no"),
        ("app", "screen", 10, "no"),
        ("unowned", "SSH access", 5, "no"),
    ]
    by_name = {row.name: row.detail for row in rows}
    assert by_name["system-services"] == "the workspace's own services agent"
    assert (
        by_name["agent-gone-from-the-listing"]
        == "not in mngr's listing of this workspace's agents"
    )
    # 3000 MiB in use: 1626 held by processes, none shared, 50 in /tmp, and the rest the system's.
    assert report.totals == workspace_memory.Totals(
        in_use_kib=3000 * 1024,
        shared_kib=0,
        temporary_files_kib=50 * 1024,
        system_kib=1324 * 1024,
    )
    printed = _cells(workspace_memory.render_everything(report))
    assert [
        "-",
        "System",
        "-",
        "1324 MiB",
        "-",
        "the rest of what is in use: the kernel, and what cannot be read",
    ] in printed
    assert ["In use: 3000 MiB"] in printed


def test_what_update_self_reads_off_the_json_is_all_there(
    tmp_path: Path, runtime_dir: Path
) -> None:
    """update-self's staged copy reads this command's JSON from whatever release the workspace is
    on, so each key it takes must keep its name: a renamed one would read as unknown there."""
    _, sources = _whole_workspace(tmp_path, runtime_dir)
    document = json.loads(
        json.dumps(
            workspace_memory.report_to_json(workspace_memory.collect_report(sources))
        )
    )

    reading = update_memory.parse_listing(update_memory.WORKSPACE_MEMORY_REL, document)

    assert reading.unknown == ()
    assert not reading.is_resident_size
    assert reading.free == update_memory.FreeMemory(
        free_kib=1000 * 1024,
        field="MemAvailable",
        source=str(sources.proc_dir / "meminfo"),
        room_kib=600 * 1024,
    )
    assert reading.agents == (
        update_memory.AgentRow(
            kind="chat",
            name="trip-plan",
            title="Trip plan",
            idle_seconds=3 * 3600.0,
            memory_kib=400 * 1024,
        ),
    )
    assert reading.apps == (
        update_memory.AppRow("notes-app", "Notes", 1, False, 150 * 1024),
        update_memory.AppRow("digest-app", "Daily Digest", 0, False, 90 * 1024),
    )
    assert reading.browsers == (update_memory.BrowserRow("browser-1", 400 * 1024),)


def test_show_prints_one_row_with_its_processes_or_one_process_with_its_row(
    tmp_path: Path, runtime_dir: Path
) -> None:
    proc, sources = _whole_workspace(tmp_path, runtime_dir)
    report = workspace_memory.collect_report(sources)

    by_title, is_found = workspace_memory.render_show(report, "trip plan", proc)

    assert is_found
    assert by_title.splitlines()[:3] == [
        "chat trip-plan (Trip plan)",
        "  Memory: 400 MiB",
        "  Can stop: yes (idle 3h 00m)",
    ]
    assert _cells(by_title)[-2:] == [
        ["100", "300 MiB", "claude"],
        ["101", "100 MiB", "pytest"],
    ]

    by_pid, is_found = workspace_memory.render_show(report, "101", proc)

    assert is_found
    assert by_pid.splitlines() == [
        "Process 101: pytest",
        "  Memory: 100 MiB (its own anonymous memory; resident size 300 MiB)",
        "  Band: 900 (oom_score_adj; a higher band is stopped sooner)",
        "  Belongs to: chat trip-plan (Trip plan); can stop: yes (idle 3h 00m)",
    ]

    # An agent's command line carries its whole prompt, so a command is printed on one line, cut short.
    report.processes[100].argv = [
        "claude",
        "--append-system-prompt",
        "Line one.\n\nLine two. " * 40,
    ]
    prompted, _ = workspace_memory.render_show(report, "trip-plan", proc)
    assert len(prompted.splitlines()) == 7
    pid, memory, command = _cells(prompted)[5]
    assert (pid, memory, len(command)) == ("100", "300 MiB", 100)
    assert command.startswith(
        "claude --append-system-prompt Line one. Line two. Line one."
    )
    assert command.endswith("...")


def test_show_asks_for_one_name_when_several_rows_match_and_says_when_none_does(
    tmp_path: Path, runtime_dir: Path
) -> None:
    proc, sources = _whole_workspace(tmp_path, runtime_dir)
    report = workspace_memory.collect_report(sources)

    # "chat" is the chat app's program and its title, so that one row is meant, not every chat.
    exact, is_found = workspace_memory.render_show(report, "Chat", proc)
    assert is_found and exact.splitlines()[0] == "app chat (Chat)"

    # An agent's id finds it whether or not it could be stopped: "budget" is mid-turn.
    mngr = sources.run_command
    assert isinstance(mngr, _FakeMngr)
    rows = workspace_memory.report_rows(report)
    for record in mngr.records:
        assert [row.name for row in workspace_memory.find_rows(rows, record["id"])] == [
            record["name"]
        ]

    several, is_found = workspace_memory.render_show(report, "app", proc)
    assert not is_found
    assert {tuple(row[:2]) for row in _cells(several)[2:]} == {
        ("app", "notes-app"),
        ("app", "digest-app"),
    }

    for missing in ("no-such-thing", "99999"):
        _, is_found = workspace_memory.render_show(report, missing, proc)
        assert not is_found


def test_the_guard_order_is_the_memory_guards_own_score(
    tmp_path: Path, runtime_dir: Path
) -> None:
    proc, sources = _whole_workspace(tmp_path, runtime_dir)
    report = workspace_memory.collect_report(sources)
    avoid = workspace_memory.read_avoid_pattern(sources.earlyoom_program_conf)

    order = workspace_memory.read_guard_order(proc, report.processes, avoid)

    # One band point is a thousandth of the 4000 MiB total. No status here carries RssAnon, as
    # under gVisor, so each process counts its anonymous memory, not the VmRSS three times it.
    point_kib = 4000 * 1024 // 1000
    assert (order.band_point_kib, order.counts_anonymous, order.error) == (
        point_kib,
        True,
        None,
    )
    expected = {
        231: 400 * 1024 + 4 + 910 * point_kib,
        101: 100 * 1024 + 4 + 900 * point_kib,
        230: 10 * 1024 + 4 + 850 * point_kib,
        # The idle chat goes before the agent a band above it: its memory outweighs 40 band points.
        100: 300 * 1024 + 4 + 560 * point_kib,
        130: 60 * 1024 + 4 + 600 * point_kib,
        110: 250 * 1024 + 4 + 300 * point_kib,
        200: 150 * 1024 + 4 + 200 * point_kib,
        210: 90 * 1024 + 4 + 200 * point_kib,
        220: 200 * 1024 + 4 + 25 * point_kib,
        120: 40 * 1024 + 4,
        # Names --avoid shields score 300 band points lower; pid 1 is never scored at all.
        2: 20 * 1024 + 4 - 300 * point_kib,
        300: 5 * 1024 + 4 - 300 * point_kib,
    }
    assert [(score.pid, score.score_kib) for score in order.scores] == list(
        expected.items()
    )
    printed = _cells(
        workspace_memory.render_guard_order(
            order, workspace_memory.report_rows(report), limit=2
        )
    )
    assert printed[2:4] == [
        ["231", "tilion", "browser https://a.test/", "910", "400 MiB", "4040 MiB"],
        ["101", "pytest", "chat Trip plan", "900", "100 MiB", "3700 MiB"],
    ]
    assert printed[4] == ["... and 10 more; `guard-order --all` prints every one."]


def test_on_a_linux_kernel_the_guard_counts_resident_size_and_skips_what_it_never_stops(
    tmp_path: Path,
) -> None:
    proc = tmp_path / "proc"
    _write_meminfo(proc, {"MemTotal": 1000 * 1000, "SwapTotal": 1000 * 1000})
    _fake_process(proc, 10, 100, oom_score_adj=10)
    _fake_process(proc, 11, 100, oom_score_adj=-1000)
    _fake_process(proc, 12, 50, oom_score_adj=10)
    for pid in (10, 12):
        with (proc / str(pid) / "status").open("a") as status:
            status.write("RssAnon:\t   1 kB\n")
    # A kernel thread has no memory of its own, and so no VmRSS line.
    write_fake_process(proc, 13, start_ticks=13, comm="kthreadd")
    (proc / "13" / "cmdline").write_bytes(b"")

    order = workspace_memory.read_guard_order(
        proc, workspace_memory.read_process_table(proc, {}, {}).processes, None
    )

    # VmRSS (three times the anonymous memory here) plus the page tables, and ten band points of
    # memory and swap together.
    assert not order.counts_anonymous
    assert [(score.pid, score.score_kib) for score in order.scores] == [
        (10, 300 + 4 + 10 * 2000),
        (12, 150 + 4 + 10 * 2000),
    ]


def test_the_guards_avoided_names_are_read_off_the_workspaces_own_earlyoom_program() -> (
    None
):
    avoid = workspace_memory.read_avoid_pattern(workspace_memory.EARLYOOM_PROGRAM_CONF)

    assert avoid is not None
    assert [
        bool(avoid.search(name)) for name in ("sshd", "tmux: server", "claude")
    ] == [
        True,
        True,
        False,
    ]


def test_what_a_source_would_have_decided_is_unknown_while_its_memory_is_still_listed(
    tmp_path: Path, runtime_dir: Path
) -> None:
    _, sources = _whole_workspace(tmp_path, runtime_dir)
    unreachable = urllib.error.URLError("timed out")

    report = workspace_memory.collect_report(
        sources._replace(
            run_command=_FakeMngr(
                [], error=FileNotFoundError(2, "No such file", "mngr")
            ),
            fetch_json=_FakeHttp(
                {
                    f"{_SCREEN_URL}/browsers": unreachable,
                    f"{_SHELL_URL}/api/inventory": unreachable,
                }
            ),
        )
    )

    assert (
        report.agents.candidates,
        report.apps.candidates,
        report.browsers.candidates,
    ) == (None, None, None)
    rows = workspace_memory.report_rows(report)
    assert {
        (row.kind, row.name, row.can_stop.value)
        for row in rows
        if row.kind != "unowned"
    } == {
        # Without mngr's listing only the registered agent is known to be one; the other chats'
        # processes are counted with what nothing started.
        ("agent", "agent-gone-from-the-listing", "unknown"),
        ("service", "notes-app", "unknown"),
        ("service", "digest-app", "unknown"),
        ("service", "chat", "unknown"),
        ("service", "screen", "unknown"),
        ("browser", "browser-1", "unknown"),
    }
    assert sum(row.anonymous_kib or 0 for row in rows) == 1626 * 1024
