"""The keybindings manifest the built-in apps serve, and the resolved per-request answer.

Each app with keyboard shortcuts declares its actions (an id, a label, and the
default chords) and serves them at ``GET /api/keybindings/actions``; its
frontend fetches ``GET /api/keybindings/me``, resolved for the request
identity, on load and whenever the shell announces a change. The shapes live
here so the shell's Keyboard settings can read every app's manifest from one
definition.
"""

from pathlib import Path
from typing import Final

from imbue.imbue_common.frozen_model import FrozenModel
from pydantic import Field

from workspace_profile.identity import RequestIdentity
from workspace_profile.keybindings import resolve_app_keybindings
from workspace_profile.snapshot import WorkspaceProfileSnapshot

# Where every app serves the two routes, relative to its own API prefix
# ("/api" for the shell, chat, and terminal; "/__files/api" for files).
KEYBINDINGS_ACTIONS_SUBPATH: Final[str] = "/keybindings/actions"
KEYBINDINGS_ME_SUBPATH: Final[str] = "/keybindings/me"


class KeybindingActionDeclaration(FrozenModel):
    """One action an app's manifest declares: what it does and what presses it by default."""

    id: str = Field(description="The action id, stable across releases (a lowercase slug)")
    label: str = Field(description="What the action does, as the Keyboard settings shows it")
    default_chords: tuple[str, ...] = Field(description="The canonical chords that trigger it out of the box")


class KeybindingsActionsResponse(FrozenModel):
    """An app's keybindings manifest: every action it lets the keyboard trigger."""

    app: str = Field(description="The app's name, the key its assignments are stored under")
    actions: tuple[KeybindingActionDeclaration, ...] = Field(description="The declared actions, in display order")


class KeybindingsMeResponse(FrozenModel):
    """The requesting user's resolved chords for one app's actions."""

    app: str = Field(description="The app's name")
    chords_by_action: dict[str, list[str]] = Field(description="The resolved chords of each declared action")


def resolve_keybindings_me_response(
    manifest: KeybindingsActionsResponse,
    *,
    snapshot: WorkspaceProfileSnapshot | None,
    identity: RequestIdentity,
    workspace_root: Path,
) -> KeybindingsMeResponse:
    """The ``/keybindings/me`` answer for one request: the manifest's actions, resolved for the identity."""
    resolved = resolve_app_keybindings(
        manifest.app,
        {action.id: action.default_chords for action in manifest.actions},
        snapshot=snapshot,
        identity=identity,
        workspace_root=workspace_root,
    )
    return KeybindingsMeResponse(app=manifest.app, chords_by_action=resolved)
