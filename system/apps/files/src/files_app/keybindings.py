"""The Files keybindings manifest: the browser actions a keystroke can trigger.

The chords are what `frontend/src/views/keyboard.ts` answered before they became
rebindable, spelled in the canonical grammar; the Cmd/Ctrl pairs declare both so
either platform's habit works out of the box. The selection-movement arrows and
the type-ahead jump are navigation primitives rather than actions, so they stay
built in.
"""

from typing import Final

from workspace_profile.manifest import KeybindingActionDeclaration, KeybindingsActionsResponse

FILES_KEYBINDINGS_MANIFEST: Final[KeybindingsActionsResponse] = KeybindingsActionsResponse(
    app="files",
    actions=(
        KeybindingActionDeclaration(id="select-all", label="Select everything", default_chords=("ctrl+a", "meta+a")),
        KeybindingActionDeclaration(
            id="go-up", label="Go up a folder", default_chords=("ctrl+arrowup", "meta+arrowup", "alt+arrowup")
        ),
        KeybindingActionDeclaration(id="back", label="Back", default_chords=("ctrl+[", "meta+[", "alt+arrowleft")),
        KeybindingActionDeclaration(
            id="forward", label="Forward", default_chords=("ctrl+]", "meta+]", "alt+arrowright")
        ),
        KeybindingActionDeclaration(
            id="new-folder", label="New folder", default_chords=("ctrl+shift+n", "meta+shift+n")
        ),
        KeybindingActionDeclaration(
            id="open",
            label="Open the selected entry",
            default_chords=("enter", "ctrl+arrowdown", "meta+arrowdown", "ctrl+o", "meta+o"),
        ),
        KeybindingActionDeclaration(id="rename", label="Rename the selected entry", default_chords=("f2",)),
        KeybindingActionDeclaration(
            id="delete",
            label="Delete the selection",
            default_chords=("delete", "ctrl+backspace", "meta+backspace"),
        ),
        KeybindingActionDeclaration(id="clear-selection", label="Clear the selection", default_chords=("escape",)),
        KeybindingActionDeclaration(id="save-file", label="Save the open file", default_chords=("ctrl+s", "meta+s")),
    ),
)
