"""Tests for port parking: the held port, the handoff on the first connection, and the pages."""

import re
from collections.abc import Callable
from collections.abc import Iterator
from pathlib import Path

import pytest

from imbue.mngr.utils.polling import wait_for
from imbue.system_interface.shell.errors import AppLifecycleRefusedError
from imbue.system_interface.shell.errors import PortInUseError
from imbue.system_interface.shell.port_parking import FAILED_REFRESH_SECONDS
from imbue.system_interface.shell.port_parking import ParkedPageKind
from imbue.system_interface.shell.port_parking import ParkedPort
from imbue.system_interface.shell.port_parking import ParkingTarget
from imbue.system_interface.shell.port_parking import SHELL_HANDSHAKE_TYPE
from imbue.system_interface.shell.port_parking import SHELL_START_WITH_TEXT_TYPE
from imbue.system_interface.shell.port_parking import STARTING_REFRESH_SECONDS
from imbue.system_interface.shell.port_parking import parked_page_html
from imbue.system_interface.shell.port_parking import parked_response_bytes
from imbue.system_interface.shell.port_parking import parking_target_of
from imbue.system_interface.testing import can_bind_loopback_port
from imbue.system_interface.testing import send_raw_get_over_socket


@pytest.fixture
def parked_port(closed_port: int) -> Iterator[ParkedPort]:
    port = _parked_port(closed_port, lambda: ParkedPageKind.STARTING)
    try:
        yield port
    finally:
        port.release()


def _parked_port(port: int, on_first_connection: Callable[[], ParkedPageKind]) -> ParkedPort:
    return ParkedPort(
        app="docs",
        target=ParkingTarget(host="127.0.0.1", port=port),
        on_first_connection=on_first_connection,
        page_for=lambda kind: parked_page_html(kind, "Docs", "docs"),
    )


def test_parking_targets_only_loopback_urls_with_a_port() -> None:
    assert parking_target_of("http://localhost:8300") == ParkingTarget(host="localhost", port=8300)
    assert parking_target_of("http://127.0.0.1:8300/") == ParkingTarget(host="127.0.0.1", port=8300)
    assert parking_target_of("http://[::1]:8300") == ParkingTarget(host="::1", port=8300)
    assert parking_target_of("http://localhost") is None
    assert parking_target_of("http://10.0.0.2:8300") is None
    assert parking_target_of("not a url") is None


