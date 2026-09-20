from datetime import datetime, timezone

import pytest

from terminal_app.data_types import TerminalPaths, TmuxSession
from terminal_app.errors import (
    InvalidTerminalValueError,
    TerminalConflictError,
    TmuxCommandError,
    UnknownTerminalError,
)
from terminal_app.primitives import TerminalTitle, TmuxSessionName, Workdir
from terminal_app.sessions import TmuxSessionSource, is_agent_session
from terminal_app.store import JsonTerminalSessionStore
from terminal_app.testing import (
    DEFAULT_TEST_WORKDIR,
    FakeTmux,
    expected_new_session_call,
    expected_session_id_file,
    fake_created_epoch,
    make_terminal_record,
    make_tmux_session,
    read_session_id_file,
    write_session_id_file,
)

_ACTIVITY = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
_LATER_ACTIVITY = datetime(2026, 9, 3, 13, 0, tzinfo=timezone.utc)


def _session(name: str, session_id: str) -> TmuxSession:
    return make_tmux_session(name, session_id, _ACTIVITY)


def test_is_agent_session_needs_a_configured_prefix() -> None:
    assert is_agent_session("mngr-alice", "mngr-") is True
    assert is_agent_session("terminal-1", "mngr-") is False
    assert is_agent_session("anything", "") is False


def test_list_merges_live_sessions_with_remembered_ones_and_hides_agents(
    fake_tmux: FakeTmux,
    session_store: JsonTerminalSessionStore,
    session_source: TmuxSessionSource,
) -> None:
    fake_tmux.set_sessions(
        [
            _session("terminal-2", "$5"),
            _session("mngr-alice", "$1"),
            _session("hand made", "$6"),
            _session("build", "$7"),
        ]
    )
    session_store.save_record(
        make_terminal_record(name="terminal-1", title=None, workdir="/srv")
    )
    session_store.save_record(
        make_terminal_record(name="build", title="The Build", workdir=None)
    )

    listed = session_source.list_terminals()

    assert [(record.name, record.title, record.is_stopped) for record in listed] == [
        ("terminal-2", "Terminal 2", False),
        ("build", "The Build", False),
        ("terminal-1", "Terminal 1", True),
    ]
    assert listed[0].last_activity == _ACTIVITY
    assert listed[2].last_activity is None


def test_list_matches_a_record_to_its_session_by_id_whatever_tmux_calls_it(
    fake_tmux: FakeTmux,
    session_store: JsonTerminalSessionStore,
    session_source: TmuxSessionSource,
) -> None:
    # Renamed inside tmux; the key and the title are the record's, not the session's name.
    fake_tmux.set_sessions([_session("my-build", "$5"), _session("terminal-2", "$9")])
    session_store.save_record(
        make_terminal_record(name="terminal-1", title="Build", workdir=None, session_id="$5")
    )
    session_store.save_record(
        make_terminal_record(name="terminal-2", title=None, workdir=None, session_id="$9")
    )

    listed = session_source.list_terminals()

    assert [(record.name, record.title, record.is_stopped) for record in listed] == [
        ("terminal-1", "Build", False),
        ("terminal-2", "Terminal 2", False),
    ]


@pytest.mark.parametrize("is_impostor_listed_first", [True, False])
def test_list_skips_a_second_session_under_a_tracked_terminals_name(
    fake_tmux: FakeTmux,
    session_store: JsonTerminalSessionStore,
    session_source: TmuxSessionSource,
    is_impostor_listed_first: bool,
) -> None:
    # terminal-1's session was renamed inside tmux and a hand-made session took its old name;
    # the activities tell the two apart, and tmux may list either first.
    real = make_tmux_session("renamed", "$5", _ACTIVITY)
    impostor = make_tmux_session("terminal-1", "$8", _LATER_ACTIVITY)
    fake_tmux.set_sessions([impostor, real] if is_impostor_listed_first else [real, impostor])
    session_store.save_record(
        make_terminal_record(name="terminal-1", title=None, workdir=None, session_id="$5")
    )

    listed = session_source.list_terminals()

    assert [(record.name, record.is_stopped, record.last_activity) for record in listed] == [
        ("terminal-1", False, _ACTIVITY)
    ]


