import json

import pytest
from app_instances.errors import InstanceStoreError

from terminal_app.primitives import TmuxSessionName
from terminal_app.store import JsonTerminalSessionStore
from terminal_app.testing import make_terminal_record


def test_store_starts_empty_and_keeps_records_in_creation_order(
    session_store: JsonTerminalSessionStore,
) -> None:
    assert session_store.list_records() == []

    session_store.save_record(
        make_terminal_record(name="terminal-1", title=None, workdir=None)
    )
    session_store.save_record(
        make_terminal_record(name="terminal-2", title=None, workdir="/home/user")
    )

    assert [record.name for record in session_store.list_records()] == [
        "terminal-1",
        "terminal-2",
    ]
    assert json.loads(session_store.store_path.read_text()) == {
        "version": 1,
        "sessions": [
            {"name": "terminal-1", "title": None, "workdir": None},
            {"name": "terminal-2", "title": None, "workdir": "/home/user"},
        ],
    }


def test_save_record_replaces_the_record_with_the_same_name(
    session_store: JsonTerminalSessionStore,
) -> None:
    session_store.save_record(
        make_terminal_record(name="terminal-1", title=None, workdir=None)
    )
    session_store.save_record(
        make_terminal_record(name="terminal-1", title="Build", workdir=None)
    )

    assert session_store.list_records() == [
        make_terminal_record(name="terminal-1", title="Build", workdir=None)
    ]


def test_replace_record_swaps_a_renamed_terminal_in_one_write(
    session_store: JsonTerminalSessionStore,
) -> None:
    session_store.save_record(
        make_terminal_record(name="terminal-1", title=None, workdir="/srv")
    )
    session_store.save_record(
        make_terminal_record(name="terminal-2", title=None, workdir=None)
    )

    session_store.replace_record(
        TmuxSessionName("terminal-1"),
        make_terminal_record(name="build", title="Build", workdir="/srv"),
    )

    assert [record.name for record in session_store.list_records()] == [
        "terminal-2",
        "build",
    ]


def test_remove_record_forgets_a_terminal_and_tolerates_an_absent_one(
    session_store: JsonTerminalSessionStore,
) -> None:
    session_store.save_record(
        make_terminal_record(name="terminal-1", title=None, workdir=None)
    )

    session_store.remove_record(TmuxSessionName("terminal-1"))
    session_store.remove_record(TmuxSessionName("terminal-1"))

    assert session_store.list_records() == []


def test_store_refuses_a_document_of_another_version(
    session_store: JsonTerminalSessionStore,
) -> None:
    session_store.store_path.parent.mkdir(parents=True)
    session_store.store_path.write_text('{"version": 2, "sessions": []}')

    with pytest.raises(InstanceStoreError, match="is version 2"):
        session_store.list_records()
