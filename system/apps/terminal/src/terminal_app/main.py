import os
import sys
from pathlib import Path
from typing import Final

import click
from app_instances.blueprint import build_instances_app
from app_instances.interfaces import InstanceNudgerInterface
from app_instances.json_store import app_store_path
from app_instances.nudge import ShellNudger, shell_base_url
from app_instances.sidecar import (
    app_url_port,
    load_instances_manifest,
    register_app,
    serve_in_background,
    split_instances_url,
    wait_for_shutdown_signal,
)
from app_manifest.primitives import AppName, AppUrl, InstancesUrl
from app_manifest.registry import registry_path
from flask import Flask
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import log_span
from pydantic import Field

from terminal_app.data_types import TerminalPaths
from terminal_app.discovery import write_server_registered_event
from terminal_app.dispatch import build_session_command
from terminal_app.hooks import HttpShellPoster, build_tmux_hook_blueprint
from terminal_app.pages import build_pages_blueprint
from terminal_app.primitives import Workdir
from terminal_app.sessions import TmuxSessionSource
from terminal_app.store import JsonTerminalSessionStore
from terminal_app.tmux import SubprocessTmux
from terminal_app.wiring import OOM_TAG_SCRIPT, STATE_DIR

# The terminal's fixed wiring, all relative to the repo root every supervised program runs from.
# The pages are the terminal origin (what a tab or window opens); ttyd is the sibling
# ``terminal-pty`` program (``pty_main.py``) on an origin of its own.
MANIFEST_PATH: Final[Path] = Path("system/apps/terminal/app.toml")
APP_NAME: Final[AppName] = AppName("terminal")
APP_URL: Final[AppUrl] = AppUrl("http://localhost:7681")
INSTANCES_URL: Final[InstancesUrl] = InstancesUrl("http://127.0.0.1:7682")
STORE_PATH: Final[Path] = app_store_path(APP_NAME)

# The wrapper pages are reached through the forwarder, which connects over loopback.
PAGES_HOST: Final[str] = "127.0.0.1"

# The mngr session-name prefix; agent sessions carry it, terminals do not.
ENV_AGENT_SESSION_PREFIX: Final[str] = "MNGR_PREFIX"
DEFAULT_AGENT_SESSION_PREFIX: Final[str] = "mngr-"
ENV_AGENT_STATE_DIR: Final[str] = "MNGR_AGENT_STATE_DIR"


class TerminalAppArguments(FrozenModel):
    """Everything the terminal app is told on its command line or by its environment."""

    manifest_path: Path = Field(description="The app.toml to register")
    app_url: AppUrl = Field(description="Where the wrapper pages are served")
    instances_url: InstancesUrl = Field(description="Where the instances API is served")
    state_dir: Path = Field(description="The app's state directory")
    store_path: Path = Field(description="The instances.json of remembered terminals")
    oom_tag_script: Path = Field(
        description="The memory-shedding tag wrapper a terminal session runs its shell through"
    )
    agent_state_dir: Path | None = Field(
        description="The mngr agent state directory the discovery event is written under; None writes none"
    )
    agent_session_prefix: str = Field(
        description="The prefix of mngr agents' tmux sessions, which are never terminals"
    )


def build_session_source(arguments: TerminalAppArguments, paths: TerminalPaths) -> TmuxSessionSource:
    return TmuxSessionSource(
        tmux=SubprocessTmux(),
        store=JsonTerminalSessionStore(store_path=arguments.store_path),
        agent_session_prefix=arguments.agent_session_prefix,
        default_workdir=Workdir(os.getcwd()),
        sessions_dir=paths.sessions_dir,
        session_command=tuple(build_session_command(arguments.oom_tag_script.absolute())),
    )


def build_pages_app(source: TmuxSessionSource, nudger: InstanceNudgerInterface) -> Flask:
    app = Flask(__name__, static_folder=None)
    app.register_blueprint(build_pages_blueprint(source=source, nudger=nudger, registry_path=registry_path()))
    return app


def build_instances_api_app(
    source: TmuxSessionSource, nudger: InstanceNudgerInterface, paths: TerminalPaths, app_name: AppName
) -> Flask:
    app = build_instances_app(source, nudger)
    app.register_blueprint(
        build_tmux_hook_blueprint(
            source=source,
            paths=paths,
            shell=HttpShellPoster(shell_url=shell_base_url()),
            nudger=nudger,
            app_name=app_name,
        )
    )
    return app


def run_terminal_app(arguments: TerminalAppArguments) -> int:
    """Append the discovery event, recreate the remembered sessions, then serve the pages and the instances API until stopped.

    In order: both servers start listening (so the shell's first fetch after registration
    succeeds), the app is registered through ``forward_port.py --manifest``, and the process
    waits for SIGTERM or SIGINT, returning the exit status for it.
    """
    paths = TerminalPaths(state_dir=arguments.state_dir.absolute())
    if arguments.agent_state_dir is not None:
        with log_span("Writing the discovery event for {}", arguments.app_url):
            write_server_registered_event(arguments.agent_state_dir, APP_NAME, arguments.app_url)
    source = build_session_source(arguments, paths)
    with log_span("Recreating the remembered terminal sessions"):
        source.recreate_remembered_sessions()
    manifest = load_instances_manifest(arguments.manifest_path, arguments.instances_url)
    nudger = ShellNudger(app_name=manifest.name, shell_url=shell_base_url())
    instances_host, instances_port = split_instances_url(arguments.instances_url)
    with (
        serve_in_background(
            instances_host, instances_port, build_instances_api_app(source, nudger, paths, manifest.name)
        ),
        serve_in_background(PAGES_HOST, app_url_port(arguments.app_url), build_pages_app(source, nudger)),
    ):
        with log_span("Registering {} at {}", manifest.name, arguments.app_url):
            register_app(arguments.manifest_path, arguments.app_url)
        return wait_for_shutdown_signal()


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
    help="Where the wrapper pages are served",
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
    help="The app's state directory (dispatch scripts and pty records)",
)
@click.option(
    "--store",
    "store_path",
    type=click.Path(path_type=Path),
    default=STORE_PATH,
    show_default=True,
    help="The instances.json the app remembers its terminals in",
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
    instances_url: str,
    state_dir: Path,
    store_path: Path,
    oom_tag_script: Path,
) -> None:
    """Run the workspace terminal: the wrapper pages plus the instances API over the workspace's tmux sessions."""
    agent_state_dir = os.environ.get(ENV_AGENT_STATE_DIR, "")
    arguments = TerminalAppArguments(
        manifest_path=manifest_path,
        app_url=AppUrl(app_url),
        instances_url=InstancesUrl(instances_url),
        state_dir=state_dir,
        store_path=store_path,
        oom_tag_script=oom_tag_script,
        agent_state_dir=Path(agent_state_dir) if agent_state_dir else None,
        agent_session_prefix=os.environ.get(ENV_AGENT_SESSION_PREFIX, DEFAULT_AGENT_SESSION_PREFIX),
    )
    sys.exit(run_terminal_app(arguments))


if __name__ == "__main__":
    main()