def test_list_is_empty_without_a_tmux_server_or_a_store(
    fake_tmux: FakeTmux, session_source: TmuxSessionSource
) -> None:
    (fake_tmux.state_dir / "sessions.tsv").unlink()

    assert session_source.list_terminals() == []


def test_create_makes_the_session_at_once_with_the_lowest_free_number(
    fake_tmux: FakeTmux,
    session_store: JsonTerminalSessionStore,
    session_source: TmuxSessionSource,
    terminal_paths: TerminalPaths,
) -> None:
    fake_tmux.set_sessions([_session("terminal-1", "$3"), _session("terminal-3", "$4")])
    session_store.save_record(
        make_terminal_record(name="terminal-2", title=None, workdir=None)
    )

    created = session_source.create_terminal(Workdir("/home/user/workspace"))

    assert created.name == "terminal-4"
    assert created.is_stopped is False
    assert created.title == "Terminal 4"
    assert fake_tmux.session_names() == ["terminal-1", "terminal-3", "terminal-4"]
    assert fake_tmux.creates() == [expected_new_session_call("terminal-4", "/home/user/workspace")]
    assert session_store.list_records() == [
        make_terminal_record(name="terminal-2", title=None, workdir=None),
        make_terminal_record(
            name="terminal-4", title=None, workdir="/home/user/workspace", session_id="$5"
        ),
    ]
    assert read_session_id_file(terminal_paths.sessions_dir, "terminal-4") == expected_session_id_file("$5")


def test_two_creates_get_distinct_names_and_the_default_workdir(
    fake_tmux: FakeTmux, session_source: TmuxSessionSource
) -> None:
    first = session_source.create_terminal(None)
    second = session_source.create_terminal(None)

    assert (first.name, second.name) == ("terminal-1", "terminal-2")
    # A create that names no directory starts the shell where the app runs (the source's default).
    assert [call[5] for call in fake_tmux.creates()] == [DEFAULT_TEST_WORKDIR] * 2


def test_create_fails_loudly_and_remembers_nothing_when_tmux_refuses(
    fake_tmux: FakeTmux,
    session_store: JsonTerminalSessionStore,
    session_source: TmuxSessionSource,
) -> None:
    fake_tmux.refuse_creates()

    with pytest.raises(TmuxCommandError, match="could not create session 'terminal-1'"):
        session_source.create_terminal(None)

    assert session_store.list_records() == []


def test_delete_kills_the_session_by_its_id_and_forgets_it(
    fake_tmux: FakeTmux,
    session_store: JsonTerminalSessionStore,
    session_source: TmuxSessionSource,
    terminal_paths: TerminalPaths,
) -> None:
    fake_tmux.set_sessions([_session("renamed-in-tmux", "$3")])
    session_store.save_record(
        make_terminal_record(name="terminal-1", title=None, workdir=None, session_id="$3")
    )
    write_session_id_file(terminal_paths.sessions_dir, "terminal-1", "$3")

    session_source.delete_terminal(TmuxSessionName("terminal-1"))

    assert fake_tmux.session_names() == []
    assert session_store.list_records() == []
    assert ["kill-session", "-t", "$3"] in fake_tmux.calls()
    assert read_session_id_file(terminal_paths.sessions_dir, "terminal-1") is None


def test_delete_kills_a_session_with_no_record_by_name(
    fake_tmux: FakeTmux, session_source: TmuxSessionSource
) -> None:
    fake_tmux.set_sessions([_session("hand-made", "$3")])

    session_source.delete_terminal(TmuxSessionName("hand-made"))

    assert fake_tmux.session_names() == []
    assert ["kill-session", "-t", "$3"] in fake_tmux.calls()