def test_a_parked_port_answers_its_first_connection_with_the_loading_page_and_lets_go(closed_port: int) -> None:
    woken: list[str] = []

    def wake() -> ParkedPageKind:
        woken.append("docs")
        return ParkedPageKind.STARTING

    parked_port = _parked_port(closed_port, wake)
    parked_port.start()
    assert parked_port.is_woken is False

    answer = send_raw_get_over_socket(parked_port.target.port)

    head, _, body = answer.partition(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 503 Service Unavailable\r\n")
    assert b"Connection: close" in head and b"Cache-Control: no-store" in head
    assert f"Retry-After: {STARTING_REFRESH_SECONDS}".encode() in head
    assert b"Starting Docs" in body and f'content="{STARTING_REFRESH_SECONDS}"'.encode() in body
    # It names no cause: the app may have stopped for want of a window, a Quit, an agent, or on its own.
    assert b"This opens on its own once it's ready." in body and b"nothing showed it" not in body
    assert woken == ["docs"]
    assert parked_port.is_woken is True
    wait_for(lambda: can_bind_loopback_port(parked_port.target.port), timeout=5.0, poll_interval=0.02)


def test_a_wake_that_fails_is_told_so_and_waits_for_the_persons_try_again(closed_port: int) -> None:
    parked_port = _parked_port(closed_port, lambda: ParkedPageKind.FAILED)
    parked_port.start()

    answer = send_raw_get_over_socket(parked_port.target.port)

    head, _, body = answer.partition(b"\r\n\r\n")
    assert b"<h1>Docs couldn't start.</h1>" in body
    assert b'onclick="location.reload()">Try again</button>' in body
    assert b"<summary>See details</summary>" in body and b"supervisorctl tail docs stderr" in body
    # A reload is a wake, which spends the budget, so the page never reloads itself; a fetch is told to wait.
    assert b"http-equiv" not in body
    assert f"Retry-After: {FAILED_REFRESH_SECONDS}".encode() in head


def test_a_wake_the_budget_refused_says_the_app_failed_several_times(closed_port: int) -> None:
    parked_port = _parked_port(closed_port, lambda: ParkedPageKind.BUDGET_SPENT)
    parked_port.start()

    body = send_raw_get_over_socket(parked_port.target.port).partition(b"\r\n\r\n")[2]

    assert b"<h1>Docs couldn't start.</h1>" in body
    assert (
        b"Docs failed to start several times in the last few minutes, so it wasn't started again. "
        b"Try again in a few minutes." in body
    )
    assert b">Try again</button>" in body and b"supervisorctl tail docs stderr" in body
    assert b"http-equiv" not in body


def test_a_wake_that_raises_still_answers_the_request(closed_port: int) -> None:
    def explode() -> ParkedPageKind:
        raise AppLifecycleRefusedError("supervisord went away")

    parked_port = _parked_port(closed_port, explode)
    parked_port.start()

    assert b"Docs couldn't start." in send_raw_get_over_socket(parked_port.target.port)


class _UnexpectedWakeError(Exception):
    """An error outside the kinds the parker answers a request for."""


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_a_wake_that_raises_unexpectedly_still_closes_the_connection(closed_port: int) -> None:
    """The exception is the accept thread's to report (the filtered warning); the requester sees its connection
    end (closed, or reset since its request was never read) rather than waiting on an answer that never comes."""

    def explode() -> ParkedPageKind:
        raise _UnexpectedWakeError("the inventory refresh raised")

    parked_port = _parked_port(closed_port, explode)
    parked_port.start()

    try:
        answer = send_raw_get_over_socket(parked_port.target.port)
    except ConnectionResetError:
        answer = b""
    # The release joins the accept thread, so its report lands in this test rather than the next one's.
    parked_port.release()

    assert answer == b""


def test_a_port_something_listens_on_is_not_parked(listening_port: int) -> None:
    with pytest.raises(PortInUseError):
        _parked_port(listening_port, lambda: ParkedPageKind.STARTING).start()


def test_release_before_any_connection_frees_the_port(parked_port: ParkedPort) -> None:
    parked_port.start()
    parked_port.release()

    assert can_bind_loopback_port(parked_port.target.port)
    assert parked_port.is_woken is False


def test_the_response_bytes_carry_the_page_length() -> None:
    page = parked_page_html(ParkedPageKind.STARTING, "A <b>Name</b>", "prog")
    response = parked_response_bytes(ParkedPageKind.STARTING, page)
    head, _, body = response.partition(b"\r\n\r\n")
    assert f"Content-Length: {len(page.encode())}".encode() in head
    assert body == page.encode()
    assert "&lt;b&gt;" in page


@pytest.mark.frontend
def test_the_could_not_start_pages_wire_types_are_the_app_contracts() -> None:
    """The page speaks the app contract without its module, so it carries the two types it uses; this pins them to
    ``app_contract.ts``, where a renamed type would otherwise leave the page's button silently doing nothing."""
    app_contract_ts = (Path(__file__).parents[5] / "libs" / "workspace_ui" / "src" / "app_contract.ts").read_text()
    for name, value in (
        ("SHELL_HANDSHAKE", SHELL_HANDSHAKE_TYPE),
        ("SHELL_START_WITH_TEXT", SHELL_START_WITH_TEXT_TYPE),
    ):
        declared = re.search(rf'export const {name} = "([^"]+)";', app_contract_ts)
        assert declared is not None and declared.group(1) == value
