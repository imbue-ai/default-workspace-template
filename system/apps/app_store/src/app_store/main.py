import sys
from collections.abc import Callable
from pathlib import Path
from typing import Final

import click
from flask import Flask
from loguru import logger
from pydantic import Field

from app_manifest.errors import RegistryReadError
from app_manifest.primitives import AppName
from app_manifest.primitives import AppUrl
from app_manifest.registry import RegistryRow
from app_manifest.registry import SHELL_APP_CONTRACT_PATH
from app_manifest.registry import read_registry
from app_manifest.registry import register_app
from app_manifest.registry_location import registry_path
from app_store.added_apps import AddedAppStore
from app_store.adding import AppAdder
from app_store.adding import local_exclude_path
from app_store.catalog import load_app_catalog
from app_store.command_finder import ClaudeCommandWriter
from app_store.command_finder import CommandFinder
from app_store.command_finder import ProcessTrialRunner
from app_store.config import Config
from app_store.config import load_config
from app_store.engine import InstallEngine
from app_store.layout import StoreLayout
from app_store.package_check import CommandResolver
from app_store.package_check import HttpRegistryClient
from app_store.package_check import LOOKUPS_CACHE_FILENAME
from app_store.package_check import LookupCache
from app_store.package_check import PackageChecker
from app_store.pages import build_pages_blueprint
from app_store.process_memory import SupervisedProcessMemoryReader
from app_store.records import RecordStore
from app_store.room import SystemResourceReader
from app_store.system import CommandPackageInstaller
from app_store.system import ForwardPortRegistrar
from app_store.system import HttpPortProbe
from app_store.system import SubprocessCommandRunner
from app_store.system import SupervisorctlSupervisor
from app_store.template_sizes import GithubRepositorySizeFetcher
from app_store.template_sizes import SIZES_CACHE_FILENAME
from app_store.template_sizes import TemplateSizeStore
from getting_started.serving import app_url_port
from getting_started.serving import serve_in_background
from getting_started.serving import wait_for_shutdown_signal
from getting_started.template_catalog import build_template_catalog_store
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import log_span

# The app's fixed wiring, relative to the repo root every supervised program runs from.
MANIFEST_PATH: Final[Path] = Path("system/apps/app_store/app.toml")
APP_NAME: Final[AppName] = AppName("app-store")
DEFAULT_STATIC_DIRECTORY: Final[Path] = Path(__file__).parent / "static"


class AppStoreArguments(FrozenModel):
    """Everything the app is told on its command line or by its environment."""

    manifest_path: Path = Field(description="The app.toml to register")
    app_url: AppUrl = Field(description="Where the page is served")
    data_dir: Path = Field(description="The store's data folder: install records and installed apps")
    state_dir: Path = Field(description="The store's state folder: the catalog cache and template sizes")
    static_directory: Path = Field(description="The frontend's built bundle")
    catalog_url: str = Field(description="Where the template catalog is fetched from; empty disables it")
    host: str = Field(description="The address the page server binds")
    is_registered: bool = Field(
        description="Whether this boot is the workspace's App Store: it registers its manifest and runs installs. A "
        "preview boots unregistered, over its own data, and runs no installs."
    )


def read_registry_rows() -> list[RegistryRow]:
    try:
        return read_registry(registry_path())
    except RegistryReadError as e:
        logger.warning("Could not read the app registry: {}", e)
        return []


def build_engine(arguments: AppStoreArguments, registry_reader: Callable[[], list[RegistryRow]]) -> InstallEngine:
    layout = StoreLayout(data_dir=arguments.data_dir.absolute(), repo_root=Path.cwd())
    runner = SubprocessCommandRunner()
    return InstallEngine(
        curated=load_app_catalog(),
        added=AddedAppStore(path=layout.added_apps_path),
        layout=layout,
        records=RecordStore(path=layout.records_path),
        installer=CommandPackageInstaller(runner=runner),
        runner=runner,
        supervisor=SupervisorctlSupervisor(),
        registrar=ForwardPortRegistrar(),
        probe=HttpPortProbe(),
        resources=SystemResourceReader(disk_path=layout.data_dir),
        read_registry_rows=registry_reader,
        memory_reader=SupervisedProcessMemoryReader(),
    )


