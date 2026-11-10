"""The default chord lists the built-in apps declared before each default was written once.

Until the keyboard shortcuts' modifier model (``meta`` is Command on macOS and Ctrl elsewhere), every
default was written twice, a Ctrl form and a Command form, and a save could store an action's whole
default list. Such a save is exactly the old default, not a choice, so resolution reads it as no
assignment and the action gets its current default. A list that differs from the old default in any
way is a real choice and is kept. Lists compare as sets: the order a save wrote them in carries no
meaning.
"""

from collections.abc import Mapping, Sequence
from typing import Final

from imbue.imbue_common.pure import pure

# CLEANUP: remove this module and its use in ``keybindings`` once no workspace holds a keybinding save
# from before the defaults were written once (keyboard shortcuts part B).
SUPERSEDED_DEFAULT_CHORDS: Final[Mapping[str, Mapping[str, frozenset[str]]]] = {
    "chat": {
        "send-message": frozenset({"enter", "ctrl+enter", "meta+enter"}),
    },
    "files": {
        "select-all": frozenset({"ctrl+a", "meta+a"}),
        "go-up": frozenset({"ctrl+arrowup", "meta+arrowup", "alt+arrowup"}),
        "back": frozenset({"ctrl+[", "meta+[", "alt+arrowleft"}),
        "forward": frozenset({"ctrl+]", "meta+]", "alt+arrowright"}),
        "new-folder": frozenset({"ctrl+shift+n", "shift+meta+n"}),
        "open": frozenset({"enter", "ctrl+arrowdown", "meta+arrowdown", "ctrl+o", "meta+o"}),
        "rename": frozenset({"f2"}),
        "delete": frozenset({"delete", "ctrl+backspace", "meta+backspace"}),
        "clear-selection": frozenset({"escape"}),
        "save-file": frozenset({"ctrl+s", "meta+s"}),
    },
    "system_interface": {
        "launcher-secondary-send": frozenset({"ctrl+enter", "meta+enter"}),
    },
}


@pure
def is_superseded_default_assignment(app_name: str, action_id: str, chords: Sequence[str]) -> bool:
    """Whether a stored assignment is exactly the action's default from before the defaults were written once."""
    superseded = SUPERSEDED_DEFAULT_CHORDS.get(app_name, {}).get(action_id)
    return superseded is not None and frozenset(chords) == superseded
