from typing import Any

from imbue.mngr.utils.polling import wait_for
from imbue.system_interface.shell.close_hints import WindowClosedHint
from imbue.system_interface.shell.close_hints import post_window_closed_hint
from imbue.system_interface.shell.testing import TEST_TERMINAL_WINDOW_CLOSED_PATH
from imbue.system_interface.shell.testing import recording_app
from imbue.system_interface.testing import find_free_port
from imbue.system_interface.testing import serve_app


def _hint(url: str) -> WindowClosedHint:
    return WindowClosedHint(
        app="terminal",
        url=url,
        body={"path": "/?session=terminal-1", "window_id": "win-0000000000000001", "desktop_id": "home"},
    )


def test_the_poster_delivers_the_closed_window_and_shrugs_off_an_app_that_is_down() -> None:
    received: list[dict[str, Any]] = []
    # An app nobody serves: the post must neither raise nor block the caller.
    post_window_closed_hint(_hint(f"http://127.0.0.1:{find_free_port()}{TEST_TERMINAL_WINDOW_CLOSED_PATH}"))
    with serve_app(recording_app(received)) as served:
        post_window_closed_hint(_hint(f"{served.http_url}{TEST_TERMINAL_WINDOW_CLOSED_PATH}"))
        wait_for(lambda: len(received) == 1, timeout=5.0, poll_interval=0.02, error_message="the hint never landed")
    assert received == [{"path": "/?session=terminal-1", "window_id": "win-0000000000000001", "desktop_id": "home"}]