def test_delete_of_an_unknown_name_is_not_an_error(
    fake_tmux: FakeTmux, session_source: TmuxSessionSource
) -> None:
    session_source.delete_terminal(TmuxSessionName("never-existed"))

    # Nothing live carries the name, so nothing is killed.
    assert [call[0] for call in fake_tmux.calls()] == ["list-sessions"]


def test_delete_refuses_an_agents_session(
    fake_tmux: FakeTmux, session_source: TmuxSessionSource
) -> None:
    fake_tmux.set_sessions([_session("mngr-alice", "$1")])

    with pytest.raises(TerminalConflictError, match="Refusing to destroy non-terminal session"):
        session_source.delete_terminal(TmuxSessionName("mngr-alice"))

    assert fake_tmux.session_names() == ["mngr-alice"]


def test_rename_changes_only_the_title_and_never_the_key_or_the_session(
    fake_tmux: FakeTmux,
    session_store: JsonTerminalSessionStore,
    session_source: TmuxSessionSource,
) -> None:
    fake_tmux.set_sessions([_session("terminal-1", "$3")])
    session_store.save_record(
        make_terminal_record(name="terminal-1", title=None, workdir="/srv", session_id="$3")
    )

    renamed = session_source.rename_terminal(TmuxSessionName("terminal-1"), TerminalTitle("My Build"))

    assert renamed.name == "terminal-1"
    assert renamed.title == "My Build"
    assert renamed.is_stopped is False
    assert fake_tmux.session_names() == ["terminal-1"]
    assert all(call[0] == "list-sessions" for call in fake_tmux.calls())
    assert session_store.list_records() == [
        make_terminal_record(name="terminal-1", title="My Build", workdir="/srv", session_id="$3")
    ]


def test_rename_of_a_live_session_the_store_never_saw_remembers_it_with_its_id(
    fake_tmux: FakeTmux,
    session_store: JsonTerminalSessionStore,
    session_source: TmuxSessionSource,
    terminal_paths: TerminalPaths,
) -> None:
    fake_tmux.set_sessions([_session("terminal-1", "$3")])

    session_source.rename_terminal(TmuxSessionName("terminal-1"), TerminalTitle("Build"))

    assert session_store.list_records() == [
        make_terminal_record(name="terminal-1", title="Build", workdir=None, session_id="$3")
    ]
    assert read_session_id_file(terminal_paths.sessions_dir, "terminal-1") == expected_session_id_file("$3")


def test_rename_takes_the_live_sessions_id_over_a_stale_one(
    fake_tmux: FakeTmux,
    session_store: JsonTerminalSessionStore,
    session_source: TmuxSessionSource,
    terminal_paths: TerminalPaths,
) -> None:
    # The session the record knew died and the dispatch recreated one by name on attach.
    fake_tmux.set_sessions([_session("terminal-1", "$9")])
    session_store.save_record(
        make_terminal_record(name="terminal-1", title=None, workdir=None, session_id="$3")
    )

    session_source.rename_terminal(TmuxSessionName("terminal-1"), TerminalTitle("Build"))

    assert session_store.list_records() == [
        make_terminal_record(name="terminal-1", title="Build", workdir=None, session_id="$9")
    ]
    assert read_session_id_file(terminal_paths.sessions_dir, "terminal-1") == expected_session_id_file("$9")


def test_rename_of_a_stopped_terminal_retitles_the_record_without_touching_tmux(
    fake_tmux: FakeTmux,
    session_store: JsonTerminalSessionStore,
    session_source: TmuxSessionSource,
) -> None:
    session_store.save_record(
        make_terminal_record(name="terminal-1", title=None, workdir=None, is_stopped=True)
    )

    renamed = session_source.rename_terminal(TmuxSessionName("terminal-1"), TerminalTitle("Later"))

    assert (renamed.name, renamed.title, renamed.is_stopped) == (
        "terminal-1",
        "Later",
        True,
    )
    assert session_store.list_records() == [
        make_terminal_record(name="terminal-1", title="Later", workdir=None, is_stopped=True)
    ]


