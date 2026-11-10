"""The keybinding actions the built-in apps declare, and the resolved per-request answer.

Each app with keyboard shortcuts declares its actions (an id, a label, and the default chords)
as ``[[keybindings]]`` in its ``app.toml``. Registration copies them onto the app's registry
row, where the shell's Keyboard shortcuts dialog reads every app's whether or not the app is
running. The app loads the same declarations to answer ``GET /api/keybindings/me``, resolved for
the request identity, which its frontend fetches on load and whenever the shell announces a
change.
"""

from collections.abc import Sequence
from pathlib import Path
from typing import Final

from app_manifest.manifest import KeybindingAction, KeybindingScope, load_manifest
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from pydantic import Field
from workspace_layout.identity import RequestIdentity

from workspace_profile.errors import InvalidKeybindingDeclarationError
from workspace_profile.keybindings import (
    MAX_CHORDS_PER_ACTION,
    is_canonical_chord,
    is_refused_chord,
    is_workspace_wide_chord,
    resolve_app_keybindings,
)
from workspace_profile.snapshot import WorkspaceProfileSnapshot

# Where every app serves its resolved chords, relative to its own API prefix
# ("/api" for the shell and chat; "/__files/api" for files).
KEYBINDINGS_ME_SUBPATH: Final[str] = "/keybindings/me"


class KeybindingsMeResponse(FrozenModel):
    """The requesting user's resolved chords for one app's actions."""

    app: str = Field(description="The app's name")
    chords_by_action: dict[str, list[str]] = Field(description="The resolved chords of each declared action")


@pure
def _is_invalid_default_chord(action: KeybindingAction, chord: str) -> bool:
    if not is_canonical_chord(chord) or is_refused_chord(chord):
        return True
    return action.scope is KeybindingScope.WORKSPACE and not is_workspace_wide_chord(chord)


@pure
def find_invalid_default_chords(actions: Sequence[KeybindingAction]) -> list[str]:
    """The declared default chords no action may have, as ``<action>: <chord>`` lines: one not in the canonical
    spelling, a refused one, or on a workspace-wide action one that typing could trigger (no modifier beyond Shift
    and no function key); and an action declaring more than ``MAX_CHORDS_PER_ACTION`` of them."""
    too_many = [
        f"{action.id}: more than {MAX_CHORDS_PER_ACTION} chords"
        for action in actions
        if len(action.default_chords) > MAX_CHORDS_PER_ACTION
    ]
    return too_many + [
        f"{action.id}: {chord}"
        for action in actions
        for chord in action.default_chords
        if _is_invalid_default_chord(action, chord)
    ]


def load_app_keybinding_actions(manifest_path: Path) -> tuple[KeybindingAction, ...]:
    """An app's declared keybinding actions, read from its ``app.toml``.

    Raises ``InvalidKeybindingDeclarationError`` when a default chord is invalid
    (``find_invalid_default_chords``), and ``app_manifest``'s ``ManifestLoadError`` when the
    manifest cannot be read.
    """
    actions = load_manifest(manifest_path).keybindings
    invalid = find_invalid_default_chords(actions)
    if invalid:
        raise InvalidKeybindingDeclarationError(
            f"{manifest_path} declares default chords it may not have: {', '.join(invalid)}"
        )
    return actions


def resolve_keybindings_me_response(
    app_name: str,
    actions: Sequence[KeybindingAction],
    *,
    snapshot: WorkspaceProfileSnapshot | None,
    identity: RequestIdentity,
    workspace_root: Path,
) -> KeybindingsMeResponse:
    """The ``/keybindings/me`` answer for one request: the app's declared actions, resolved for the identity."""
    resolved = resolve_app_keybindings(
        app_name,
        {str(action.id): tuple(str(chord) for chord in action.default_chords) for action in actions},
        snapshot=snapshot,
        identity=identity,
        workspace_root=workspace_root,
    )
    return KeybindingsMeResponse(app=app_name, chords_by_action=resolved)
