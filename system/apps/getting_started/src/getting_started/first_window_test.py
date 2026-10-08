"""Tests for the first-visit opener: delivered once, held while nobody is connected, retried after a refusal, and
the ledger read back."""

from pathlib import Path

from app_manifest.primitives import AppName
from getting_started.first_window import FIRST_WINDOW_FRAME
from getting_started.first_window import FIRST_WINDOW_PATH
from getting_started.first_window import FirstWindowLedger
from getting_started.first_window import FirstWindowOpener
from workspace_layout.errors import ShellUnreachableError
from workspace_layout.ops import OpenArgs
from workspace_layout.ops import PlaceArgs
from workspace_layout.primitives import ClientId
from workspace_layout.primitives import IfPresent
from workspace_layout.primitives import LayoutOp
from workspace_layout.testing import FAKE_WINDOW_ID
from workspace_layout.testing import FakeShell
from workspace_layout.testing import connected_client

_APP = AppName("getting-started")


def _opener(tmp_path: Path, shell: FakeShell) -> FirstWindowOpener:
    return FirstWindowOpener(app=_APP, ledger=FirstWindowLedger(path=tmp_path / "first_window.json"), shell=shell)


def _open_request(client_id: str) -> OpenArgs:
    return OpenArgs(
        app=_APP, path=FIRST_WINDOW_PATH, if_present=IfPresent.FOCUS, client=ClientId(client_id), desktop="home"
    )


def test_the_window_is_opened_and_placed_for_the_first_connected_client_and_then_never_again(tmp_path: Path) -> None:
    shell = FakeShell(clients=[connected_client("client-b"), connected_client("client-a")])
    opener = _opener(tmp_path, shell)

    first = opener.deliver_once()

    assert first.is_delivered is True and first.client_id == "client-b"
    assert shell.opens == [_open_request("client-b")]
    assert shell.placements == [
        PlaceArgs(window=FAKE_WINDOW_ID, frame=FIRST_WINDOW_FRAME, client=ClientId("client-b"), desktop="home")
    ]
    assert opener.ledger.is_delivered() is True
    # A second attempt (a restart, say) opens nothing: the ledger says so.
    second = _opener(tmp_path, shell).deliver_once()
    assert second.is_delivered is True and second.client_id is None
    assert len(shell.opens) == 1


def test_the_open_is_held_while_nobody_is_connected_or_there_is_no_desktop(tmp_path: Path) -> None:
    shell = FakeShell()
    opener = _opener(tmp_path, shell)
    assert opener.deliver_once().is_delivered is False
    assert shell.opens == []

    shell.clients = [connected_client("client-a")]
    shell.desktop_list = []
    assert opener.deliver_once().is_delivered is False
    assert shell.opens == [] and opener.ledger.is_delivered() is False


def test_the_open_is_held_while_the_shell_cannot_list_its_clients(tmp_path: Path) -> None:
    shell = FakeShell(clients=[connected_client("client-a")], listing_error=ShellUnreachableError("restarting"))
    opener = _opener(tmp_path, shell)

    assert opener.deliver_once().is_delivered is False

    shell.listing_error = None
    assert opener.deliver_once().is_delivered is True
    assert shell.opens == [_open_request("client-a")]


def test_a_refused_open_or_place_leaves_the_delivery_owed(tmp_path: Path) -> None:
    shell = FakeShell(clients=[connected_client("client-a")], refused_ops=[LayoutOp.OPEN])
    opener = _opener(tmp_path, shell)
    assert opener.deliver_once().is_delivered is False
    assert shell.placements == [] and opener.ledger.is_delivered() is False

    shell.refused_ops = [LayoutOp.PLACE]
    assert opener.deliver_once().is_delivered is False
    assert opener.ledger.is_delivered() is False

    shell.refused_ops = []
    assert opener.deliver_once().is_delivered is True
    assert opener.ledger.is_delivered() is True


def test_a_window_the_open_found_popped_out_is_delivered_where_it_is(tmp_path: Path) -> None:
    """The open raised the window in its own window: placing it would be refused, and retrying would raise it again
    every poll, so the delivery is done without a place."""
    shell = FakeShell(clients=[connected_client("client-a")], is_open_raised_in_own_window=True)
    opener = _opener(tmp_path, shell)

    delivery = opener.deliver_once()

    assert delivery.is_delivered is True and delivery.client_id == "client-a"
    assert shell.placements == []
    assert opener.ledger.is_delivered() is True
    assert opener.deliver_once().is_delivered is True
    assert len(shell.opens) == 1


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
    shell = FakeShell(clients=[connected_client("client-a")])
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
    assert len(shell.opens) == 1
