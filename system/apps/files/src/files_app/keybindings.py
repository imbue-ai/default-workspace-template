"""The Files keybinding actions: the browser actions a keystroke can trigger, declared in the app's ``app.toml``.

The chords are what `frontend/src/views/keyboard.ts` answered before they became rebindable,
spelled in the canonical grammar.
"""

from functools import cache
from pathlib import Path
from typing import Final

from workspace_profile.manifest import load_app_keybinding_actions

from app_manifest.manifest import KeybindingAction

FILES_APP_NAME: Final[str] = "files"
FILES_APP_MANIFEST_PATH: Final[Path] = Path(__file__).resolve().parents[2] / "app.toml"


@cache
def files_keybinding_actions() -> tuple[KeybindingAction, ...]:
    """The keybinding actions Files declares, read once per process."""
    return load_app_keybinding_actions(FILES_APP_MANIFEST_PATH)
