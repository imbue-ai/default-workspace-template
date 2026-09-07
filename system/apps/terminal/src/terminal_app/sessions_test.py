import urllib.parse
from datetime import datetime, timezone

import pytest
from app_instances.data_types import InstanceStatus
from app_instances.errors import (
    InstanceConflictError,
    InvalidInstanceValueError,
    InvalidParamsError,
    LocationNotTrackedError,
    UnknownActionError,
    UnknownInstanceError,
)
from app_instances.primitives import InstanceKey, InstanceTitle, LocationPath
from app_manifest.primitives import ActionId

from terminal_app.data_types import TerminalPaths, TmuxSession
from terminal_app.errors import TmuxCommandError
from terminal_app.sessions import TmuxSessionSource, is_agent_session
from terminal_app.store import JsonTerminalSessionStore
from terminal_app.testing import (
    DEFAULT_TEST_WORKDIR,
    TEST_SESSION_COMMAND,
    FakeTmux,
    make_terminal_record,
)

_NEW = ActionId("new")
_ACTIVITY = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
_LATER_ACTIVITY = datetime(2026, 9, 3, 13, 0, tzinfo=timezone.utc)


def _session(name: str, session_id: str) -> TmuxSession:
    return TmuxSession(name=name, session_id=session_id, last_activity=_ACTIVITY)


def _creates(fake_tmux: FakeTmux) -> list[list[str]]:
    return [call for call in fake_tmux.calls() if call[0] == "new-session"]


