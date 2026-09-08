import argparse
import atexit
import signal
from collections.abc import Sequence
from pathlib import Path
from types import FrameType

from app_manifest.registry import registry_path
from flask import Flask

from imbue.system_interface.app_context import SystemInterfaceState
from imbue.system_interface.app_context import get_state
from imbue.system_interface.config import Config
from imbue.system_interface.config import load_config
from imbue.system_interface.server import create_application
from imbue.system_interface.shell.state import build_shell_state
from imbue.system_interface.shell.state_files import DEFAULT_STATE_DIRECTORY
from imbue.system_interface.ws_broadcaster import WebSocketBroadcaster
from imbue.system_interface.wsgi import make_threaded_server


def _exit_on_signal(signum: int, frame: FrameType | None) -> None:
    """Turn SIGTERM/SIGINT into a clean exit so the ``atexit`` teardown runs.

    The shutdown itself (broadcaster, the inventory, the relay's http client) is registered
    via ``atexit`` in ``main``; raising ``SystemExit`` here ensures that interpreter-exit
    path runs instead of the default abrupt termination.
    """
    raise SystemExit(0)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="System Interface")
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=DEFAULT_STATE_DIRECTORY,
        help="Where the shell keeps its projects, layouts, and client records (contracts.md section 7)",
    )
    parser.add_argument(
        "--preview",
        action="store_true",
        help=(
            "Boot as a preview of a proposed change: read-only over the state directory it is given "
            "(a seeded copy) and the registry MINDS_APPS_FILE names, refusing every verb that would "
            "act on a live instance"
        ),
    )
    return parser.parse_args(argv)


def build_production_state(config: Config, state_directory: Path, is_preview: bool = False) -> SystemInterfaceState:
    """Construct the real object graph -- the composition root.

    This is the single place the production collaborators are wired together. It builds but
    does not start the shell (``main`` does that once the app is assembled), so it watches
    nothing and fetches nothing by itself. Tests build a state via ``testing.build_test_state``.
    """
    return SystemInterfaceState(
        config=config,
        shell=build_shell_state(
            state_directory=state_directory, registry_path=registry_path(), broadcaster=WebSocketBroadcaster()
        ),
        is_preview=is_preview,
    )


def build_application(config: Config, args: argparse.Namespace) -> Flask:
    """Build the Flask app from parsed CLI args: the state over the state directory, and the routes over it."""
    return create_application(build_production_state(config, state_directory=args.state_dir, is_preview=args.preview))


def main() -> None:
    """Run the system-interface server."""
    args = _parse_args(None)
    config = load_config()
    application = build_application(config, args)
    with application.app_context():
        state = get_state()

    # Start the shell now that the app is assembled: the registry watch, the liveness sweep,
    # and the instance fetches. This is the one place it is started; ``build_application``
    # only constructs, so tests that build an app never start it.
    state.shell.start()

    # Tear down the broadcaster and the inventory on exit. ``atexit`` covers a normal return;
    # the signal handlers cover supervisord's SIGTERM and an interactive SIGINT (Ctrl-C), which
    # ``serve_forever`` would otherwise turn into an abrupt exit.
    atexit.register(state.shutdown)
    signal.signal(signal.SIGTERM, _exit_on_signal)
    signal.signal(signal.SIGINT, _exit_on_signal)

    # Threaded HTTP/1.1 server: each request (and each long-lived WebSocket connection) owns
    # its own OS thread, which is what flask-sock needs. HTTP/1.1 (vs werkzeug's HTTP/1.0
    # default) is required for keepalive.
    server = make_threaded_server(
        config.system_interface_host,
        config.system_interface_port,
        application,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
