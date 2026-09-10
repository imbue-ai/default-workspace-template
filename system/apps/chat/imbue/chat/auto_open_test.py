"""Tests for the reactor that surfaces an app-launched chat's tab: once, to the clients connected when it can."""

import json
from pathlib import Path
from typing import Any

import pytest
from app_instances.sidecar import serve_in_background
from app_instances.testing import LOOPBACK_HOST
from app_instances.testing import free_port
from flask import Flask
from flask import jsonify

from imbue.chat.auto_open import AutoOpenLedger
from imbue.chat.auto_open import AutoOpenReactor
from imbue.chat.auto_open import DisconnectedShell
from imbue.chat.auto_open import ShellLayoutClient
from imbue.chat.auto_open import is_auto_open_labeled
from imbue.chat.testing import RecordingShell

_LABELED = {"assist": "true"}


def _reactor(shell: RecordingShell, ledger: AutoOpenLedger | None = None) -> AutoOpenReactor:
    return AutoOpenReactor(ledger=ledger if ledger is not None else AutoOpenLedger(path=None), shell=shell)


def test_only_the_two_auto_open_labels_ask_for_a_tab() -> None:
    assert is_auto_open_labeled({"assist": "true"})
    assert is_auto_open_labeled({"auto_open": "true", "user_created": "true"})
    assert not is_auto_open_labeled({"assist": "false"})
    assert not is_auto_open_labeled({"user_created": "true"})


def test_a_labeled_chat_is_opened_once_in_every_connected_client_and_recorded() -> None:
    shell = RecordingShell(client_ids=["c1", "c2"])
    reactor = _reactor(shell)

    reactor.note_appeared("chat-1", _LABELED)
    reactor.flush()
    reactor.flush()

    assert shell.opens == [("chat-1", "c1"), ("chat-1", "c2")]
    assert reactor.ledger.is_delivered("chat-1")
    assert reactor.pending_agent_ids() == set()


def test_an_unlabeled_chat_is_ignored() -> None:
    shell = RecordingShell(client_ids=["c1"])
    reactor = _reactor(shell)

    reactor.note_appeared("chat-1", {"user_created": "true"})
    reactor.flush()

    assert shell.opens == []
    assert not reactor.ledger.is_delivered("chat-1")


def test_with_no_client_the_open_is_held_until_one_arrives() -> None:
    """The app starts the chat while the user is still on their way in; the open must wait for them."""
    shell = RecordingShell()
    reactor = _reactor(shell)
    reactor.note_appeared("chat-1", _LABELED)

    reactor.flush()
    assert shell.opens == []
    assert reactor.pending_agent_ids() == {"chat-1"}
    assert not reactor.ledger.is_delivered("chat-1")

    shell.client_ids = ["c1"]
    reactor.flush()
    assert shell.opens == [("chat-1", "c1")]
    assert reactor.ledger.is_delivered("chat-1")


def test_a_refused_open_keeps_the_chat_pending() -> None:
    shell = RecordingShell(client_ids=["c1"], refused_client_ids=["c1"])
    reactor = _reactor(shell)
    reactor.note_appeared("chat-1", _LABELED)

    reactor.flush()

    assert reactor.pending_agent_ids() == {"chat-1"}
    assert not reactor.ledger.is_delivered("chat-1")


def test_a_delivered_chat_survives_a_ledger_reload(tmp_path: Path) -> None:
    """The update run restarts this app; the tab it already surfaced must not pop again."""
    path = tmp_path / "ledger.json"
    first = _reactor(RecordingShell(client_ids=["c1"]), AutoOpenLedger(path=path))
    first.note_appeared("chat-1", _LABELED)
    first.flush()

    shell = RecordingShell(client_ids=["c1"])
    second = _reactor(shell, AutoOpenLedger(path=path))
    second.note_appeared("chat-1", _LABELED)
    second.flush()

    assert shell.opens == []


def test_the_startup_seed_holds_every_undelivered_chat_the_ledger_does_not_name() -> None:
    """A labeled chat nobody was shown is still owed its tab after a restart, however long it has
    waited; one the ledger names is left as the saved layout has it and never pops later."""
    ledger = AutoOpenLedger(path=None)
    ledger.mark_delivered("delivered")
    shell = RecordingShell()
    reactor = _reactor(shell, ledger)

    reactor.seed_at_startup(
        {"waiting": _LABELED, "delivered": _LABELED, "plain": {"user_created": "true"}},
    )

    assert reactor.pending_agent_ids() == {"waiting"}
    assert not ledger.is_delivered("plain")
    shell.client_ids = ["c1"]
    reactor.flush()
    assert shell.opens == [("waiting", "c1")]


