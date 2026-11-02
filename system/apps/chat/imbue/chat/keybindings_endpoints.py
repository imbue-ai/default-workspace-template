"""The chat's resolved chords for one request (user-profile-state spec, section 10).

The chat declares one rebindable action, sending the composed message, as ``[[keybindings]]``
in its ``app.toml``; the shell's Keyboard shortcuts dialog reads it from the registry.
"""

from functools import cache
from pathlib import Path
from typing import Final

from app_manifest.manifest import KeybindingAction
from app_schema.api_schema import RouteModels
from app_schema.api_schema import RouteTable
from app_schema.routes import model_response
from flask import Flask
from flask import Response
from workspace_profile.manifest import KEYBINDINGS_ME_SUBPATH
from workspace_profile.manifest import KeybindingsMeResponse
from workspace_profile.manifest import load_app_keybinding_actions
from workspace_profile.manifest import resolve_keybindings_me_response
from workspace_profile.snapshot import load_workspace_profile_snapshot

from imbue.chat.identity import request_identity

KEYBINDINGS_ME_ROUTE: Final[str] = f"/api{KEYBINDINGS_ME_SUBPATH}"

CHAT_APP_NAME: Final[str] = "chat"
CHAT_APP_MANIFEST_PATH: Final[Path] = Path(__file__).resolve().parents[2] / "app.toml"


@cache
def chat_keybinding_actions() -> tuple[KeybindingAction, ...]:
    """The keybinding actions the chat declares, read once per process."""
    return load_app_keybinding_actions(CHAT_APP_MANIFEST_PATH)


def keybindings_me_endpoint() -> Response:
    """``GET /api/keybindings/me``: the requesting user's resolved chords for the chat's actions."""
    workspace_root = Path.cwd()
    return model_response(
        resolve_keybindings_me_response(
            CHAT_APP_NAME,
            chat_keybinding_actions(),
            snapshot=load_workspace_profile_snapshot(workspace_root),
            identity=request_identity(),
            workspace_root=workspace_root,
        )
    )


ROUTE_TABLE: Final[RouteTable] = {
    ("GET", KEYBINDINGS_ME_ROUTE): RouteModels(response_model=KeybindingsMeResponse),
}


def register_routes(application: Flask) -> None:
    """Wire the keybindings route onto the Flask application."""
    application.add_url_rule(KEYBINDINGS_ME_ROUTE, view_func=keybindings_me_endpoint, methods=["GET"])
