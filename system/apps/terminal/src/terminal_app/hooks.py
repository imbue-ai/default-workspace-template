from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final, assert_never

from app_instances.blueprint import HTTP_BAD_REQUEST, HTTP_NO_CONTENT
from app_instances.interfaces import InstanceNudgerInterface
from app_instances.nudge import post_to_shell
from app_manifest.manifest import describe_validation_error
from app_manifest.primitives import AppName
from flask import Blueprint, jsonify, request
from flask.typing import ResponseReturnValue
from loguru import logger
from pydantic import Field, ValidationError

from terminal_app.data_types import TerminalPaths, TmuxHookEvent, TmuxHookKind
from terminal_app.errors import InvalidTerminalValueError
from terminal_app.interfaces import ShellPosterInterface, TmuxInterface
from terminal_app.primitives import ClientTty, TerminalTabId, TmuxSessionName

TMUX_HOOK_PATH: Final[str] = "/tmux-hook"
BLUEPRINT_NAME: Final[str] = "tmux_hooks"

# The shell's own loopback rule (``_LOOPBACK_CLIENT_HOSTS`` in its server module).
LOOPBACK_CLIENT_HOSTS: Final[frozenset[str]] = frozenset(
    {"127.0.0.1", "::1", "localhost"}
)

# The shell routes this app posts to: the generic tab route of contracts.md section 5 (the shell
# answers 404 until phase 7 of the model), and the terminal notify route it serves today.
TAB_INSTANCE_ROUTE_TEMPLATE: Final[str] = "/api/tabs/{tab_id}/instance"
# CLEANUP: drop the forward to /api/terminals/notify (and this constant) in phase 7 of the
# workspace app model, when the shell re-addresses tabs through /api/tabs/<tab_id>/instance
# and its terminal_session broadcast is gone.
TERMINAL_NOTIFY_ROUTE: Final[str] = "/api/terminals/notify"

# The one status the library's routes never answer; the others come from the blueprint.
HTTP_FORBIDDEN: Final[int] = 403


class HttpShellPoster(ShellPosterInterface):
    """Posts to the shell over loopback through the library's ``post_to_shell``, which swallows an unreachable or refusing shell at debug level."""

    shell_url: str = Field(
        frozen=True, description="The shell's base URL, without a trailing slash"
    )

    def post_json(self, path: str, body: Mapping[str, Any]) -> None:
        post_to_shell(f"{self.shell_url}{path}", body)


def resolve_tab_id_for_tty(
    clients_dir: Path, client_tty: ClientTty
) -> TerminalTabId | None:
    """The tab whose attach recorded ``client_tty``: the file under ``clients_dir`` holding that pty, named by tab id."""
    if not clients_dir.is_dir():
        return None
    for entry in clients_dir.iterdir():
        if not entry.is_file():
            continue
        try:
            recorded_tty = entry.read_text().strip()
        except OSError as e:
            logger.debug("Skipped the pty record {}: {}", entry, e)
            continue
        if recorded_tty != client_tty:
            continue
        try:
            return TerminalTabId(entry.name)
        except InvalidTerminalValueError:
            logger.debug("Skipped the pty record {}: its name is not a tab id", entry)
    return None


def build_tmux_hook_blueprint(
    tmux: TmuxInterface,
    paths: TerminalPaths,
    shell: ShellPosterInterface,
    nudger: InstanceNudgerInterface,
    app_name: AppName,
) -> Blueprint:
    """``POST /tmux-hook``, which the tmux hooks call when a client switches sessions or a session is renamed.

    A session switch re-points the switching client's tab at the session it now shows; a rename
    re-points every tab attached to the renamed session, then nudges the shell because the
    instance list changed (the key is the name).
    """
    blueprint = Blueprint(BLUEPRINT_NAME, __name__)

    def rebind_tab(tab_id: TerminalTabId, session_name: str) -> None:
        try:
            key = TmuxSessionName(session_name)
        except InvalidTerminalValueError:
            logger.debug(
                "Skipped re-pointing tab {}: session {!r} cannot be an instance key",
                tab_id,
                session_name,
            )
            return
        shell.post_json(
            TAB_INSTANCE_ROUTE_TEMPLATE.format(tab_id=tab_id),
            {"app": app_name, "key": key},
        )

    def forward_to_shell(
        event: TmuxHookEvent, terminal_id: TerminalTabId | None
    ) -> None:
        # CLEANUP: delete this forward in phase 7 of the workspace app model; until then it
        # keeps today's terminal_session broadcast, and with it live tab titles, working.
        shell.post_json(
            TERMINAL_NOTIFY_ROUTE,
            {
                "kind": event.kind.value,
                "client_tty": event.client_tty,
                "session_name": event.session_name,
                "session_id": event.session_id,
                "terminal_id": terminal_id,
            },
        )

    def handle_session_changed(event: TmuxHookEvent) -> None:
        try:
            client_tty = ClientTty(event.client_tty)
        except InvalidTerminalValueError:
            logger.debug(
                "Ignored a session switch on {!r}: that is no client pty",
                event.client_tty,
            )
            return
        tab_id = resolve_tab_id_for_tty(paths.clients_dir, client_tty)
        if tab_id is None:
            # An mngr agent's own client, or a tab that has not recorded its pty: no tab to re-point.
            logger.debug(
                "Ignored a session switch on {!r}: no tab has recorded that pty",
                event.client_tty,
            )
            return
        rebind_tab(tab_id, event.session_name)
        forward_to_shell(event, tab_id)

    def handle_session_renamed(event: TmuxHookEvent) -> None:
        for client in tmux.list_clients():
            if client.session_id != event.session_id:
                continue
            tab_id = resolve_tab_id_for_tty(paths.clients_dir, client.client_tty)
            if tab_id is not None:
                rebind_tab(tab_id, event.session_name)
        forward_to_shell(event, None)
        nudger.nudge()

    @blueprint.post(TMUX_HOOK_PATH)
    def receive_tmux_hook() -> ResponseReturnValue:
        if (request.remote_addr or "") not in LOOPBACK_CLIENT_HOSTS:
            return jsonify(
                {"detail": "the tmux hook is only callable from loopback"}
            ), HTTP_FORBIDDEN
        body = request.get_json(force=True, silent=True)
        if not isinstance(body, dict):
            return jsonify(
                {"detail": "the request body must be a JSON object"}
            ), HTTP_BAD_REQUEST
        try:
            event = TmuxHookEvent.model_validate(body)
        except ValidationError as e:
            return jsonify({"detail": describe_validation_error(e)}), HTTP_BAD_REQUEST
        match event.kind:
            case TmuxHookKind.SESSION_CHANGED:
                handle_session_changed(event)
            case TmuxHookKind.SESSION_RENAMED:
                handle_session_renamed(event)
            case _ as unreachable:
                assert_never(unreachable)
        return "", HTTP_NO_CONTENT

    return blueprint
