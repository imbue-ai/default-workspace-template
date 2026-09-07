import argparse
import atexit
import signal
from collections.abc import Sequence
from pathlib import Path
from types import FrameType
from typing import Final

import httpx
from app_instances.nudge import ShellNudger
from app_instances.nudge import ThreadedNudger
from app_instances.nudge import shell_base_url
from app_instances.sidecar import register_app
from app_manifest.primitives import AppUrl
from flask import Flask
from loguru import logger as _loguru_logger

from imbue.chat.accounts import AccountError
from imbue.chat.accounts import reconcile
from imbue.chat.agent_manager import AgentManager
from imbue.chat.config import Config
from imbue.chat.config import load_config
from imbue.chat.event_queues import AgentEventQueues
from imbue.chat.harnesses.auth_flows import AuthFlowService
from imbue.chat.harnesses.auth_flows import reap_orphaned_auth_processes
from imbue.chat.harnesses.claude.auth import ClaudeAuthService
from imbue.chat.instances import CHAT_APP_NAME
from imbue.chat.message_stamps import DEFAULT_STAMPS_PATH
from imbue.chat.message_stamps import MessageStampStore
from imbue.chat.server import create_application
from imbue.chat.state import ChatState
from imbue.chat.state import state_of
from imbue.chat.ws_broadcaster import WebSocketBroadcaster
from imbue.chat.wsgi import make_threaded_server

logger = _loguru_logger

# The chat's manifest, relative to the repo root every supervised program runs from.
# ``test_app_manifests.py`` reads this constant from the source to check the program registers
# with this manifest.
MANIFEST_PATH: Final[Path] = Path("system/apps/chat/app.toml")


def _exit_on_signal(signum: int, frame: FrameType | None) -> None:
    """Turn SIGTERM/SIGINT into a clean exit so the ``atexit`` teardown runs.

    The shutdown itself (broadcaster, watchers, agent manager, http clients) is
    registered via ``atexit`` in ``main``; raising ``SystemExit`` here ensures
    that interpreter-exit path runs instead of the default abrupt termination.
    """
    raise SystemExit(0)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="The chat app")
    parser.add_argument("--provider", action="append", default=[], help="Filter agents by provider name (repeatable)")
    parser.add_argument("--include", action="append", default=[], help="CEL include filter for agents (repeatable)")
    parser.add_argument("--exclude", action="append", default=[], help="CEL exclude filter for agents (repeatable)")
    parser.add_argument(
        "--manifest",
        type=Path,
        default=MANIFEST_PATH,
        help="The app.toml to register with the shell at startup",
    )
    parser.add_argument(
        "--no-register",
        action="store_true",
        help="Skip the registration (a throwaway boot that must not re-point the live chat row)",
    )
    parser.add_argument(
        "--preflight",
        action="store_true",
        help=(
            "Boot without side effects, for the update apply's pre-flight check: no account "
            "reconciliation (it reaps sign-in processes), no agent manager (no mngr observe, "
            "no sweep, no memory prioritizer, no nudges to the shell), and no registration"
        ),
    )
    return parser.parse_args(argv)


def build_production_state(
    config: Config,
    provider_names: tuple[str, ...] | None = None,
    include_filters: tuple[str, ...] = (),
    exclude_filters: tuple[str, ...] = (),
) -> ChatState:
    """Construct the real object graph -- the composition root.

    This is the single place the production collaborators are wired together.
    It builds but does not start the agent manager (``main`` starts it once the
    app is assembled), so it spawns no ``mngr observe`` pipeline by itself.
    Tests do not use this; they build a ``ChatState`` with fakes via
    ``testing.build_test_state``.
    """
    broadcaster = WebSocketBroadcaster()
    agent_manager = AgentManager.build(broadcaster, message_stamps=MessageStampStore(path=DEFAULT_STAMPS_PATH))
    # The codex ledger owns live user-turns (Fix 1); route each committed user-turn it emits onto
    # the same per-agent event fan-out the session watchers use. Wired here (not at manager build)
    # because the manager is constructed before its event-queue collaborator.
    event_queues = AgentEventQueues()
    agent_manager.set_transcript_broadcaster(event_queues.broadcast_batch)
    state = ChatState(
        config=config,
        provider_names=provider_names,
        include_filters=include_filters,
        exclude_filters=exclude_filters,
        agent_manager=agent_manager,
        event_queues=event_queues,
        # One long-lived service per app: it holds the in-flight sign-in PTY between the
        # start call and the polls that advance it. A successful re-auth restarts the agents
        # bound to that account -- they do not pick up a swapped credential on their own.
        auth_flows=AuthFlowService.create(restart_bound_agents=agent_manager.restart_agents_on_account_in_background),
        # Read-only: it reports claude's auth state and writes and restarts nothing, so it
        # needs no collaborators.
        claude_auth_service=ClaudeAuthService(),
        # One shared synchronous httpx client for server-side calls to local services; a
        # separate one for the latchkey catalog proxy.
        http_client=httpx.Client(follow_redirects=False, timeout=30.0),
        latchkey_http_client=httpx.Client(timeout=30.0),
    )
    # Eviction wiring: when the manager sees an agent destroyed or its lifecycle
    # transition into a dead state, the state drops that agent's watcher -- the
    # resident transcript, watch thread, and inotify watches go with it.
    agent_manager.set_watcher_eviction_callback(state.stop_and_remove_watcher)
    return state


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


