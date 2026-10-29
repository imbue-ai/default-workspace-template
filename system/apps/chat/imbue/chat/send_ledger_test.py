import threading

from flask import Response

from imbue.chat.send_ledger import SendLedger


def _answer(status: int, body: bytes = b'{"status": "ok"}') -> Response:
    return Response(body, status, mimetype="application/json")


def _replay(data: bytes, status: int) -> Response:
    return _answer(status, data)


def test_a_repeat_while_the_send_is_under_way_gets_its_answer_without_sending() -> None:
    ledger = SendLedger()
    release = threading.Event()
    sends: list[str] = []

    def slow_send() -> Response:
        sends.append("first")
        release.wait(timeout=10)
        return _answer(200)

    def repeat_send() -> Response:
        sends.append("repeat")
        return _answer(200)

    answers: list[Response] = []
    first = threading.Thread(target=lambda: answers.append(ledger.send_once("chat:m-1", slow_send, _replay)))
    first.start()
    while not sends:
        threading.Event().wait(0.01)
    repeat = threading.Thread(target=lambda: answers.append(ledger.send_once("chat:m-1", repeat_send, _replay)))
    repeat.start()
    release.set()
    first.join(timeout=10)
    repeat.join(timeout=10)

    assert sends == ["first"]
    assert [answer.get_data() for answer in answers] == [b'{"status": "ok"}', b'{"status": "ok"}']


def test_a_refused_send_can_be_sent_again() -> None:
    ledger = SendLedger()
    results = iter([_answer(500, b'{"detail": "failed"}'), _answer(200)])
    sends: list[int] = []

    def send() -> Response:
        answer = next(results)
        sends.append(answer.status_code)
        return answer

    assert ledger.send_once("chat:m-1", send, _replay).status_code == 500
    assert ledger.send_once("chat:m-1", send, _replay).status_code == 200
    assert sends == [500, 200]


def test_a_taken_send_is_answered_again_from_the_ledger() -> None:
    ledger = SendLedger()
    sends: list[str] = []

    def send() -> Response:
        sends.append("sent")
        return _answer(202, b'{"status": "held"}')

    assert ledger.send_once("chat:m-1", send, _replay).status_code == 202
    repeat = ledger.send_once("chat:m-1", send, _replay)
    assert (repeat.status_code, repeat.get_data()) == (202, b'{"status": "held"}')
    assert sends == ["sent"]


def test_a_repeat_waiting_on_a_send_that_is_refused_sends_itself() -> None:
    ledger = SendLedger()
    release = threading.Event()
    sends: list[str] = []

    def refused_send() -> Response:
        sends.append("first")
        release.wait(timeout=10)
        return _answer(500, b'{"detail": "failed"}')

    def repeat_send() -> Response:
        sends.append("repeat")
        return _answer(200)

    answers: list[int] = []
    first = threading.Thread(
        target=lambda: answers.append(ledger.send_once("chat:m-1", refused_send, _replay).status_code)
    )
    first.start()
    while not sends:
        threading.Event().wait(0.01)
    repeat = threading.Thread(
        target=lambda: answers.append(ledger.send_once("chat:m-1", repeat_send, _replay).status_code)
    )
    repeat.start()
    release.set()
    first.join(timeout=10)
    repeat.join(timeout=10)

    assert sends == ["first", "repeat"]
    assert sorted(answers) == [200, 500]
