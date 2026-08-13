#!/usr/bin/env python3
"""Install Atlas into a workspace: wire its hooks and scaffold the book.

Atlas ships as a skill only -- it does NOT pre-modify a workspace's
settings.json. This installer (safe to run on every invocation) does two things,
idempotently:

  1. Adds the Atlas hook entries to PostToolUse / UserPromptSubmit / Stop if
     missing -- never duplicating, leaving all other hooks untouched.
  2. Creates `atlas/topics/` so the book exists. The checkpoint/router/reminder
     hooks and the sweep only act once that directory is present, so wiring the
     hooks without scaffolding it would leave them no-op'ing forever.

So a workspace opts into Atlas by using it, and an uninstall is just removing the
hook entries.

Usage:
    atlas_install_hooks.py [--repo-root R]
Exit 0 always (never block the caller); prints what it changed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import atlas_common  # noqa: E402

# The command prefix every Atlas hook shares.
_H = "${MNGR_AGENT_WORK_DIR:-.}/.agents/skills/atlas/scripts/"

# event -> the Atlas hook commands that must be present, in order.
WANTED: dict[str, list[str]] = {
    "PostToolUse": [f"{_H}atlas_checkpoint_hook.sh posttooluse"],
    "UserPromptSubmit": [f"{_H}atlas_live_reminder.sh", f"{_H}atlas_route_hook.sh"],
    "Stop": [f"{_H}atlas_checkpoint_hook.sh turn_end", f"{_H}atlas_summary_hook.sh"],
}


def ensure_hooks(settings: dict) -> bool:
    """Add any missing Atlas hooks to `settings` in place. Return True if changed."""
    changed = False
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        return False
    for event, commands in WANTED.items():
        groups = hooks.setdefault(event, [])
        if not isinstance(groups, list):
            continue
        present = {
            h.get("command")
            for g in groups
            if isinstance(g, dict)
            for h in (g.get("hooks") or [])
            if isinstance(h, dict)
        }
        missing = [c for c in commands if c not in present]
        if not missing:
            continue
        entries = [{"type": "command", "command": c} for c in missing]
        if groups and isinstance(groups[0], dict):
            groups[0].setdefault("hooks", []).extend(entries)
        else:
            groups.append({"hooks": entries})
        changed = True
    return changed


def scaffold_book(repo_root: Path) -> bool:
    """Create the book directory so the router/checkpoint hooks activate.

    The hooks and the sweep only act once `atlas/topics/` exists, and the router
    can't create the first page until it does -- so installation must scaffold it,
    otherwise the wired hooks no-op forever. Returns True if it created the dir.
    """
    topics = repo_root / "atlas" / "topics"
    if topics.is_dir():
        return False
    topics.mkdir(parents=True, exist_ok=True)
    return True


def _settings_path(repo_root: Path) -> Path:
    return repo_root / ".claude" / "settings.json"


def install(repo_root: Path) -> bool:
    path = _settings_path(repo_root)
    if path.is_file():
        try:
            settings = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return False  # never corrupt a settings file we can't parse
        if not isinstance(settings, dict):
            return False
    else:
        settings = {}
    if not ensure_hooks(settings):
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return True


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Wire Atlas hooks into settings.json.")
    parser.add_argument("--repo-root", default=None)
    args = parser.parse_args(argv)
    repo_root = atlas_common.resolve_repo_root(args.repo_root)
    hooks_changed = install(repo_root)
    book_created = scaffold_book(repo_root)
    parts = []
    parts.append("wired hooks" if hooks_changed else "hooks already present")
    if book_created:
        parts.append("scaffolded atlas/topics")
    print("atlas_install_hooks: " + "; ".join(parts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
