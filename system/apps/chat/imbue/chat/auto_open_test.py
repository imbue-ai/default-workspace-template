"""Tests for the reactor that surfaces an app-launched chat's tab: once, to the clients connected when it can."""

import json
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from typing import Any

import pytest
from app_instances.sidecar import serve_in_background
from app_instances.testing import LOOPBACK_HOST
from app_instances.testing import free_port
from flask import Flask
from flask import jsonify

from imbue.chat.auto_open import AUTO_OPEN_FRESHNESS
from imbue.chat.auto_open import AutoOpenLedger
from imbue.chat.auto_open import AutoOpenReactor
from imbue.chat.auto_open import DisconnectedShell
from imbue.chat.auto_open import ShellLayoutClient
from imbue.chat.auto_open import is_auto_open_labeled
from imbue.chat.testing import RecordingShell

_NOW = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)
_LABELED = {"assist": "true"}


def _clock() -> datetime:
    return _NOW


def _reactor(shell: RecordingShell, ledger: AutoOpenLedger | None = None) -> AutoOpenReactor:
    return AutoOpenReactor(
        ledger=ledger if ledger is not None else AutoOpenLedger(path=None), shell=shell, clock=_clock
    )


def test_only_the_two_auto_open_labels_ask_for_a_tab() -> None:
    assert is_auto_open_labeled({"assist": "true"})
    assert is_auto_open_labeled({"auto_open": "true", "user_created": "true"})
    assert not is_auto_open_labeled({"assist": "false"})
    assert not is_auto_open_labeled({"user_created": "true"})


def test_a_labeled_chat_is_opened_once_in_every_connected_client_and_recorded() -> None:
    shell = RecordingShell(client_ids=["c1", "c2"])
    reactor = _reactor(shell)

    reactor.note_appeared("chat-1", _LABELED, _NOW)
    reactor.flush()
    reactor.flush()

    assert shell.opens == [("chat-1", "c1"), ("chat-1", "c2")]
    assert reactor.ledger.is_delivered("chat-1")
    assert reactor.pending_agent_ids() == set()


def test_an_unlabeled_chat_is_ignored() -> None:
    shell = RecordingShell(client_ids=["c1"])
    reactor = _reactor(shell)

    reactor.note_appeared("chat-1", {"user_created": "true"}, _NOW)
    reactor.flush()

    assert shell.opens == []
    assert not reactor.ledger.is_delivered("chat-1")


def test_with_no_client_the_open_is_held_until_one_arrives() -> None:
    """The app starts the chat while the user is still on their way in; the open must wait for them."""
    shell = RecordingShell()
    reactor = _reactor(shell)
    reactor.note_appeared("chat-1", _LABELED, _NOW)

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
    reactor.note_appeared("chat-1", _LABELED, _NOW)

    reactor.flush()

    assert reactor.pending_agent_ids() == {"chat-1"}
    assert not reactor.ledger.is_delivered("chat-1")


def test_a_delivered_chat_survives_a_ledger_reload(tmp_path: Path) -> None:
    """The update run restarts this app; the tab it already surfaced must not pop again."""
    path = tmp_path / "ledger.json"
    first = _reactor(RecordingShell(client_ids=["c1"]), AutoOpenLedger(path=path))
    first.note_appeared("chat-1", _LABELED, _NOW)
    first.flush()

    shell = RecordingShell(client_ids=["c1"])
    second = _reactor(shell, AutoOpenLedger(path=path))
    second.note_appeared("chat-1", _LABELED, _NOW)
    second.flush()

    assert shell.opens == []


def test_the_startup_seed_holds_a_fresh_undelivered_chat_and_settles_the_rest() -> None:
    """A recent labeled chat nobody was shown is still owed its tab after a restart; an old one, or
    one already delivered, is left as the saved layout has it and never pops later."""
    ledger = AutoOpenLedger(path=None)
    ledger.mark_delivered("delivered")
    shell = RecordingShell()
    reactor = _reactor(shell, ledger)

    reactor.seed_at_startup(
        {
            "fresh": (_LABELED, _NOW - timedelta(hours=1)),
            "old": (_LABELED, _NOW - AUTO_OPEN_FRESHNESS - timedelta(minutes=1)),
            "delivered": (_LABELED, _NOW),
            "plain": ({"user_created": "true"}, _NOW),
        }
    )

    assert reactor.pending_agent_ids() == {"fresh"}
    assert ledger.is_delivered("old")
    assert not ledger.is_delivered("plain")
    shell.client_ids = ["c1"]
    reactor.flush()
    assert shell.opens == [("fresh", "c1")]


def test_a_held_open_expires_with_the_chats_freshness() -> None:
    shell = RecordingShell()
    reactor = _reactor(shell)
    reactor.note_appeared("chat-1", _LABELED, _NOW - AUTO_OPEN_FRESHNESS - timedelta(seconds=1))

    shell.client_ids = ["c1"]
    reactor.flush()

    assert shell.opens == []
    assert reactor.pending_agent_ids() == set()
    assert reactor.ledger.is_delivered("chat-1")


def test_a_chat_with_no_creation_time_is_held_from_when_it_was_seen_rather_than_forever() -> None:
    """mngr always says when it made an agent, but the reactor's input allows it not to; an open
    that could never go stale would be retried for the life of the process and pop a tab of any
    age at whoever eventually connected."""
    now = _NOW
    shell = RecordingShell()
    reactor = AutoOpenReactor(ledger=AutoOpenLedger(path=None), shell=shell, clock=lambda: now)
    reactor.note_appeared("chat-1", _LABELED, None)

    reactor.flush()
    assert reactor.pending_agent_ids() == {"chat-1"}

    now = _NOW + AUTO_OPEN_FRESHNESS + timedelta(seconds=1)
    shell.client_ids = ["c1"]
    reactor.flush()

    assert shell.opens == []
    assert reactor.pending_agent_ids() == set()
    assert reactor.ledger.is_delivered("chat-1")


def test_a_removed_chat_is_forgotten_everywhere() -> None:
    ledger = AutoOpenLedger(path=None)
    reactor = _reactor(RecordingShell(), ledger)
    reactor.note_appeared("pending", _LABELED, _NOW)
    ledger.mark_delivered("done")

    reactor.forget("pending")
    reactor.forget("done")

    assert reactor.pending_agent_ids() == set()
    assert not ledger.is_delivered("done")


def test_a_ledger_of_the_wrong_shape_starts_empty_with_a_warning(tmp_path: Path, loguru_records: list[str]) -> None:
    """Starting empty in silence is the one failure that re-pops every delivered tab."""
    path = tmp_path / "ledger.json"
    path.write_text(json.dumps(["chat-1"]))

    ledger = AutoOpenLedger(path=path)

    assert not ledger.is_delivered("chat-1")
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
