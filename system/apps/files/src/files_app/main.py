import sys
from pathlib import Path
from typing import Final

import click
from app_instances.data_types import InstanceLifetime
from app_instances.json_store import JsonStoreInstanceSource, app_store_path
from app_instances.primitives import InstanceKeyPrefix, TitleTemplate
from app_instances.sidecar import app_url_port, run_sidecar
from app_manifest.primitives import AppName, AppUrl, InstancesUrl
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from pydantic import Field

# The file viewer's fixed wiring, all relative to the repo root every supervised program runs
# from. ``test_app_manifests.py`` checks the program registers with this manifest by importing it.
MANIFEST_PATH: Final[Path] = Path("system/apps/files/app.toml")
APP_NAME: Final[AppName] = AppName("files")
APP_URL: Final[AppUrl] = AppUrl("http://localhost:8300")
INSTANCES_URL: Final[InstancesUrl] = InstancesUrl("http://127.0.0.1:8301")
STORE_PATH: Final[Path] = app_store_path(APP_NAME)
DUFS_EXECUTABLE: Final[str] = "dufs"

# dufs serves the workspace's user-facing file tree with every operation enabled, bound to
# loopback (the workspace origin is what gates access), from the vendored, patched frontend.
DUFS_BIND_HOST: Final[str] = "127.0.0.1"
DUFS_ASSETS_DIR: Final[Path] = Path("system/apps/files/assets")
SERVED_DIR: Final[Path] = Path("data")

# The files row of contracts.md section 4.3: ``files-<N>`` keys, ``File Viewer <N>`` titles,
# deleted by the shell once nothing references them, never renamed, reopened where they were.
KEY_PREFIX: Final[InstanceKeyPrefix] = InstanceKeyPrefix("files")
TITLE_TEMPLATE: Final[TitleTemplate] = TitleTemplate("File Viewer {n}")
LIFETIME: Final[InstanceLifetime] = InstanceLifetime.REFERENCED


class FilesAppArguments(FrozenModel):
    """Everything the files app is told on its command line."""

    manifest_path: Path = Field(description="The app.toml to register")
    app_url: AppUrl = Field(description="Where dufs serves the file viewer pages")
    instances_url: InstancesUrl = Field(description="Where the instances API is served")
    store_path: Path = Field(description="The instances.json of file-viewer records")
    dufs_executable: str = Field(description="The dufs binary to run")


@pure
def build_dufs_argv(dufs_executable: str, port: int) -> list[str]:
    """Today's dufs command line, exactly as the supervisord program used to spell it."""
    return [
        dufs_executable,
        "--allow-all",
        "--bind",
        DUFS_BIND_HOST,
        "--port",
        str(port),
        "--assets",
        str(DUFS_ASSETS_DIR),
        str(SERVED_DIR),
    ]


def build_files_source(store_path: Path) -> JsonStoreInstanceSource:
    return JsonStoreInstanceSource(
        store_path=store_path,
        key_prefix=KEY_PREFIX,
        title_template=TITLE_TEMPLATE,
        lifetime=LIFETIME,
        is_renameable=False,
        is_location_tracked=True,
    )


def run_files_app(arguments: FilesAppArguments) -> int:
    """Serve the instances API from the JSON store and run dufs under the sidecar, returning dufs's exit status."""
    return run_sidecar(
        manifest_path=arguments.manifest_path,
        app_url=arguments.app_url,
        instances_url=arguments.instances_url,
        child_argv=build_dufs_argv(
            dufs_executable=arguments.dufs_executable,
            port=app_url_port(arguments.app_url),
        ),
        source=build_files_source(arguments.store_path),
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
    "--app-url",
    default=APP_URL,
    show_default=True,
    help="Where dufs serves the file viewer pages",
)
@click.option(
    "--instances-url",
    default=INSTANCES_URL,
    show_default=True,
    help="Where the instances API is served",
)
@click.option(
    "--store",
    "store_path",
    type=click.Path(path_type=Path),
    default=STORE_PATH,
    show_default=True,
    help="The instances.json the app keeps its file-viewer records in",
)
@click.option(
    "--dufs",
    "dufs_executable",
    default=DUFS_EXECUTABLE,
    show_default=True,
    help="The dufs binary",
)
def main(
    manifest_path: Path,
    app_url: str,
    instances_url: str,
    store_path: Path,
    dufs_executable: str,
) -> None:
    """Run the workspace file viewer: dufs plus the instances API over a JSON store."""
    arguments = FilesAppArguments(
        manifest_path=manifest_path,
        app_url=AppUrl(app_url),
        instances_url=InstancesUrl(instances_url),
        store_path=store_path,
        dufs_executable=dufs_executable,
    )
    sys.exit(run_files_app(arguments))


if __name__ == "__main__":
    main()
