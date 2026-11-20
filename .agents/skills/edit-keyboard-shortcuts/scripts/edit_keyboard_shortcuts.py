"""List, check, save and reset the workspace's keyboard shortcuts, with every refusal, clash and warning computed.

The Keyboard shortcuts screen decides three things about a shortcut before it saves one: whether it may be saved
at all, whether another action already uses it, and where it cannot work (a browser keeps it, an operating system
keeps it, or the key does not exist on that platform). This script decides the same three from the same sources,
so an agent changing a shortcut reports them the same way every time instead of recalling them. It is stricter
than the screen in one place: a key an operating system keeps from every app (Command-Space, Alt+Tab) is refused
here, on every platform, where the screen saves it with a warning. Its sources:

- the shell's catalog (``GET /api/keybindings/catalog``) for the actions, their scopes and the keys each has now;
- the desktop's ``workspace_profile_contract.json``, vendored with the workspace, for the chords no binding may
  claim and, per platform, the ones a browser keeps, the ones the OS keeps and the ones the desktop app takes
  from the OS;
- the account's profile snapshot for Imbue Studio's own shortcuts the user changed.

A check is made for every platform and for both the desktop app and a browser, since the workspace cannot know
which one the user is on.

Usage:
    python3 .agents/skills/edit-keyboard-shortcuts/scripts/edit_keyboard_shortcuts.py list
    python3 .agents/skills/edit-keyboard-shortcuts/scripts/edit_keyboard_shortcuts.py check APP ACTION 'CHORD'...
        [--add] [--none] [--everywhere] [--take]
    python3 .agents/skills/edit-keyboard-shortcuts/scripts/edit_keyboard_shortcuts.py save APP ACTION 'CHORD'...
        [--add] [--none] [--everywhere] [--take]
    python3 .agents/skills/edit-keyboard-shortcuts/scripts/edit_keyboard_shortcuts.py reset APP ACTION [--take]

Exit codes:
    0  -- done (``save``, ``reset``) or it would be saved (``check``); warnings may still be printed
    1  -- it could not run, and nothing changed: the command was not understood, the shell did not answer, or
          the contract file is missing or unreadable
    2  -- refused: the spelling is wrong, the action is unknown, or the chord may not be saved; nothing changed
    3  -- a clash: another action has the key, and the user has not agreed to take it (``--take``); nothing changed
    4  -- something changed, but the step did not finish or could not be confirmed; the output says what changed

Environment:
    MINDS_WORKSPACE_SERVER_URL  The shell's address (default http://127.0.0.1:8000).

It imports only the standard library, so it runs under the system ``python3`` with no venv.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

EXIT_OK: Final[int] = 0
EXIT_COULD_NOT_RUN: Final[int] = 1
EXIT_REFUSED: Final[int] = 2
EXIT_CLASH: Final[int] = 3
EXIT_PARTLY_DONE: Final[int] = 4

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[4]
_CONTRACT_PATH: Final[Path] = (
    _REPO_ROOT
    / "system"
    / "vendor"
    / "mngr-assets"
    / "apps"
    / "minds"
    / "imbue"
    / "minds"
    / "desktop_client"
    / "workspace_profile_contract.json"
)
_DEFAULT_SHELL_URL: Final[str] = "http://127.0.0.1:8000"
_READ_TIMEOUT_SECONDS: Final[float] = 15.0
# An "everywhere" save waits on the desktop app's answer before the shell answers.
_SAVE_TIMEOUT_SECONDS: Final[float] = 60.0

PLATFORMS: Final[tuple[str, ...]] = ("mac", "windows", "linux")
_OS_NAMES: Final[Mapping[str, str]] = {
    "mac": "macOS",
    "windows": "Windows",
    "linux": "Linux",
}

# The most chords one action may hold (``MAX_CHORDS_PER_ACTION`` in ``workspace_profile.keybindings``).
MAX_CHORDS_PER_ACTION: Final[int] = 16

# Spellings a person writes that the grammar stores another way (``_TOKEN_ALIASES`` in
# ``workspace_profile.keybindings``, which the save route applies too).
_TOKEN_ALIASES: Final[Mapping[str, str]] = {
    "cmd": "meta",
    "esc": "escape",
    "return": "enter",
    "spacebar": "space",
}

# What each printable key of the US layout types with Shift held (``US_LAYOUT_CHARACTERS`` in ``workspace_ui``'s
# ``keybindings.ts``). A keystroke is spelled with the character it types, so Shift with ``]`` is ``shift+}``.
_SHIFTED_CHARACTERS: Final[Mapping[str, str]] = {
    "`": "~",
    "1": "!",
    "2": "@",
    "3": "#",
    "4": "$",
    "5": "%",
    "6": "^",
    "7": "&",
    "8": "*",
    "9": "(",
    "0": ")",
    "-": "_",
    "=": "plus",
    "[": "{",
    "]": "}",
    "\\": "|",
    ";": ":",
    "'": '"',
    ",": "<",
    ".": ">",
    "/": "?",
}
# The shifted characters no key types without Shift. ``*`` and ``plus`` are left out: the number pad types both.
_CHARACTERS_NEEDING_SHIFT: Final[frozenset[str]] = frozenset(
    _SHIFTED_CHARACTERS.values()
) - {"*", "plus"}

# The app Imbue Studio's own shortcuts are stored under in the account, and their defaults
# (``apps/minds/frontend/src/models/appShortcuts.ts`` in the mngr repo). The desktop app runs them before any
# page sees the key, and the shell's catalog does not list them.
IMBUE_STUDIO_APP: Final[str] = "imbue-studio"
IMBUE_STUDIO_DEFAULT_SHORTCUTS: Final[Mapping[str, tuple[str, tuple[str, ...]]]] = {
    "new-window": ("New window", ("meta+n",)),
    "close-window": ("Close window", ("meta+w",)),
    "quit": ("Quit", ("meta+q",)),
    "zoom-in": ("Zoom in", ("meta+=",)),
    "zoom-out": ("Zoom out", ("meta+-",)),
    "zoom-reset": ("Reset zoom", ("meta+0",)),
}

# How each token is written on a Mac and elsewhere (``LABELS_BY_TOKEN`` in ``workspace_ui``'s ``keybindings.ts``).
_APPLE_KEYCAPS: Final[Mapping[str, str]] = {
    "ctrl": "⌃",
    "alt": "⌥",
    "shift": "⇧",
    "meta": "⌘",
    "enter": "↩",
    "tab": "⇥",
    "backspace": "⌫",
    "delete": "⌦",
}
_OTHER_KEYCAPS: Final[Mapping[str, str]] = {
    "ctrl": "Ctrl",
    "alt": "Alt",
    "shift": "Shift",
    "meta": "Ctrl",
    "enter": "Enter",
    "tab": "Tab",
    "backspace": "Backspace",
    "delete": "Del",
}
_SHARED_KEYCAPS: Final[Mapping[str, str]] = {
    "escape": "Esc",
    "space": "Space",
    "insert": "Ins",
    "home": "Home",
    "end": "End",
    "pageup": "PgUp",
    "pagedown": "PgDn",
    "arrowup": "↑",
    "arrowdown": "↓",
    "arrowleft": "←",
    "arrowright": "→",
    "plus": "+",
}

# What to tell the user for each way an "everywhere" save can fall short of the account
# (``KeybindingsDeliveryStatus`` in the shell's ``keybindings_delivery``).
_DELIVERED: Final[str] = "DELIVERED"
_UNDELIVERED_ADVICE: Final[Mapping[str, str]] = {
    "PERMISSION_PENDING": "Imbue Studio is showing the user a request to approve; it reaches the account once they do",
    "PERMISSION_REQUIRED": "the user has to allow this workspace to change their account's shortcuts in Imbue Studio",
    "UNAVAILABLE": "this workspace has no connection to the account",
    "UNREACHABLE": "the account could not be reached just now",
    "NOTHING_TO_DELIVER": "there was nothing to send",
}


class CouldNotRunError(Exception):
    """The step could not be made at all: the shell or the contract file was out of reach. Nothing changed."""


class RefusedError(Exception):
    """The request names something that cannot be saved: a bad spelling, an unknown action, a refused chord."""


class UsageError(Exception):
    """The command line asks for something the command cannot mean."""


class PartlyDoneError(Exception):
    """Something was already changed when the step failed or could not be confirmed."""

    def __init__(self, problem: str, done_lines: Sequence[str]) -> None:
        super().__init__(problem)
        self.problem = problem
        self.done_lines = tuple(done_lines)


@dataclass(frozen=True)
class Contract:
    """The chord vocabulary and the per-platform lists, as the desktop's contract file publishes them."""

    modifier_order: tuple[str, ...]
    named_keys: frozenset[str]
    refused_chords: frozenset[str]
    browser_reserved_by_platform: Mapping[str, frozenset[str]]
    os_kept_reason_by_platform: Mapping[str, Mapping[str, str]]
    host_acted_reason_by_platform: Mapping[str, Mapping[str, str]]
    snapshot_relative_path: str