def _reconcile_account_store() -> None:
    """Make the account rows and folders agree before anything reads them.

    An account is a row plus a folder, and boot is where the two are made to agree. A
    folder with no row is an abandoned sign-in nothing can reach; a row with no folder is
    an account that LOOKS usable and silently is not, which is worse. `reconcile` logs
    both, so a dropped row is visible rather than a mystery.

    Never fatal. supervisord restarts this program a million times, so an unreadable index
    -- a truncated write from a hard host kill, a file from a newer build -- would be an
    unbounded crash loop with no UI and therefore no way to delete the offending account.
    Every other JSON reader in this app degrades with a warning; so does this one.
    """
    try:
        # Before the sweep, not after: an orphan from a previous process is still holding an
        # account folder open and can still write a credential into it, so reaping first is
        # what stops it writing into a folder the sweep is about to delete -- or over one
        # whose parked backup the sweep is about to restore.
        reaped = reap_orphaned_auth_processes()
        if reaped:
            logger.warning("Reaped {} sign-in process(es) left by a previous run", reaped)
        reconcile()
    except (AccountError, OSError) as e:
        # OSError as well as AccountError: the sweep walks the accounts root, reads and writes
        # credential files and rewrites the index, and a full disk or a bad mount raises from
        # any of them. Every one of those is a reason to start WITHOUT the account store, not
        # a reason to not start.
        logger.opt(exception=e).error("Could not reconcile the account store; continuing without it")


def main() -> None:
    """Run the chat app: register with the shell, start ``mngr observe``, and serve.

    Under ``--preflight`` the app only imports, builds, and serves: the update apply boots
    the merged chat this way on a throwaway port to learn whether it can start at all
    (mngr and the harness plugins import here, so a broken plugin table or a missing
    dependency surfaces here first) without touching the live workspace.
    """
    args = _parse_args(None)
    config = load_config()
    if args.preflight:
        logger.info("Booting in pre-flight mode: no account reconciliation, no agent manager, no registration")
    else:
        _reconcile_account_store()
    application = build_application(config, args)
    state = state_of(application)

    if not args.preflight:
        # The chat app tells the shell when its instance list changes (contracts.md section
        # 5). Installed here, at the process entry point, so a manager a test builds nudges
        # nobody; on a thread of its own, so an agent event never waits on the shell.
        state.agent_manager.set_nudger(
            ThreadedNudger(inner=ShellNudger(app_name=CHAT_APP_NAME, shell_url=shell_base_url()))
        )

        # Start the ``mngr observe`` pipeline now that the app is assembled. This is
        # the one place observe is started; ``build_application`` only constructs, so
        # tests that build an app never spawn it.
        state.agent_manager.start()

    # Tear down the broadcaster, watchers, agent manager, and http clients on
    # exit. ``atexit`` covers a normal return; the signal handlers cover
    # supervisord's SIGTERM and an interactive SIGINT (Ctrl-C), which
    # ``serve_forever`` would otherwise turn into an abrupt exit.
    atexit.register(state.shutdown)
    signal.signal(signal.SIGTERM, _exit_on_signal)
    signal.signal(signal.SIGINT, _exit_on_signal)

    # Threaded HTTP/1.1 server: each request (and each long-lived SSE/WebSocket
    # connection) owns its own OS thread, which is what flask-sock needs. HTTP/1.1
    # (vs werkzeug's HTTP/1.0 default) is required for keepalive and incremental
    # SSE streaming.
    server = make_threaded_server(config.chat_host, config.chat_port, application)

    # Registered once the socket is bound and just before serving, so the shell's first
    # fetch after the registration finds the app answering (a 503 until the agent list is
    # known, never a refused connection).
    if not (args.no_register or args.preflight):
        register_app(args.manifest, AppUrl(f"http://localhost:{config.chat_port}"))
    server.serve_forever()


if __name__ == "__main__":
    main()