def test_a_workspace_with_no_ledger_adopts_what_it_already_has_instead_of_popping_every_tab(
    tmp_path: Path,
) -> None:
    """The first boot that keeps a ledger meets every chat the app ever labeled here, going back to
    the workspace's first day, and cannot tell the one owed a tab from the rest -- so it opens none
    of them, and leaves the ledger the next boot reads for real."""
    path = tmp_path / "ledger.json"
    shell = RecordingShell(client_ids=["c1"])
    reactor = _reactor(shell, AutoOpenLedger(path=path))

    reactor.seed_at_startup({"old-1": _LABELED, "old-2": _LABELED, "plain": {"user_created": "true"}})
    reactor.flush()

    assert shell.opens == []
    assert path.exists()

    next_boot = _reactor(shell, AutoOpenLedger(path=path))
    next_boot.seed_at_startup({"old-1": _LABELED, "old-2": _LABELED, "since": _LABELED})
    next_boot.flush()

    assert shell.opens == [("since", "c1")]


def test_a_fresh_workspace_adopting_nothing_still_leaves_a_ledger_behind(tmp_path: Path) -> None:
    """Without the file the next boot cannot tell "nothing was ever delivered here" from "the record
    is gone", and would adopt away the very chat this feature exists to surface."""
    path = tmp_path / "ledger.json"
    shell = RecordingShell()
    reactor = _reactor(shell, AutoOpenLedger(path=path))

    reactor.seed_at_startup({})

    assert path.exists()
    assert AutoOpenLedger(path=path).is_history_known


def test_a_removed_chat_is_forgotten_everywhere() -> None:
    ledger = AutoOpenLedger(path=None)
    reactor = _reactor(RecordingShell(), ledger)
    reactor.note_appeared("pending", _LABELED)
    ledger.mark_delivered("done")

    reactor.forget("pending")
    reactor.forget("done")

    assert reactor.pending_agent_ids() == set()
    assert not ledger.is_delivered("done")


def test_a_ledger_of_the_wrong_shape_starts_empty_and_says_its_history_is_gone(
    tmp_path: Path, loguru_records: list[str]
) -> None:
    """Reading it as an empty history rather than a lost one re-pops every tab it named."""
    path = tmp_path / "ledger.json"
    path.write_text(json.dumps(["chat-1"]))

    ledger = AutoOpenLedger(path=path)

    assert not ledger.is_delivered("chat-1")
    assert not ledger.is_history_known
    assert any("wrong shape" in record for record in loguru_records)


def test_the_disconnected_shell_reaches_nobody() -> None:
    shell = DisconnectedShell()
    assert shell.connected_client_ids() == []
    assert shell.open_chat("chat-1", "c1") is False


@pytest.mark.parametrize(
    ("body", "expected"),
    (
        ({"clients": [{"id": "c1", "is_connected": True}, {"id": "c2", "is_connected": False}]}, ["c1"]),
        ({"clients": []}, []),
        ({}, []),
        ({"clients": {"c1": True}}, []),
        ({"clients": "c1"}, []),
        ([{"id": "c1", "is_connected": True}], []),
        ({"clients": [{"is_connected": True}, "c2"]}, []),
    ),
    ids=("the-contract", "nobody", "no-key", "a-map", "a-string", "a-bare-list", "entries-without-an-id"),
)
def test_a_client_list_of_the_wrong_shape_reads_as_nobody_rather_than_killing_the_flush_thread(
    body: Any, expected: list[str]
) -> None:
    """The flush thread's own catch does not cover a KeyError or TypeError from reading this, so an
    answer the shell should never give would end the thread and silently stop surfacing every tab."""
    application = Flask("stub-shell")
    application.add_url_rule("/api/clients", view_func=lambda: jsonify(body), endpoint="clients")
    port = free_port()

    with serve_in_background(LOOPBACK_HOST, port, application):
        assert ShellLayoutClient(shell_url=f"http://{LOOPBACK_HOST}:{port}").connected_client_ids() == expected