@dataclass(frozen=True)
class Action:
    """One action of the catalog, with the app it belongs to."""

    app: str
    app_display_name: str
    id: str
    label: str
    scope: str
    chords: tuple[str, ...]
    default_chords: tuple[str, ...]
    # What a reset would leave: the keys without this workspace's own choice.
    reset_chords: tuple[str, ...]
    source: str


@dataclass(frozen=True)
class Catalog:
    """The shell's catalog: every action, and whether a save can reach the account."""

    actions: tuple[Action, ...]
    can_save_everywhere: bool


@dataclass(frozen=True)
class Clash:
    """A chord another action of the catalog holds now."""

    chord: str
    other: Action


@dataclass(frozen=True)
class Report:
    """Everything decided about giving an action a list of chords."""

    action: Action
    chords: tuple[str, ...]
    # The chords of the list the action does not hold yet: the only ones checked for clashes and warnings.
    brought: tuple[str, ...]
    refusals: tuple[str, ...]
    clashes: tuple[Clash, ...]
    warnings: tuple[str, ...]


def parse_contract(raw: Mapping[str, Any]) -> Contract:
    """The contract file's lists; a missing or misshapen one raises ``CouldNotRunError`` rather than read as empty."""
    try:
        return Contract(
            modifier_order=tuple(str(token) for token in raw["chord_modifier_order"]),
            named_keys=frozenset(str(key) for key in raw["chord_named_keys"]),
            refused_chords=frozenset(str(chord) for chord in raw["refused_chords"]),
            browser_reserved_by_platform={
                platform: frozenset(
                    str(chord) for chord in raw["browser_reserved_chords"][platform]
                )
                for platform in PLATFORMS
            },
            os_kept_reason_by_platform={
                platform: {
                    str(entry["chord"]): str(entry["reason"])
                    for entry in raw["os_kept_chords"][platform]
                }
                for platform in PLATFORMS
            },
            host_acted_reason_by_platform={
                platform: {
                    str(entry["chord"]): str(entry["reason"])
                    for entry in raw["host_acted_chords"][platform]
                }
                for platform in PLATFORMS
            },
            snapshot_relative_path=str(raw["snapshot_relative_path"]),
        )
    except (KeyError, TypeError) as error:
        raise CouldNotRunError(
            f"the shortcut contract file does not have the expected shape ({error!r})"
        ) from error


