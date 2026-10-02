import shutil
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Final

import click
import httpx
from flask import Flask
from pydantic import Field

from activity.config import Config
from activity.config import load_config
from activity.cron_entry import RECORDER_LOG_PATH
from activity.cron_entry import SYSTEM_CRON_DIR
from activity.cron_entry import WORKSPACE_ROOT
from activity.cron_entry import cron_entry_text
from activity.cron_entry import install_cron_entry
from activity.history import HISTORY_PATH
from activity.memory_reading import DEFAULT_MEMORY_SOURCES
from activity.pages import APP_NAME
from activity.pages import build_pages_blueprint
from activity.pages import utc_now
from activity.processes import PROC_DIR
from activity.readings import ReadingSources
from activity.serving import app_url_port
from activity.serving import serve_in_background
from activity.serving import wait_for_shutdown_signal
from activity.supervised_programs import socket_process_info_reader
from activity.supervised_programs import supervisor_socket_path
from app_manifest.primitives import AppUrl
from app_manifest.registry import SHELL_APP_CONTRACT_PATH
from app_manifest.registry import register_app
from app_manifest.registry import registry_path
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import log_span
from oom_priority.paths import shed_ledger_path

# The app's fixed wiring, relative to the repo root every supervised program runs from.
MANIFEST_PATH: Final[Path] = Path("system/apps/activity/app.toml")
# The recorder's console script, as the image build installs the app's uv tool.
RECORDER_COMMAND: Final[str] = "activity-record-memory"
DEFAULT_RECORDER_PATH: Final[Path] = Path("/root/.local/bin/activity-record-memory")
# The frontend's build output, inside the package.
DEFAULT_STATIC_DIRECTORY: Final[Path] = Path(__file__).parent / "static"


class ActivityArguments(FrozenModel):
    """Everything the app is told on its command line or by its environment."""

    manifest_path: Path = Field(description="The app.toml to register")
    app_url: AppUrl = Field(description="Where the page is served")
    static_directory: Path = Field(description="The frontend's built bundle")
    host: str = Field(description="The address the page server binds")
    is_registered: bool = Field(
        description="Whether this boot is the workspace's Activity app and registers the manifest; a preview boots "
        "unregistered so it does not re-point the live row"
    )


def build_pages_app(arguments: ActivityArguments, client: httpx.Client) -> Flask:
    app = Flask(__name__, static_folder=None)
    app.register_blueprint(
        build_pages_blueprint(
            static_directory=arguments.static_directory,
            contract_path=SHELL_APP_CONTRACT_PATH,
            sources=ReadingSources(memory=DEFAULT_MEMORY_SOURCES, proc_dir=PROC_DIR, registry_path=registry_path()),
            client=client,
            read_process_info=socket_process_info_reader(supervisor_socket_path()),
            now=utc_now,
            history_path=HISTORY_PATH,
            shed_ledger_path=shed_ledger_path(),
        )
    )
    return app


def install_memory_recorder() -> None:
    """Install the once-a-minute recorder behind the memory-over-time chart (see ``cron_entry``)."""
    recorder_path = Path(shutil.which(RECORDER_COMMAND) or DEFAULT_RECORDER_PATH)
    install_cron_entry(cron_entry_text(recorder_path, WORKSPACE_ROOT, RECORDER_LOG_PATH), SYSTEM_CRON_DIR)


def run_activity_app(arguments: ActivityArguments, wait_for_shutdown: Callable[[], int]) -> int:
    """Serve the page, register the app (unless this is a preview), and wait for SIGTERM or SIGINT."""
    with httpx.Client() as client:
        with serve_in_background(arguments.host, app_url_port(arguments.app_url), build_pages_app(arguments, client)):
            if arguments.is_registered:
                with log_span("Registering {} at {}", APP_NAME, arguments.app_url):
                    register_app(arguments.manifest_path, arguments.app_url)
                install_memory_recorder()
            return wait_for_shutdown()


def arguments_from_config(
    config: Config, manifest_path: Path, static_directory: Path, is_registered: bool
) -> ActivityArguments:
    return ActivityArguments(
        manifest_path=manifest_path,
        app_url=AppUrl(f"http://localhost:{config.activity_port}"),
        static_directory=static_directory,
        host=config.activity_host,
        is_registered=is_registered,
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
    "--static-dir",
    "static_directory",
    type=click.Path(path_type=Path),
    default=DEFAULT_STATIC_DIRECTORY,
    show_default=True,
    help="The frontend's built bundle",
)
@click.option(
    "--no-register",
    "is_unregistered",
    is_flag=True,
    default=False,
    help="Skip the registration: a throwaway boot, such as a preview, that must not re-point the live row",
)
def main(manifest_path: Path, static_directory: Path, is_unregistered: bool) -> None:
    """Run the Activity app: what is using the workspace's memory, in terms the user recognizes."""
    arguments = arguments_from_config(
        config=load_config(),
        manifest_path=manifest_path,
        static_directory=static_directory,
        is_registered=not is_unregistered,
    )
    sys.exit(run_activity_app(arguments, wait_for_shutdown_signal))


if __name__ == "__main__":
    main()
