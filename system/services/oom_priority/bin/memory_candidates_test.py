"""Tests for the memory candidates command, against a fake ``/proc``, a fake ``mngr list`` and fake
browser-service and shell answers (all injected through ``Sources``)."""

import importlib.util
import json
import os
import string
import subprocess
import sys
import urllib.error
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from imbue.mngr.cli.field_catalog import FieldContext, build_list_field_catalog
from mngr_cli_contract.contract import assert_mngr_argv_valid

_SCRIPT = Path(__file__).parent / "memory_candidates.py"
_spec = importlib.util.spec_from_file_location("memory_candidates", _SCRIPT)
assert _spec is not None and _spec.loader is not None
memory_candidates = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(memory_candidates)

_NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
_BROWSER_URL = "http://browser.test"
_SHELL_URL = "http://shell.test"
_PROFILE_ROOT = "/home/user/.mngr/browser-profiles"


def _fake_process(
    proc_dir: Path, pid: int, rss_kib: int, children: Sequence[int] = (), argv: Sequence[str] = ("python3",)
) -> None:
    """One process in the fake ``/proc``: its status (VmRSS), children, and argv."""
    task_dir = proc_dir / str(pid) / "task" / str(pid)
    task_dir.mkdir(parents=True)
    (task_dir / "children").write_text(" ".join(str(child) for child in children) + " ")
    (proc_dir / str(pid) / "status").write_text(f"Name:\t{Path(argv[0]).name}\nVmRSS:\t   {rss_kib} kB\n")
    (proc_dir / str(pid) / "cmdline").write_bytes("\0".join(argv).encode() + b"\0")


def _write_meminfo(proc_dir: Path, fields: Mapping[str, int]) -> None:
    proc_dir.mkdir(parents=True, exist_ok=True)
    (proc_dir / "meminfo").write_text("".join(f"{key}:{value:>12} kB\n" for key, value in fields.items()))


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
    directory = runtime_dir / "agent_pids"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{pid}.json").write_text(json.dumps({"agent_name": "x", "is_worker": False, "agent_id": agent_id}))


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

    def __call__(self, argv: Sequence[str], timeout_seconds: float) -> "subprocess.CompletedProcess[str]":
        if self.error is not None:
            raise self.error
        options = dict(zip(argv[2::2], argv[3::2]))
        provider = options.get("--provider")
        listed = [r for r in self.records if provider is None or r["host"]["provider_name"] == provider]
        rendered = [_render_like_mngr(options["--format"], record) for record in listed]
        stdout = "".join(f"{line}\n" for line in [*rendered, *self.extra_lines])
        return subprocess.CompletedProcess(list(argv), self.returncode, stdout=stdout, stderr=self.stderr)


class _FakeHttp:
    """Answers each URL with a JSON payload, or raises the exception given for it."""

    def __init__(self, answers: Mapping[str, object]) -> None:
        self.answers = answers

    def __call__(self, url: str, timeout_seconds: float) -> object:
        answer = self.answers[url]
        if isinstance(answer, Exception):
            raise answer
        return answer


def _fleet(*browsers: Mapping[str, object]) -> dict[str, object]:
    return {"browsers": list(browsers), "can_create": True, "browser_count": len(browsers), "browser_max": 1}


def _browser(name: str, lifecycle: str, tabs: Sequence[tuple[str, bool]] = ()) -> dict[str, object]:
    return {
        "id": name,
        "lifecycle": lifecycle,
        "controller": None,
        "owner_agent_id": None,
        "owner_name": None,
        "human_pinned": False,
        "waiting": [],
        "crashed": False,
        "tabs": [{"index": i, "title": "", "url": url, "active": active} for i, (url, active) in enumerate(tabs)],
    }


def _desktops(*windows: Mapping[str, object]) -> dict[str, object]:
    return {"desktops": [{"id": "desktop-1", "windows": list(windows)}]}


