"""What ``post_to_shell`` logs for a shell that is down, refuses the body, or fails."""

from flask import Flask
from flask import Response

from imbue.chat.shell_client import post_to_shell
from imbue.chat.testing import serve_app
from imbue.system_interface.testing import find_free_port


def _shell_answering(status: int, body: str) -> Flask:
    application = Flask("answering-shell")
    application.add_url_rule(
        "/api/client-activity",
        view_func=lambda: Response(body, status=status),
        methods=["POST"],
        endpoint="client_activity",
    )
    return application


def test_an_unreachable_shell_is_a_debug_log_and_no_error(loguru_records: list[str]) -> None:
    post_to_shell(f"http://127.0.0.1:{find_free_port()}/api/client-activity", {"kind": "message"})

    assert any(record.startswith("DEBUG Skipped posting to the shell") for record in loguru_records)
    assert not any("refused the body" in record for record in loguru_records)


def test_a_shell_that_refuses_the_body_is_a_warning_quoting_the_refusal(loguru_records: list[str]) -> None:
    with serve_app(_shell_answering(400, "desktop_id is required")) as served:
        post_to_shell(f"{served.http_url}/api/client-activity", {"kind": "message"})

    assert any(
        record.startswith("WARNING") and "refused the body with 400: desktop_id is required" in record
        for record in loguru_records
    )


def test_a_failing_shell_is_a_debug_log_and_no_warning(loguru_records: list[str]) -> None:
    with serve_app(_shell_answering(500, "boom")) as served:
        post_to_shell(f"{served.http_url}/api/client-activity", {"kind": "message"})

    assert any(record.startswith("DEBUG") and "answered 500" in record for record in loguru_records)
    assert not any("refused the body" in record for record in loguru_records)
