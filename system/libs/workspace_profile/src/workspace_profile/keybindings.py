"""The keybinding chord grammar and the per-assignment resolution (user-profile-state spec, section 10).

A chord is lowercase tokens joined with ``+``: the held modifiers in one fixed
order (ctrl, alt, shift, meta), then exactly one key -- a single printable
character, or a named key spelled as its lowercased ``KeyboardEvent.key`` value
(the built-in apps handle raw keydown events, so their names are the aligned
vocabulary). ``cmd`` is a recorder-side alias normalized to ``meta``; the
canonical form never contains it.

The Imbue Studio desktop mirrors this grammar on its write path (the
``account-settings`` route validates every stored chord), and the vendored
``workspace_profile_contract.json`` fixture pins the two vocabularies against
each other -- a change on either side fails the other until mirrored.

A keybindings value is one map of app name, then action id, then the assigned
chords. Unlike a scalar setting it resolves per assignment: for each action,
the requesting user's override in this workspace wins, then (owner only) the
shell's unsent account changes, then (owner only) the owner account's value
from the snapshot, then the app's declared default.
"""

import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final

from imbue.imbue_common.pure import pure

from workspace_profile.errors import InvalidChordError
from workspace_profile.identity import RequestIdentity
from workspace_profile.overrides import (
    load_unsent_change_values,
    load_user_override_values,
)
from workspace_profile.resolve import resolve_request_user_id
from workspace_profile.snapshot import WorkspaceProfileSnapshot

# The one snapshot/override/unsent key keybindings live under.
KEYBINDINGS_SETTING_KEY: Final[str] = "keybindings"

# The held modifiers, in the one order a canonical chord spells them.
CHORD_MODIFIER_ORDER: Final[tuple[str, ...]] = ("ctrl", "alt", "shift", "meta")

# The named keys a chord may end in, each the lowercased KeyboardEvent.key value
# ("plus" stands in for the literal "+", which the separator makes unspellable).
CHORD_NAMED_KEYS: Final[tuple[str, ...]] = (
    "enter",
    "escape",
    "tab",
    "space",
    "backspace",
    "delete",
    "insert",
    "home",
    "end",
    "pageup",
    "pagedown",
    "arrowup",
    "arrowdown",
    "arrowleft",
    "arrowright",
    "plus",
    "f1",
    "f2",
    "f3",
    "f4",
    "f5",
    "f6",
    "f7",
    "f8",
    "f9",
    "f10",
    "f11",
    "f12",
)

# Recorder-side spellings normalized into the canonical vocabulary.
_TOKEN_ALIASES: Final[dict[str, str]] = {
    "cmd": "meta",
    "esc": "escape",
    "return": "enter",
    " ": "space",
    "spacebar": "space",
}

# A single printable character key: anything but whitespace and "+" (spelled "plus").
_SINGLE_CHARACTER_KEY_RE: Final[re.Pattern[str]] = re.compile(r"^[^\s+]$")

# App names and action ids as the manifests declare them.
APP_OR_ACTION_ID_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


@pure
def _is_canonical_key(token: str) -> bool:
    if token in CHORD_NAMED_KEYS:
        return True
    if _SINGLE_CHARACTER_KEY_RE.match(token) is not None:
        return token == token.lower()
    return False


@pure
def is_canonical_chord(chord: str) -> bool:
    """Whether the chord is already in the canonical spelling the grammar stores."""
    if not chord:
        return False
    tokens = chord.split("+")
    modifiers, key = tokens[:-1], tokens[-1]
    if not _is_canonical_key(key):
        return False
    modifier_indexes = []
    for modifier in modifiers:
        if modifier not in CHORD_MODIFIER_ORDER:
            return False
        modifier_indexes.append(CHORD_MODIFIER_ORDER.index(modifier))
    is_ordered_without_repeats = modifier_indexes == sorted(set(modifier_indexes))
    return is_ordered_without_repeats


@pure
def normalize_chord(spelled: str) -> str:
    """The canonical spelling of a recorder-side chord. Raises ``InvalidChordError``.

    Lowercases, maps the aliases (``cmd`` to ``meta``, ``esc`` to ``escape``, ...),
    and reorders the modifiers; the key itself must already be one the grammar
    knows.
    """
    raw_tokens = spelled.split("+")
    # "ctrl++" splits as ["ctrl", "", ""]: the chord ends in the literal "+"
    # key, which the canonical grammar spells "plus".
    if len(raw_tokens) >= 2 and raw_tokens[-1] == "" and raw_tokens[-2] == "":
        raw_tokens = raw_tokens[:-2] + ["plus"]
    tokens: list[str] = []
    for raw_token in raw_tokens:
        lowered = raw_token.lower()
        tokens.append(_TOKEN_ALIASES.get(lowered, lowered))
    modifiers, key = tokens[:-1], tokens[-1]
    if not _is_canonical_key(key):
        raise InvalidChordError(f"{spelled!r} does not end in a key the grammar knows")
    if len(set(modifiers)) != len(modifiers) or any(modifier not in CHORD_MODIFIER_ORDER for modifier in modifiers):
        raise InvalidChordError(f"{spelled!r} does not spell held modifiers")
    ordered = [modifier for modifier in CHORD_MODIFIER_ORDER if modifier in modifiers]
    return "+".join([*ordered, key])


