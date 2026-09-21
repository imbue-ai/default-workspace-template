import sys
from pathlib import Path
from typing import Final

import click
from flask import Flask
from pydantic import Field

from app_manifest.primitives import AppName
from app_manifest.primitives import AppUrl
from app_manifest.registry import SHELL_APP_CONTRACT_PATH
from app_manifest.registry import register_app
from app_manifest.shell_windows import shell_base_url
from getting_started.config import Config
from getting_started.config import load_config
from getting_started.first_window import FirstWindowLedger
from getting_started.first_window import FirstWindowOpener
from getting_started.first_window import HttpShellOps
from getting_started.first_window import LEDGER_FILENAME
from getting_started.pages import build_pages_blueprint
from getting_started.serving import app_url_port
from getting_started.serving import serve_in_background
from getting_started.serving import wait_for_shutdown_signal
from getting_started.state_files import DEFAULT_STATE_DIRECTORY
from getting_started.template_catalog import build_template_catalog_store
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import log_span

# The app's fixed wiring, relative to the repo root every supervised program runs from.
MANIFEST_PATH: Final[Path] = Path("system/apps/getting_started/app.toml")
APP_NAME: Final[AppName] = AppName("getting-started")
# The frontend's build output, inside the package.
DEFAULT_STATIC_DIRECTORY: Final[Path] = Path(__file__).parent / "static"


class GettingStartedArguments(FrozenModel):
    """Everything the app is told on its command line or by its environment."""

    manifest_path: Path = Field(description="The app.toml to register")
    app_url: AppUrl = Field(description="Where the page is served")
    state_dir: Path = Field(description="The app's state directory: the catalog cache and the first-visit ledger")
    static_directory: Path = Field(description="The frontend's built bundle")
    catalog_url: str = Field(description="Where the template catalog is fetched from; empty disables it")
    host: str = Field(description="The address the page server binds")


def build_pages_app(arguments: GettingStartedArguments) -> Flask:
    app = Flask(__name__, static_folder=None)
    app.register_blueprint(
        build_pages_blueprint(
            static_directory=arguments.static_directory,
            catalog=build_template_catalog_store(
                catalog_url=arguments.catalog_url, state_directory=arguments.state_dir, fetcher=None
            ),
            contract_path=SHELL_APP_CONTRACT_PATH,
        )
    )
    return app


def build_first_window_opener(arguments: GettingStartedArguments) -> FirstWindowOpener:
    return FirstWindowOpener(
        app=APP_NAME,
        ledger=FirstWindowLedger(path=arguments.state_dir / LEDGER_FILENAME),
        shell=HttpShellOps(shell_url=shell_base_url()),
    )


def run_getting_started_app(arguments: GettingStartedArguments) -> int:
    """Serve the page, start the first-visit opener, register the app, and wait for SIGTERM or SIGINT."""
    opener = build_first_window_opener(arguments)
    with serve_in_background(arguments.host, app_url_port(arguments.app_url), build_pages_app(arguments)):
        opener.start()
        try:
            with log_span("Registering {} at {}", APP_NAME, arguments.app_url):
                register_app(arguments.manifest_path, arguments.app_url)
            return wait_for_shutdown_signal()
        finally:
            opener.stop()


def arguments_from_config(
    config: Config, manifest_path: Path, state_dir: Path, static_directory: Path
) -> GettingStartedArguments:
    return GettingStartedArguments(
        manifest_path=manifest_path,
        app_url=AppUrl(f"http://localhost:{config.getting_started_port}"),
        state_dir=state_dir,
        static_directory=static_directory,
        catalog_url=config.system_interface_template_catalog_url,
        host=config.getting_started_host,
    )


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
    help="The app's state directory (the catalog cache and the first-visit ledger)",
)
@click.option(
    "--static-dir",
    "static_directory",
    type=click.Path(path_type=Path),
    default=DEFAULT_STATIC_DIRECTORY,
    show_default=True,
    help="The frontend's built bundle",
)
def main(manifest_path: Path, state_dir: Path, static_directory: Path) -> None:
    """Run the Getting Started app: the ways into the workspace, each starting a chat through the desktop."""
    sys.exit(run_getting_started_app(arguments_from_config(load_config(), manifest_path, state_dir, static_directory)))


if __name__ == "__main__":
    main()