def build_adder(arguments: AppStoreArguments, engine: InstallEngine) -> AppAdder:
    """Adding apps: registry lookups cached in the state folder; the drop-ins listed in the repository's excludes
    (only for the workspace's own store: a preview touches no repository)."""
    return AppAdder(
        engine=engine,
        checker=PackageChecker(
            registry=HttpRegistryClient(),
            resolver=CommandResolver(),
            cache=LookupCache(path=arguments.state_dir / LOOKUPS_CACHE_FILENAME),
        ),
        exclude_path=local_exclude_path(engine.layout.repo_root) if arguments.is_registered else None,
    )


def build_finder(engine: InstallEngine, adder: AppAdder) -> CommandFinder:
    """Working out added apps' start commands: AI writes them, and each is test-launched before it is offered."""
    return CommandFinder(
        engine=engine,
        checker=adder.checker,
        writer=ClaudeCommandWriter(),
        trial_runner=ProcessTrialRunner(repo_root=engine.layout.repo_root),
    )


def build_pages_app(arguments: AppStoreArguments, engine: InstallEngine, adder: AppAdder) -> Flask:
    app = Flask(__name__, static_folder=None)
    app.register_blueprint(
        build_pages_blueprint(
            static_directory=arguments.static_directory,
            engine=engine,
            adder=adder,
            finder=build_finder(engine, adder),
            template_catalog=build_template_catalog_store(
                catalog_url=arguments.catalog_url, state_directory=arguments.state_dir, fetcher=None
            ),
            template_sizes=TemplateSizeStore(
                cache_path=arguments.state_dir / SIZES_CACHE_FILENAME, fetcher=GithubRepositorySizeFetcher()
            ),
            contract_path=SHELL_APP_CONTRACT_PATH,
        )
    )
    return app


def run_app_store(arguments: AppStoreArguments, engine: InstallEngine, wait_for_shutdown: Callable[[], int]) -> int:
    """Reconcile and start the install worker (registered boots only), serve the page, register the app, and wait
    for SIGTERM or SIGINT."""
    adder = build_adder(arguments, engine)
    if arguments.is_registered:
        with log_span("Reconciling the store's installs with the workspace"):
            adder.sync_excludes()
            engine.start()
    try:
        with serve_in_background(
            arguments.host, app_url_port(arguments.app_url), build_pages_app(arguments, engine, adder)
        ):
            if arguments.is_registered:
                with log_span("Registering {} at {}", APP_NAME, arguments.app_url):
                    register_app(arguments.manifest_path, arguments.app_url)
            return wait_for_shutdown()
    finally:
        engine.stop()


def arguments_from_config(
    config: Config, manifest_path: Path, static_directory: Path, is_registered: bool
) -> AppStoreArguments:
    return AppStoreArguments(
        manifest_path=manifest_path,
        app_url=AppUrl(f"http://localhost:{config.app_store_port}"),
        data_dir=config.app_store_data_dir,
        state_dir=config.app_store_state_dir,
        static_directory=static_directory,
        catalog_url=config.app_store_template_catalog_url,
        host=config.app_store_host,
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
    help="Skip the registration and the install worker: a throwaway boot, such as a preview",
)
def main(manifest_path: Path, static_directory: Path, is_unregistered: bool) -> None:
    """Run the App Store app: one-click installs of open-source apps, and published templates to adopt."""
    arguments = arguments_from_config(
        load_config(), manifest_path, static_directory, is_registered=not is_unregistered
    )
    sys.exit(run_app_store(arguments, build_engine(arguments, read_registry_rows), wait_for_shutdown_signal))


if __name__ == "__main__":
    main()