def load_contract(path: Path = _CONTRACT_PATH) -> Contract:
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise CouldNotRunError(
            f"the shortcut contract file could not be read at {path} ({error})"
        ) from error
    if not isinstance(raw, Mapping):
        raise CouldNotRunError(
            f"the shortcut contract file at {path} is not a JSON object"
        )
    return parse_contract(raw)


def normalize_chord(spelling: str, contract: Contract) -> str:
    """The canonical chord a spelling names: lowercase tokens, the modifiers in the grammar's order, then one key.

    A keystroke is spelled with the character it types, so Shift with a digit or a punctuation key is rewritten to
    the shifted character (``shift+meta+]`` is ``shift+meta+}``), and a character only Shift types gains ``shift``.
    Raises ``RefusedError`` for anything the grammar has no chord for.
    """
    tokens = [
        _TOKEN_ALIASES.get(token, token)
        for token in spelling.strip().lower().split("+")
    ]
    if not spelling.strip() or any(token == "" for token in tokens):
        raise RefusedError(
            f"{spelling!r} is not a shortcut: write tokens joined with '+', and the plus key itself as 'plus'"
        )
    modifiers, key = tokens[:-1], tokens[-1]
    for modifier in modifiers:
        if modifier not in contract.modifier_order:
            raise RefusedError(
                f"{spelling!r}: {modifier!r} is not a modifier (the modifiers are {', '.join(contract.modifier_order)})"
            )
    if len(set(modifiers)) != len(modifiers):
        raise RefusedError(f"{spelling!r} names a modifier twice")
    is_single_character = len(key) == 1 and not key.isspace() and key != "+"
    if key in contract.modifier_order:
        raise RefusedError(
            f"{spelling!r} ends in a modifier; a shortcut ends in one key"
        )
    if key not in contract.named_keys and not is_single_character:
        raise RefusedError(
            f"{spelling!r}: {key!r} is not a key (a single character, or one of {', '.join(sorted(contract.named_keys))})"
        )
    if "shift" in modifiers:
        key = _SHIFTED_CHARACTERS.get(key, key)
    elif key in _CHARACTERS_NEEDING_SHIFT:
        modifiers = [*modifiers, "shift"]
    ordered = [
        modifier for modifier in contract.modifier_order if modifier in modifiers
    ]
    return "+".join([*ordered, key])


def is_workspace_wide_chord(chord: str) -> bool:
    """Whether the chord may fire anywhere in the workspace: it holds a modifier other than Shift, or is a function
    key, so typing never triggers it (``is_workspace_wide_chord`` in ``workspace_profile.keybindings``)."""
    tokens = chord.split("+")
    key = tokens[-1]
    is_function_key = (
        key.startswith("f") and key[1:].isdigit() and 1 <= int(key[1:]) <= 12
    )
    return is_function_key or any(modifier != "shift" for modifier in tokens[:-1])


def holds_mac_control(chord: str) -> bool:
    return "ctrl" in chord.split("+")[:-1]


def chord_label(chord: str, is_apple: bool) -> str:
    """The chord as a menu writes it (``chordDisplayLabel`` in ``workspace_ui``'s ``keybindings.ts``): Apple's
    symbols run together in the grammar's order, or names joined with '+' and the primary modifier first."""
    tokens = chord.split("+")
    if not is_apple:
        modifiers = tokens[:-1]
        tokens = [
            *[token for token in modifiers if token == "meta"],
            *[token for token in modifiers if token != "meta"],
            tokens[-1],
        ]
    platform_keycaps = _APPLE_KEYCAPS if is_apple else _OTHER_KEYCAPS
    keycaps = [
        platform_keycaps.get(token) or _SHARED_KEYCAPS.get(token) or token.upper()
        for token in tokens
    ]
    return "".join(keycaps) if is_apple else "+".join(keycaps)


def chord_label_on(chord: str, platform: str) -> str:
    """The chord as the platform shows it; a chord holding the Mac's Control key is a Mac keystroke everywhere."""
    return chord_label(chord, is_apple=platform == "mac" or holds_mac_control(chord))


