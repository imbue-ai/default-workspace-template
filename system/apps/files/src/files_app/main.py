"""Run Files: dufs on a loopback port of its own, the Files server in front of it on the app's port, the app
registered, and both stopped together on supervisord's SIGTERM."""

import os
import shutil
import subprocess
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
from files_app.serving import wait_for_port
from files_app.serving import wait_for_shutdown_signal
from imbue.imbue_common.logging import log_span

MANIFEST_PATH: Final[Path] = Path("system/apps/files/app.toml")
# The frontend's build, which dufs serves as its assets and the server fills for shared items' pages.
STATIC_DIRECTORY: Final[Path] = Path("system/apps/files/static")
APP_PORT: Final[int] = 8300
DUFS_PORT: Final[int] = 8301
HOST: Final[str] = "127.0.0.1"
DUFS_START_SECONDS: Final[float] = 15.0


def start_dufs(dufs: str, port: int, static_directory: Path, root: str) -> subprocess.Popen[bytes]:
    """dufs over ``root`` with every operation allowed, bound to loopback; the workspace origin gates access."""
    command = [dufs, "--allow-all", "--bind", HOST, "--port", str(port), "--assets", str(static_directory), root]
    return subprocess.Popen(command)


def run_files_app(
    dufs: str,
    app_port: int,
    dufs_port: int,
    static_directory: Path,
    root: str,
    is_registered: bool,
    wait_for_shutdown: Callable[[], int],
) -> int:
    with log_span("Starting dufs on port {}", dufs_port):
        dufs_process = start_dufs(dufs, dufs_port, static_directory, root)
    try:
        wait_for_port(HOST, dufs_port, DUFS_START_SECONDS, dufs_process)
        app = create_app(
            dufs_url=f"http://{HOST}:{dufs_port}",
            gateway=Gateway.from_environment(),
            static_directory=static_directory,
            agent_id=os.environ.get("MNGR_AGENT_ID", ""),
        )
        with serve_in_background(HOST, app_port, app):
            if is_registered:
                with log_span("Registering files at port {}", app_port):
                    register_app(MANIFEST_PATH, AppUrl(f"http://localhost:{app_port}"))
            return wait_for_shutdown()
    finally:
        dufs_process.terminate()
        try:
            dufs_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            dufs_process.kill()


@click.command()
@click.option("--port", "app_port", type=int, default=APP_PORT, show_default=True, help="The app's port")
@click.option("--dufs-port", type=int, default=DUFS_PORT, show_default=True, help="dufs's own loopback port")
@click.option(
    "--static-dir",
    "static_directory",
    type=click.Path(path_type=Path),
    default=STATIC_DIRECTORY,
    show_default=True,
    help="The frontend's build",
)
@click.option("--root", default="/", show_default=True, help="The folder dufs serves")
@click.option("--no-register", "is_unregistered", is_flag=True, default=False, help="Skip registering the app")
def main(app_port: int, dufs_port: int, static_directory: Path, root: str, is_unregistered: bool) -> None:
    """Run Files: the workspace's file browser, with the files shared from the user's computers."""
    dufs = shutil.which("dufs")
    if dufs is None:
        sys.exit("dufs is not installed (system/scripts/install_dufs.sh installs it)")
    sys.exit(
        run_files_app(
            dufs,
            app_port,
            dufs_port,
            static_directory,
            root,
            is_registered=not is_unregistered,
            wait_for_shutdown=wait_for_shutdown_signal,
        )
    )


if __name__ == "__main__":
    main()