def test_rename_of_a_stopped_terminal_whose_session_came_back_adopts_it_as_running(
    fake_tmux: FakeTmux,
    session_store: JsonTerminalSessionStore,
    session_source: TmuxSessionSource,
    terminal_paths: TerminalPaths,
) -> None:
    # The user stopped the terminal, then opened its window: the dispatch recreated the session by
    # name, so the rename is where the app first sees it.
    fake_tmux.set_sessions([_session("terminal-1", "$4")])
    session_store.save_record(
        make_terminal_record(name="terminal-1", title=None, workdir="/srv", is_stopped=True)
    )

    renamed = session_source.rename_terminal(TmuxSessionName("terminal-1"), TerminalTitle("Build"))

    assert (renamed.name, renamed.title, renamed.is_stopped) == ("terminal-1", "Build", False)
    assert session_store.list_records() == [
        make_terminal_record(name="terminal-1", title="Build", workdir="/srv", session_id="$4")
    ]
    assert read_session_id_file(terminal_paths.sessions_dir, "terminal-1") == expected_session_id_file("$4")


@pytest.mark.parametrize(
    ("title", "expected_problem"),
    [
        ("...", "contains no usable characters"),
        ("x" * 200, "over the 128-character limit"),
    ],
)
def test_rename_refuses_a_title_that_makes_no_usable_name(
    fake_tmux: FakeTmux,
    session_source: TmuxSessionSource,
    title: str,
    expected_problem: str,
) -> None:
    fake_tmux.set_sessions([_session("terminal-1", "$3")])

    with pytest.raises(InvalidTerminalValueError, match=expected_problem):
        session_source.rename_terminal(TmuxSessionName("terminal-1"), TerminalTitle(title))


def test_rename_refuses_a_title_another_terminal_holds_case_insensitively(
    fake_tmux: FakeTmux,
    session_store: JsonTerminalSessionStore,
    session_source: TmuxSessionSource,
) -> None:
    fake_tmux.set_sessions([_session("terminal-1", "$3"), _session("build", "$4")])
    session_store.save_record(
        make_terminal_record(name="terminal-2", title="Deploy", workdir=None)
    )

    with pytest.raises(TerminalConflictError, match="already named 'Build'"):
        session_source.rename_terminal(TmuxSessionName("terminal-1"), TerminalTitle("Build"))
    with pytest.raises(TerminalConflictError, match="already named 'deploy'"):
        session_source.rename_terminal(TmuxSessionName("terminal-1"), TerminalTitle("deploy"))
    # A terminal may keep its own title under another spelling.
    retitled = session_source.rename_terminal(TmuxSessionName("build"), TerminalTitle("BUILD"))
    assert retitled.title == "BUILD"


def test_rename_of_an_unknown_key_is_404_and_of_an_agent_is_refused(
    fake_tmux: FakeTmux, session_source: TmuxSessionSource
) -> None:
    fake_tmux.set_sessions([_session("mngr-alice", "$1")])

    with pytest.raises(UnknownTerminalError):
        session_source.rename_terminal(TmuxSessionName("ghost"), TerminalTitle("Boo"))
    with pytest.raises(TerminalConflictError, match="non-terminal session"):
        session_source.rename_terminal(TmuxSessionName("mngr-alice"), TerminalTitle("Alice"))


