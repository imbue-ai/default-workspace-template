from datetime import datetime, timezone

import pytest

from terminal_app.data_types import TmuxClient, TmuxSession
from terminal_app.errors import TmuxCommandError
from terminal_app.primitives import TmuxSessionName
from terminal_app.testing import FakeTmux
from terminal_app.tmux import (
    CLIENTS_FORMAT,
    SESSIONS_FORMAT,
    SubprocessTmux,
    parse_tmux_clients,
    parse_tmux_sessions,
)


def test_parse_tmux_sessions_reads_the_activity_timestamp_and_skips_short_lines() -> (
    None
):
    parsed = parse_tmux_sessions(
        "terminal-1\t$3\t1756900000\nmngr-agent\t$1\t\nbroken line\n"
    )

    assert parsed == [
        TmuxSession(
            name="terminal-1",
            session_id="$3",
            last_activity=datetime.fromtimestamp(1756900000, timezone.utc),
        ),
        TmuxSession(name="mngr-agent", session_id="$1", last_activity=None),
    ]


def test_parse_tmux_clients_reads_tty_name_and_id_and_skips_a_client_with_no_pty() -> (
    None
):
    parsed = parse_tmux_clients("/dev/pts/7\tterminal-1\t$3\n\tbuild\t$4\n")

    assert parsed == [
        TmuxClient(client_tty="/dev/pts/7", session_name="terminal-1", session_id="$3")
    ]


def test_list_sessions_is_empty_when_no_server_runs(fake_tmux: FakeTmux) -> None:
    (fake_tmux.state_dir / "sessions.tsv").unlink()

    assert SubprocessTmux().list_sessions() == []
    assert fake_tmux.calls() == [["list-sessions", "-F", SESSIONS_FORMAT]]


def test_list_clients_asks_for_the_tty_name_and_id(fake_tmux: FakeTmux) -> None:
    fake_tmux.set_clients(
        [TmuxClient(client_tty="/dev/pts/2", session_name="build", session_id="$4")]
    )

    assert SubprocessTmux().list_clients() == [
        TmuxClient(client_tty="/dev/pts/2", session_name="build", session_id="$4")
    ]
    assert fake_tmux.calls() == [["list-clients", "-F", CLIENTS_FORMAT]]


def test_kill_session_targets_the_exact_name_and_tolerates_an_absent_session(
    fake_tmux: FakeTmux,
) -> None:
    fake_tmux.set_sessions(
        [TmuxSession(name="terminal-1", session_id="$3", last_activity=None)]
    )
    tmux = SubprocessTmux()

    tmux.kill_session(TmuxSessionName("terminal-1"))
    tmux.kill_session(TmuxSessionName("terminal-1"))

    assert fake_tmux.session_names() == []
    assert fake_tmux.calls()[0] == ["kill-session", "-t", "=terminal-1"]


def test_kill_session_raises_when_the_session_survives(fake_tmux: FakeTmux) -> None:
    fake_tmux.set_sessions(
        [TmuxSession(name="terminal-1", session_id="$3", last_activity=None)]
    )
    fake_tmux.refuse_kills()

    with pytest.raises(TmuxCommandError, match="could not kill session 'terminal-1'"):
        SubprocessTmux().kill_session(TmuxSessionName("terminal-1"))


def test_rename_session_renames_and_reports_a_refusal(fake_tmux: FakeTmux) -> None:
    fake_tmux.set_sessions(
        [
            TmuxSession(name="terminal-1", session_id="$3", last_activity=None),
            TmuxSession(name="build", session_id="$4", last_activity=None),
        ]
    )
    tmux = SubprocessTmux()

    tmux.rename_session(TmuxSessionName("terminal-1"), TmuxSessionName("deploy"))

    assert fake_tmux.session_names() == ["deploy", "build"]
    assert fake_tmux.calls()[-1] == ["rename-session", "-t", "=terminal-1", "deploy"]
    with pytest.raises(TmuxCommandError, match="duplicate session: build"):
        tmux.rename_session(TmuxSessionName("deploy"), TmuxSessionName("build"))


def test_a_missing_tmux_binary_is_a_command_error() -> None:
    with pytest.raises(TmuxCommandError, match="cannot run"):
        SubprocessTmux(tmux_executable="/nonexistent/tmux-binary").list_sessions()
