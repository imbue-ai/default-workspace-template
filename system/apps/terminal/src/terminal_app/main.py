import os
import sys
import urllib.parse
from pathlib import Path
from typing import Final

import click
from app_instances.blueprint import build_instances_app
from app_instances.interfaces import InstanceNudgerInterface
from app_instances.nudge import shell_base_url
from app_instances.sidecar import run_sidecar_app
from app_manifest.manifest import AppManifest
from app_manifest.primitives import AppName, AppUrl, InstancesUrl
from flask import Flask
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import log_span
from imbue.imbue_common.pure import pure
from pydantic import Field

from terminal_app.data_types import TerminalPaths
from terminal_app.discovery import write_server_registered_event
from terminal_app.dispatch import (
    build_ttyd_argv,
    install_dispatch_scripts,
    install_ttyd_web_client,
)
from terminal_app.errors import TerminalAppError
from terminal_app.hooks import HttpShellPoster, build_tmux_hook_blueprint
from terminal_app.sessions import TmuxSessionSource
from terminal_app.store import JsonTerminalSessionStore
from terminal_app.tmux import SubprocessTmux

# The terminal's fixed wiring, all relative to the repo root every supervised program runs from.
# ``test_app_manifests.py`` checks the program registers with this manifest by importing it here.
MANIFEST_PATH: Final[Path] = Path("system/apps/terminal/app.toml")
APP_NAME: Final[AppName] = AppName("terminal")
APP_URL: Final[AppUrl] = AppUrl("http://localhost:7681")
INSTANCES_URL: Final[InstancesUrl] = InstancesUrl("http://127.0.0.1:7682")
STATE_DIR: Final[Path] = Path("data/.state/terminal")
TTYD_WEB_CLIENT_ARCHIVE: Final[Path] = Path(
    "system/vendor/mngr/libs/mngr_ttyd/imbue/mngr_ttyd/resources/ttyd_index.html.gz"
)
TTYD_EXECUTABLE: Final[str] = "ttyd"

# The mngr session-name prefix; agent sessions carry it, terminals do not (as the shell reads it).
ENV_AGENT_SESSION_PREFIX: Final[str] = "MNGR_PREFIX"
DEFAULT_AGENT_SESSION_PREFIX: Final[str] = "mngr-"
ENV_AGENT_STATE_DIR: Final[str] = "MNGR_AGENT_STATE_DIR"


class TerminalAppArguments(FrozenModel):
    """Everything the terminal app is told on its command line or by its environment."""

    manifest_path: Path = Field(description="The app.toml to register")
    app_url: AppUrl = Field(description="Where ttyd serves the terminal pages")
    instances_url: InstancesUrl = Field(description="Where the instances API is served")
    state_dir: Path = Field(description="The app's state directory")
    ttyd_web_client_archive: Path = Field(
        description="The vendored, gzip-compressed OSC 52-capable ttyd web client"
    )
    ttyd_executable: str = Field(description="The ttyd binary to run")
    agent_state_dir: Path | None = Field(
        description="The mngr agent state directory the discovery event is written under; None writes none"
    )
    agent_session_prefix: str = Field(
        description="The prefix of mngr agents' tmux sessions, which are never terminals"
    )


@pure
def ttyd_port(app_url: AppUrl) -> int:
    try:
        port = urllib.parse.urlsplit(app_url).port
    except ValueError as e:
        raise TerminalAppError(
            f"the app URL {app_url!r} names no usable port for ttyd: {e}"
        ) from e
    if port is None:
        raise TerminalAppError(f"the app URL {app_url!r} names no port for ttyd")
    return port


def run_terminal_app(arguments: TerminalAppArguments) -> int:
    """Do what the launcher script did (dispatch scripts, web client, discovery event), then run ttyd under the sidecar."""
    # The dispatch scripts bake the directory in, so it is anchored here rather than left to the
    # cwd of every shell ttyd spawns.
    paths = TerminalPaths(state_dir=arguments.state_dir.absolute())
    with log_span("Installing the ttyd dispatch scripts under {}", paths.commands_dir):
        install_dispatch_scripts(paths)
    is_client_installed = install_ttyd_web_client(
        arguments.ttyd_web_client_archive, paths.ttyd_index_path
    )
    if arguments.agent_state_dir is not None:
        with log_span("Writing the discovery event for {}", arguments.app_url):
            write_server_registered_event(
                arguments.agent_state_dir, APP_NAME, arguments.app_url
            )
    tmux = SubprocessTmux()
    source = TmuxSessionSource(
        tmux=tmux,
        store=JsonTerminalSessionStore(store_path=paths.store_path),
        agent_session_prefix=arguments.agent_session_prefix,
    )

    def build_app(manifest: AppManifest, nudger: InstanceNudgerInterface) -> Flask:
        app = build_instances_app(source, nudger)
        app.register_blueprint(
            build_tmux_hook_blueprint(
                tmux=tmux,
                paths=paths,
                shell=HttpShellPoster(shell_url=shell_base_url()),
                nudger=nudger,
                app_name=manifest.name,
            )
        )
        return app

    return run_sidecar_app(
        manifest_path=arguments.manifest_path,
        app_url=arguments.app_url,
        instances_url=arguments.instances_url,
        child_argv=build_ttyd_argv(
            ttyd_executable=arguments.ttyd_executable,
            port=ttyd_port(arguments.app_url),
            index_path=paths.ttyd_index_path if is_client_installed else None,
            commands_dir=paths.commands_dir,
        ),
        build_app=build_app,
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
    help="Where ttyd serves the terminal pages",
)
@click.option(
    "--instances-url",
    default=INSTANCES_URL,
    show_default=True,
    help="Where the instances API is served",
)
@click.option(
    "--state-dir",
    type=click.Path(path_type=Path),
    default=STATE_DIR,
    show_default=True,
    help="The app's state directory (dispatch scripts, pty records, the instance store)",
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
def main(
    manifest_path: Path,
    app_url: str,
    instances_url: str,
    state_dir: Path,
    ttyd_web_client_archive: Path,
    ttyd_executable: str,
) -> None:
    """Run the workspace terminal: ttyd plus the instances API over the workspace's tmux sessions."""
    agent_state_dir = os.environ.get(ENV_AGENT_STATE_DIR, "")
    arguments = TerminalAppArguments(
        manifest_path=manifest_path,
        app_url=AppUrl(app_url),
        instances_url=InstancesUrl(instances_url),
        state_dir=state_dir,
        ttyd_web_client_archive=ttyd_web_client_archive,
        ttyd_executable=ttyd_executable,
        agent_state_dir=Path(agent_state_dir) if agent_state_dir else None,
        agent_session_prefix=os.environ.get(
            ENV_AGENT_SESSION_PREFIX, DEFAULT_AGENT_SESSION_PREFIX
        ),
    )
    sys.exit(run_terminal_app(arguments))


if __name__ == "__main__":
    main()
