import os
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

from terminal_app.data_types import TmuxSession
from terminal_app.sessions import TmuxSessionSource, is_agent_session
from terminal_app.store import JsonTerminalSessionStore
from terminal_app.testing import FakeTmux, make_terminal_record

_NEW = ActionId("new")
_ACTIVITY = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)


def _session(name: str, session_id: str) -> TmuxSession:
    return TmuxSession(name=name, session_id=session_id, last_activity=_ACTIVITY)


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


def test_list_is_empty_without_a_tmux_server_or_a_store(
    fake_tmux: FakeTmux, session_source: TmuxSessionSource
) -> None:
    (fake_tmux.state_dir / "sessions.tsv").unlink()

    assert session_source.list_instances() == []


def test_create_allocates_the_lowest_free_number_over_live_and_remembered_names(
    fake_tmux: FakeTmux,
    session_store: JsonTerminalSessionStore,
    session_source: TmuxSessionSource,
) -> None:
    fake_tmux.set_sessions([_session("terminal-1", "$3"), _session("terminal-3", "$4")])
    session_store.save_record(
        make_terminal_record(name="terminal-2", title=None, workdir=None)
    )

    created = session_source.create_instance(_NEW, {"workdir": "/home/user/workspace"})

    assert created.key == "terminal-4"
    assert created.status == InstanceStatus.STOPPED
    assert (
        created.url
        == "/?arg=_&arg=session&arg=terminal-4&arg={tab}&arg=%2Fhome%2Fuser%2Fworkspace"
    )
    assert [record.name for record in session_store.list_records()] == [
        "terminal-2",
        "terminal-4",
    ]
    # The session itself is created on first attach, so no tmux command ran beyond the listing.
    assert all(call[0] == "list-sessions" for call in fake_tmux.calls())


def test_two_creates_before_any_attach_get_distinct_names(
    session_source: TmuxSessionSource,
) -> None:
    first = session_source.create_instance(_NEW, {})
    second = session_source.create_instance(_NEW, {"workdir": ""})

    assert (first.key, second.key) == ("terminal-1", "terminal-2")
    # A create that names no directory starts the shell where this app runs.
    own_directory = urllib.parse.quote(os.getcwd(), safe="")
    assert second.url == f"/?arg=_&arg=session&arg=terminal-2&arg={{tab}}&arg={own_directory}"


def test_create_refuses_other_actions_and_other_params(
    session_source: TmuxSessionSource,
) -> None:
    with pytest.raises(UnknownActionError, match="only declares 'new'"):
        session_source.create_instance(ActionId("split"), {})
    with pytest.raises(InvalidParamsError, match="unknown params \\['path'\\]"):
        session_source.create_instance(_NEW, {"path": "/"})
    with pytest.raises(InvalidParamsError, match="invalid 'workdir'"):
        session_source.create_instance(_NEW, {"workdir": "/tmp/\x00"})


def test_delete_kills_the_session_and_forgets_it(
    fake_tmux: FakeTmux,
    session_store: JsonTerminalSessionStore,
    session_source: TmuxSessionSource,
) -> None:
    fake_tmux.set_sessions([_session("terminal-1", "$3")])
    session_store.save_record(
        make_terminal_record(name="terminal-1", title=None, workdir=None)
    )

    session_source.delete_instance(InstanceKey("terminal-1"))

    assert fake_tmux.session_names() == []
    assert session_store.list_records() == []
    assert ["kill-session", "-t", "=terminal-1"] in fake_tmux.calls()


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


def test_rename_canonicalizes_the_title_renames_in_tmux_and_rekeys_the_record(
    fake_tmux: FakeTmux,
    session_store: JsonTerminalSessionStore,
    session_source: TmuxSessionSource,
) -> None:
    fake_tmux.set_sessions([_session("terminal-1", "$3")])
    session_store.save_record(
        make_terminal_record(name="terminal-1", title=None, workdir="/srv")
    )

    renamed = session_source.rename_instance(
        InstanceKey("terminal-1"), InstanceTitle("My Build")
    )

    assert renamed.key == "My-Build"
    assert renamed.title == "My Build"
    assert renamed.status == InstanceStatus.IDLE
    assert renamed.url == "/?arg=_&arg=session&arg=My-Build&arg={tab}&arg=%2Fsrv"
    assert ["rename-session", "-t", "=terminal-1", "My-Build"] in fake_tmux.calls()
    assert fake_tmux.session_names() == ["My-Build"]
    assert session_store.list_records() == [
        make_terminal_record(name="My-Build", title="My Build", workdir="/srv")
    ]


