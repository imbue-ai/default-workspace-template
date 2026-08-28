import argparse
import atexit
import signal
from collections.abc import Sequence
from types import FrameType

import httpx
from flask import Flask
from loguru import logger as _loguru_logger

from imbue.system_interface.agent_manager import AgentManager
from imbue.system_interface.app_context import SystemInterfaceState
from imbue.system_interface.app_context import get_state
from imbue.system_interface.config import Config
from imbue.system_interface.config import load_config
from imbue.system_interface.event_queues import AgentEventQueues
from imbue.system_interface.accounts import AccountError
from imbue.system_interface.accounts import reconcile
from imbue.system_interface.harnesses.auth_flows import AuthFlowService
from imbue.system_interface.harnesses.claude.auth import ClaudeAuthService
from imbue.system_interface.layout_ops import LayoutMutex
from imbue.system_interface.server import create_application
from imbue.system_interface.ws_broadcaster import WebSocketBroadcaster
from imbue.system_interface.wsgi import make_threaded_server

logger = _loguru_logger


def _exit_on_signal(signum: int, frame: FrameType | None) -> None:
    """Turn SIGTERM/SIGINT into a clean exit so the ``atexit`` teardown runs.

    The shutdown itself (broadcaster, watchers, agent manager, http clients) is
    registered via ``atexit`` in ``main``; raising ``SystemExit`` here ensures
    that interpreter-exit path runs instead of the default abrupt termination.
    """
    raise SystemExit(0)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="System Interface")
    parser.add_argument("--provider", action="append", default=[], help="Filter agents by provider name (repeatable)")
    parser.add_argument("--include", action="append", default=[], help="CEL include filter for agents (repeatable)")
    parser.add_argument("--exclude", action="append", default=[], help="CEL exclude filter for agents (repeatable)")
    return parser.parse_args(argv)


def build_production_state(
    config: Config,
    provider_names: tuple[str, ...] | None = None,
    include_filters: tuple[str, ...] = (),
    exclude_filters: tuple[str, ...] = (),
) -> SystemInterfaceState:
    """Construct the real object graph -- the composition root.

    This is the single place the production collaborators are wired together.
    It builds but does not start the agent manager (``main`` starts it once the
    app is assembled), so it spawns no ``mngr observe`` pipeline by itself.
    Tests do not use this; they build a ``SystemInterfaceState`` with fakes via
    ``testing.build_test_state``.
    """
    broadcaster = WebSocketBroadcaster()
    agent_manager = AgentManager.build(broadcaster)
    # The codex ledger owns live user-turns (Fix 1); route each committed user-turn it emits onto
    # the same per-agent event fan-out the session watchers use. Wired here (not at manager build)
    # because the manager is constructed before its event-queue collaborator.
    event_queues = AgentEventQueues()
    agent_manager.set_transcript_broadcaster(event_queues.broadcast_all_ignored)
    return SystemInterfaceState(
        config=config,
        provider_names=provider_names,
        include_filters=include_filters,
        exclude_filters=exclude_filters,
        agent_manager=agent_manager,
        event_queues=event_queues,
        # Advisory in-process mutex serializing layout-mutating ops. The agent
        # script never auto-retries on contention -- it surfaces the 409 to the
        # agent along with the in-flight holder's metadata.
        layout_mutex=LayoutMutex(),
        # One long-lived ClaudeAuthService per app so the in-flight OAuth
        # One long-lived service per app: it holds the in-flight sign-in PTY between the
        # start call and the polls that advance it. A successful re-auth restarts the agents
        # bound to that account -- they do not pick up a swapped credential on their own.
        auth_flows=AuthFlowService.create(restart_bound_agents=agent_manager.restart_agents_on_account),
        # Read-only: it reports claude's auth state and writes and restarts nothing, so it
        # needs no collaborators.
        claude_auth_service=ClaudeAuthService(),
        # Single shared synchronous httpx client for server-side API calls to
        # local services (e.g. the /api/browsers passthrough to the browser
        # daemon); a separate one for the latchkey catalog proxy.
        http_client=httpx.Client(follow_redirects=False, timeout=30.0),
        latchkey_http_client=httpx.Client(timeout=30.0),
    )


def build_application(config: Config, args: argparse.Namespace) -> Flask:
    """Build the Flask app from parsed CLI args, threading the agent filters through.

    Wires the production object graph and assembles the app, but does not start
    the agent manager's ``mngr observe`` pipeline -- ``main`` does that once the
    app is built.
    """
    state = build_production_state(
        config,
        provider_names=tuple(args.provider) if args.provider else None,
        include_filters=tuple(args.include),
        exclude_filters=tuple(args.exclude),
    )
    return create_application(state)


def main() -> None:
    """Run the system-interface server."""
    args = _parse_args(None)

    config = load_config()
    # An account is a row plus a folder, and boot is where the two are made to agree. A
    # folder with no row is an abandoned sign-in nothing can reach; a row with no folder is
    # an account that LOOKS usable and silently is not, which is worse. `reconcile` logs
    # both, so a dropped row is visible rather than a mystery.
    #
    # Never fatal. supervisord restarts this program a million times, so an unreadable index
    # -- a truncated write from a hard host kill, a file from a newer build -- would be an
    # unbounded crash loop with no UI and therefore no way to delete the offending account.
    # Every other JSON reader in this app degrades with a warning; so does this one.
    try:
        reconcile()
    except (AccountError, OSError) as e:
        # OSError as well as AccountError: the sweep walks the accounts root, reads and writes
        # credential files and rewrites the index, and a full disk or a bad mount raises from
        # any of them. Every one of those is a reason to start WITHOUT the account store, not
        # a reason to not start.
        logger.opt(exception=e).error("Could not reconcile the account store; continuing without it")
    application = build_application(config, args)
    with application.app_context():
        state = get_state()

    # Start the ``mngr observe`` pipeline now that the app is assembled. This is
    # the one place observe is started; ``build_application`` only constructs, so
    # tests that build an app never spawn it.
    state.agent_manager.start()

    # Tear down the broadcaster, watchers, agent manager, and http clients on
    # exit. ``atexit`` covers a normal return; the signal handlers cover
    # supervisord's SIGTERM and an interactive SIGINT (Ctrl-C), which
    # ``run_simple`` would otherwise turn into an abrupt exit.
    atexit.register(state.shutdown)
    signal.signal(signal.SIGTERM, _exit_on_signal)
    signal.signal(signal.SIGINT, _exit_on_signal)

    # Threaded HTTP/1.1 server: each request (and each long-lived SSE/WebSocket
    # connection) owns its own OS thread, which is what flask-sock needs and what
    # replaces uvicorn's single asyncio event loop. HTTP/1.1 (vs werkzeug's
    # HTTP/1.0 default) is required for keepalive and incremental SSE streaming.
    server = make_threaded_server(
        config.system_interface_host,
        config.system_interface_port,
        application,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