def _sources(proc_dir: Path, mngr: _FakeMngr, http: _FakeHttp) -> object:
    return memory_candidates.Sources(
        proc_dir=proc_dir,
        run_command=mngr,
        fetch_json=http,
        browser_service_url=_BROWSER_URL,
        shell_url=_SHELL_URL,
        now=_NOW,
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
    _write_meminfo(proc, {"MemTotal": 4 * 1024 * 1024, "MemFree": 300 * 1024, "MemAvailable": 300 * 1024})
    chat_activity = _NOW - timedelta(hours=2, minutes=5)
    chat = _agent_record("chat-a", "WAITING", {"user_created": "true", "display_name": "Trip plan"}, chat_activity, 100)
    worker = _agent_record("fix-login", "WAITING", {"agent_created": "true"}, _NOW - timedelta(minutes=30), 200)
    # The chat's harness runs a second registered process (codex's app-server) mngr does not report.
    _fake_process(proc, 100, 1000, children=[101])
    _fake_process(proc, 101, 500)
    _fake_process(proc, 150, 700)
    _register_pid(runtime_dir, 150, str(chat["id"]))
    _register_pid(runtime_dir, 151, str(chat["id"]))  # exited: no /proc entry
    _fake_process(proc, 200, 300)

    report = memory_candidates.collect_report(
        _sources(proc, _FakeMngr([chat, worker]), _FakeHttp({f"{_BROWSER_URL}/browsers": _fleet()}))
    )

    agents = report.agents.candidates
    assert [(a.kind, a.name, a.display_name) for a in agents] == [
        ("chat", "chat-a", "Trip plan"),
        ("worker", "fix-login", None),
    ]
    assert agents[0].last_activity == chat_activity
    assert agents[0].pids == (100, 150)
    assert agents[0].rss_kib == 2200
    assert agents[1].rss_kib == 300
    assert report.agents.notes == ()
    table = memory_candidates.render_table(report)
    assert "Free memory: 300 MiB (MemAvailable from /proc/meminfo; MemTotal 4096 MiB)" in table
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
        _agent_record("just-answered", "WAITING", {"user_created": "true"}, _NOW - timedelta(minutes=5), 11),
        _agent_record("services", "WAITING", {"is_primary": "true", "user_created": "true"}, old, 12),
        _agent_record("caretaker", "WAITING", {"automation": "caretaker"}, old, 13),
        _agent_record("stopped-chat", "STOPPED", {"user_created": "true"}, old),
        _agent_record("done-worker", "DONE", {"agent_created": "true"}, old),
        _agent_record("remote-chat", "WAITING", {"user_created": "true"}, old, 14, provider="modal"),
    ]

    report = memory_candidates.collect_report(
        _sources(proc, _FakeMngr(records), _FakeHttp({f"{_BROWSER_URL}/browsers": _fleet()}))
    )

    assert report.agents.candidates == ()
    assert report.browsers.candidates == ()
    table = memory_candidates.render_table(report)
    assert "(waiting, no activity for 15m or more):\n  none" in table
    assert "Browsers no window shows (running):\n  none" in table


def test_lists_running_browsers_no_window_shows_with_their_chromium_memory(tmp_path: Path, runtime_dir: Path) -> None:
    proc = tmp_path / "proc"
    # browser-1's Chromium (a renderer beneath it); browser-10 is shown, so its match must not leak into browser-1.
    _fake_process(proc, 300, 400, children=[301], argv=("tilion", f"--user-data-dir={_PROFILE_ROOT}/browser-use-user-data-dir-browser-1"))
    _fake_process(proc, 301, 600, argv=("tilion", "--type=renderer"))
    _fake_process(proc, 310, 900, argv=("tilion", f"--user-data-dir={_PROFILE_ROOT}/browser-use-user-data-dir-browser-10"))
    fleet = _fleet(
        _browser("browser-1", "running", tabs=[("https://a.test/", False), ("https://b.test/", True)]),
        _browser("browser-10", "running", tabs=[("https://c.test/", True)]),
        _browser("browser-2", "stopped"),
        _browser("browser-3", "init"),
    )
    # browser-10 is shown only in one client's own path of a window whose home path names no browser.
    desktops = _desktops(
        {"app": "browser", "path": "/new", "client_paths": {"client-1": "/?session=browser-10"}},
        {"app": "chat", "path": "/?session=browser-1"},
    )

    report = memory_candidates.collect_report(
        _sources(
            proc,
            _FakeMngr([]),
            _FakeHttp({f"{_BROWSER_URL}/browsers": fleet, f"{_SHELL_URL}/api/desktops": desktops}),
        )
    )

    browsers = report.browsers.candidates
    assert [(b.name, b.controller, b.tab_count, b.active_url) for b in browsers] == [
        ("browser-1", "free", 2, "https://b.test/")
    ]
    assert browsers[0].pids == (300,)
    assert browsers[0].rss_kib == 1000