def chord_in_words(chord: str) -> str:
    """The chord for a sentence that is about no one platform: both spellings, unless they are the same."""
    apple, other = chord_label(chord, is_apple=True), chord_label(chord, is_apple=False)
    if holds_mac_control(chord):
        return f"{apple} (a Mac key)"
    if apple == other:
        return apple
    return f"{apple} on a Mac / {other} on Windows and Linux"


def parse_catalog(raw: Any) -> Catalog:
    """The shell's catalog answer; one without the expected fields raises ``CouldNotRunError``."""
    try:
        actions = tuple(
            Action(
                app=str(app["app"]),
                app_display_name=str(app["display_name"]),
                id=str(action["id"]),
                label=str(action["label"]),
                scope=str(action["scope"]),
                chords=tuple(str(chord) for chord in action["chords"]),
                default_chords=tuple(str(chord) for chord in action["default_chords"]),
                reset_chords=tuple(str(chord) for chord in action["reset_chords"]),
                source=str(action["source"]),
            )
            for app in raw["apps"]
            for action in app["actions"]
        )
        return Catalog(
            actions=actions, can_save_everywhere=bool(raw["can_save_everywhere"])
        )
    except (KeyError, TypeError) as error:
        raise CouldNotRunError(
            f"the workspace's shortcut catalog does not have the expected shape ({error!r})"
        ) from error


def find_action(actions: Sequence[Action], app: str, action_id: str) -> Action:
    for action in actions:
        if action.app == app and action.id == action_id:
            return action
    known = ", ".join(f"{action.app} {action.id}" for action in actions)
    raise RefusedError(
        f"there is no action {action_id!r} in app {app!r}; the catalog has: {known}"
    )


def clash_candidates(actions: Sequence[Action], own: Action) -> tuple[Action, ...]:
    """The actions a chord of ``own`` could collide with (``clashCandidates`` in the Keyboard shortcuts screen):
    the other actions of its app; every action of every app for a workspace-wide action, which wins inside any
    app's page; and, for an app's own action, the workspace-wide actions of every other app."""
    is_workspace_wide = own.scope == "workspace"
    return tuple(
        other
        for other in actions
        if not (other.app == own.app and other.id == own.id)
        and (other.app == own.app or is_workspace_wide or other.scope == "workspace")
    )


def find_clashes(
    actions: Sequence[Action], own: Action, chords: Sequence[str]
) -> tuple[Clash, ...]:
    """Every candidate holding one of ``chords`` now, once per chord. A chord clashes wherever it is held: the
    workspace cannot know which platform the user is on, so none is left out as unable to arrive."""
    candidates = clash_candidates(actions, own)
    return tuple(
        Clash(chord=chord, other=other)
        for chord in chords
        for other in candidates
        if chord in other.chords
    )


def os_kept_uses(chord: str, contract: Contract) -> tuple[str, ...]:
    """What each operating system that keeps the chord from every app uses it for, as "macOS uses ⌘Space for
    Spotlight"; empty for a chord no OS keeps."""
    return tuple(
        f"{_OS_NAMES[platform]} uses {chord_label_on(chord, platform)} for {reason}"
        for platform in PLATFORMS
        for reason in [contract.os_kept_reason_by_platform[platform].get(chord)]
        if reason is not None
    )


def find_refusals(
    own: Action, chords: Sequence[str], brought: Sequence[str], contract: Contract
) -> tuple[str, ...]:
    """Why the list may not be saved. The rules the save route enforces hold for every chord of the list; a chord
    an operating system keeps is refused only when this step brings it, so an action that already has one (set
    on the Keyboard shortcuts screen, which saves it with a warning) can still be given other keys."""
    refusals = []
    if len(chords) > MAX_CHORDS_PER_ACTION:
        refusals.append(
            f"An action takes at most {MAX_CHORDS_PER_ACTION} shortcuts; this would give it {len(chords)}."
        )
    for chord in chords:
        if chord in contract.refused_chords:
            refusals.append(
                f"{chord_in_words(chord)} is one an operating system keeps entirely for itself, so no action may "
                "have it on any platform."
            )
        elif own.scope == "workspace" and not is_workspace_wide_chord(chord):
            refusals.append(
                f"{chord_in_words(chord)} would fire while typing: this action runs anywhere in the workspace, so its "
                "shortcut needs Command/Ctrl, Option/Alt or Control, or to be a function key."
            )
        elif chord in brought and os_kept_uses(chord, contract):
            refusals.append(
                f"{chord_in_words(chord)} is kept by an operating system: {'; '.join(os_kept_uses(chord, contract))}. "
                "It would never reach Imbue Studio there, and a shortcut is saved for every platform, so it is "
                "refused. It can still be set by hand on the Keyboard shortcuts screen, which only warns."
            )
    return tuple(refusals)


