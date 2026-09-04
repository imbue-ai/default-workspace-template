import pytest
from app_manifest.primitives import AppName

from app_instances.nudge import (
    DEFAULT_SHELL_URL,
    ENV_SHELL_URL,
    ShellNudger,
    SilentNudger,
    ThreadedNudger,
    shell_base_url,
)
from app_instances.testing import (
    LOOPBACK_HOST,
    RecordedShellRequests,
    RecordingNudger,
    free_port,
    wait_until,
)


def test_nudge_posts_the_changed_route_for_the_app_and_tolerates_a_refusing_shell(
    recording_shell: RecordedShellRequests,
) -> None:
    nudger = ShellNudger(app_name=AppName("files"), shell_url=recording_shell.base_url)

    nudger.nudge()
    nudger.nudge()

    assert recording_shell.paths() == [("POST", "/api/apps/files/changed")] * 2


def test_nudge_swallows_an_unreachable_shell() -> None:
    nudger = ShellNudger(
        app_name=AppName("files"), shell_url=f"http://{LOOPBACK_HOST}:{free_port()}"
    )

    nudger.nudge()


def test_shell_base_url_defaults_to_the_local_shell_and_honours_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(ENV_SHELL_URL, raising=False)
    assert shell_base_url() == DEFAULT_SHELL_URL

    monkeypatch.setenv(ENV_SHELL_URL, "http://127.0.0.1:9000/")
    assert shell_base_url() == "http://127.0.0.1:9000"


def test_threaded_nudger_delivers_the_nudge_off_the_calling_thread(
    recording_shell: RecordedShellRequests,
) -> None:
    nudger = ThreadedNudger(
        inner=ShellNudger(
            app_name=AppName("browser"), shell_url=recording_shell.base_url
        )
    )

    nudger.nudge()

    assert wait_until(lambda: len(recording_shell.requests) == 1, timeout_seconds=5)
    assert recording_shell.paths() == [("POST", "/api/apps/browser/changed")]


def test_threaded_nudger_counts_every_nudge_it_was_asked_for() -> None:
    inner = RecordingNudger()
    nudger = ThreadedNudger(inner=inner)

    nudger.nudge()
    nudger.nudge()

    assert wait_until(lambda: inner.nudge_count == 2, timeout_seconds=5)


def test_silent_nudger_does_nothing() -> None:
    SilentNudger().nudge()