def test_a_failed_mngr_list_reports_agents_as_unknown_and_still_lists_browsers(tmp_path: Path, runtime_dir: Path) -> None:
    proc = tmp_path / "proc"
    fleet = _fleet(_browser("browser-1", "running"))
    http = _FakeHttp({f"{_BROWSER_URL}/browsers": fleet, f"{_SHELL_URL}/api/desktops": _desktops()})

    report = memory_candidates.collect_report(
        _sources(proc, _FakeMngr([], error=FileNotFoundError(2, "No such file or directory", "mngr")), http)
    )

    assert report.agents.candidates is None
    assert len(report.agents.notes) == 1
    assert report.agents.notes[0].startswith("could not run `mngr list`: ")
    assert [b.name for b in report.browsers.candidates] == ["browser-1"]
    assert "Idle chats and workers (waiting, no activity for 15m or more):\n  unknown\n  note: could not run" in (
        memory_candidates.render_table(report)
    )


def test_a_listing_error_keeps_the_agents_mngr_did_list_and_notes_the_error(tmp_path: Path, runtime_dir: Path) -> None:
    proc = tmp_path / "proc"
    chat = _agent_record("chat-a", "WAITING", {"user_created": "true"}, _NOW - timedelta(hours=1), 100)
    mngr = _FakeMngr([chat], returncode=1, stderr="Errors while listing:\n  host-2: ssh timed out\n")

    report = memory_candidates.collect_report(_sources(proc, mngr, _FakeHttp({f"{_BROWSER_URL}/browsers": _fleet()})))

    assert [a.name for a in report.agents.candidates] == ["chat-a"]
    assert report.agents.notes == ("mngr list exited 1: Errors while listing:; host-2: ssh timed out",)


def test_a_listing_that_fails_with_no_agents_is_unknown_not_empty(tmp_path: Path, runtime_dir: Path) -> None:
    proc = tmp_path / "proc"
    mngr = _FakeMngr([], returncode=1, stderr="Error: cannot load the local provider\n")

    report = memory_candidates.collect_report(_sources(proc, mngr, _FakeHttp({f"{_BROWSER_URL}/browsers": _fleet()})))

    assert report.agents.candidates is None
    assert report.agents.notes == ("mngr list exited 1: Error: cannot load the local provider",)


def test_a_listing_none_of_whose_lines_parse_is_unknown_not_empty(tmp_path: Path, runtime_dir: Path) -> None:
    proc = tmp_path / "proc"
    mngr = _FakeMngr([], extra_lines=["NAME  STATE  (an output shape this command does not read)"])

    report = memory_candidates.collect_report(_sources(proc, mngr, _FakeHttp({f"{_BROWSER_URL}/browsers": _fleet()})))

    assert report.agents.candidates is None
    assert len(report.agents.notes) == 1
    assert report.agents.notes[0].startswith("1 line(s) of `mngr list` output did not parse as an agent")


def test_the_listing_argv_and_every_template_field_exist_in_the_pinned_mngr() -> None:
    assert_mngr_argv_valid(memory_candidates.MNGR_LIST_ARGV)
    template_fields = {
        row.key.replace("$KEY", "") for row in build_list_field_catalog() if FieldContext.TEMPLATE in row.contexts
    }
    for field in memory_candidates.MNGR_LIST_FIELDS:
        label_prefix = "labels."
        assert (field if not field.startswith(label_prefix) else label_prefix) in template_fields, field


def test_an_unreachable_browser_service_reports_browsers_as_unknown_and_still_lists_agents(
    tmp_path: Path, runtime_dir: Path
) -> None:
    proc = tmp_path / "proc"
    chat = _agent_record("chat-a", "WAITING", {"user_created": "true"}, _NOW - timedelta(hours=1), 100)
    http = _FakeHttp({f"{_BROWSER_URL}/browsers": urllib.error.URLError(ConnectionRefusedError(61, "Connection refused"))})

    report = memory_candidates.collect_report(_sources(proc, _FakeMngr([chat]), http))

    assert report.browsers.candidates is None
    assert report.browsers.notes[0].startswith(f"could not reach the browser service at {_BROWSER_URL}/browsers:")
    assert [a.name for a in report.agents.candidates] == ["chat-a"]


