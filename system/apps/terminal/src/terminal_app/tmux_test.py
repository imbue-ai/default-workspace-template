from datetime import datetime, timezone

import pytest

from terminal_app.data_types import TmuxClient, TmuxSession
from terminal_app.errors import TmuxCommandError
from terminal_app.primitives import TmuxSessionId, TmuxSessionName, Workdir
from terminal_app.testing import FakeTmux, fake_created_epoch, make_tmux_session
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
        "terminal-1\t$3\t1756900000\t1756899000\nmngr-agent\t$1\t\t\nold-format\t$2\t\nbroken line\n"
    )

    assert parsed == [
        TmuxSession(
            name="terminal-1",
            session_id="$3",
            created_epoch=1756899000,
            last_activity=datetime.fromtimestamp(1756900000, timezone.utc),
        ),
        TmuxSession(name="mngr-agent", session_id="$1", created_epoch=None, last_activity=None),
        TmuxSession(name="old-format", session_id="$2", created_epoch=None, last_activity=None),
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
        [make_tmux_session("terminal-1", "$3")]
    )
    tmux = SubprocessTmux()

    tmux.kill_session(TmuxSessionName("terminal-1"))
    tmux.kill_session(TmuxSessionName("terminal-1"))

    assert fake_tmux.session_names() == []
    assert fake_tmux.calls()[0] == ["kill-session", "-t", "=terminal-1"]


def test_kill_session_raises_when_the_session_survives(fake_tmux: FakeTmux) -> None:
    fake_tmux.set_sessions(
        [make_tmux_session("terminal-1", "$3")]
    )
    fake_tmux.refuse_kills()

    with pytest.raises(TmuxCommandError, match="could not kill session 'terminal-1'"):
        SubprocessTmux().kill_session(TmuxSessionName("terminal-1"))


def test_a_missing_tmux_binary_is_a_command_error() -> None:
    with pytest.raises(TmuxCommandError, match="cannot run"):
        SubprocessTmux(tmux_executable="/nonexistent/tmux-binary").list_sessions()


def test_create_session_returns_the_new_sessions_id_and_runs_the_command(
    fake_tmux: FakeTmux,
) -> None:
    fake_tmux.set_sessions([make_tmux_session("terminal-1", "$3")])

    session_id = SubprocessTmux().create_session(
        TmuxSessionName("terminal-2"), Workdir("/srv"), ["python3", "tag.py", "bash", "-l"]
    )

    assert (session_id.session_id, session_id.created_epoch) == ("$4", fake_created_epoch("$4"))
    assert [session.name for session in fake_tmux.sessions()] == ["terminal-1", "terminal-2"]
    assert fake_tmux.calls()[-1] == [
        "new-session",
        "-d",
        "-s",
        "terminal-2",
        "-c",
        "/srv",
        "-P",
        "-F",
        "#{session_id}\t#{session_created}",
        "python3",
        "tag.py",
        "bash",
        "-l",
    ]


def test_create_session_refuses_a_name_tmux_already_has(fake_tmux: FakeTmux) -> None:
    fake_tmux.set_sessions([make_tmux_session("terminal-1", "$3")])

    with pytest.raises(TmuxCommandError, match="duplicate session"):
        SubprocessTmux().create_session(TmuxSessionName("terminal-1"), Workdir("/srv"), ["bash"])


def test_kill_session_targets_a_name_exactly_or_an_id_verbatim(fake_tmux: FakeTmux) -> None:
    fake_tmux.set_sessions(
        [
            make_tmux_session("terminal-1", "$3"),
            make_tmux_session("terminal-10", "$4"),
        ]
    )

    SubprocessTmux().kill_session(TmuxSessionName("terminal-1"))
    SubprocessTmux().kill_session(TmuxSessionId("$4"))

    assert fake_tmux.session_names() == []
    assert [call for call in fake_tmux.calls() if call[0] == "kill-session"] == [
        ["kill-session", "-t", "=terminal-1"],
        ["kill-session", "-t", "$4"],
    ]
