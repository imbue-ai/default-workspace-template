import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Final

import click
from loguru import logger
from pydantic import Field

from app_manifest.primitives import AppUrl
from app_manifest.registry import register_app
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import log_span
from screen.brokers import brokers_from_environ
from screen.connect_info import ConnectInfoAggregator
from screen.launch import DetachedProcessLauncher
from screen.messenger import ChatAgentMessenger
from screen.neko_client import NekoClient
from screen.primitives import APP_NAME
from screen.primitives import SERVICE_HOST
from screen.primitives import SERVICE_PORT
from screen.proxy import NekoProxy
from screen.routes import build_app
from screen.service import ScreenService
from screen.serving import app_url_port
from screen.serving import serve_in_background
from screen.serving import wait_for_shutdown_signal
from screen.stack import ScreenStack
from screen.supervisor import SupervisorctlClient
from screen.windows import DisplayWindowTools

# The app's fixed wiring, relative to the repo root every supervised program runs from.
MANIFEST_PATH: Final[Path] = Path("system/apps/screen/app.toml")
DEFAULT_STATE_DIRECTORY: Final[Path] = Path("data/.state/screen")
# The frontend's build output, inside the package.
DEFAULT_STATIC_DIRECTORY: Final[Path] = Path(__file__).parent / "static"


class ScreenServiceArguments(FrozenModel):
    """Everything the service is told on its command line."""

    manifest_path: Path = Field(description="The app.toml to register")
    app_url: AppUrl = Field(description="Where the page is served")
    state_dir: Path = Field(description="data/.state/screen")
    static_directory: Path = Field(description="The frontend's built bundle")
    host: str = Field(description="The address the page server binds")
    is_registered: bool = Field(description="Whether this boot registers the manifest (a throwaway boot does not)")


def build_service(arguments: ScreenServiceArguments) -> ScreenService:
    repo_root = Path.cwd()
    neko = NekoClient()
    service = ScreenService(
        repo_root=repo_root,
        state_dir=arguments.state_dir,
        neko=neko,
        window_tools=DisplayWindowTools(),
        launcher=DetachedProcessLauncher(working_directory=repo_root),
        messenger=ChatAgentMessenger(repo_root=repo_root),
        connectivity=ConnectInfoAggregator(brokers=brokers_from_environ(os.environ)),
    )
    stack = ScreenStack(
        supervisor=SupervisorctlClient(),
        neko=neko,
        state_dir=arguments.state_dir,
        on_lifecycle_changed=service.on_lifecycle_changed,
        on_running=service.on_running,
        set_api_token=service.set_api_token,
    )
    service.attach_stack(stack)
    return service


def run_screen_service(arguments: ScreenServiceArguments, wait_for_shutdown: Callable[[], int]) -> int:
    """Serve the routes, start the service's clocks, register the app, and wait for SIGTERM or SIGINT."""
    service = build_service(arguments)
    app = build_app(service, arguments.static_directory, NekoProxy())
    with serve_in_background(arguments.host, app_url_port(arguments.app_url), app):
        service.start()
        try:
            if arguments.is_registered:
                with log_span("Registering {} at {}", APP_NAME, arguments.app_url):
                    register_app(arguments.manifest_path, arguments.app_url)
            return wait_for_shutdown()
        finally:
            logger.info("The Screen service is shutting down; stopping the stack")
            service.shutdown("the Screen service stopped")


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
    "--state-dir",
    type=click.Path(path_type=Path),
    default=DEFAULT_STATE_DIRECTORY,
    show_default=True,
    help="The service's state directory",
)
@click.option(
    "--static-dir",
    "static_directory",
    type=click.Path(path_type=Path),
    default=DEFAULT_STATIC_DIRECTORY,
    show_default=True,
    help="The frontend's built bundle",
)
@click.option("--port", type=int, default=SERVICE_PORT, show_default=True, help="The loopback port to serve on")
@click.option(
    "--no-register",
    "is_unregistered",
    is_flag=True,
    default=False,
    help="Skip the registration: a throwaway boot that must not re-point the live row",
)
def main(manifest_path: Path, state_dir: Path, static_directory: Path, port: int, is_unregistered: bool) -> None:
    """Run the Screen service: the workspace's streamed desktop as a window."""
    arguments = ScreenServiceArguments(
        manifest_path=manifest_path,
        app_url=AppUrl(f"http://localhost:{port}"),
        state_dir=state_dir,
        static_directory=static_directory,
        host=SERVICE_HOST,
        is_registered=not is_unregistered,
    )
    sys.exit(run_screen_service(arguments, wait_for_shutdown_signal))


if __name__ == "__main__":
    main()
