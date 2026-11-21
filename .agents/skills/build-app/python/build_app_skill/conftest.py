from collections.abc import Generator

import pytest
from imbue.app_sdk.shell_client import SHELL_URL_ENVIRONMENT_VARIABLE

from build_app_skill.testing import ShellStandIn, running_shell_stand_in


@pytest.fixture
def shell_stand_in(
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[ShellStandIn, None, None]:
    """A running stand-in for the workspace shell, which ``MINDS_WORKSPACE_SERVER_URL`` points this process at."""
    with running_shell_stand_in() as stand_in:
        monkeypatch.setenv(SHELL_URL_ENVIRONMENT_VARIABLE, stand_in.url)
        yield stand_in
