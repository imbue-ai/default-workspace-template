"""Tests for port parking: the held port, the handoff on the first connection, and the pages."""

from collections.abc import Callable
from collections.abc import Iterator

import pytest

from imbue.mngr.utils.polling import wait_for
from imbue.system_interface.shell.errors import AppLifecycleRefusedError
from imbue.system_interface.shell.errors import PortInUseError
from imbue.system_interface.shell.port_parking import FAILED_REFRESH_SECONDS
from imbue.system_interface.shell.port_parking import ParkedPageKind
from imbue.system_interface.shell.port_parking import ParkedPort
from imbue.system_interface.shell.port_parking import ParkingTarget
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
    assert woken == ["docs"]
    assert parked_port.is_woken is True
    wait_for(lambda: can_bind_loopback_port(parked_port.target.port), timeout=5.0, poll_interval=0.02)


def test_a_wake_that_fails_is_told_so(closed_port: int) -> None:
    parked_port = _parked_port(closed_port, lambda: ParkedPageKind.FAILED)
    parked_port.start()

    answer = send_raw_get_over_socket(parked_port.target.port)

    assert b"Docs could not start" in answer and b"supervisorctl tail docs stderr" in answer
    assert f'content="{FAILED_REFRESH_SECONDS}"'.encode() in answer
    # The header makes the page's slower retry the promise a fetch sees too.
    assert f"Retry-After: {FAILED_REFRESH_SECONDS}".encode() in answer


def test_a_wake_that_raises_still_answers_the_request(closed_port: int) -> None:
    def explode() -> ParkedPageKind:
        raise AppLifecycleRefusedError("supervisord went away")

    parked_port = _parked_port(closed_port, explode)
    parked_port.start()

    assert b"could not start" in send_raw_get_over_socket(parked_port.target.port)


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