@pure
def describe_keybindings_value_problem(value: object) -> str | None:
    """Why the value is not a valid keybindings map, or None when it is.

    The valid shape is app name, then action id, then a list of canonical
    chords (an empty list unbinds the action). The desktop's write path runs
    the same check, so the two sides refuse the same shapes.
    """
    if not isinstance(value, Mapping):
        return f"keybindings must be an object of apps, not {type(value).__name__}"
    for app_name, chords_by_action in value.items():
        if not isinstance(app_name, str) or APP_OR_ACTION_ID_RE.match(app_name) is None:
            return f"{app_name!r} is not an app name"
        if not isinstance(chords_by_action, Mapping):
            return f"the {app_name} entry must be an object of actions"
        for action_id, chords in chords_by_action.items():
            if not isinstance(action_id, str) or APP_OR_ACTION_ID_RE.match(action_id) is None:
                return f"{action_id!r} is not an action id"
            if isinstance(chords, str) or not isinstance(chords, (list, tuple)):
                return f"{app_name}.{action_id} must assign a list of chords"
            for chord in chords:
                if not isinstance(chord, str) or not is_canonical_chord(chord):
                    return f"{chord!r} is not a canonical chord"
    return None


@pure
def parse_keybindings_value(raw: object) -> dict[str, dict[str, list[str]]]:
    """One layer's keybindings map, tolerantly: anything malformed reads as no assignments.

    Reads must tolerate a shape a newer writer stored; only whole-map
    malformation drops the layer (a single bad chord string still names a
    deliberate assignment, so it is kept as-is and simply never matches a
    keystroke).
    """
    if not isinstance(raw, Mapping):
        return {}
    parsed: dict[str, dict[str, list[str]]] = {}
    for app_name, chords_by_action in raw.items():
        if not isinstance(app_name, str) or not isinstance(chords_by_action, Mapping):
            continue
        actions: dict[str, list[str]] = {}
        for action_id, chords in chords_by_action.items():
            if not isinstance(action_id, str) or isinstance(chords, str) or not isinstance(chords, Sequence):
                continue
            actions[action_id] = [chord for chord in chords if isinstance(chord, str)]
        parsed[app_name] = actions
    return parsed


def _layer_assignment(layer_value_by_key: Mapping[str, object], app_name: str, action_id: str) -> list[str] | None:
    assignments = parse_keybindings_value(layer_value_by_key.get(KEYBINDINGS_SETTING_KEY))
    by_action = assignments.get(app_name)
    if by_action is None or action_id not in by_action:
        return None
    return by_action[action_id]


def resolve_app_keybindings(
    app_name: str,
    # The app's declared actions with their default chords; unknown actions in a
    # stored layer are ignored, so a newer manifest's assignments never leak in.
    default_chords_by_action: Mapping[str, Sequence[str]],
    *,
    snapshot: WorkspaceProfileSnapshot | None,
    identity: RequestIdentity,
    workspace_root: Path,
) -> dict[str, list[str]]:
    """The chords of each declared action for this request, by the spec's per-assignment order."""
    user_id = resolve_request_user_id(identity, snapshot)
    override_by_key: Mapping[str, object] = (
        load_user_override_values(workspace_root, user_id) if user_id is not None else {}
    )
    unsent_by_key: Mapping[str, object] = load_unsent_change_values(workspace_root) if identity.owner else {}
    snapshot_by_key: Mapping[str, object] = (
        snapshot.value_by_key if identity.owner and snapshot is not None else {}
    )
    resolved: dict[str, list[str]] = {}
    for action_id, default_chords in default_chords_by_action.items():
        layered = _layer_assignment(override_by_key, app_name, action_id)
        if layered is None:
            layered = _layer_assignment(unsent_by_key, app_name, action_id)
        if layered is None:
            layered = _layer_assignment(snapshot_by_key, app_name, action_id)
        resolved[action_id] = list(default_chords) if layered is None else layered
    return resolved


@pure
def with_keybinding_assignment(
    value_by_key: Mapping[str, object], app_name: str, action_id: str, chords: Sequence[str]
) -> dict[str, object]:
    """A copy of one layer's flat map with the given assignment set under the keybindings key."""
    assignments = parse_keybindings_value(value_by_key.get(KEYBINDINGS_SETTING_KEY))
    by_action = dict(assignments.get(app_name, {}))
    by_action[action_id] = list(chords)
    updated_assignments: dict[str, object] = {**assignments, app_name: by_action}
    return {**value_by_key, KEYBINDINGS_SETTING_KEY: updated_assignments}


@pure
def without_keybinding_assignment(
    value_by_key: Mapping[str, object], app_name: str, action_id: str
) -> dict[str, object]:
    """A copy of one layer's flat map with the given assignment removed (empty levels dropped)."""
    assignments = parse_keybindings_value(value_by_key.get(KEYBINDINGS_SETTING_KEY))
    by_action = {action: chords for action, chords in assignments.get(app_name, {}).items() if action != action_id}
    remaining = {app: actions for app, actions in assignments.items() if app != app_name}
    if by_action:
        remaining[app_name] = by_action
    updated: dict[str, object] = {key: value for key, value in value_by_key.items() if key != KEYBINDINGS_SETTING_KEY}
    if remaining:
        updated[KEYBINDINGS_SETTING_KEY] = remaining
    return updated


@pure
def keybinding_assignment(value_by_key: Mapping[str, object], app_name: str, action_id: str) -> list[str] | None:
    """One layer's assignment for the action, or None when the layer holds none."""
    return _layer_assignment(value_by_key, app_name, action_id)
