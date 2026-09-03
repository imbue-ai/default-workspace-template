import pytest
from app_manifest.primitives import AppName

from app_instances.conftest import RecordedShellRequests
from app_instances.nudge import (
    DEFAULT_SHELL_URL,
    ENV_SHELL_URL,
    ShellNudger,
    shell_base_url,
)
from app_instances.testing import LOOPBACK_HOST, free_port


def test_nudge_posts_the_changed_route_for_the_app_and_tolerates_a_refusing_shell(
    recording_shell: RecordedShellRequests,
) -> None:
    nudger = ShellNudger(app_name=AppName("files"), shell_url=recording_shell.base_url)

    nudger.nudge()
    nudger.nudge()

    assert recording_shell.requests == [("POST", "/api/apps/files/changed")] * 2


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
