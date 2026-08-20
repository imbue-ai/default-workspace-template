"""Pin the workspace terminal's tmux lockdown to a whitelist.

``system/apps/terminal/terminal_tmux.conf`` empties all four of tmux's key
tables and puts back only the keys a scroll-and-type terminal needs. That shape
is the whole point: a blacklist (naming the unwanted keys) silently stops
covering whatever tmux adds next, and the first attempt at this did exactly
that -- it rebound the 95 printable characters and left every control and meta
key live, so Ctrl-S still opened "(search down)" and Ctrl-R "(search up)".

These tests fail if anyone reintroduces a binding outside the approved set, or
if the committed config drifts from what gen_lockdown.py produces.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

_TERMINAL_DIR = Path(__file__).parents[1] / "system" / "apps" / "terminal"
_CONF_PATH = _TERMINAL_DIR / "terminal_tmux.conf"
_GENERATOR_PATH = _TERMINAL_DIR / "gen_lockdown.py"

# Every key table tmux has. Each must be emptied before anything is put back.
_TABLES = ("prefix", "root", "copy-mode", "copy-mode-vi")

# The only non-printable keys allowed back. Printable characters (0x20-0x7e)
# are allowed separately: they exit copy-mode and type themselves.
_ALLOWED_ROOT_KEYS = frozenset(
    {"MouseDown1Pane", "WheelUpPane", "MouseDrag1Pane", "DoubleClick1Pane", "TripleClick1Pane"}
)
_ALLOWED_COPY_MODE_KEYS = frozenset(
    {
        "WheelUpPane",
        "WheelDownPane",
        "Up",
        "Down",
        "PPage",
        "NPage",
        "MouseDown1Pane",
        "MouseDrag1Pane",
        "MouseDragEnd1Pane",
        "DoubleClick1Pane",
        "TripleClick1Pane",
        "Escape",
        "Any",
    }
)
_PRINTABLE = frozenset(chr(i) for i in range(0x20, 0x7F))

_KEY = r'(?P<key>"(?:[^"\\]|\\.)*"|\S+)'
_BIND_RE = re.compile(r"^bind\s+(?:-n|-T\s+(?P<table>\S+))\s+" + _KEY)


def _config_lines() -> list[str]:
    """Config lines with comments and blanks dropped."""
    return [
        line for line in _CONF_PATH.read_text().splitlines() if line.strip() and not line.lstrip().startswith("#")
    ]


def _binds() -> list[tuple[str, str]]:
    """Every bind in the config as (table, key). `-n` is tmux's alias for the root table."""
    out: list[tuple[str, str]] = []
    for line in _config_lines():
        match = _BIND_RE.match(line)
        if match is not None:
            out.append((match.group("table") or "root", match.group("key")))
    return out


def test_every_key_table_is_emptied_before_anything_is_rebound() -> None:
    """Each table gets `unbind -a`. This is what makes the config version-proof.

    `unbind -a` removes whatever bindings the running tmux shipped, however many
    and whatever they are named, so a tmux upgrade that adds copy-mode keys
    cannot reopen the hole.
    """
    lines = _config_lines()
    for table in _TABLES:
        assert f"unbind -a -T {table}" in lines, f"{table} table is not emptied"


def test_the_prefix_key_itself_is_removed() -> None:
    """Belt and braces alongside the emptied prefix table: no key reaches it."""
    lines = _config_lines()
    assert "set -g prefix  None" in lines or "set -g prefix None" in lines
    assert "set -g prefix2 None" in lines


def test_no_binding_can_open_a_prompt_or_a_menu() -> None:
    """Every stuck-terminal report traced back to one of these two commands.

    `(jump forward)`, `(search down)`, `(search up)`, `(repeat)` and goto-line are
    all `command-prompt`; the pane and status right-click menus are `display-menu`.
    Both swallow keystrokes until dismissed.
    """
    for line in _config_lines():
        assert "command-prompt" not in line, f"a prompt binding came back: {line}"
        assert "display-menu" not in line, f"a menu binding came back: {line}"


def test_no_binding_leads_to_another_mode() -> None:
    """copy-mode is unavoidable (it is tmux's only scrollback viewer). The rest are not."""
    banned = ("clock-mode", "choose-tree", "choose-client", "choose-buffer", "customize-mode", "switch-client -T")
    for line in _config_lines():
        for command in banned:
            assert command not in line, f"{command} became reachable again: {line}"


def test_only_approved_keys_are_rebound() -> None:
    """The whitelist itself. A new binding outside these sets fails here."""
    for table, key in _binds():
        assert table in _TABLES, f"bind into an unknown table {table!r}"
        assert table != "prefix", f"the prefix table must stay empty, got {key!r}"
        if table == "root":
            assert key in _ALLOWED_ROOT_KEYS, f"unapproved root binding: {key!r}"
            continue
        # Printable keys are quoted in the config ("t"), except ';' which tmux
        # requires backslash-escaped and unquoted because its parser splits on a
        # bare semicolon even inside quotes.
        literal = key[1:-1] if key.startswith('"') and key.endswith('"') else key
        if literal == "\\;":
            literal = ";"
        literal = literal.replace('\\"', '"').replace("\\$", "$").replace("\\#", "#").replace("\\\\", "\\")
        if literal in _PRINTABLE:
            continue
        assert key in _ALLOWED_COPY_MODE_KEYS, f"unapproved {table} binding: {key!r}"


def test_every_printable_character_is_rebound_in_both_copy_mode_tables() -> None:
    """The reason typing after a scroll works at all.

    No tmux format exposes the key that triggered a binding, so "leave the mode
    AND type the character" needs one binding per character. A missing one is a
    character that silently disappears.
    """
    for table in ("copy-mode", "copy-mode-vi"):
        bound = set()
        for line in _config_lines():
            match = re.match(rf'^bind -T {re.escape(table)}\s+("(?:[^"\\\\]|\\\\.)*"|\S+)\s+\{{ send -X cancel ; send-keys -l (.+) \}}$', line)
            if match is not None:
                bound.add(match.group(1))
        missing = []
        for char in sorted(_PRINTABLE):
            token = "\\;" if char == ";" else '"' + char.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("#", "\\#") + '"'
            if token not in bound:
                missing.append(char)
        assert not missing, f"{table} does not pass through: {missing!r}"


def test_the_committed_config_matches_its_generator() -> None:
    """Guards against hand-editing the 287 generated lines instead of the generator."""
    generated = subprocess.run(
        [sys.executable, str(_GENERATOR_PATH)], capture_output=True, text=True, check=True
    ).stdout
    assert _CONF_PATH.read_text().endswith(generated), (
        "terminal_tmux.conf does not end with gen_lockdown.py's output; "
        "regenerate it instead of editing the block by hand"
    )