def imbue_studio_chords(
    snapshot: Mapping[str, Any] | None,
) -> dict[str, tuple[str, tuple[str, ...]]]:
    """Imbue Studio's own shortcuts as ``{action id: (label, chords)}``: the defaults, with what the account's
    snapshot says the user assigned in their place."""
    assigned: Mapping[str, Any] = {}
    if snapshot is not None:
        keybindings = snapshot.get("keybindings")
        if isinstance(keybindings, Mapping) and isinstance(
            keybindings.get(IMBUE_STUDIO_APP), Mapping
        ):
            assigned = keybindings[IMBUE_STUDIO_APP]
    resolved = {}
    for action_id, (label, default_chords) in IMBUE_STUDIO_DEFAULT_SHORTCUTS.items():
        stored = assigned.get(action_id)
        is_list_of_chords = isinstance(stored, list) and all(
            isinstance(chord, str) for chord in stored
        )
        resolved[action_id] = (
            label,
            tuple(stored) if is_list_of_chords else default_chords,
        )
    return resolved


def find_warnings(
    chords: Sequence[str], contract: Contract, snapshot: Mapping[str, Any] | None
) -> tuple[str, ...]:
    """What to tell the user about where each chord cannot work, none of it blocking: one line per finding. A
    chord an operating system keeps is not among them: it is refused (``find_refusals``)."""
    warnings = []
    studio_shortcuts = imbue_studio_chords(snapshot)
    for chord in chords:
        if holds_mac_control(chord):
            warnings.append(
                f"{chord_label(chord, is_apple=True)} holds the Mac's Control key, which Windows and Linux keyboards "
                "have no partner for: it works on a Mac only, and is hidden on Windows and Linux."
            )
        for studio_label, studio_chords in studio_shortcuts.values():
            if chord in studio_chords:
                warnings.append(
                    f'Imbue Studio\'s own "{studio_label}" uses {chord_in_words(chord)}, and the desktop app runs its '
                    "own shortcuts first: in the app this key keeps doing that until it is changed in Workspace "
                    "settings > Keyboard shortcuts."
                )
        for platform in PLATFORMS:
            if platform != "mac" and holds_mac_control(chord):
                continue
            os_name = _OS_NAMES[platform]
            label = chord_label_on(chord, platform)
            if chord in contract.browser_reserved_by_platform[platform]:
                warnings.append(
                    f"On {os_name}, a browser keeps {label} for itself, so this never runs when the workspace is "
                    "open in a browser there."
                )
            acted_reason = contract.host_acted_reason_by_platform[platform].get(chord)
            if acted_reason is not None:
                warnings.append(
                    f"On {os_name}, {label} is normally used for {acted_reason}; in the Imbue Studio app it runs "
                    "this action instead."
                )
    return tuple(warnings)


def build_report(
    actions: Sequence[Action],
    own: Action,
    chords: Sequence[str],
    contract: Contract,
    snapshot: Mapping[str, Any] | None,
    is_everywhere: bool = False,
) -> Report:
    """Decide everything about giving ``own`` exactly ``chords``. Keys the action already has are not judged
    again: only the ones this step brings are checked for clashes and warnings. For a save to the account, what
    the action already has is what the account gives it: a key only this workspace's own choice holds is brought
    to every other workspace by the save."""
    is_held_here_only = is_everywhere and own.source == "workspace"
    already_held = own.reset_chords if is_held_here_only else own.chords
    brought = tuple(chord for chord in chords if chord not in already_held)
    return Report(
        action=own,
        chords=tuple(chords),
        brought=brought,
        refusals=find_refusals(own, chords, brought, contract),
        clashes=find_clashes(actions, own, brought),
        warnings=find_warnings(brought, contract, snapshot),
    )


def describe_action(action: Action) -> str:
    reach = (
        "runs anywhere in the workspace"
        if action.scope == "workspace"
        else "runs in its own app's window"
    )
    return f'"{action.label}" ({action.app_display_name}; {reach})'


def describe_chords(chords: Sequence[str]) -> str:
    if not chords:
        return "none (the action has no shortcut)"
    return "; ".join(f"{chord_in_words(chord)} [{chord}]" for chord in chords)


def render_report(report: Report, outcome: str, is_key_taken: bool = False) -> str:
    lines = [
        f"ACTION: {describe_action(report.action)}",
        f"KEYS: {describe_chords(report.chords)}",
        f"RESULT: {outcome}",
    ]
    if report.refusals:
        lines.append("REFUSED (it cannot be saved; tell the user why):")
        lines.extend(f"  - {refusal}" for refusal in report.refusals)
    if report.clashes:
        lines.append("CLASHES (ask the user before taking a key from another action):")
        lines.extend(
            f"  - {chord_in_words(clash.chord)} is the shortcut of {describe_action(clash.other)} now."
            for clash in report.clashes
        )
    if report.warnings:
        lines.append("WARNINGS (tell the user every one of these):")
        lines.extend(f"  - {warning}" for warning in report.warnings)
    if not report.refusals and not report.clashes and not report.warnings:
        if not report.brought:
            lines.append(
                "No new key was checked: this step brings the action no key it did not already have."
            )
        else:
            opening = "Nothing" if is_key_taken else "No clash, and nothing"
            lines.append(
                f"{opening} known to keep {describe_chords(report.brought)} from working on any platform or in a "
                "browser."
            )
    return "\n".join(lines)


