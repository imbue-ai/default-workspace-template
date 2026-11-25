"""Ending abandoned ``playwright-cli`` sessions: which daemons ``/proc`` shows as attached through the proxy, and which
of them the reaper ends, over a fleet whose tokens, lease and clock are real (spec section 16)."""

import os
from pathlib import Path

from mock_cli_sessions_test import RecordingCliSessions
from screen.browser_fleet import Browser
from screen.cdp_proxy import ProxyServer
from screen.cli_sessions import AbandonReason
from screen.cli_sessions import AttachedCliSession
from screen.cli_sessions import CliSessionReaper
from screen.cli_sessions import IDLE_LIMIT_SECONDS
from screen.cli_sessions import ProcCliSessions
from screen.cli_sessions import abandonment_reason
from screen.primitives import AgentId
from testing import FleetHarness

_AGENT = AgentId("chat-a")
_DAEMON = "/usr/local/lib/node_modules/@playwright/cli/node_modules/playwright-core/lib/entry/cliDaemon.js"


def _session(name: str, attach_url: str, proxy_server: ProxyServer, pid: int) -> AttachedCliSession:
    target = proxy_server.parse_attach_url(attach_url)
    assert target is not None, attach_url
    return AttachedCliSession(
        pid=pid,
        session_name=name,
        browser_name=target[0],
        token=target[1],
        working_dir=Path("/home/user/workspace"),
        environment={"HOME": "/root"},
    )


def _reaper(harness: FleetHarness, sessions: RecordingCliSessions) -> CliSessionReaper:
    return CliSessionReaper(fleet=harness.fleet, sessions=sessions)


def _attach(harness: FleetHarness, browser: Browser) -> str:
    answer = harness.fleet.attach_for(browser.name, _AGENT, "Alice")
    assert answer["ok"], answer
    return str(answer["attach_url"])


def test_a_session_whose_token_a_later_attach_replaced_is_ended_and_the_latest_is_kept(tmp_path: Path) -> None:
    harness = FleetHarness(tmp_path)
    harness.stack_up()
    browser = harness.running_browser()
    first = _session("plaud-sync-1", _attach(harness, browser), harness.proxy_server, pid=101)
    second = _session("plaud-sync-2", _attach(harness, browser), harness.proxy_server, pid=102)
    sessions = RecordingCliSessions(running=[first, second])
    ended = _reaper(harness, sessions).sweep()
    assert [(session.session_name, reason) for session, reason in ended] == [
        ("plaud-sync-1", AbandonReason.SUPERSEDED)
    ]
    assert [session.session_name for session in sessions.running] == ["plaud-sync-2"]


def test_the_current_session_is_ended_only_once_idle_past_the_limit_and_a_frame_restarts_the_clock(
    tmp_path: Path,
) -> None:
    harness = FleetHarness(tmp_path)
    harness.stack_up()
    browser = harness.running_browser()
    url = _attach(harness, browser)
    token = url.rsplit("/", 1)[1]
    sessions = RecordingCliSessions(running=[_session("browser-1", url, harness.proxy_server, pid=201)])
    reaper = _reaper(harness, sessions)
    harness.clock.advance(IDLE_LIMIT_SECONDS - 60)
    # A frame through the gate counts as use.
    assert harness.fleet.token_may_drive(browser, token) is True
    harness.control.release(_AGENT)
    harness.clock.advance(IDLE_LIMIT_SECONDS - 60)
    assert reaper.sweep() == []
    harness.clock.advance(120)
    assert [reason for _, reason in reaper.sweep()] == [AbandonReason.IDLE]
    assert sessions.ended == ["browser-1"]


def test_an_idle_session_is_kept_while_its_agent_holds_the_lease(tmp_path: Path) -> None:
    harness = FleetHarness(tmp_path)
    harness.stack_up()
    browser = harness.running_browser()
    session = _session("browser-1", _attach(harness, browser), harness.proxy_server, pid=301)
    later = browser.token_used_at + IDLE_LIMIT_SECONDS + 1
    assert abandonment_reason(session, browser, _AGENT, later, IDLE_LIMIT_SECONDS) is None
    assert abandonment_reason(session, browser, AgentId("chat-b"), later, IDLE_LIMIT_SECONDS) == AbandonReason.IDLE
    assert abandonment_reason(session, browser, None, later, IDLE_LIMIT_SECONDS) == AbandonReason.IDLE


def test_every_session_of_a_stopped_or_unknown_browser_is_ended(tmp_path: Path) -> None:
    harness = FleetHarness(tmp_path)
    harness.stack_up()
    browser = harness.running_browser()
    current = _session("browser-1", _attach(harness, browser), harness.proxy_server, pid=401)
    elsewhere = current.model_copy(update={"pid": 402, "session_name": "other", "browser_name": "browser-9"})
    sessions = RecordingCliSessions(running=[current, elsewhere])
    harness.fleet.stop_browser(browser.name)
    ended = _reaper(harness, sessions).sweep()
    assert sorted((session.session_name, reason) for session, reason in ended) == [
        ("browser-1", AbandonReason.BROWSER_GONE),
        ("other", AbandonReason.BROWSER_GONE),
    ]


def _write_process(proc: Path, pid: int, argv: list[str], environ: dict[str, str], cwd: Path) -> None:
    entry = proc / str(pid)
    entry.mkdir(parents=True)
    (entry / "cmdline").write_bytes(b"\0".join(arg.encode() for arg in argv) + b"\0")
    (entry / "environ").write_bytes(b"\0".join(f"{k}={v}".encode() for k, v in environ.items()) + b"\0")
    os.symlink(cwd, entry / "cwd")


def test_only_daemons_attached_through_this_proxy_are_found_with_their_directory_and_environment(
    tmp_path: Path,
) -> None:
    proc = tmp_path / "proc"
    proxy_server = ProxyServer(requested_port=0)
    ours = proxy_server.attach_url("browser-1", "tok-ours")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    environ = {"HOME": "/root", "PATH": "/usr/bin", "MNGR_AGENT_ID": "agent-recordings"}
    _write_process(proc, 11, ["/usr/local/bin/node", _DAEMON, "plaud-sync-1", f"--cdp={ours}"], environ, workspace)
    # The same session attached to some other endpoint, the client command that started a daemon, and other node.
    _write_process(
        proc, 12, ["node", _DAEMON, "elsewhere", "--cdp=http://127.0.0.1:9222/browser-1/x"], environ, workspace
    )
    _write_process(
        proc, 13, ["node", "/usr/local/bin/playwright-cli", "-s=x", "attach", f"--cdp={ours}"], {}, workspace
    )
    _write_process(proc, 14, ["node", "server.js"], {}, workspace)
    (proc / "self").mkdir()
    found = ProcCliSessions(proc_dir=proc, playwright_cli=None).attached_through(proxy_server)
    assert found == [
        AttachedCliSession(
            pid=11,
            session_name="plaud-sync-1",
            browser_name="browser-1",
            token="tok-ours",
            working_dir=workspace,
            environment=environ,
        )
    ]
