import threading

import pytest

from imbue.chat.new_chat_sends import NewChatCreateFailedError
from imbue.chat.new_chat_sends import NewChatSendGate


def _wait_in_thread(gate: NewChatSendGate, ticket: int, delivered: list[int], errors: list[str]) -> threading.Thread:
    def run() -> None:
        try:
            gate.wait_for_turn(ticket)
        except NewChatCreateFailedError as e:
            errors.append(str(e))
            return
        delivered.append(ticket)
        gate.finish_turn(ticket)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def test_sends_wait_for_the_create_and_then_go_in_the_order_they_arrived() -> None:
    gate = NewChatSendGate.build()
    delivered: list[int] = []
    errors: list[str] = []
    first = gate.take_ticket()
    second = gate.take_ticket()
    # The later send's thread starts first: the order is the tickets', not the threads'.
    threads = [_wait_in_thread(gate, second, delivered, errors), _wait_in_thread(gate, first, delivered, errors)]

    assert not gate.is_drained()
    assert delivered == []
    gate.open()
    for thread in threads:
        thread.join(timeout=5.0)

    assert delivered == [first, second]
    assert errors == []
    assert gate.is_drained()


def test_a_failed_create_refuses_every_waiting_send_with_its_reason() -> None:
    gate = NewChatSendGate.build()
    delivered: list[int] = []
    errors: list[str] = []
    threads = [_wait_in_thread(gate, gate.take_ticket(), delivered, errors) for _ in range(2)]

    gate.fail("the chat could not be started")
    for thread in threads:
        thread.join(timeout=5.0)

    assert delivered == []
    assert errors == ["the chat could not be started", "the chat could not be started"]


def test_a_gate_with_no_sends_is_drained_as_soon_as_it_opens() -> None:
    gate = NewChatSendGate.build()
    assert not gate.has_issued_tickets()
    assert not gate.is_drained()

    gate.open()

    assert gate.is_drained()


def test_a_send_that_joins_after_the_create_waits_only_for_the_sends_before_it() -> None:
    gate = NewChatSendGate.build()
    first = gate.take_ticket()
    gate.open()
    late = gate.take_ticket()
    delivered: list[int] = []
    errors: list[str] = []

    late_thread = _wait_in_thread(gate, late, delivered, errors)
    assert not gate.is_drained()
    gate.wait_for_turn(first)
    delivered.append(first)
    gate.finish_turn(first)
    late_thread.join(timeout=5.0)

    assert delivered == [first, late]
    assert gate.is_drained()


def test_a_failed_wait_raises_for_a_send_on_the_calling_thread() -> None:
    gate = NewChatSendGate.build()
    ticket = gate.take_ticket()
    gate.fail("mngr create exited with code 3")

    with pytest.raises(NewChatCreateFailedError, match="mngr create exited with code 3"):
        gate.wait_for_turn(ticket)
