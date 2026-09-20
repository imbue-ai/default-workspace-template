"""The ``terminal-pty`` program: ttyd on its own origin, framed by the terminal app's wrapper page.

ttyd cannot serve a launch path or report a location, so it runs as an internal app of its own
(desktop-interface plan section 9.2) and the ``terminal`` program serves the pages users open.
This entry point prepares what ttyd needs (the dispatch scripts and the patched web client),
registers the pty's manifest, and then becomes ttyd, so supervisord's signals reach ttyd itself.
"""

import os
from pathlib import Path
from typing import Final, NoReturn

import click
from app_instances.sidecar import app_url_port
from app_manifest.manifest import load_manifest
from app_manifest.primitives import AppUrl
from app_manifest.registry import register_app
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import log_span
from loguru import logger
from pydantic import Field

from terminal_app.data_types import TerminalPaths
from terminal_app.dispatch import (
    build_ttyd_argv,
    install_dispatch_scripts,
    install_ttyd_web_client,
    warn_if_oom_tag_script_is_missing,
)
from terminal_app.errors import TtydStartError
from terminal_app.wiring import (
    OOM_TAG_SCRIPT,
    STATE_DIR,
    TTYD_EXECUTABLE,
    TTYD_WEB_CLIENT_ARCHIVE,
)

# The pty's fixed wiring, relative to the repo root every supervised program runs from. The
# state directory is the terminal's: the dispatch scripts ttyd runs and the session id files the
# wrapper writes are one directory, whichever program touches them.
MANIFEST_PATH: Final[Path] = Path("system/apps/terminal_pty/app.toml")
APP_URL: Final[AppUrl] = AppUrl("http://localhost:7683")


class TerminalPtyArguments(FrozenModel):
    """Everything the pty program is told on its command line."""

    manifest_path: Path = Field(description="The app.toml to register")
    app_url: AppUrl = Field(description="Where ttyd serves the terminal pages")
    state_dir: Path = Field(description="The terminal's state directory")
    ttyd_web_client_archive: Path = Field(
        description="The vendored, gzip-compressed OSC 52-capable ttyd web client"
    )
    ttyd_executable: str = Field(description="The ttyd binary to run")
    oom_tag_script: Path = Field(
        description="The memory-shedding tag wrapper a terminal session runs its shell through"
    )


def prepare_ttyd(arguments: TerminalPtyArguments) -> list[str]:
    """Install the dispatch scripts and the web client, register the pty, and return the ttyd command line."""
    paths = TerminalPaths(state_dir=arguments.state_dir.absolute())
    oom_tag_script = arguments.oom_tag_script.absolute()
    warn_if_oom_tag_script_is_missing(oom_tag_script)
    with log_span("Installing the ttyd dispatch scripts under {}", paths.commands_dir):
        install_dispatch_scripts(paths, oom_tag_script)
    is_client_installed = install_ttyd_web_client(
        arguments.ttyd_web_client_archive, paths.ttyd_index_path
    )
    manifest = load_manifest(arguments.manifest_path)
    with log_span("Registering {} at {}", manifest.name, arguments.app_url):
        register_app(arguments.manifest_path, arguments.app_url)
    return build_ttyd_argv(
        ttyd_executable=arguments.ttyd_executable,
        port=app_url_port(arguments.app_url),
        index_path=paths.ttyd_index_path if is_client_installed else None,
        commands_dir=paths.commands_dir,
    )


def run_terminal_pty(arguments: TerminalPtyArguments) -> NoReturn:
    """Prepare ttyd and replace this process with it; only returns by raising."""
    argv = prepare_ttyd(arguments)
    logger.info("Starting ttyd: {}", argv)
    try:
        os.execvp(argv[0], argv)
    except OSError as e:
        raise TtydStartError(f"cannot start ttyd {argv!r}: {e}") from e


@click.command()
@click.option(
    "--manifest",
    "manifest_path",
    type=click.Path(path_type=Path),
    default=MANIFEST_PATH,
    show_default=True,
    help="The app.toml to register",
)
@click.option(
    "--app-url",
    default=APP_URL,
    show_default=True,
    help="Where ttyd serves the terminal pages",
)
@click.option(
    "--state-dir",
    type=click.Path(path_type=Path),
    default=STATE_DIR,
    show_default=True,
    help="The terminal's state directory (dispatch scripts and pty records)",
)
@click.option(
    "--ttyd-web-client",
    "ttyd_web_client_archive",
    type=click.Path(path_type=Path),
    default=TTYD_WEB_CLIENT_ARCHIVE,
    show_default=True,
    help="The gzip-compressed OSC 52-capable ttyd web client to serve",
)
@click.option(
    "--ttyd",
    "ttyd_executable",
    default=TTYD_EXECUTABLE,
    show_default=True,
    help="The ttyd binary",
)
@click.option(
    "--oom-tag-script",
    "oom_tag_script",
    type=click.Path(path_type=Path),
    default=OOM_TAG_SCRIPT,
    show_default=True,
    help="The memory-shedding tag wrapper a terminal session runs its shell through",
)
def main(
    manifest_path: Path,
    app_url: str,
    state_dir: Path,
    ttyd_web_client_archive: Path,
    ttyd_executable: str,
    oom_tag_script: Path,
) -> None:
    """Run the terminal's pty origin: install the dispatch scripts, register, and become ttyd."""
    run_terminal_pty(
        TerminalPtyArguments(
            manifest_path=manifest_path,
            app_url=AppUrl(app_url),
            state_dir=state_dir,
            ttyd_web_client_archive=ttyd_web_client_archive,
            ttyd_executable=ttyd_executable,
            oom_tag_script=oom_tag_script,
        )
    )


if __name__ == "__main__":
    main()