def test_startup_recreates_lost_sessions_adopts_live_ones_and_leaves_stopped_ones(
    fake_tmux: FakeTmux,
    session_store: JsonTerminalSessionStore,
    session_source: TmuxSessionSource,
    terminal_paths: TerminalPaths,
) -> None:
    # terminal-1 survived (by id, under another name); terminal-2 survived under its name, its
    # record holding no id; terminal-3 was lost to a container restart; terminal-4 was stopped.
    fake_tmux.set_sessions([_session("renamed", "$5"), _session("terminal-2", "$6")])
    session_store.save_record(
        make_terminal_record(name="terminal-1", title=None, workdir=None, session_id="$5")
    )
    session_store.save_record(
        make_terminal_record(name="terminal-2", title=None, workdir=None)
    )
    session_store.save_record(
        make_terminal_record(name="terminal-3", title="Build", workdir="/srv")
    )
    session_store.save_record(
        make_terminal_record(name="terminal-4", title=None, workdir=None, is_stopped=True)
    )
    write_session_id_file(terminal_paths.sessions_dir, "terminal-4", "$2")

    session_source.recreate_remembered_sessions()

    assert fake_tmux.session_names() == ["renamed", "terminal-2", "terminal-3"]
    assert fake_tmux.creates() == [
        expected_new_session_call("terminal-3", "/srv")
    ]
    assert session_store.list_records() == [
        make_terminal_record(name="terminal-1", title=None, workdir=None, session_id="$5"),
        make_terminal_record(name="terminal-2", title=None, workdir=None, session_id="$6"),
        make_terminal_record(name="terminal-3", title="Build", workdir="/srv", session_id="$7"),
        make_terminal_record(name="terminal-4", title=None, workdir=None, is_stopped=True),
    ]
    assert {
        name: read_session_id_file(terminal_paths.sessions_dir, name)
        for name in ("terminal-1", "terminal-2", "terminal-3", "terminal-4")
    } == {
        "terminal-1": expected_session_id_file("$5"),
        "terminal-2": expected_session_id_file("$6"),
        "terminal-3": expected_session_id_file("$7"),
        "terminal-4": None,
    }
    assert [(record.name, record.is_stopped) for record in session_source.list_terminals()] == [
        ("terminal-1", False),
        ("terminal-2", False),
        ("terminal-3", False),
        ("terminal-4", True),
    ]


def test_startup_leaves_a_terminal_stopped_when_tmux_cannot_recreate_it(
    fake_tmux: FakeTmux,
    session_store: JsonTerminalSessionStore,
    session_source: TmuxSessionSource,
) -> None:
    fake_tmux.refuse_creates()
    session_store.save_record(
        make_terminal_record(name="terminal-1", title=None, workdir=None)
    )

    session_source.recreate_remembered_sessions()

    assert session_store.list_records() == [
        make_terminal_record(name="terminal-1", title=None, workdir=None)
    ]
    assert [record.is_stopped for record in session_source.list_terminals()] == [True]


def test_stop_kills_the_session_and_remembers_the_terminal_as_stopped(
    fake_tmux: FakeTmux,
    session_store: JsonTerminalSessionStore,
    session_source: TmuxSessionSource,
    terminal_paths: TerminalPaths,
) -> None:
    fake_tmux.set_sessions([_session("renamed", "$3")])
    session_store.save_record(
        make_terminal_record(name="terminal-1", title="Build", workdir="/srv", session_id="$3")
    )
    write_session_id_file(terminal_paths.sessions_dir, "terminal-1", "$3")

    stopped = session_source.stop_terminal(TmuxSessionName("terminal-1"))

    assert (stopped.name, stopped.title, stopped.is_stopped) == ("terminal-1", "Build", True)
    assert fake_tmux.session_names() == []
    assert ["kill-session", "-t", "$3"] in fake_tmux.calls()
    assert session_store.list_records() == [
        make_terminal_record(name="terminal-1", title="Build", workdir="/srv", is_stopped=True)
    ]
    assert read_session_id_file(terminal_paths.sessions_dir, "terminal-1") is None
    # Stopping again is a no-op that answers the same record.
    assert session_source.stop_terminal(TmuxSessionName("terminal-1")) == stopped


