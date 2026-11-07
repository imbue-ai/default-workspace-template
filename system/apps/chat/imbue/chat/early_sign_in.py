"""Sign in to an AI provider while the workspace is still being built.

The Imbue Studio app runs ``chat-early-sign-in`` over SSH as soon as a new workspace's machine
answers, well before supervisord starts this app, and shows the page it serves on the creation
page. That page is this app's provider chooser, and the routes behind it are this app's own
(``accounts_endpoints.py``) driven by an ``AuthFlowService``, so a sign-in made there is the same
sign-in the workspace's chooser runs.

Two things differ, both because the chat app boots while this runs:

* A sign-in runs under a store of its own, under ``STAGING_HOME``. The chat app's boot reaps
  sign-in CLIs and sweeps folders with no index row, but only in the live store, so it leaves a
  sign-in under way here alone.
* A finished sign-in is moved into the live store and indexed in one step
  (``accounts.move_in_account``), under the live store's lock, so the account is in the
  workspace whether or not the chat app has started.

Studio's side of the contract: once the page is served on ``127.0.0.1:<port>`` the program prints
one line, ``early-sign-in: {"port": <port>}``, and it ends when its stdin closes, which is Studio
hanging up, or after ``MAX_LIFETIME_SECONDS``. ``GET /api/early-sign-in`` says whether a sign-in
is under way and whether the user is done with the chooser; the page reports the latter with
``POST /api/early-sign-in/done``.
"""

import argparse
import json
import shutil
import sys
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from app_manifest.content_security_policy import attach_document_policy
from app_schema.routes import model_response
from flask import Flask
from flask import Response
from flask import current_app
from flask import send_from_directory
from loguru import logger as _loguru_logger
from pydantic import Field
from pydantic import PrivateAttr
from werkzeug.exceptions import NotFound
from workspace_themes.flask_routes import register_workspace_theme_route

from imbue.chat import accounts
from imbue.chat import accounts_endpoints
from imbue.chat.documents import document_response
from imbue.chat.documents import inject_base_path_meta_tag
from imbue.chat.harnesses.auth_flows import AuthFlowService
from imbue.chat.harnesses.auth_flows import reap_orphaned_auth_processes
from imbue.chat.models import OkResponse
from imbue.chat.state import DEFAULT_STATIC_DIRECTORY
from imbue.chat.wsgi import make_threaded_server
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.cli.output_helpers import write_human_line

logger = _loguru_logger

STAGING_HOME: Final[Path] = Path.home() / ".minds-early-sign-in"
STARTUP_LINE_PREFIX: Final[str] = "early-sign-in: "
# A bound on a program whose stdin never closes (a connection that hung rather than dropped).
MAX_LIFETIME_SECONDS: Final[float] = 30 * 60
SIGN_IN_DOCUMENT_FILENAME: Final[str] = "sign_in.html"


class EarlySignInStatus(FrozenModel):
    """Where the early sign-in stands, for the Imbue Studio app."""

    is_flow_pending: bool = Field(description="Whether a sign-in is under way")
    is_done: bool = Field(description="Whether the user is done with the chooser: closed it, or signed in")
    account_ids: tuple[str, ...] = Field(description="The accounts signed in here, already in the live store")


class EarlySignIn(MutableModel):
    """The accounts this program has moved into the live store, and whether the user is done."""

    model_config = {"arbitrary_types_allowed": True}

    staging_home: Path = Field(frozen=True, description="Where sign-ins run until they finish")
    live_home: Path | None = Field(frozen=True, description="The live store's home; None for the user's own")
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _account_ids: list[str] = PrivateAttr(default_factory=list)
    _is_done: bool = PrivateAttr(default=False)

    def commit_account(self, account_id: str, lane: str, display: str) -> accounts.Account:
        """The sign-in service's commit: move the finished folder into the live store and index it there."""
        account = accounts.move_in_account(account_id, lane, display, self.staging_home, self.live_home)
        with self._lock:
            self._account_ids.append(account.id)
        logger.info("Moved early sign-in account {} into the workspace", account.id)
        return account

    def mark_done(self) -> None:
        with self._lock:
            self._is_done = True

    def status(self, is_flow_pending: bool) -> EarlySignInStatus:
        """Where it stands, given whether the sign-in service has a flow under way.

        Asked of the service by the caller, never under this lock: the service commits through
        `commit_account` while holding its own lock, so taking the two the other way round deadlocks.
        """
        with self._lock:
            return EarlySignInStatus(
                is_flow_pending=is_flow_pending,
                is_done=self._is_done,
                account_ids=tuple(self._account_ids),
            )


_EARLY_SIGN_IN_CONFIG_KEY: Final[str] = "imbue.chat.early_sign_in.early_sign_in"
_AUTH_FLOWS_CONFIG_KEY: Final[str] = "imbue.chat.early_sign_in.auth_flows"
_STATIC_DIRECTORY_CONFIG_KEY: Final[str] = "imbue.chat.early_sign_in.static_directory"