def _session_id_file(paths: TerminalPaths, name: str) -> str | None:
    path = paths.sessions_dir / name
    return path.read_text() if path.exists() else None


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

    listed = session_source.list_instances()

    assert [(record.key, record.title, record.status) for record in listed] == [
        ("terminal-2", "Terminal 2", InstanceStatus.IDLE),
        ("build", "The Build", InstanceStatus.IDLE),
        ("terminal-1", "Terminal 1", InstanceStatus.STOPPED),
    ]
    assert listed[0].url == "/?arg=_&arg=session&arg=terminal-2&arg={tab}"
    assert listed[0].last_active == _ACTIVITY
    assert listed[2].url == "/?arg=_&arg=session&arg=terminal-1&arg={tab}&arg=%2Fsrv"
    assert listed[2].last_active is None
    assert all(record.renameable for record in listed)


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

    listed = session_source.list_instances()

    assert [(record.key, record.title, record.status) for record in listed] == [
        ("terminal-1", "Build", InstanceStatus.IDLE),
        ("terminal-2", "Terminal 2", InstanceStatus.IDLE),
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
    real = TmuxSession(name="renamed", session_id="$5", last_activity=_ACTIVITY)
    impostor = TmuxSession(name="terminal-1", session_id="$8", last_activity=_LATER_ACTIVITY)
    fake_tmux.set_sessions([impostor, real] if is_impostor_listed_first else [real, impostor])
    session_store.save_record(
        make_terminal_record(name="terminal-1", title=None, workdir=None, session_id="$5")
    )

    listed = session_source.list_instances()

    assert [(record.key, record.status, record.last_active) for record in listed] == [
        ("terminal-1", InstanceStatus.IDLE, _ACTIVITY)
    ]


def test_list_is_empty_without_a_tmux_server_or_a_store(
    fake_tmux: FakeTmux, session_source: TmuxSessionSource
) -> None:
    (fake_tmux.state_dir / "sessions.tsv").unlink()

    assert session_source.list_instances() == []


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

    created = session_source.create_instance(_NEW, {"workdir": "/home/user/workspace"})

    assert created.key == "terminal-4"
    assert created.status == InstanceStatus.IDLE
    assert (
        created.url
        == "/?arg=_&arg=session&arg=terminal-4&arg={tab}&arg=%2Fhome%2Fuser%2Fworkspace"
    )
    assert fake_tmux.session_names() == ["terminal-1", "terminal-3", "terminal-4"]
    assert _creates(fake_tmux) == [
        [
            "new-session",
            "-d",
            "-s",
            "terminal-4",
            "-c",
            "/home/user/workspace",
            "-P",
            "-F",
            "#{session_id}",
            *TEST_SESSION_COMMAND,
        ]
    ]
    assert session_store.list_records() == [
        make_terminal_record(name="terminal-2", title=None, workdir=None),
        make_terminal_record(
            name="terminal-4", title=None, workdir="/home/user/workspace", session_id="$5"
        ),
    ]
    assert _session_id_file(terminal_paths, "terminal-4") == "$5\n"


def test_two_creates_get_distinct_names_and_the_default_workdir(
    fake_tmux: FakeTmux, session_source: TmuxSessionSource
) -> None:
    first = session_source.create_instance(_NEW, {})
    second = session_source.create_instance(_NEW, {"workdir": ""})

    assert (first.key, second.key) == ("terminal-1", "terminal-2")
    # A create that names no directory starts the shell where the app runs (the source's default).
    default_directory = urllib.parse.quote(DEFAULT_TEST_WORKDIR, safe="")
    assert (
        second.url
        == f"/?arg=_&arg=session&arg=terminal-2&arg={{tab}}&arg={default_directory}"
    )
    assert [call[5] for call in _creates(fake_tmux)] == [DEFAULT_TEST_WORKDIR] * 2


def test_create_refuses_other_actions_and_other_params(
    session_source: TmuxSessionSource,
) -> None:
    with pytest.raises(UnknownActionError, match="only declares 'new'"):
        session_source.create_instance(ActionId("split"), {})
    with pytest.raises(InvalidParamsError, match="unknown params \\['path'\\]"):
        session_source.create_instance(_NEW, {"path": "/"})
    with pytest.raises(InvalidParamsError, match="invalid 'workdir'"):
        session_source.create_instance(_NEW, {"workdir": "/tmp/\x00"})


def test_create_fails_loudly_and_remembers_nothing_when_tmux_refuses(
    fake_tmux: FakeTmux,
    session_store: JsonTerminalSessionStore,
    session_source: TmuxSessionSource,
) -> None:
    fake_tmux.refuse_creates()

    with pytest.raises(TmuxCommandError, match="could not create session 'terminal-1'"):
        session_source.create_instance(_NEW, {})

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
    terminal_paths.sessions_dir.mkdir(parents=True)
    (terminal_paths.sessions_dir / "terminal-1").write_text("$3\n")

    session_source.delete_instance(InstanceKey("terminal-1"))

    assert fake_tmux.session_names() == []
    assert session_store.list_records() == []
    assert ["kill-session", "-t", "$3"] in fake_tmux.calls()
    assert _session_id_file(terminal_paths, "terminal-1") is None


def test_delete_kills_a_session_with_no_record_by_name(
    fake_tmux: FakeTmux, session_source: TmuxSessionSource
) -> None:
    fake_tmux.set_sessions([_session("hand-made", "$3")])

    session_source.delete_instance(InstanceKey("hand-made"))

    assert fake_tmux.session_names() == []
    assert ["kill-session", "-t", "=hand-made"] in fake_tmux.calls()


def test_delete_of_an_unknown_or_impossible_key_is_not_an_error(
    fake_tmux: FakeTmux, session_source: TmuxSessionSource
) -> None:
    session_source.delete_instance(InstanceKey("never-existed"))
    session_source.delete_instance(InstanceKey("not.a.tmux.name"))

    assert [call[0] for call in fake_tmux.calls()] == ["kill-session", "list-sessions"]


def test_delete_refuses_an_agents_session(
    fake_tmux: FakeTmux, session_source: TmuxSessionSource
) -> None:
    fake_tmux.set_sessions([_session("mngr-alice", "$1")])

    with pytest.raises(
        InstanceConflictError, match="Refusing to destroy non-terminal session"
    ):
        session_source.delete_instance(InstanceKey("mngr-alice"))

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

    renamed = session_source.rename_instance(
        InstanceKey("terminal-1"), InstanceTitle("My Build")
    )

    assert renamed.key == "terminal-1"
    assert renamed.title == "My Build"
    assert renamed.status == InstanceStatus.IDLE
    assert renamed.url == "/?arg=_&arg=session&arg=terminal-1&arg={tab}&arg=%2Fsrv"
    assert fake_tmux.session_names() == ["terminal-1"]
    assert all(call[0] == "list-sessions" for call in fake_tmux.calls())
    assert session_store.list_records() == [
        make_terminal_record(name="terminal-1", title="My Build", workdir="/srv", session_id="$3")
    ]


def test_rename_of_a_live_session_the_store_never_saw_remembers_it_with_its_id(
    fake_tmux: FakeTmux,
    session_store: JsonTerminalSessionStore,
    session_source: TmuxSessionSource,
) -> None:
    fake_tmux.set_sessions([_session("terminal-1", "$3")])

    session_source.rename_instance(InstanceKey("terminal-1"), InstanceTitle("Build"))

    assert session_store.list_records() == [
        make_terminal_record(name="terminal-1", title="Build", workdir=None, session_id="$3")
    ]


def test_rename_of_a_stopped_terminal_retitles_the_record_without_touching_tmux(
    fake_tmux: FakeTmux,
    session_store: JsonTerminalSessionStore,
    session_source: TmuxSessionSource,
) -> None:
    session_store.save_record(
        make_terminal_record(name="terminal-1", title=None, workdir=None, is_stopped=True)
    )

    renamed = session_source.rename_instance(
        InstanceKey("terminal-1"), InstanceTitle("Later")
    )

    assert (renamed.key, renamed.title, renamed.status) == (
        "terminal-1",
        "Later",
        InstanceStatus.STOPPED,
    )
    assert session_store.list_records() == [
        make_terminal_record(name="terminal-1", title="Later", workdir=None, is_stopped=True)
    ]


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

    with pytest.raises(InvalidInstanceValueError, match=expected_problem):
        session_source.rename_instance(InstanceKey("terminal-1"), InstanceTitle(title))


def test_rename_refuses_a_title_another_terminal_holds_case_insensitively(
    fake_tmux: FakeTmux,
    session_store: JsonTerminalSessionStore,
    session_source: TmuxSessionSource,
) -> None:
    fake_tmux.set_sessions([_session("terminal-1", "$3"), _session("build", "$4")])
    session_store.save_record(
        make_terminal_record(name="terminal-2", title="Deploy", workdir=None)
    )

    with pytest.raises(InstanceConflictError, match="already named 'Build'"):
        session_source.rename_instance(
            InstanceKey("terminal-1"), InstanceTitle("Build")
        )
    with pytest.raises(InstanceConflictError, match="already named 'deploy'"):
        session_source.rename_instance(
            InstanceKey("terminal-1"), InstanceTitle("deploy")
        )
    # A terminal may keep its own title under another spelling.
    retitled = session_source.rename_instance(InstanceKey("build"), InstanceTitle("BUILD"))
    assert retitled.title == "BUILD"


def test_rename_of_an_unknown_key_is_404_and_of_an_agent_is_refused(
    fake_tmux: FakeTmux, session_source: TmuxSessionSource
) -> None:
    fake_tmux.set_sessions([_session("mngr-alice", "$1")])

    with pytest.raises(UnknownInstanceError):
        session_source.rename_instance(InstanceKey("ghost"), InstanceTitle("Boo"))
    with pytest.raises(InstanceConflictError, match="non-terminal session"):
        session_source.rename_instance(
            InstanceKey("mngr-alice"), InstanceTitle("Alice")
        )


def test_set_location_is_not_tracked(session_source: TmuxSessionSource) -> None:
    with pytest.raises(LocationNotTrackedError):
        session_source.set_location(InstanceKey("terminal-1"), LocationPath("/"))


def test_startup_recreates_lost_sessions_adopts_live_ones_and_leaves_stopped_ones(
    fake_tmux: FakeTmux,
    session_store: JsonTerminalSessionStore,
    session_source: TmuxSessionSource,
    terminal_paths: TerminalPaths,
) -> None:
    # terminal-1 survived (by id, under another name); terminal-2 survived under its name from
    # before ids were kept; terminal-3 was lost to a container restart; terminal-4 was stopped.
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
    terminal_paths.sessions_dir.mkdir(parents=True)
    (terminal_paths.sessions_dir / "terminal-4").write_text("$2\n")

    session_source.recreate_remembered_sessions()

    assert fake_tmux.session_names() == ["renamed", "terminal-2", "terminal-3"]
    assert _creates(fake_tmux) == [
        ["new-session", "-d", "-s", "terminal-3", "-c", "/srv", "-P", "-F", "#{session_id}", *TEST_SESSION_COMMAND]
    ]
    assert session_store.list_records() == [
        make_terminal_record(name="terminal-1", title=None, workdir=None, session_id="$5"),
        make_terminal_record(name="terminal-2", title=None, workdir=None, session_id="$6"),
        make_terminal_record(name="terminal-3", title="Build", workdir="/srv", session_id="$7"),
        make_terminal_record(name="terminal-4", title=None, workdir=None, is_stopped=True),
    ]
    assert {
        name: _session_id_file(terminal_paths, name)
        for name in ("terminal-1", "terminal-2", "terminal-3", "terminal-4")
    } == {"terminal-1": "$5\n", "terminal-2": "$6\n", "terminal-3": "$7\n", "terminal-4": None}
    assert [(record.key, record.status) for record in session_source.list_instances()] == [
        ("terminal-1", InstanceStatus.IDLE),
        ("terminal-2", InstanceStatus.IDLE),
        ("terminal-3", InstanceStatus.IDLE),
        ("terminal-4", InstanceStatus.STOPPED),
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
    assert [record.status for record in session_source.list_instances()] == [
        InstanceStatus.STOPPED
    ]


def test_observe_attached_session_keys_by_id_adopts_by_name_and_ignores_agents(
    fake_tmux: FakeTmux,
    session_store: JsonTerminalSessionStore,
    session_source: TmuxSessionSource,
    terminal_paths: TerminalPaths,
) -> None:
    session_store.save_record(
        make_terminal_record(name="terminal-1", title=None, workdir=None, session_id="$5")
    )
    session_store.save_record(
        make_terminal_record(name="terminal-2", title=None, workdir=None, is_stopped=True)
    )

    assert session_source.observe_attached_session("$5", "renamed") == "terminal-1"
    assert session_source.observe_attached_session("$8", "terminal-2") == "terminal-2"
    assert session_source.observe_attached_session("$9", "hand-made") == "hand-made"
    assert session_source.observe_attached_session("$1", "mngr-alice") is None
    assert session_source.observe_attached_session("$2", "hand made") is None

    assert session_store.list_records()[1] == make_terminal_record(
        name="terminal-2", title=None, workdir=None, session_id="$8"
    )
    assert _session_id_file(terminal_paths, "terminal-2") == "$8\n"


def test_every_terminal_is_stoppable(
    fake_tmux: FakeTmux,
    session_store: JsonTerminalSessionStore,
    session_source: TmuxSessionSource,
) -> None:
    fake_tmux.set_sessions([_session("terminal-1", "$3")])
    session_store.save_record(make_terminal_record(name="terminal-2", title=None, workdir=None))

    assert [record.stoppable for record in session_source.list_instances()] == [True, True]


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
    terminal_paths.sessions_dir.mkdir(parents=True)
    (terminal_paths.sessions_dir / "terminal-1").write_text("$3\n")

    stopped = session_source.stop_instance(InstanceKey("terminal-1"))

    assert (stopped.key, stopped.title, stopped.status) == ("terminal-1", "Build", InstanceStatus.STOPPED)
    assert fake_tmux.session_names() == []
    assert ["kill-session", "-t", "$3"] in fake_tmux.calls()
    assert session_store.list_records() == [
        make_terminal_record(name="terminal-1", title="Build", workdir="/srv", is_stopped=True)
    ]
    assert _session_id_file(terminal_paths, "terminal-1") is None
    # Stopping again is a no-op that answers the same record.
    assert session_source.stop_instance(InstanceKey("terminal-1")) == stopped


def test_stop_of_a_hand_made_session_remembers_it_so_it_can_be_started(
    fake_tmux: FakeTmux,
    session_store: JsonTerminalSessionStore,
    session_source: TmuxSessionSource,
) -> None:
    fake_tmux.set_sessions([_session("scratch", "$3")])

    stopped = session_source.stop_instance(InstanceKey("scratch"))

    assert (stopped.key, stopped.status) == ("scratch", InstanceStatus.STOPPED)
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

    started = session_source.start_instance(InstanceKey("terminal-1"))

    assert (started.key, started.title, started.status) == ("terminal-1", "Build", InstanceStatus.IDLE)
    assert _creates(fake_tmux) == [
        ["new-session", "-d", "-s", "terminal-1", "-c", "/srv", "-P", "-F", "#{session_id}", *TEST_SESSION_COMMAND]
    ]
    assert session_store.list_records() == [
        make_terminal_record(name="terminal-1", title="Build", workdir="/srv", session_id="$1")
    ]
    assert _session_id_file(terminal_paths, "terminal-1") == "$1\n"
    # Starting a running terminal changes nothing.
    assert session_source.start_instance(InstanceKey("terminal-1")).status == InstanceStatus.IDLE
    assert len(_creates(fake_tmux)) == 1


def test_stop_and_start_refuse_unknown_keys_and_agent_sessions(
    fake_tmux: FakeTmux, session_source: TmuxSessionSource
) -> None:
    fake_tmux.set_sessions([_session("mngr-alice", "$1")])

    with pytest.raises(UnknownInstanceError):
        session_source.stop_instance(InstanceKey("ghost"))
    with pytest.raises(UnknownInstanceError):
        session_source.start_instance(InstanceKey("ghost"))
    with pytest.raises(InstanceConflictError, match="non-terminal session"):
        session_source.stop_instance(InstanceKey("mngr-alice"))
    with pytest.raises(InstanceConflictError, match="non-terminal session"):
        session_source.start_instance(InstanceKey("mngr-alice"))
    assert fake_tmux.session_names() == ["mngr-alice"]