def test_an_unreadable_shell_leaves_browsers_unknown_rather_than_calling_them_unviewed(
    tmp_path: Path, runtime_dir: Path
) -> None:
    proc = tmp_path / "proc"
    fleet = _fleet(_browser("browser-1", "running"))
    malformed_browser_window = _desktops({"app": "browser", "path": None, "client_paths": {"c": "/?session=browser-1"}})
    for desktops_answer in (urllib.error.URLError("timed out"), {"unexpected": "shape"}, malformed_browser_window):
        http = _FakeHttp({f"{_BROWSER_URL}/browsers": fleet, f"{_SHELL_URL}/api/desktops": desktops_answer})

        report = memory_candidates.collect_report(_sources(proc, _FakeMngr([]), http))

        assert report.browsers.candidates is None
        assert "so which browsers no window shows is unknown" in report.browsers.notes[0]


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"MemTotal": 8192, "MemFree": 1024, "MemAvailable": 2048}, ("MemAvailable", 2048, 8192)),
        ({"MemTotal": 8192, "MemFree": 1024}, ("MemFree", 1024, 8192)),
    ],
)
def test_free_memory_names_the_meminfo_field_it_read(
    tmp_path: Path, fields: Mapping[str, int], expected: tuple[str, int, int]
) -> None:
    _write_meminfo(tmp_path, fields)
    free = memory_candidates.read_free_memory(tmp_path / "meminfo")
    assert free is not None
    assert (free.field, free.free_kib, free.total_kib) == expected


def test_json_report_shape(tmp_path: Path, runtime_dir: Path) -> None:
    proc = tmp_path / "proc"
    _write_meminfo(proc, {"MemTotal": 4096, "MemAvailable": 1024})
    chat = _agent_record("chat-a", "WAITING", {"user_created": "true"}, _NOW - timedelta(hours=1), 100)
    _fake_process(proc, 100, 2048)
    _fake_process(proc, 300, 512, argv=("tilion", f"--user-data-dir={_PROFILE_ROOT}/browser-use-user-data-dir-browser-1"))
    browser = _browser("browser-1", "running", tabs=[("https://a.test/", True)])
    browser.update({"controller": "agent", "owner_name": "chat-b"})
    http = _FakeHttp({f"{_BROWSER_URL}/browsers": _fleet(browser), f"{_SHELL_URL}/api/desktops": _desktops()})

    report = memory_candidates.collect_report(_sources(proc, _FakeMngr([chat]), http))
    document = json.loads(json.dumps(memory_candidates.report_to_json(report)))

    assert document == {
        "generated_at": "2026-09-24T12:00:00Z",
        "idle_after_seconds": 900.0,
        "free_memory": {"field": "MemAvailable", "free_kib": 1024, "total_kib": 4096, "source": "/proc/meminfo"},
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
                    "rss_kib": 2048,
                }
            ],
            "notes": [],
        },
        "browsers": {
            "candidates": [
                {
                    "name": "browser-1",
                    "controller": "agent chat-b",
                    "tab_count": 1,
                    "active_url": "https://a.test/",
                    "pids": [300],
                    "rss_kib": 512,
                }
            ],
            "notes": [],
        },
    }


def test_the_script_runs_under_a_plain_python3_and_prints_json(tmp_path: Path) -> None:
    """End to end through the real wiring: a fake ``mngr`` on PATH printing one rendered agent, and a browser
    service nothing listens on."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    record = _agent_record("chat-a", "WAITING", {"user_created": "true"}, datetime.now(timezone.utc) - timedelta(hours=3))
    line = _render_like_mngr(memory_candidates.MNGR_LIST_ARGV[-1], record)
    fake_mngr = bindir / "mngr"
    fake_mngr.write_text(f"#!/bin/sh\ncat <<'EOF'\n{line}\nEOF\n")
    fake_mngr.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        "OOM_PRIORITY_RUNTIME_DIR": str(tmp_path / "runtime"),
        "MINDS_BROWSER_SERVICE_URL": "http://127.0.0.1:9",
    }

    result = subprocess.run(
        # -S keeps site-packages off the path, so a non-stdlib import fails here as it would under python3.
        [sys.executable, "-S", str(_SCRIPT), "--json"], env=env, capture_output=True, text=True, timeout=60
    )

    assert result.returncode == 0, result.stderr
    document = json.loads(result.stdout)
    assert [(a["name"], a["kind"]) for a in document["agents"]["candidates"]] == [("chat-a", "chat")]
    assert document["browsers"]["candidates"] is None
    assert document["browsers"]["notes"][0].startswith("could not reach the browser service at http://127.0.0.1:9/browsers")
