import errno
import os

from disk_headroom.full import DISK_FULL_MESSAGE
from flask import Flask

from imbue.system_interface.request_helpers import handle_unhandled_exception


def _app_raising(error: Exception) -> Flask:
    application = Flask(__name__)
    application.register_error_handler(Exception, handle_unhandled_exception)

    def _raise() -> str:
        raise error

    application.add_url_rule("/write", view_func=_raise, methods=["POST"])
    return application


def test_a_write_the_full_disk_refused_answers_the_disk_full_line() -> None:
    response = _app_raising(OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))).test_client().post("/write")

    assert response.status_code == 507
    assert response.get_json()["detail"] == DISK_FULL_MESSAGE


def test_another_os_error_is_still_an_internal_error() -> None:
    response = _app_raising(OSError(errno.EACCES, "denied")).test_client().post("/write")

    assert response.status_code == 500
