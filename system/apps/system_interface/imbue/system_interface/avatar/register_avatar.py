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
from loguru import logger
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.system_interface.avatar.catalog import DesignRegistration
from imbue.system_interface.avatar.designs import MAX_SVG_BYTES
from imbue.system_interface.avatar.primitives import DesignId
from imbue.system_interface.shell.errors import InvalidShellValueError

ENV_WORKSPACE_URL: Final[str] = "MINDS_WORKSPACE_SERVER_URL"
DEFAULT_WORKSPACE_URL: Final[str] = "http://127.0.0.1:8000"
_REQUEST_TIMEOUT_SECONDS: Final[float] = 15.0


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


def register_design(arguments: RegisterAvatarArguments) -> None:
    """Post the design and, when asked, the selection; raises for a refused registration."""
    source = arguments.source.resolve()
    with source.open("rb") as stream:
        contents = stream.read(MAX_SVG_BYTES + 1)
    if len(contents) > MAX_SVG_BYTES:
        raise InvalidShellValueError(f"the design exceeds {MAX_SVG_BYTES // 1024} KiB")
    registration = DesignRegistration(
        id=arguments.design_id, label=arguments.label, svg=contents.decode("utf-8"), source_path=str(source)
    )
    with httpx.Client(base_url=arguments.shell_url, timeout=_REQUEST_TIMEOUT_SECONDS) as client:
        client.post("/api/avatars", json=registration.model_dump()).raise_for_status()
        if arguments.is_selected:
            client.post("/api/avatar-selection", json={"design": str(registration.id)}).raise_for_status()
    logger.info("Registered avatar design {} (selected: {})", registration.id, arguments.is_selected)


def main(argv: Sequence[str] | None = None) -> None:
    register_design(_parse_arguments(argv))


if __name__ == "__main__":
    main()
