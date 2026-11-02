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

from app_manifest.manifest import KeybindingAction, load_manifest
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from pydantic import Field
from workspace_layout.identity import RequestIdentity

from workspace_profile.errors import InvalidKeybindingDeclarationError
from workspace_profile.keybindings import is_canonical_chord, resolve_app_keybindings
from workspace_profile.snapshot import WorkspaceProfileSnapshot

# Where every app serves its resolved chords, relative to its own API prefix
# ("/api" for the shell and chat; "/__files/api" for files).
KEYBINDINGS_ME_SUBPATH: Final[str] = "/keybindings/me"


class KeybindingsMeResponse(FrozenModel):
    """The requesting user's resolved chords for one app's actions."""

    app: str = Field(description="The app's name")
    chords_by_action: dict[str, list[str]] = Field(description="The resolved chords of each declared action")


@pure
def find_off_grammar_default_chords(actions: Sequence[KeybindingAction]) -> list[str]:
    """The declared default chords not in the canonical spelling, as ``<action>: <chord>`` lines."""
    return [
        f"{action.id}: {chord}" for action in actions for chord in action.default_chords if not is_canonical_chord(chord)
    ]


def load_app_keybinding_actions(manifest_path: Path) -> tuple[KeybindingAction, ...]:
    """An app's declared keybinding actions, read from its ``app.toml``.

    Raises ``InvalidKeybindingDeclarationError`` when a default chord is off the grammar, and
    ``app_manifest``'s ``ManifestLoadError`` when the manifest cannot be read.
    """
    actions = load_manifest(manifest_path).keybindings
    off_grammar = find_off_grammar_default_chords(actions)
    if off_grammar:
        raise InvalidKeybindingDeclarationError(
            f"{manifest_path} declares default chords off the grammar: {', '.join(off_grammar)}"
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
