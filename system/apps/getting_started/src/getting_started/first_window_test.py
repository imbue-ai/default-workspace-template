"""Tests for the first-visit opener: delivered once, held while nobody is connected, retried after a refusal, and
the ledger read back."""

from pathlib import Path
from typing import Any

import pytest
from flask import Flask
from flask import jsonify
from flask import request
from flask.typing import ResponseReturnValue

from app_manifest.primitives import AppName
from getting_started.errors import ShellAnswerError
from getting_started.first_window import FIRST_WINDOW_FRAME
from getting_started.first_window import FIRST_WINDOW_PATH
from getting_started.first_window import FirstWindowLedger
from getting_started.first_window import FirstWindowOpener
from getting_started.first_window import HttpShellOps
from getting_started.first_window import connected_client_ids_of
from getting_started.first_window import first_desktop_id_of
from getting_started.first_window import open_op_body
from getting_started.first_window import place_op_body
from getting_started.serving import serve_in_background
from getting_started.testing import FakeShellOps

_APP = AppName("getting-started")


def _opener(tmp_path: Path, shell: FakeShellOps) -> FirstWindowOpener:
    return FirstWindowOpener(app=_APP, ledger=FirstWindowLedger(path=tmp_path / "first_window.json"), shell=shell)


def test_the_window_is_opened_and_placed_for_the_first_connected_client_and_then_never_again(tmp_path: Path) -> None:
    shell = FakeShellOps(client_ids=["client-b", "client-a"])
    opener = _opener(tmp_path, shell)

    first = opener.deliver_once()

    assert first.is_delivered is True and first.client_id == "client-b"
    assert shell.opened == [("getting-started", FIRST_WINDOW_PATH, "client-b", "home")]
    assert shell.placed == [("win-0123456789abcdef", FIRST_WINDOW_FRAME, "client-b", "home")]
    assert opener.ledger.is_delivered() is True
    # A second attempt (a restart, say) opens nothing: the ledger says so.
    second = _opener(tmp_path, shell).deliver_once()
    assert second.is_delivered is True and second.client_id is None
    assert len(shell.opened) == 1


def test_the_open_is_held_while_nobody_is_connected_or_there_is_no_desktop(tmp_path: Path) -> None:
    shell = FakeShellOps(client_ids=[])
    opener = _opener(tmp_path, shell)
    assert opener.deliver_once().is_delivered is False
    assert shell.opened == []

    shell.client_ids = ["client-a"]
    shell.desktop_id = None
    assert opener.deliver_once().is_delivered is False
    assert shell.opened == [] and opener.ledger.is_delivered() is False


def test_a_refused_open_or_place_leaves_the_delivery_owed(tmp_path: Path) -> None:
    shell = FakeShellOps(client_ids=["client-a"], is_open_refused=True)
    opener = _opener(tmp_path, shell)
    assert opener.deliver_once().is_delivered is False
    assert shell.placed == [] and opener.ledger.is_delivered() is False

    shell.is_open_refused = False
    shell.is_place_refused = True
    assert opener.deliver_once().is_delivered is False
    assert opener.ledger.is_delivered() is False

    shell.is_place_refused = False
    assert opener.deliver_once().is_delivered is True
    assert opener.ledger.is_delivered() is True


def test_the_ledger_reads_an_absent_or_malformed_file_as_undelivered(tmp_path: Path) -> None:
    ledger = FirstWindowLedger(path=tmp_path / "first_window.json")
    assert ledger.is_delivered() is False
    ledger.path.write_text("not json")
    assert ledger.is_delivered() is False
    ledger.path.write_text('{"is_delivered": "yes"}')
    assert ledger.is_delivered() is False
    ledger.mark_delivered()
    assert ledger.is_delivered() is True


def test_the_thread_stops_once_delivered_and_never_starts_when_already_delivered(tmp_path: Path) -> None:
    shell = FakeShellOps(client_ids=["client-a"])
    opener = _opener(tmp_path, shell)
    opener.start()
    # The thread returns once it has delivered; a stop before its first attempt would find nothing to assert on.
    assert opener._thread is not None
    opener._thread.join(timeout=5)
    assert opener._thread.is_alive() is False
    opener.stop()
    assert opener.ledger.is_delivered() is True
    again = _opener(tmp_path, shell)
    again.start()
    again.stop()
    assert len(shell.opened) == 1


