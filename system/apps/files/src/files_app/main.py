"""Run Files: the Files server on the app's port and the app registered, until supervisord's SIGTERM."""

import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Final

import click

from app_manifest.primitives import AppUrl
from app_manifest.registry import register_app
from files_app.gateway import Gateway
from files_app.server import create_app
from files_app.serving import serve_in_background
from files_app.serving import wait_for_shutdown_signal
from imbue.imbue_common.logging import log_span

MANIFEST_PATH: Final[Path] = Path("system/apps/files/app.toml")
# The frontend's build: its assets, and the page the server fills for every folder and file.
STATIC_DIRECTORY: Final[Path] = Path("system/apps/files/static")
APP_PORT: Final[int] = 8300
HOST: Final[str] = "127.0.0.1"


def run_files_app(
    app_port: int,
    static_directory: Path,
    root: Path,
    is_registered: bool,
    wait_for_shutdown: Callable[[], int],
) -> int:
    app = create_app(
        gateway=Gateway.from_environment(),
        static_directory=static_directory,
        root=root,
        agent_id=os.environ.get("MNGR_AGENT_ID", ""),
    )
    with serve_in_background(HOST, app_port, app):
        if is_registered:
            with log_span("Registering files at port {}", app_port):
                register_app(MANIFEST_PATH, AppUrl(f"http://localhost:{app_port}"))
        return wait_for_shutdown()


@click.command()
@click.option("--port", "app_port", type=int, default=APP_PORT, show_default=True, help="The app's port")
@click.option(
    "--static-dir",
    "static_directory",
    type=click.Path(path_type=Path),
    default=STATIC_DIRECTORY,
    show_default=True,
    help="The frontend's build",
)
@click.option(
    "--root", type=click.Path(path_type=Path), default=Path("/"), show_default=True, help="The folder Files serves"
)
@click.option("--no-register", "is_unregistered", is_flag=True, default=False, help="Skip registering the app")
def main(app_port: int, static_directory: Path, root: Path, is_unregistered: bool) -> None:
    """Run Files: the workspace's file browser, with the files shared from the user's computers."""
    sys.exit(
        run_files_app(
            app_port,
            static_directory,
            root,
            is_registered=not is_unregistered,
            wait_for_shutdown=wait_for_shutdown_signal,
        )
    )


if __name__ == "__main__":
    main()
