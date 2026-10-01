import sys
from collections.abc import Callable
from pathlib import Path
from typing import Final

import click
import httpx
from flask import Flask
from pydantic import Field

from app_manifest.primitives import AppName
from app_manifest.primitives import AppUrl
from app_manifest.registry import SHELL_APP_CONTRACT_PATH
from app_manifest.registry import register_app
from app_manifest.registry import registry_path
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import log_span
from memories.attribution import default_transcript_sources
from memories.config import Config
from memories.config import load_config
from memories.pages import build_pages_blueprint
from memories.serving import app_url_port
from memories.serving import serve_in_background
from memories.serving import wait_for_shutdown_signal

# The app's fixed wiring, relative to the repo root every supervised program runs from.
MANIFEST_PATH: Final[Path] = Path("system/apps/memories/app.toml")
APP_NAME: Final[AppName] = AppName("memories")
# The frontend's build output, inside the package.
DEFAULT_STATIC_DIRECTORY: Final[Path] = Path(__file__).parent / "static"


class MemoriesArguments(FrozenModel):
    """Everything the app is told on its command line or by its environment."""

    manifest_path: Path = Field(description="The app.toml to register")
    app_url: AppUrl = Field(description="Where the page is served")
    static_directory: Path = Field(description="The frontend's built bundle")
    notes_dir: Path = Field(description="The Claude memory notes folder")
    backup_config_path: Path = Field(description="The backups' retention settings")
    restic_env_path: Path = Field(description="The backups' credentials, whose presence says backups are set up")
    host: str = Field(description="The address the page server binds")
    is_registered: bool = Field(
        description="Whether this boot is the workspace's memories app and registers the manifest; a preview boots "
        "unregistered so it does not re-point the live row"
    )


def build_pages_app(arguments: MemoriesArguments, client: httpx.Client) -> Flask:
    app = Flask(__name__, static_folder=None)
    app.register_blueprint(
        build_pages_blueprint(
            static_directory=arguments.static_directory,
            contract_path=SHELL_APP_CONTRACT_PATH,
            notes_dir=arguments.notes_dir,
            backup_config_path=arguments.backup_config_path,
            restic_env_path=arguments.restic_env_path,
            transcript_sources=default_transcript_sources(notes_dir=arguments.notes_dir, work_dir=Path.cwd()),
            registry_path=registry_path(),
            client=client,
        )
    )
    return app


def run_memories_app(arguments: MemoriesArguments, wait_for_shutdown: Callable[[], int]) -> int:
    """Serve the page, register the app (unless this is a preview), and wait for SIGTERM or SIGINT."""
    with httpx.Client() as client:
        with serve_in_background(arguments.host, app_url_port(arguments.app_url), build_pages_app(arguments, client)):
            if arguments.is_registered:
                with log_span("Registering {} at {}", APP_NAME, arguments.app_url):
                    register_app(arguments.manifest_path, arguments.app_url)
            return wait_for_shutdown()


def arguments_from_config(
    config: Config, manifest_path: Path, static_directory: Path, is_registered: bool
) -> MemoriesArguments:
    return MemoriesArguments(
        manifest_path=manifest_path,
        app_url=AppUrl(f"http://localhost:{config.memories_port}"),
        static_directory=static_directory,
        notes_dir=config.memories_notes_dir,
        backup_config_path=config.memories_backup_config_path,
        restic_env_path=config.memories_restic_env_path,
        host=config.memories_host,
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
    """Run the memories app: what the workspace's Claude chats have written down, and a way to correct or delete it."""
    arguments = arguments_from_config(
        config=load_config(),
        manifest_path=manifest_path,
        static_directory=static_directory,
        is_registered=not is_unregistered,
    )
    sys.exit(run_memories_app(arguments, wait_for_shutdown_signal))


if __name__ == "__main__":
    main()