def test_stop_of_a_hand_made_session_remembers_it_so_it_can_be_started(
    fake_tmux: FakeTmux,
    session_store: JsonTerminalSessionStore,
    session_source: TmuxSessionSource,
) -> None:
    fake_tmux.set_sessions([_session("scratch", "$3")])

    stopped = session_source.stop_terminal(TmuxSessionName("scratch"))

    assert (stopped.name, stopped.is_stopped) == ("scratch", True)
    assert session_store.list_records() == [
        make_terminal_record(name="scratch", title=None, workdir=None, is_stopped=True)
    ]


def test_start_recreates_a_stopped_terminals_session_in_its_workdir(
    fake_tmux: FakeTmux,
    session_store: JsonTerminalSessionStore,
    session_source: TmuxSessionSource,
    terminal_paths: TerminalPaths,
) -> None:
    session_store.save_record(
        make_terminal_record(name="terminal-1", title="Build", workdir="/srv", is_stopped=True)
    )

    started = session_source.start_terminal(TmuxSessionName("terminal-1"))

    assert (started.name, started.title, started.is_stopped) == ("terminal-1", "Build", False)
    assert fake_tmux.creates() == [
        expected_new_session_call("terminal-1", "/srv")
    ]
    assert session_store.list_records() == [
        make_terminal_record(name="terminal-1", title="Build", workdir="/srv", session_id="$1")
    ]
    assert read_session_id_file(terminal_paths.sessions_dir, "terminal-1") == expected_session_id_file("$1")
    # Starting a running terminal changes nothing.
    assert session_source.start_terminal(TmuxSessionName("terminal-1")).is_stopped is False
    assert len(fake_tmux.creates()) == 1


def test_start_adopts_the_live_session_when_the_records_id_is_stale(
    fake_tmux: FakeTmux,
    session_store: JsonTerminalSessionStore,
    session_source: TmuxSessionSource,
    terminal_paths: TerminalPaths,
) -> None:
    fake_tmux.set_sessions([_session("terminal-1", "$9")])
    session_store.save_record(
        make_terminal_record(name="terminal-1", title=None, workdir=None, session_id="$3")
    )

    started = session_source.start_terminal(TmuxSessionName("terminal-1"))

    assert (started.name, started.is_stopped) == ("terminal-1", False)
    assert fake_tmux.creates() == []
    assert session_store.list_records() == [
        make_terminal_record(name="terminal-1", title=None, workdir=None, session_id="$9")
    ]
    assert read_session_id_file(terminal_paths.sessions_dir, "terminal-1") == expected_session_id_file("$9")


def test_stop_and_start_refuse_unknown_keys_and_agent_sessions(
    fake_tmux: FakeTmux, session_source: TmuxSessionSource
) -> None:
    fake_tmux.set_sessions([_session("mngr-alice", "$1")])

    with pytest.raises(UnknownTerminalError):
        session_source.stop_terminal(TmuxSessionName("ghost"))
    with pytest.raises(UnknownTerminalError):
        session_source.start_terminal(TmuxSessionName("ghost"))
    with pytest.raises(TerminalConflictError, match="non-terminal session"):
        session_source.stop_terminal(TmuxSessionName("mngr-alice"))
    with pytest.raises(TerminalConflictError, match="non-terminal session"):
        session_source.start_terminal(TmuxSessionName("mngr-alice"))
    assert fake_tmux.session_names() == ["mngr-alice"]


