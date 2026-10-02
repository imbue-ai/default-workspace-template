import httpx
import pytest

from activity.apps import app_quit_route
from activity.apps import is_app_stoppable
from activity.apps import is_quittable_by_shell
from activity.apps import request_app_quit
from activity.errors import ShellUnavailableError
from app_manifest.registry import RegistryRow


def _row(name: str, program: str | None, critical: bool) -> RegistryRow:
    return RegistryRow.model_validate(
        {
            "name": name,
            "url": f"http://{name}.test",
            "display_name": name.title(),
            "program": program,
            "critical": critical,
        }
    )


_SHELL = _row("system_interface", "system_interface", True)
_TERMINAL = _row("terminal", "terminal", True)
_TERMINAL_PTY = _row("terminal-pty", "terminal", False)
_FILES = _row("files", "files", False)
_BROWSER = _row("browser", "browser", False)
_SELF = _row("activity", "activity", False)
_UNSUPERVISED = _row("notes", None, False)
_ROWS = (_SHELL, _TERMINAL, _TERMINAL_PTY, _FILES, _BROWSER, _SELF, _UNSUPERVISED)


@pytest.mark.parametrize(
    ("row", "is_stoppable"),
    [
        (_FILES, True),
        # The shell's own refusals: a critical app, a row inside a critical app's program, and no program at all.
        (_TERMINAL, False),
        (_TERMINAL_PTY, False),
        (_UNSUPERVISED, False),
        # This app's: the browser agents drive, and itself.
        (_BROWSER, False),
        (_SELF, False),
    ],
)
def test_stop_is_offered_only_for_an_app_the_shell_would_quit_less_the_browser_and_this_app(
    row: RegistryRow, is_stoppable: bool
) -> None:
    assert is_app_stoppable(row, _ROWS) is is_stoppable


def test_the_browser_and_this_app_are_ones_the_shell_would_still_quit_and_restart_on_open() -> None:
    assert [is_quittable_by_shell(row, _ROWS) for row in (_BROWSER, _SELF, _FILES)] == [True, True, True]
    assert [is_quittable_by_shell(row, _ROWS) for row in (_TERMINAL, _TERMINAL_PTY, _UNSUPERVISED)] == [
        False,
        False,
        False,
    ]


def test_a_quit_goes_to_the_shells_route_with_the_name_quoted() -> None:
    posted: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        posted.append(f"{request.method} {request.url}")
        return httpx.Response(200, json={"name": "my app", "is_running": False})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        request_app_quit(client, "http://shell.test", "my app")
    assert posted == ["POST http://shell.test/api/apps/my%20app/quit"]
    assert app_quit_route("a/b") == "/api/apps/a%2Fb/quit"


def test_a_refusal_or_an_unreachable_shell_carries_the_reason() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"detail": "App 'terminal' is critical to the workspace"})

    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with httpx.Client(transport=httpx.MockTransport(refuse)) as client:
        with pytest.raises(ShellUnavailableError, match="refused to stop the app: App 'terminal' is critical"):
            request_app_quit(client, "http://shell.test", "terminal")
    with httpx.Client(transport=httpx.MockTransport(unreachable)) as client:
        with pytest.raises(ShellUnavailableError, match="could not reach the desktop"):
            request_app_quit(client, "http://shell.test", "files")