def chords_taken_by_action(clashes: Sequence[Clash]) -> dict[Action, tuple[str, ...]]:
    """For each action a clash names, the chords that clash with it: the only ones a take removes from it."""
    taken: dict[Action, tuple[str, ...]] = {}
    for clash in clashes:
        held = taken.get(clash.other, ())
        if clash.chord not in held:
            taken[clash.other] = (*held, clash.chord)
    return taken


class Shell:
    """The shell's keybinding routes on loopback."""

    def __init__(self, base_url: str) -> None:
        self._base_url = base_url.rstrip("/")

    def _request(
        self,
        method: str,
        path: str,
        timeout: float,
        payload: Mapping[str, Any] | None = None,
    ) -> Any:
        data = None if payload is None else json.dumps(payload).encode()
        request = urllib.request.Request(
            f"{self._base_url}{path}",
            data=data,
            headers={"Content-Type": "application/json"},
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as error:
            detail = error.read().decode(errors="replace")
            if error.code == 400:
                raise RefusedError(f"the workspace refused it: {detail}") from error
            raise CouldNotRunError(
                f"the workspace answered {error.code} to {method} {path}: {detail}"
            ) from error
        except (OSError, http.client.HTTPException, ValueError) as error:
            raise CouldNotRunError(
                f"the workspace did not answer {method} {path} ({error!r})"
            ) from error

    def catalog(self) -> Catalog:
        return parse_catalog(
            self._request("GET", "/api/keybindings/catalog", _READ_TIMEOUT_SECONDS)
        )

    def save(self, action: Action, chords: Sequence[str], is_everywhere: bool) -> Any:
        return self._request(
            "POST",
            "/api/keybindings",
            _SAVE_TIMEOUT_SECONDS,
            {
                "app": action.app,
                "action": action.id,
                "chords": list(chords),
                "scope": "account" if is_everywhere else "workspace",
            },
        )

    def reset(self, action: Action) -> Any:
        return self._request(
            "POST",
            "/api/keybindings/reset",
            _SAVE_TIMEOUT_SECONDS,
            {"app": action.app, "action": action.id},
        )


def load_snapshot(
    contract: Contract, repo_root: Path = _REPO_ROOT
) -> Mapping[str, Any] | None:
    """The account's profile snapshot, or None when the workspace has none (no account has synced here yet)."""
    try:
        raw = json.loads((repo_root / contract.snapshot_relative_path).read_text())
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, Mapping) else None


def requested_chords(
    own: Action, spellings: Sequence[str], is_added: bool, contract: Contract
) -> tuple[str, ...]:
    """The whole list the action would hold: the given chords, after the ones it has when adding."""
    given = [normalize_chord(spelling, contract) for spelling in spellings]
    combined = [*own.chords, *given] if is_added else given
    return tuple(dict.fromkeys(combined))


def take_keys(shell: Shell, clashes: Sequence[Clash]) -> list[str]:
    """Remove each clashing chord from the action that holds it, in this workspace; answers a line per action for
    the output. Raises ``PartlyDoneError`` when one fails after another went through."""
    lines: list[str] = []
    for other, taken in chords_taken_by_action(clashes).items():
        remaining = tuple(chord for chord in other.chords if chord not in taken)
        try:
            shell.save(other, remaining, is_everywhere=False)
        except (RefusedError, CouldNotRunError) as error:
            if not lines:
                raise
            raise PartlyDoneError(
                f"taking a key from {describe_action(other)} failed ({error})", lines
            ) from error
        lines.append(
            f"TOOK {describe_chords(taken)} FROM: {describe_action(other)}, which keeps: {describe_chords(remaining)}"
        )
    return lines


def run_list(shell: Shell) -> int:
    catalog = shell.catalog()
    for action in catalog.actions:
        print(f"{action.app} {action.id}: {describe_action(action)}")
        print(f"    keys now: {describe_chords(action.chords)} (from: {action.source})")
        if action.chords != action.default_chords:
            print(f"    default:  {describe_chords(action.default_chords)}")
    print(
        "Imbue Studio's own shortcuts (New window, Close window, Quit, zoom) are not listed and cannot be changed "
        "from here: they are set in Workspace settings > Keyboard shortcuts."
    )
    if not catalog.can_save_everywhere:
        print(
            "This workspace cannot save to the account: --everywhere is not possible here."
        )
    return EXIT_OK


def account_outcome(answer: Any) -> tuple[bool, str]:
    """Whether an "everywhere" save reached the account, and the line that says how it went."""
    delivery = answer.get("delivery") if isinstance(answer, Mapping) else None
    status = str(delivery.get("status")) if isinstance(delivery, Mapping) else "UNKNOWN"
    detail = str(delivery.get("detail") or "") if isinstance(delivery, Mapping) else ""
    if status == _DELIVERED:
        return True, f"ACCOUNT DELIVERY: {status}"
    advice = _UNDELIVERED_ADVICE.get(status, "it has not reached the account")
    said = f' The workspace said: "{detail}".' if detail else ""
    return False, f"ACCOUNT DELIVERY: {status}: {advice}.{said}"


