"""Tests that every hook script this repo wires up actually exists.

The `agent_*` scripts here are reached three different ways -- `.claude/settings.json`
for claude, `.codex/hooks.json` for codex, and `.pi/extensions/policy_guards.ts`, which
spawns the `*_check.py` checkers directly, for pi. All three name the scripts as plain
strings, and a name that no longer resolves fails *silently*: the hook never runs and
the guard is simply gone. So a rename that misses one of these files, or a guard added
to one harness and forgotten on another, is invisible without this test.

See tool-call-policies.md; "Keeping the three in step" is the invariant asserted here.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS_DIR = _REPO_ROOT / "system" / "scripts"

_CLAUDE_SETTINGS = _REPO_ROOT / ".claude" / "settings.json"
_CODEX_HOOKS = _REPO_ROOT / ".codex" / "hooks.json"
_PI_POLICY_GUARDS = _REPO_ROOT / ".pi" / "extensions" / "policy_guards.ts"

# A `system/scripts/<name>` path inside a hook command, however the harness spells the
# work-dir prefix (`${MNGR_AGENT_WORK_DIR:-.}/`, `"$MNGR_AGENT_WORK_DIR/`, `./`).
_SCRIPT_REF_RE = re.compile(r"system/scripts/([A-Za-z0-9_]+\.(?:sh|py))")
# The checker paths policy_guards.ts builds, as `join(SCRIPTS, "<name>")`.
_PI_CHECKER_RE = re.compile(r'join\(SCRIPTS,\s*"([^"]+)"\)')


def _hook_commands(config: dict[str, Any], event: str) -> list[str]:
    """Every hook command string registered for ``event`` in a claude-shaped config."""
    commands: list[str] = []
    for matcher in config.get("hooks", {}).get(event, []):
        for hook in matcher.get("hooks", []):
            command = hook.get("command")
            if isinstance(command, str):
                commands.append(command)
    return commands


def _referenced_scripts(commands: list[str]) -> set[str]:
    return {name for command in commands for name in _SCRIPT_REF_RE.findall(command)}


def _all_events(config: dict[str, Any]) -> list[str]:
    return list(config.get("hooks", {}))


def test_every_wired_hook_script_exists() -> None:
    """A hook naming a script that is not here would never run, and never say so."""
    for config_path in (_CLAUDE_SETTINGS, _CODEX_HOOKS):
        config = json.loads(config_path.read_text())
        commands = [
            c for event in _all_events(config) for c in _hook_commands(config, event)
        ]
        names = _referenced_scripts(commands)
        assert names, f"{config_path.name} references no system/scripts hook at all"
        for name in sorted(names):
            assert (_SCRIPTS_DIR / name).is_file(), (
                f"{config_path.name} wires a missing script: {name}"
            )


def test_claude_and_codex_run_the_same_pretooluse_guards() -> None:
    """The safety/workflow guards are cross-harness by design, so the two configs must
    name the same set. (Only PreToolUse: claude also wires SessionStart and Stop hooks
    that have no codex counterpart -- see tool-call-policies.md, category C.)"""
    claude = json.loads(_CLAUDE_SETTINGS.read_text())
    codex = json.loads(_CODEX_HOOKS.read_text())
    claude_guards = _referenced_scripts(_hook_commands(claude, "PreToolUse"))
    codex_guards = _referenced_scripts(_hook_commands(codex, "PreToolUse"))
    assert claude_guards == codex_guards


def test_pi_spawns_checkers_that_exist() -> None:
    """pi reaches the same checkers without a hook wrapper, so its paths need the same
    check. Asserting the count guards against the regex silently matching nothing if
    the extension stops building its paths with `join(SCRIPTS, ...)`."""
    checkers = _PI_CHECKER_RE.findall(_PI_POLICY_GUARDS.read_text())
    assert len(checkers) == 2, (
        f"expected 2 checkers in policy_guards.ts, found {checkers}"
    )
    for name in checkers:
        assert (_SCRIPTS_DIR / name).is_file(), (
            f"policy_guards.ts spawns a missing checker: {name}"
        )