def test_rename_of_a_live_session_the_store_never_saw_remembers_it(
    fake_tmux: FakeTmux,
    session_store: JsonTerminalSessionStore,
    session_source: TmuxSessionSource,
) -> None:
    fake_tmux.set_sessions([_session("terminal-1", "$3")])

    session_source.rename_instance(InstanceKey("terminal-1"), InstanceTitle("Build"))

    assert [record.name for record in session_store.list_records()] == ["Build"]


def test_rename_to_a_title_that_canonicalizes_to_the_same_name_only_stores_the_title(
    fake_tmux: FakeTmux,
    session_store: JsonTerminalSessionStore,
    session_source: TmuxSessionSource,
) -> None:
    fake_tmux.set_sessions([_session("terminal-1", "$3")])

    renamed = session_source.rename_instance(
        InstanceKey("terminal-1"), InstanceTitle("terminal 1")
    )

    assert renamed.key == "terminal-1"
    assert renamed.title == "terminal 1"
    assert not any(call[0] == "rename-session" for call in fake_tmux.calls())
    assert session_store.list_records()[0].title == "terminal 1"


def test_rename_of_a_stopped_terminal_rekeys_the_record_without_touching_tmux(
    fake_tmux: FakeTmux,
    session_store: JsonTerminalSessionStore,
    session_source: TmuxSessionSource,
) -> None:
    session_store.save_record(
        make_terminal_record(name="terminal-1", title=None, workdir=None)
    )

    renamed = session_source.rename_instance(
        InstanceKey("terminal-1"), InstanceTitle("Later")
    )

    assert (renamed.key, renamed.status) == ("Later", InstanceStatus.STOPPED)
    assert not any(call[0] == "rename-session" for call in fake_tmux.calls())


@pytest.mark.parametrize(
    ("title", "expected_problem"),
    [
        ("...", "contains no usable characters"),
        ("x" * 200, "over the 128-character limit"),
    ],
)
def test_rename_refuses_a_title_that_makes_no_session_name(
    fake_tmux: FakeTmux,
    session_source: TmuxSessionSource,
    title: str,
    expected_problem: str,
) -> None:
    fake_tmux.set_sessions([_session("terminal-1", "$3")])

    with pytest.raises(InvalidInstanceValueError, match=expected_problem):
        session_source.rename_instance(InstanceKey("terminal-1"), InstanceTitle(title))


def test_rename_refuses_a_name_another_terminal_holds_case_insensitively(
    fake_tmux: FakeTmux,
    session_store: JsonTerminalSessionStore,
    session_source: TmuxSessionSource,
) -> None:
    fake_tmux.set_sessions([_session("terminal-1", "$3"), _session("build", "$4")])
    session_store.save_record(
        make_terminal_record(name="deploy", title=None, workdir=None)
    )

    with pytest.raises(InstanceConflictError, match="already named 'Build'"):
        session_source.rename_instance(
            InstanceKey("terminal-1"), InstanceTitle("Build")
        )
    with pytest.raises(InstanceConflictError, match="already named 'Deploy'"):
        session_source.rename_instance(
            InstanceKey("terminal-1"), InstanceTitle("Deploy")
        )
    assert fake_tmux.session_names() == ["terminal-1", "build"]


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
    with pytest.raises(InstanceConflictError, match="non-terminal session"):
        session_source.rename_instance(InstanceKey("ghost"), InstanceTitle("mngr-bob"))


def test_set_location_is_not_tracked(session_source: TmuxSessionSource) -> None:
    with pytest.raises(LocationNotTrackedError):
        session_source.set_location(InstanceKey("terminal-1"), LocationPath("/"))