def run_check_or_save(
    args: argparse.Namespace, shell: Shell, contract: Contract, is_saving: bool
) -> int:
    if args.none and (args.chords or args.add):
        raise UsageError(
            "--none leaves the action with no shortcut; it takes no keys and no --add"
        )
    if not args.chords and not args.none:
        raise UsageError(
            "name at least one key, or pass --none to leave the action with no shortcut"
        )
    catalog = shell.catalog()
    own = find_action(catalog.actions, args.app, args.action)
    chords = requested_chords(own, args.chords, args.add, contract)
    report = build_report(
        catalog.actions,
        own,
        chords,
        contract,
        load_snapshot(contract),
        args.everywhere,
    )
    if report.refusals:
        print(render_report(report, "NOT SAVED: refused."))
        return EXIT_REFUSED
    if args.everywhere and not catalog.can_save_everywhere:
        print(render_report(report, "NOT SAVED: refused."))
        print(
            "REFUSED (tell the user why):\n  - This workspace has no connection to the account, so a shortcut cannot "
            "be saved everywhere from it; it can be saved for this workspace only."
        )
        return EXIT_REFUSED
    if report.clashes and args.everywhere:
        print(render_report(report, "NOT SAVED: refused."))
        print(
            "REFUSED (tell the user why):\n  - Another action has a key (under CLASHES), and taking a key cannot be "
            "combined with saving everywhere from here. Either save for this workspace only (the same command "
            "without --everywhere, which asks before taking the key), or the user does this one on the Keyboard "
            "shortcuts screen."
        )
        return EXIT_REFUSED
    if report.clashes and not args.take:
        print(
            render_report(
                report,
                "NOT SAVED: another action has a key. Ask the user, then save with --take.",
            )
        )
        return EXIT_CLASH
    if not is_saving:
        would_take = [
            f"WOULD TAKE {describe_chords(taken)} FROM: {describe_action(other)}"
            for other, taken in chords_taken_by_action(report.clashes).items()
        ]
        print(
            render_report(
                report, "NOT SAVED: this was a check. It can be saved as it is."
            )
        )
        print("\n".join(would_take), end="\n" if would_take else "")
        return EXIT_OK

    done_lines = take_keys(shell, report.clashes)
    try:
        answer = shell.save(own, chords, args.everywhere)
    except (RefusedError, CouldNotRunError) as error:
        if not done_lines:
            raise
        raise PartlyDoneError(
            f"saving {describe_action(own)} failed ({error})", done_lines
        ) from error
    done_lines.append(f"SAVED {describe_action(own)} as: {describe_chords(chords)}")
    # The clashes are settled by now: the TOOK lines name each action a key was taken from.
    settled = Report(
        action=report.action,
        chords=report.chords,
        brought=report.brought,
        refusals=(),
        clashes=(),
        warnings=report.warnings,
    )
    is_key_taken = bool(report.clashes)
    is_delivered, delivery_line = (
        account_outcome(answer) if args.everywhere else (False, "")
    )
    if is_delivered:
        # The workspace's copy of the account follows a few seconds later, so a read-back now proves nothing.
        outcome = "SAVED to the account, for every workspace. This workspace shows it within a few seconds."
        print(render_report(settled, outcome, is_key_taken))
        print(delivery_line)
        return EXIT_OK
    try:
        saved = find_action(shell.catalog().actions, own.app, own.id)
    except (RefusedError, CouldNotRunError) as error:
        raise PartlyDoneError(
            f"the save went through, but it could not be read back ({error})",
            done_lines,
        ) from error
    if saved.chords != chords:
        raise PartlyDoneError(
            f"the workspace stored the list but does not apply it as asked: the action now has "
            f"{describe_chords(saved.chords)}. It reads some lists (an action's older default) as no choice at all; "
            "`reset` removes the stored list",
            done_lines,
        )
    outcome = (
        "SAVED in this workspace only so far: it has NOT reached the account yet (see ACCOUNT DELIVERY)."
        if args.everywhere
        else "SAVED in this workspace."
    )
    print(render_report(settled, outcome, is_key_taken))
    for line in [*done_lines[:-1], *([delivery_line] if args.everywhere else [])]:
        print(line)
    return EXIT_OK


