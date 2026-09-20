import os
import sys
from pathlib import Path
from typing import Final

import click
from app_manifest.primitives import AppName, AppUrl
from app_manifest.registry import register_app, registry_path
from flask import Flask
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import log_span
from pydantic import Field

from terminal_app.data_types import TerminalPaths
from terminal_app.discovery import write_server_registered_event
from terminal_app.dispatch import (
    build_session_command,
    warn_if_oom_tag_script_is_missing,
)
from terminal_app.pages import build_pages_blueprint
from terminal_app.primitives import Workdir
from terminal_app.serving import (
    app_url_port,
    serve_in_background,
    wait_for_shutdown_signal,
)
from terminal_app.sessions import TmuxSessionSource
from terminal_app.store import JsonTerminalSessionStore
from terminal_app.tmux import SubprocessTmux
from terminal_app.wiring import OOM_TAG_SCRIPT, STATE_DIR

# The terminal's fixed wiring, all relative to the repo root every supervised program runs from.
# The pages are the terminal origin (what a window opens); ttyd is the sibling ``terminal-pty``
# program (``pty_main.py``) on an origin of its own.
MANIFEST_PATH: Final[Path] = Path("system/apps/terminal/app.toml")
APP_NAME: Final[AppName] = AppName("terminal")
APP_URL: Final[AppUrl] = AppUrl("http://localhost:7681")
# The store of remembered terminals keeps the file name it has always had, so an upgraded workspace
# keeps its terminals.
STORE_PATH: Final[Path] = Path("data/.apps") / APP_NAME / "instances.json"

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
    # The command bakes the directory in, so it is anchored here rather than left to the cwd of
    # every shell tmux spawns.
    oom_tag_script = arguments.oom_tag_script.absolute()
    warn_if_oom_tag_script_is_missing(oom_tag_script)
    return TmuxSessionSource(
        tmux=SubprocessTmux(),
        store=JsonTerminalSessionStore(store_path=arguments.store_path),
        agent_session_prefix=arguments.agent_session_prefix,
        default_workdir=Workdir(os.getcwd()),
        sessions_dir=paths.sessions_dir,
        session_command=tuple(build_session_command(oom_tag_script)),
    )


def build_pages_app(source: TmuxSessionSource) -> Flask:
    app = Flask(__name__, static_folder=None)
    app.register_blueprint(build_pages_blueprint(source=source, registry_path=registry_path()))
    return app


def run_terminal_app(arguments: TerminalAppArguments) -> int:
    """Append the discovery event, recreate the remembered sessions, then serve the pages until stopped.

    In order: the pages start listening (so a window the shell opens right after the registration
    finds them answering), the app is registered through ``forward_port.py --manifest``, and the
    process waits for SIGTERM or SIGINT, returning the exit status for it.
    """
    paths = TerminalPaths(state_dir=arguments.state_dir.absolute())
    if arguments.agent_state_dir is not None:
        with log_span("Writing the discovery event for {}", arguments.app_url):
            write_server_registered_event(arguments.agent_state_dir, APP_NAME, arguments.app_url)
    source = build_session_source(arguments, paths)
    with log_span("Recreating the remembered terminal sessions"):
        source.recreate_remembered_sessions()
    with serve_in_background(PAGES_HOST, app_url_port(arguments.app_url), build_pages_app(source)):
        with log_span("Registering {} at {}", APP_NAME, arguments.app_url):
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
    "--state-dir",
    type=click.Path(path_type=Path),
    default=STATE_DIR,
    show_default=True,
    help="The app's state directory (dispatch scripts and session id files)",
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
    state_dir: Path,
    store_path: Path,
    oom_tag_script: Path,
) -> None:
    """Run the workspace terminal: the wrapper pages over the workspace's tmux sessions."""
    agent_state_dir = os.environ.get(ENV_AGENT_STATE_DIR, "")
    arguments = TerminalAppArguments(
        manifest_path=manifest_path,
        app_url=AppUrl(app_url),
        state_dir=state_dir,
        store_path=store_path,
        oom_tag_script=oom_tag_script,
        agent_state_dir=Path(agent_state_dir) if agent_state_dir else None,
        agent_session_prefix=os.environ.get(ENV_AGENT_SESSION_PREFIX, DEFAULT_AGENT_SESSION_PREFIX),
    )
    sys.exit(run_terminal_app(arguments))


if __name__ == "__main__":
    main()
