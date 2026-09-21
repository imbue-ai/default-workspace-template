import json

from app_manifest.primitives import AppName
from app_manifest.testing import ShellStub
from imbue.mngr.utils.polling import wait_for

from terminal_app.sessions import TmuxSessionSource
from terminal_app.store import JsonTerminalSessionStore
from terminal_app.testing import FakeTmux, make_terminal_record, make_tmux_session
from terminal_app.window_sweep import WindowSweeper


def _desktops_showing(*names: str) -> str:
    windows = [{"id": f"win-{index}", "app": "terminal", "path": f"/?session={name}"} for index, name in enumerate(names)]
    return json.dumps({"desktops": [{"id": "home", "windows": windows}]})


def _sweeper(session_source: TmuxSessionSource, shell_stub: ShellStub, interval_seconds: float) -> WindowSweeper:
    return WindowSweeper(
        source=session_source, shell_url=shell_stub.url, app_name=AppName("terminal"), interval_seconds=interval_seconds
    )


def test_a_sweep_skips_a_shell_it_cannot_read_and_collects_from_one_it_can(
    fake_tmux: FakeTmux,
    session_store: JsonTerminalSessionStore,
    session_source: TmuxSessionSource,
    shell_stub: ShellStub,
) -> None:
    fake_tmux.set_sessions([make_tmux_session("terminal-1", "$1")])
    session_store.save_record(make_terminal_record("terminal-1", None, "/srv", session_id="$1"))
    sweeper = _sweeper(session_source, shell_stub, interval_seconds=3600.0)

    shell_stub.answer(200, _desktops_showing("terminal-1"))
    assert sweeper.sweep_once() == []
    shell_stub.answer(503, "restarting")
    assert sweeper.sweep_once() is None
    assert fake_tmux.session_names() == ["terminal-1"]
    shell_stub.answer(200, _desktops_showing())
    assert sweeper.sweep_once() == ["terminal-1"]
    assert fake_tmux.session_names() == []


def test_the_thread_sweeps_on_its_interval_and_at_once_when_asked(
    fake_tmux: FakeTmux,
    session_store: JsonTerminalSessionStore,
    session_source: TmuxSessionSource,
    shell_stub: ShellStub,
) -> None:
    fake_tmux.set_sessions([make_tmux_session("terminal-1", "$1"), make_tmux_session("terminal-2", "$2")])
    for name, session_id in (("terminal-1", "$1"), ("terminal-2", "$2")):
        session_store.save_record(make_terminal_record(name, None, "/srv", session_id=session_id))
    shell_stub.answer(200, _desktops_showing("terminal-1", "terminal-2"))
    sweeper = _sweeper(session_source, shell_stub, interval_seconds=0.05)
    sweeper.start()
    try:
        wait_for(
            lambda: all(record.is_window_seen for record in session_store.list_records()),
            timeout=5.0,
            poll_interval=0.02,
            error_message="the periodic sweep never marked the terminals",
        )
        # A long interval from here on: only a hint can bring the next sweep.
        sweeper.stop()
        slow = _sweeper(session_source, shell_stub, interval_seconds=3600.0)
        slow.start()
        try:
            shell_stub.answer(200, _desktops_showing("terminal-1"))
            slow.request_sweep()
            wait_for(
                lambda: fake_tmux.session_names() == ["terminal-1"],
                timeout=5.0,
                poll_interval=0.02,
                error_message="the hinted sweep never collected the closed terminal",
            )
        finally:
            slow.stop()
    finally:
        sweeper.stop()