def run_reset(args: argparse.Namespace, shell: Shell, contract: Contract) -> int:
    catalog = shell.catalog()
    own = find_action(catalog.actions, args.app, args.action)
    # With no choice of this workspace's in effect, the reset still runs: it clears a stored list the workspace
    # does not apply, and the keys stay as they are.
    had_own_choice = own.source == "workspace"
    brought = tuple(chord for chord in own.reset_chords if chord not in own.chords)
    # A reset restores what the account or the app holds, so a key an operating system keeps is not refused
    # here; the user is told about it instead.
    kept_by_an_os = tuple(
        f"{chord_in_words(chord)} is kept by an operating system: {'; '.join(os_kept_uses(chord, contract))}. "
        "It may never reach Imbue Studio there."
        for chord in brought
        if os_kept_uses(chord, contract)
    )
    report = Report(
        action=own,
        chords=own.reset_chords,
        brought=brought,
        refusals=(),
        clashes=find_clashes(catalog.actions, own, brought),
        warnings=(
            *kept_by_an_os,
            *find_warnings(brought, contract, load_snapshot(contract)),
        ),
    )
    if report.clashes and not args.take:
        outcome = "NOT RESET: a key the reset brings back is another action's now. Ask the user, then reset with --take."
        print(render_report(report, outcome))
        return EXIT_CLASH
    done_lines = take_keys(shell, report.clashes)
    try:
        shell.reset(own)
    except (RefusedError, CouldNotRunError) as error:
        if not done_lines:
            raise
        raise PartlyDoneError(
            f"resetting {describe_action(own)} failed ({error})", done_lines
        ) from error
    done_lines.append(f"RESET {describe_action(own)}")
    try:
        after = find_action(shell.catalog().actions, own.app, own.id)
    except (RefusedError, CouldNotRunError) as error:
        raise PartlyDoneError(
            f"the reset went through, but it could not be read back ({error})",
            done_lines,
        ) from error
    settled = Report(
        action=after,
        chords=after.chords,
        brought=brought,
        refusals=(),
        clashes=(),
        warnings=report.warnings,
    )
    keys_from = (
        "the app's default"
        if after.source == "default"
        else f"the {after.source}, not the app's default"
    )
    outcome = (
        f"RESET: this workspace's own choice is gone, and the action's keys now come from {keys_from}."
        if had_own_choice
        else f"NOTHING TO RESET: this workspace had no choice of its own for this action. Its keys come from {keys_from}."
    )
    print(render_report(settled, outcome, bool(report.clashes)))
    for line in done_lines[:-1]:
        print(line)
    return EXIT_OK


_COMMAND_SUMMARIES: Final[Mapping[str, str]] = {
    "list": "every action with the keys it has now",
    "check": "say what saving these keys would do, without saving",
    "save": "save these keys, unless they are refused or another action has one",
    "reset": "drop this workspace's choice for one action",
}


def build_command_parser(command: str) -> argparse.ArgumentParser:
    """The parser of one command's own arguments. Each command has a flat parser, parsed intermixed, so a flag
    may stand before, between or after the keys."""
    program = f"{Path(__file__).name} {command}"
    parser = argparse.ArgumentParser(
        prog=program, description=_COMMAND_SUMMARIES[command]
    )
    if command == "list":
        return parser
    parser.add_argument("app", help="the app's name, as `list` prints it")
    parser.add_argument("action", help="the action's id, as `list` prints it")
    if command == "reset":
        parser.add_argument(
            "--take",
            action="store_true",
            help="take a key the reset brings back from its holder",
        )
        return parser
    parser.add_argument(
        "chords", nargs="*", help="the keys, each in single quotes, e.g. 'meta+j'"
    )
    parser.add_argument(
        "--add", action="store_true", help="keep the keys the action has and add these"
    )
    parser.add_argument(
        "--none", action="store_true", help="leave the action with no shortcut at all"
    )
    parser.add_argument(
        "--everywhere",
        action="store_true",
        help="save to the user's account, for all workspaces",
    )
    parser.add_argument(
        "--take",
        action="store_true",
        help="take a clashing key from the action that has it",
    )
    return parser


def _usage() -> str:
    commands = "\n".join(
        f"  {name:<6} {summary}" for name, summary in _COMMAND_SUMMARIES.items()
    )
    return (
        f"usage: {Path(__file__).name} {{{','.join(_COMMAND_SUMMARIES)}}} ...\n\n"
        f"{(__doc__ or '').split(chr(10) + chr(10))[0]}\n\ncommands:\n{commands}\n\n"
        "Give a command --help for its own arguments."
    )


def _could_not_run(problem: str) -> int:
    print(
        f"RESULT: COULD NOT RUN: {problem}\nNothing was checked and nothing changed. Do not save by any other route; "
        "tell the user.",
        file=sys.stderr,
    )
    return EXIT_COULD_NOT_RUN


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] in ("-h", "--help"):
        print(_usage())
        return EXIT_OK
    if not arguments or arguments[0] not in _COMMAND_SUMMARIES:
        print(_usage(), file=sys.stderr)
        return _could_not_run("the command was not understood (the usage is above)")
    command = arguments[0]
    try:
        args = build_command_parser(command).parse_intermixed_args(arguments[1:])
    except SystemExit as stop:
        if stop.code in (0, None):
            return EXIT_OK
        return _could_not_run("the command was not understood (the usage is above)")
    shell = Shell(os.environ.get("MINDS_WORKSPACE_SERVER_URL", _DEFAULT_SHELL_URL))
    try:
        if command == "list":
            return run_list(shell)
        if command == "reset":
            return run_reset(args, shell, load_contract())
        return run_check_or_save(
            args, shell, load_contract(), is_saving=command == "save"
        )
    except UsageError as error:
        return _could_not_run(str(error))
    except RefusedError as error:
        print(f"RESULT: NOT SAVED: refused.\nREFUSED (tell the user why):\n  - {error}")
        return EXIT_REFUSED
    except CouldNotRunError as error:
        return _could_not_run(str(error))
    except PartlyDoneError as error:
        print(f"RESULT: PARTLY DONE: {error.problem}.")
        print(
            "ALREADY CHANGED (tell the user, and run `list` to see what each action has now):"
        )
        for line in error.done_lines:
            print(f"  - {line}")
        return EXIT_PARTLY_DONE


if __name__ == "__main__":
    sys.exit(main())
