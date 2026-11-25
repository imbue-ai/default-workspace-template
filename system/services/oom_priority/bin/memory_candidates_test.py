"""Tests for the memory candidates command, against a fake ``/proc``, a fake ``mngr list`` and fake
Screen-service and shell answers (all injected through ``Sources``)."""

import json
import os
import string
import subprocess
import sys
import urllib.error
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import memory_candidates
import pytest
from chat_background_tasks import markers
from chat_background_tasks.testing import runner_task
from imbue.chat.config import DEFAULT_CHAT_DATA_DIR
from imbue.chat.main_chat import MAIN_CHAT_FILENAME, MainChatStore
from imbue.chat.primitives import ChatId
from imbue.mngr.cli.field_catalog import FieldContext, build_list_field_catalog
from mngr_cli_contract.contract import assert_mngr_argv_valid
from oom_priority.meminfo import HOST_MEMINFO_MAX_AGE_SECONDS
from oom_priority.testing import FAKE_BOOT_ID, write_fake_process

_SCRIPT = Path(__file__).parent / "memory_candidates.py"

_NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
_SCREEN_URL = "http://screen.test"
_SHELL_URL = "http://shell.test"
_PROFILE_ROOT = "/home/user/.mngr/browser-profiles"


def _fake_process(
    proc_dir: Path,
    pid: int,
    anonymous_kib: int,
    children: Sequence[int] = (),
    argv: Sequence[str] = ("python3",),
) -> None:
    """One process in the fake ``/proc``, started at tick ``pid``: its ``smaps`` (two mappings whose
    anonymous memory sums to ``anonymous_kib``), its status (a VmRSS three times that, as gVisor
    inflates it), children, and argv."""
    write_fake_process(proc_dir, pid, start_ticks=pid, comm=Path(argv[0]).name)
    task_dir = proc_dir / str(pid) / "task" / str(pid)
    task_dir.mkdir(parents=True)
    (task_dir / "children").write_text(" ".join(str(child) for child in children) + " ")
    (proc_dir / str(pid) / "status").write_text(
        f"Name:\t{Path(argv[0]).name}\nTgid:\t{pid}\nVmRSS:\t   {anonymous_kib * 3} kB\n"
    )
    first = anonymous_kib // 2
    (proc_dir / str(pid) / "smaps").write_text(
        f"00400000-00500000 r-xp 00000000 00:00 0\nRss:   {anonymous_kib} kB\nAnonymous:   {first} kB\n"
        f"00600000-00700000 rw-p 00000000 00:00 0\nRss:   {anonymous_kib} kB\n"
        f"Anonymous:   {anonymous_kib - first} kB\n"
    )
    (proc_dir / str(pid) / "cmdline").write_bytes("\0".join(argv).encode() + b"\0")


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
) -> memory_candidates.Sources:
    return memory_candidates.Sources(
        proc_dir=proc_dir,
        host_meminfo=host_meminfo if host_meminfo is not None else proc_dir / "absent",
        earlyoom_program_conf=earlyoom_program_conf
        if earlyoom_program_conf is not None
        else proc_dir / "absent-earlyoom.conf",
        run_command=mngr,
        fetch_json=http,
        read_supervised_pids=lambda: dict(supervised_pids or {}),
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
    _fake_process(proc, 100, 1000, children=[101])
    _fake_process(proc, 101, 500)
    _fake_process(proc, 150, 700)
    _register_pid(runtime_dir, 150, str(chat["id"]))
    _register_pid(runtime_dir, 151, str(chat["id"]))  # exited: no /proc entry
    _fake_process(proc, 200, 300)

    report = memory_candidates.collect_report(
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
    table = memory_candidates.render_table(report)
    assert (
        f"Free memory: 300 MiB (MemAvailable from {proc / 'meminfo'}; MemTotal 4096 MiB)"
        in table
    )
    assert "chat    chat-a     Trip plan  2h 05m  2026-09-24 09:55" in table


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

    report = memory_candidates.collect_report(
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
    table = memory_candidates.render_table(report)
    assert (
        "(waiting, not on a background task, no activity for 15m or more):\n  none"
        in table
    )
    assert "Browsers running while no window shows the Screen:\n  none" in table


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
    is_chat_busy = memory_candidates.busy_chat_check(
        {markers.MARKER_ROOT_ENV: str(root)}
    )
    mngr = _FakeMngr([lead, successor, worker, finished])

    report = memory_candidates.collect_report(
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

    report = memory_candidates.collect_report(
        _sources(
            proc,
            _FakeMngr([main_chat, other_chat]),
            _FakeHttp({f"{_SCREEN_URL}/browsers": _fleet()}),
            main_chat_file=tmp_path / memory_candidates.MAIN_CHAT_FILE,
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

    report = memory_candidates.collect_report(
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
        children=[301],
        argv=(
            "tilion",
            f"--user-data-dir={_PROFILE_ROOT}/browser-use-user-data-dir-browser-1",
        ),
    )
    _fake_process(proc, 301, 600, argv=("tilion", "--type=renderer"))
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

    report = memory_candidates.collect_report(
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
    _fake_process(proc, 200, 30 * 1024, children=[210, 300])
    _fake_process(proc, 210, 10 * 1024)
    _fake_process(
        proc,
        300,
        500 * 1024,
        children=[301],
        argv=(
            "tilion",
            f"--user-data-dir={_PROFILE_ROOT}/browser-use-user-data-dir-browser-1",
        ),
    )
    _fake_process(proc, 301, 200 * 1024, argv=("tilion", "--type=renderer"))

    report = memory_candidates.collect_report(
        _sources(proc, _FakeMngr([]), http, supervised_pids={"screen": 200})
    )

    assert report.browsers.candidates == ()
    # Not a candidate, but still the answer to "how much is the browser using".
    assert report.browsers.others == (
        memory_candidates.HeldEntry(
            kind="browser",
            name="browser-1",
            title="https://a.test/",
            why="a window shows the Screen it runs on",
            pids=(300,),
            anonymous_kib=700 * 1024,
        ),
    )
    # The Screen's own row leaves its browser out, so the browser is not counted twice.
    assert report.apps.others is not None
    assert [(e.name, e.anonymous_kib) for e in report.apps.others] == [
        ("screen", 40 * 1024)
    ]
    table = memory_candidates.render_table(report)
    assert (
        "  browser  browser-1  https://a.test/  a window shows the Screen it runs on"
        "                    700 MiB\n"
        "  app      screen     screen           the Screen's service: its browser is listed on its own"
        "  40 MiB\n" in table
    )


def test_lists_the_apps_the_workspace_may_stop_with_how_many_windows_show_them(
    tmp_path: Path, runtime_dir: Path
) -> None:
    proc = tmp_path / "proc"
    _fake_process(proc, 400, 1024, children=[401])
    _fake_process(proc, 401, 2048)
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

    report = memory_candidates.collect_report(
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
    # The table carries the auto-stop field too: an app the shell does not stop by itself may be
    # doing work between requests, which is what a reader ranks the candidates by.
    table = memory_candidates.render_table(report)
    assert (
        "daily-digest  Daily Digest  daily-digest  2        no         3 MiB" in table
    )
    assert (
        "lineup-scout  lineup-scout  lineup-scout  0        yes        0 MiB" in table
    )


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

    report = memory_candidates.collect_report(
        _sources(proc, _FakeMngr([]), http, supervised_pids={"terminal": 500})
    )

    assert report.apps.candidates == ()


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

    report = memory_candidates.collect_report(
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

    report = memory_candidates.collect_report(
        _sources(
            proc,
            _FakeMngr(
                [], error=FileNotFoundError(2, "No such file or directory", "mngr")
            ),
            http,
        )
    )

    assert report.agents.candidates is None
    # Unknown, not "nothing else holds memory" either.
    assert report.agents.others is None
    assert len(report.agents.notes) == 1
    assert report.agents.notes[0].startswith("could not run `mngr list`: ")
    assert report.browsers.candidates is not None
    assert [b.name for b in report.browsers.candidates] == ["browser-1"]
    table = memory_candidates.render_table(report)
    assert (
        "Idle chats and workers (waiting, not on a background task, no activity for 15m or more):\n  unknown\n  note: could not run"
        in table
    )
    assert (
        "Also holding memory, not candidates:\n  unknown\n"
        "  note: which chats and workers hold memory is unknown (see the note above)\n"
        "  note: which apps and services hold memory is unknown (see the note above)\n"
        in table
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

    report = memory_candidates.collect_report(
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

    report = memory_candidates.collect_report(
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

    report = memory_candidates.collect_report(
        _sources(proc, mngr, _FakeHttp({f"{_SCREEN_URL}/browsers": _fleet()}))
    )

    assert report.agents.candidates is None
    assert len(report.agents.notes) == 1
    assert report.agents.notes[0].startswith(
        "1 line(s) of `mngr list` output did not parse as an agent"
    )


def test_the_listing_argv_and_every_template_field_exist_in_the_pinned_mngr() -> None:
    assert_mngr_argv_valid(memory_candidates.MNGR_LIST_ARGV)
    template_fields = {
        row.key.replace("$KEY", "")
        for row in build_list_field_catalog()
        if FieldContext.TEMPLATE in row.contexts
    }
    for field in memory_candidates.MNGR_LIST_FIELDS:
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

    report = memory_candidates.collect_report(_sources(proc, _FakeMngr([chat]), http))

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
    fleet = _fleet(_browser("browser-1", "running"))
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

        report = memory_candidates.collect_report(_sources(proc, _FakeMngr([]), http))

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
    free = memory_candidates.read_free_memory(
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

    fresh = memory_candidates.read_free_memory(
        proc / "meminfo", host, _NOW, shed_percent=None
    )
    assert fresh is not None
    assert (fresh.source, fresh.free_kib) == (str(host), 1000)

    # earlyoom ignores the file once it is stale, and so must this: a publisher that died would
    # otherwise freeze the reported headroom at whatever it last wrote.
    stale = _NOW + timedelta(seconds=HOST_MEMINFO_MAX_AGE_SECONDS + 1)
    fallen_back = memory_candidates.read_free_memory(
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

    free = memory_candidates.read_free_memory(
        tmp_path / "meminfo",
        tmp_path / "absent-host-meminfo",
        _NOW,
        shed_percent=memory_candidates.read_shed_percent(program),
    )

    assert memory_candidates.render_free_memory(free) == (
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

    free = memory_candidates.read_free_memory(
        tmp_path / "meminfo",
        tmp_path / "absent-host-meminfo",
        _NOW,
        shed_percent=memory_candidates.read_shed_percent(program),
    )

    assert "memory guard" not in memory_candidates.render_free_memory(free)


def test_the_guard_threshold_is_read_off_the_workspaces_own_earlyoom_program() -> None:
    assert (
        memory_candidates.read_shed_percent(memory_candidates.EARLYOOM_PROGRAM_CONF)
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

    report = memory_candidates.collect_report(
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
    document = json.loads(json.dumps(memory_candidates.report_to_json(report)))

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
        _render_like_mngr(memory_candidates.MNGR_LIST_ARGV[-1], r)
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
        [sys.executable, "-S", str(_SCRIPT), "--json"],
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


def test_the_free_memory_flag_prints_only_the_free_memory_line() -> None:
    # The worker polls this while memory is short, so it must not run mngr or call the services.
    completed = subprocess.run(
        [sys.executable, str(_SCRIPT), "--free-memory"],
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "PATH": "/nonexistent"},
        check=True,
    )
    lines = completed.stdout.splitlines()
    assert len(lines) == 1
    assert lines[0].startswith("Free memory: ")