def test_a_session_id_from_an_earlier_server_binds_nothing(
    fake_tmux: FakeTmux,
    session_store: JsonTerminalSessionStore,
    session_source: TmuxSessionSource,
    terminal_paths: TerminalPaths,
) -> None:
    # tmux hands ids out afresh on every server: the record's $3 was created on the last
    # server, and this server's $3 is a hand-made session created later.
    fake_tmux.set_sessions([_session("scratch", "$3")])
    session_store.save_record(
        make_terminal_record(
            name="terminal-1", title="Build", workdir="/srv", session_id="$3", session_created=fake_created_epoch("$3") - 3600
        )
    )

    listed = session_source.list_terminals()
    assert [(record.name, record.is_stopped) for record in listed] == [
        ("scratch", False),
        ("terminal-1", True),
    ]

    # A delete of the terminal kills nothing of the impostor's, and a startup recreates the
    # terminal's own session rather than adopting the impostor.
    session_source.recreate_remembered_sessions()
    assert fake_tmux.session_names() == ["scratch", "terminal-1"]
    assert session_store.list_records() == [
        make_terminal_record(name="terminal-1", title="Build", workdir="/srv", session_id="$4")
    ]
    assert read_session_id_file(terminal_paths.sessions_dir, "terminal-1") == expected_session_id_file("$4")
    session_source.delete_terminal(TmuxSessionName("terminal-1"))
    assert fake_tmux.session_names() == ["scratch"]


def test_startup_gives_a_record_without_a_creation_time_its_live_sessions_time(
    fake_tmux: FakeTmux,
    session_store: JsonTerminalSessionStore,
    session_source: TmuxSessionSource,
    terminal_paths: TerminalPaths,
) -> None:
    # A record with no creation time matches its session by id alone, and the dispatch attaches
    # only by id and creation time together.
    fake_tmux.set_sessions([_session("renamed", "$3")])
    session_store.save_record(
        make_terminal_record(
            name="terminal-1", title=None, workdir=None, session_id="$3", is_session_created_known=False
        )
    )

    session_source.recreate_remembered_sessions()

    assert fake_tmux.creates() == []
    assert session_store.list_records() == [
        make_terminal_record(name="terminal-1", title=None, workdir=None, session_id="$3")
    ]
    assert read_session_id_file(terminal_paths.sessions_dir, "terminal-1") == expected_session_id_file("$3")


def test_a_record_without_a_creation_time_still_matches_its_session_by_id(
    fake_tmux: FakeTmux,
    session_store: JsonTerminalSessionStore,
    session_source: TmuxSessionSource,
) -> None:
    fake_tmux.set_sessions([_session("renamed", "$3")])
    session_store.save_record(
        make_terminal_record(
            name="terminal-1", title=None, workdir=None, session_id="$3", is_session_created_known=False
        )
    )

    assert [(record.name, record.is_stopped) for record in session_source.list_terminals()] == [
        ("terminal-1", False)
    ]


def test_a_session_renamed_to_another_terminals_key_stays_with_the_terminal_that_holds_its_id(
    fake_tmux: FakeTmux,
    session_store: JsonTerminalSessionStore,
    session_source: TmuxSessionSource,
) -> None:
    # terminal-1's session was renamed inside tmux to terminal-2's key while terminal-2's own
    # session is gone: the name must not let terminal-2 claim terminal-1's session, at startup
    # or on a start, or the two would share one shell and stopping either would kill it. tmux
    # refuses a second session of that name, so terminal-2 stays stopped and a start says why.
    fake_tmux.set_sessions([_session("terminal-2", "$5")])
    session_store.save_record(
        make_terminal_record(name="terminal-1", title=None, workdir=None, session_id="$5")
    )
    session_store.save_record(
        make_terminal_record(name="terminal-2", title=None, workdir="/srv", session_id="$8")
    )

    session_source.recreate_remembered_sessions()

    assert fake_tmux.creates() == [expected_new_session_call("terminal-2", "/srv")]
    assert fake_tmux.session_names() == ["terminal-2"]
    assert [(record.name, record.is_stopped) for record in session_source.list_terminals()] == [
        ("terminal-1", False),
        ("terminal-2", True),
    ]
    assert session_store.list_records()[0].session_id == "$5"

    with pytest.raises(TerminalConflictError, match="duplicate session"):
        session_source.start_terminal(TmuxSessionName("terminal-2"))
    assert session_store.list_records()[0].session_id == "$5"
