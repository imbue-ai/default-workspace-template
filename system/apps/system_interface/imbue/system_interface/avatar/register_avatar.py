"""Register an avatar design drawn inside the workspace through the running shell, optionally selecting it.

Usage (from the workspace root):
    uv run python -m imbue.system_interface.avatar.register_avatar --source data/avatar-designs/my-design.svg --id my-design --label "My design" [--select]

The shell's loopback-only registration route validates the drawing (docs/system/avatar-designs.md) and keeps the
original under the app data directory; ``--select`` then makes it the workspace's design.
"""

import argparse
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Final

import httpx
from app_manifest.manifest import describe_validation_error
from loguru import logger
from pydantic import Field
from pydantic import ValidationError

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.system_interface.avatar.catalog import DesignRegistration
from imbue.system_interface.avatar.designs import MAX_SVG_BYTES
from imbue.system_interface.avatar.primitives import DesignId
from imbue.system_interface.shell.errors import ShellError

ENV_WORKSPACE_URL: Final[str] = "MINDS_WORKSPACE_SERVER_URL"
DEFAULT_WORKSPACE_URL: Final[str] = "http://127.0.0.1:8000"
_REQUEST_TIMEOUT_SECONDS: Final[float] = 15.0
_JSON_MIMETYPE: Final[str] = "application/json"


class AvatarRegistrationError(ShellError):
    """The file is not a design the shell takes, or the shell refused the registration or the selection."""


class RegisterAvatarArguments(FrozenModel):
    """The parsed command line."""

    source: Path = Field(description="The design's SVG file")
    design_id: DesignId = Field(description="The catalog id to register it under")
    label: str = Field(description="What the chooser calls it")
    is_selected: bool = Field(description="Whether to select it for the workspace once registered")
    shell_url: str = Field(description="The shell's URL")


def _parse_arguments(argv: Sequence[str] | None) -> RegisterAvatarArguments:
    parser = argparse.ArgumentParser(description="Register an avatar design through the running shell")
    parser.add_argument("--source", type=Path, required=True, help="The design's SVG file")
    parser.add_argument("--id", required=True, help="The catalog id (lowercase letters, digits, and dashes)")
    parser.add_argument("--label", required=True, help="What the chooser calls the design")
    parser.add_argument("--select", action="store_true", help="Select the design for the whole workspace")
    parser.add_argument(
        "--shell-url",
        default=os.environ.get(ENV_WORKSPACE_URL, DEFAULT_WORKSPACE_URL),
        help="The shell's URL (default: MINDS_WORKSPACE_SERVER_URL, else the loopback port)",
    )
    parsed = parser.parse_args(argv)
    return RegisterAvatarArguments(
        source=parsed.source,
        design_id=DesignId(parsed.id),
        label=parsed.label,
        is_selected=parsed.select,
        shell_url=parsed.shell_url.rstrip("/"),
    )


def _read_registration(arguments: RegisterAvatarArguments) -> DesignRegistration:
    """The design as the shell will validate it; raises AvatarRegistrationError with the reason a file is not one."""
    source = arguments.source.resolve()
    with source.open("rb") as stream:
        contents = stream.read(MAX_SVG_BYTES + 1)
    if len(contents) > MAX_SVG_BYTES:
        raise AvatarRegistrationError(f"the design at {source} exceeds {MAX_SVG_BYTES // 1024} KiB")
    try:
        svg = contents.decode("utf-8")
    except UnicodeDecodeError as e:
        raise AvatarRegistrationError(f"the design at {source} is not UTF-8 encoded") from e
    try:
        return DesignRegistration(id=arguments.design_id, label=arguments.label, svg=svg, source_path=str(source))
    except ValidationError as e:
        raise AvatarRegistrationError(f"the design at {source} was refused: {describe_validation_error(e)}") from e


def _check_answer(response: httpx.Response, action: str) -> None:
    """Raises AvatarRegistrationError carrying the shell's reason when it refused ``action``."""
    if response.is_success:
        return
    is_json = response.headers.get("content-type", "").startswith(_JSON_MIMETYPE)
    detail = response.json().get("detail") if is_json else None
    reason = detail if isinstance(detail, str) else response.reason_phrase
    raise AvatarRegistrationError(f"the shell refused to {action}: {reason} ({response.status_code})")


def register_design(arguments: RegisterAvatarArguments) -> None:
    """Post the design and, when asked, the selection; raises AvatarRegistrationError with the shell's reason for a
    refusal."""
    registration = _read_registration(arguments)
    with httpx.Client(base_url=arguments.shell_url, timeout=_REQUEST_TIMEOUT_SECONDS) as client:
        _check_answer(client.post("/api/avatars", json=registration.model_dump()), "register the design")
        if arguments.is_selected:
            _check_answer(
                client.post("/api/avatar-selection", json={"design": str(registration.id)}), "select the design"
            )
    logger.info("Registered avatar design {} (selected: {})", registration.id, arguments.is_selected)


def main(argv: Sequence[str] | None = None) -> None:
    register_design(_parse_arguments(argv))


if __name__ == "__main__":
    main()