def _sign_in_document() -> Response:
    """The chooser page, served at the root of its own origin, so its base path is empty."""
    document_path = current_app.config[_STATIC_DIRECTORY_CONFIG_KEY] / SIGN_IN_DOCUMENT_FILENAME
    html_content = inject_base_path_meta_tag(document_path.read_text(), "")
    return document_response(html_content, is_frontend_built=True)


def _asset(filename: str) -> Response:
    """The page's built bundle, by the hashed name the document links; a plain 404 for a missing one."""
    try:
        return send_from_directory(current_app.config[_STATIC_DIRECTORY_CONFIG_KEY] / "assets", filename)
    except NotFound:
        logger.warning("The chooser page asked for an asset that is not built: {}", filename)
        return Response(status=404)


def _status() -> Response:
    early_sign_in: EarlySignIn = current_app.config[_EARLY_SIGN_IN_CONFIG_KEY]
    auth_flows: AuthFlowService = current_app.config[_AUTH_FLOWS_CONFIG_KEY]
    return model_response(early_sign_in.status(auth_flows.is_flow_pending()))


def _done() -> Response:
    early_sign_in: EarlySignIn = current_app.config[_EARLY_SIGN_IN_CONFIG_KEY]
    early_sign_in.mark_done()
    return model_response(OkResponse())


def build_early_sign_in_application(
    early_sign_in: EarlySignIn, auth_flows: AuthFlowService, static_directory: Path
) -> Flask:
    """The chooser page, its assets and theme, the account routes, and the status routes."""
    application = Flask(__name__, static_folder=None)
    application.config[_EARLY_SIGN_IN_CONFIG_KEY] = early_sign_in
    application.config[_AUTH_FLOWS_CONFIG_KEY] = auth_flows
    application.config[_STATIC_DIRECTORY_CONFIG_KEY] = static_directory
    attach_document_policy(application, {})
    application.add_url_rule("/", view_func=_sign_in_document, methods=["GET"])
    application.add_url_rule("/assets/<path:filename>", view_func=_asset, methods=["GET"])
    application.add_url_rule("/api/early-sign-in", view_func=_status, methods=["GET"])
    application.add_url_rule("/api/early-sign-in/done", view_func=_done, methods=["POST"])
    register_workspace_theme_route(application)
    accounts_endpoints.register_routes(application, auth_flows)
    return application


def _clear_staging(staging_home: Path) -> None:
    """Kill any sign-in CLI an earlier run left, and remove its folders.

    Never fatal, like the chat app's own boot sweep: a reap that cannot read the process table
    is no reason to leave the user without a chooser.
    """
    try:
        reaped = reap_orphaned_auth_processes(staging_home)
    except OSError as e:
        logger.warning("Could not look for sign-in processes left by an earlier early sign-in: {}", e)
    else:
        if reaped:
            logger.warning("Reaped {} sign-in process(es) left by an earlier early sign-in", reaped)
    shutil.rmtree(staging_home, ignore_errors=True)


def _wait_for_hang_up(is_hung_up: threading.Event) -> None:
    """Set `is_hung_up` once stdin closes: Studio ending the session, or the session dropping."""
    sys.stdin.buffer.read()
    is_hung_up.set()


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Serve the provider chooser while the workspace is built")
    parser.add_argument("--port", type=int, default=0, help="The loopback port to serve on; 0 picks a free one")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    """Serve the chooser until stdin closes, then abandon any sign-in under way and clear the staging store."""
    args = _parse_args(argv)
    _clear_staging(STAGING_HOME)
    early_sign_in = EarlySignIn(staging_home=STAGING_HOME, live_home=None)
    auth_flows = AuthFlowService.create(home=STAGING_HOME, commit_account=early_sign_in.commit_account)
    application = build_early_sign_in_application(early_sign_in, auth_flows, DEFAULT_STATIC_DIRECTORY)
    server = make_threaded_server("127.0.0.1", args.port, application)
    threading.Thread(target=server.serve_forever, name="early-sign-in-server", daemon=True).start()
    write_human_line(STARTUP_LINE_PREFIX + json.dumps({"port": server.server_port}))

    is_hung_up = threading.Event()
    threading.Thread(target=_wait_for_hang_up, args=(is_hung_up,), name="early-sign-in-stdin", daemon=True).start()
    if not is_hung_up.wait(MAX_LIFETIME_SECONDS):
        logger.warning("The early sign-in ran for {}s without being hung up on; ending it", MAX_LIFETIME_SECONDS)
    auth_flows.abandon()
    server.shutdown()
    _clear_staging(STAGING_HOME)


if __name__ == "__main__":
    main()
