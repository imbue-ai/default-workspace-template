import os
import sys
from typing import Final

import pytest
from imbue.imbue_common.pure import pure

ALLOW_MACOS_ENV_VAR: Final[str] = "DWT_ALLOW_MACOS_TESTS"
_MACOS_PLATFORM: Final[str] = "darwin"
_ALLOWED_VALUE: Final[str] = "1"

_REFUSAL_MESSAGE: Final[str] = (
    "This repo's tests target the Linux workspace container and do not run faithfully on "
    "macOS: some suites cannot install here (Linux-only wheels, the Fortress browser), and "
    "others fail for macOS reasons (bash 3.2, socket path limits, no /proc). Push and let CI "
    f"run them, or run them in a workspace. To run pure-Python tests here anyway, set "
    f"{ALLOW_MACOS_ENV_VAR}={_ALLOWED_VALUE}, and treat macOS-only failures as noise."
)


@pure
def refusal_message(platform: str, allow_value: str | None) -> str | None:
    """Why a run on ``platform`` must not go ahead, or None when it may."""
    if platform != _MACOS_PLATFORM or allow_value == _ALLOWED_VALUE:
        return None
    return _REFUSAL_MESSAGE


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    message = refusal_message(sys.platform, os.environ.get(ALLOW_MACOS_ENV_VAR))
    if message is not None:
        pytest.exit(message, returncode=pytest.ExitCode.USAGE_ERROR)
