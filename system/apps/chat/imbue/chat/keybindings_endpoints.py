"""The chat's keybindings manifest and the resolved chords of one request (user-profile-state spec, section 10).

One rebindable action: sending the composed message. Its default chords are what
``MessageInput.ts`` answered before they became rebindable (Enter sends; the
Cmd/Ctrl pair sends too, so a hard-learned habit from other chat apps works).
Shift+Enter stays the fixed line break -- a keystroke that matches no action
falls through to the composer.
"""

from pathlib import Path
from typing import Final

from app_schema.api_schema import RouteModels
from app_schema.api_schema import RouteTable
from app_schema.routes import model_response
from flask import Flask
from flask import Response
from flask import request
from workspace_profile.identity import IDENTITY_HEADER
from workspace_profile.identity import parse_identity_header
from workspace_profile.manifest import KeybindingActionDeclaration
from workspace_profile.manifest import KeybindingsActionsResponse
from workspace_profile.manifest import KeybindingsMeResponse
from workspace_profile.manifest import resolve_keybindings_me_response
from workspace_profile.snapshot import load_workspace_profile_snapshot

KEYBINDINGS_ACTIONS_ROUTE: Final[str] = "/api/keybindings/actions"
KEYBINDINGS_ME_ROUTE: Final[str] = "/api/keybindings/me"

CHAT_KEYBINDINGS_MANIFEST: Final[KeybindingsActionsResponse] = KeybindingsActionsResponse(
    app="chat",
    actions=(
        KeybindingActionDeclaration(
            id="send-message",
            label="Send the message",
            default_chords=("enter", "ctrl+enter", "meta+enter"),
        ),
    ),
)


def keybindings_actions_endpoint() -> Response:
    """``GET /api/keybindings/actions``: the chat's keybindings manifest."""
    return model_response(CHAT_KEYBINDINGS_MANIFEST)


def keybindings_me_endpoint() -> Response:
    """``GET /api/keybindings/me``: the requesting user's resolved chords for the chat's actions."""
    workspace_root = Path.cwd()
    return model_response(
        resolve_keybindings_me_response(
            CHAT_KEYBINDINGS_MANIFEST,
            snapshot=load_workspace_profile_snapshot(workspace_root),
            identity=parse_identity_header(request.headers.get(IDENTITY_HEADER)),
            workspace_root=workspace_root,
        )
    )


ROUTE_TABLE: Final[RouteTable] = {
    ("GET", KEYBINDINGS_ACTIONS_ROUTE): RouteModels(response_model=KeybindingsActionsResponse),
    ("GET", KEYBINDINGS_ME_ROUTE): RouteModels(response_model=KeybindingsMeResponse),
}


def register_routes(application: Flask) -> None:
    """Wire the two keybindings routes onto the Flask application."""
    application.add_url_rule(KEYBINDINGS_ACTIONS_ROUTE, view_func=keybindings_actions_endpoint, methods=["GET"])
    application.add_url_rule(KEYBINDINGS_ME_ROUTE, view_func=keybindings_me_endpoint, methods=["GET"])