def test_the_op_bodies_name_the_client_the_desktop_and_a_focus_open() -> None:
    assert open_op_body(_APP, "/", "client-a", "home") == {
        "op": "open",
        "args": {
            "app": "getting-started",
            "path": "/",
            "client": "client-a",
            "desktop": "home",
            "if_present": "focus",
        },
        "requester": None,
    }
    assert place_op_body("win-1", FIRST_WINDOW_FRAME, "client-a", "home") == {
        "op": "place",
        "args": {"window": "win-1", "frame": FIRST_WINDOW_FRAME, "client": "client-a", "desktop": "home"},
        "requester": None,
    }


def test_the_shells_answers_are_read_by_their_contract_shapes() -> None:
    clients = {
        "clients": [
            {"id": "a", "is_connected": True},
            {"id": "b", "is_connected": False},
            {"id": "", "is_connected": True},
            "junk",
        ]
    }
    assert connected_client_ids_of(clients) == ["a"]
    assert first_desktop_id_of({"desktops": [{"id": "home"}, {"id": "work"}]}) == "home"
    assert first_desktop_id_of({"desktops": []}) is None
    with pytest.raises(ShellAnswerError):
        connected_client_ids_of({"clients": "none"})
    with pytest.raises(ShellAnswerError):
        first_desktop_id_of(["home"])
    with pytest.raises(ShellAnswerError):
        first_desktop_id_of({"desktops": [{"name": "Home"}]})


def test_the_http_shell_ops_read_the_shell_and_post_the_ops() -> None:
    """The loopback adapter against a stub shell: the connected clients and the first desktop are read off their
    routes, the two ops go to the broadcast route as their bodies spell them, the open's answer yields the window id,
    and a refusal, an answer of another shape, or a shell that cannot be reached each read as no answer."""
    posted: list[dict[str, Any]] = []
    # What the broadcast route does next: "accept", "refuse", "list" (a JSON array), or "nameless" (no window_id).
    behaviour = {"mode": "accept"}
    stub = Flask("stub-shell")

    @stub.get("/api/clients")
    def clients() -> ResponseReturnValue:
        return jsonify({"clients": [{"id": "c1", "is_connected": True}, {"id": "c2", "is_connected": False}]})

    @stub.get("/api/desktops")
    def desktops() -> ResponseReturnValue:
        return jsonify({"desktops": [{"id": "home"}, {"id": "work"}]})

    @stub.post("/api/layout/broadcast")
    def broadcast() -> ResponseReturnValue:
        body = request.get_json()
        posted.append(body)
        match behaviour["mode"]:
            case "refuse":
                return jsonify({"detail": "no such client"}), 404
            case "list":
                return jsonify([])
            case "nameless":
                return jsonify({"window_id": None})
            case _:
                return jsonify({"window_id": "win-1" if body["op"] == "open" else None})

    with serve_in_background("127.0.0.1", 0, stub) as server:
        port = server.socket.getsockname()[1]
        shell = HttpShellOps(shell_url=f"http://127.0.0.1:{port}")

        assert shell.connected_client_ids() == ["c1"]
        assert shell.first_desktop_id() == "home"
        assert shell.open_window(_APP, FIRST_WINDOW_PATH, "c1", "home") == "win-1"
        assert shell.place_window("win-1", FIRST_WINDOW_FRAME, "c1", "home") is True
        assert posted == [
            open_op_body(_APP, FIRST_WINDOW_PATH, "c1", "home"),
            place_op_body("win-1", FIRST_WINDOW_FRAME, "c1", "home"),
        ]

        behaviour["mode"] = "refuse"
        assert shell.open_window(_APP, FIRST_WINDOW_PATH, "c1", "home") is None
        assert shell.place_window("win-1", FIRST_WINDOW_FRAME, "c1", "home") is False
        behaviour["mode"] = "list"
        assert shell.open_window(_APP, FIRST_WINDOW_PATH, "c1", "home") is None
        behaviour["mode"] = "nameless"
        assert shell.open_window(_APP, FIRST_WINDOW_PATH, "c1", "home") is None

    # The server is gone: every read and op is a non-answer rather than an error.
    unreachable = HttpShellOps(shell_url=f"http://127.0.0.1:{port}")
    assert unreachable.connected_client_ids() == []
    assert unreachable.first_desktop_id() is None
    assert unreachable.open_window(_APP, FIRST_WINDOW_PATH, "c1", "home") is None
    assert unreachable.place_window("win-1", FIRST_WINDOW_FRAME, "c1", "home") is False
