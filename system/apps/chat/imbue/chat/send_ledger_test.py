import threading

from flask import Response

from imbue.chat.send_ledger import RepeatedSendOutcomeUnknownError
from imbue.chat.send_ledger import SendLedger
from imbue.mngr.utils.polling import wait_for


def _answer(status: int, body: bytes = b'{"status": "ok"}') -> Response:
    return Response(body, status, mimetype="application/json")


def _replay(data: bytes, status: int) -> Response:
    return _answer(status, data)


def _send_and_repeat_while_the_first_is_under_way(
    ledger: SendLedger, first_answer: Response, repeat_answer: Response
) -> tuple[list[str], list[Response]]:
    """Send under one key, repeat it while the first send is held, then let the first finish.

    Returns which sends ran and the two answers, in the order they came back.
    """
    release = threading.Event()
    sends: list[str] = []

    def first_send() -> Response:
        sends.append("first")
        release.wait(timeout=10)
        return first_answer

    def repeat_send() -> Response:
        sends.append("repeat")
        return repeat_answer

    answers: list[Response] = []
    first = threading.Thread(target=lambda: answers.append(ledger.send_once("chat:m-1", first_send, _replay, False)))
    first.start()
    wait_for(lambda: sends == ["first"], error_message="the first send never started")
    repeat = threading.Thread(target=lambda: answers.append(ledger.send_once("chat:m-1", repeat_send, _replay, True)))
    repeat.start()
    repeat.join(timeout=0.2)
    assert repeat.is_alive(), "the repeat did not wait for the send under way"
    release.set()
    first.join(timeout=10)
    repeat.join(timeout=10)
    return sends, answers


def test_a_repeat_while_the_send_is_under_way_gets_its_answer_without_sending() -> None:
    sends, answers = _send_and_repeat_while_the_first_is_under_way(SendLedger(), _answer(200), _answer(200))

    assert sends == ["first"]
    assert [answer.get_data() for answer in answers] == [b'{"status": "ok"}', b'{"status": "ok"}']


def test_a_refused_send_can_be_sent_again_by_a_send_that_is_not_a_repeat() -> None:
    ledger = SendLedger()
    results = iter([_answer(500, b'{"detail": "failed"}'), _answer(200)])
    sends: list[int] = []

    def send() -> Response:
        answer = next(results)
        sends.append(answer.status_code)
        return answer

    assert ledger.send_once("chat:m-1", send, _replay, False).status_code == 500
    assert ledger.send_once("chat:m-1", send, _replay, False).status_code == 200
    assert sends == [500, 200]


def test_a_taken_send_is_answered_again_from_the_ledger() -> None:
    ledger = SendLedger()
    sends: list[str] = []

    def send() -> Response:
        sends.append("sent")
        return _answer(202, b'{"status": "held"}')

    assert ledger.send_once("chat:m-1", send, _replay, False).status_code == 202
    repeat = ledger.send_once("chat:m-1", send, _replay, False)
    assert (repeat.status_code, repeat.get_data()) == (202, b'{"status": "held"}')
    assert sends == ["sent"]


def test_a_repeat_waiting_on_a_send_that_is_refused_gets_the_refusal_without_sending() -> None:
    """A refused send may still have been typed into the agent (mngr could not confirm it), so the repeat it was
    waiting on does not send it again."""
    refusal = b'{"detail": "unconfirmed", "kind": "submission_unconfirmed"}'
    sends, answers = _send_and_repeat_while_the_first_is_under_way(SendLedger(), _answer(500, refusal), _answer(200))

    assert sends == ["first"]
    assert [(answer.status_code, answer.get_data()) for answer in answers] == [(500, refusal), (500, refusal)]


def test_a_repeat_arriving_after_its_send_was_refused_gets_the_refusal_without_sending() -> None:
    """The page repeats a send that got no answer some seconds later; by then the first send can have ended refused
    (mngr could not confirm it, so it may have been typed), and the repeat must not type it again."""
    ledger = SendLedger()
    refusal = b'{"detail": "unconfirmed", "kind": "submission_unconfirmed"}'
    sends: list[int] = []

    def send() -> Response:
        sends.append(500)
        return _answer(500, refusal)

    assert ledger.send_once("chat:m-1", send, _replay, False).status_code == 500
    repeat = ledger.send_once("chat:m-1", send, _replay, True)

    assert (repeat.status_code, repeat.get_data()) == (500, refusal)
    assert sends == [500]


def test_a_repeat_waiting_on_a_send_that_failed_without_an_answer_reports_it_unknown_without_sending() -> None:
    """A send that raised may still have typed the message into the agent, so its waiting repeat sends nothing."""
    ledger = SendLedger()
    release = threading.Event()
    sends: list[str] = []
    outcomes: list[str] = []

    def first_send() -> Response:
        sends.append("first")
        release.wait(timeout=10)
        raise ConnectionError("the agent's host went away mid-send")

    def repeat_send() -> Response:
        sends.append("repeat")
        return _answer(200)

    def run_first() -> None:
        try:
            ledger.send_once("chat:m-1", first_send, _replay, False)
        except ConnectionError:
            outcomes.append("first raised")

    def run_repeat() -> None:
        try:
            ledger.send_once("chat:m-1", repeat_send, _replay, True)
        except RepeatedSendOutcomeUnknownError:
            outcomes.append("repeat unknown")

    first = threading.Thread(target=run_first)
    first.start()
    wait_for(lambda: sends == ["first"], error_message="the first send never started")
    repeat = threading.Thread(target=run_repeat)
    repeat.start()
    repeat.join(timeout=0.2)
    assert repeat.is_alive(), "the repeat did not wait for the send under way"
    release.set()
    first.join(timeout=10)
    repeat.join(timeout=10)

    assert sends == ["first"]
    assert sorted(outcomes) == ["first raised", "repeat unknown"]
