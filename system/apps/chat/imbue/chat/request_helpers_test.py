import errno
import os

from disk_headroom.full import DISK_FULL_MESSAGE
from flask import Flask

from imbue.chat.errors import ChatAppError
from imbue.chat.request_helpers import answer_chat_app_error
from imbue.chat.request_helpers import handle_unhandled_exception


def _app_raising(error: Exception) -> Flask:
    application = Flask(__name__)
    application.register_error_handler(Exception, handle_unhandled_exception)
    application.register_error_handler(ChatAppError, answer_chat_app_error)

    def _raise() -> str:
        raise error

    application.add_url_rule("/write", view_func=_raise, methods=["POST"])
    return application


def _disk_full() -> OSError:
    return OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))


def test_a_write_the_full_disk_refused_answers_the_disk_full_line() -> None:
    response = _app_raising(_disk_full()).test_client().post("/write")

    assert response.status_code == 507
    assert response.get_json()["detail"] == DISK_FULL_MESSAGE


def test_a_chat_error_raised_from_a_full_disk_answers_the_disk_full_line() -> None:
    error = ChatAppError("could not save the chat's settings")
    error.__cause__ = _disk_full()

    response = _app_raising(error).test_client().post("/write")

    assert response.status_code == 507
    assert response.get_json()["detail"] == DISK_FULL_MESSAGE


def test_other_failures_keep_their_answers() -> None:
    assert _app_raising(ChatAppError("bad name")).test_client().post("/write").status_code == 400
    assert _app_raising(OSError(errno.EACCES, "denied")).test_client().post("/write").status_code == 500
