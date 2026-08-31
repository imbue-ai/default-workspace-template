"""Tests that `autoMemoryDirectory` in `.claude/settings.json` actually takes effect.

Claude Code only honors an absolute or `~`-rooted `autoMemoryDirectory`. A
repo-relative value like `data/memories` is dropped *silently*: no warning, and
Claude falls back to `~/.claude/projects/<slug>/memory/`, so the `MEMORY.md`
maintained under `data/memories/` is never loaded into new chats. Nothing else
would catch that regression -- memory just quietly stops persisting -- so this
test pins the invariant.
"""

from __future__ import annotations

import json
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CLAUDE_SETTINGS = _REPO_ROOT / ".claude" / "settings.json"


def test_auto_memory_directory_is_absolute_or_home_rooted() -> None:
    """A relative path here is silently ignored by Claude Code, killing memory."""
    settings = json.loads(_CLAUDE_SETTINGS.read_text())
    memory_dir = settings.get("autoMemoryDirectory")
    assert isinstance(memory_dir, str) and memory_dir, (
        "settings.json must configure autoMemoryDirectory so Claude memory "
        "lands in the backed-up workspace data directory"
    )
    assert memory_dir.startswith("~/") or Path(memory_dir).is_absolute(), (
        f"autoMemoryDirectory is {memory_dir!r}, a relative path, which Claude "
        "Code silently ignores (memory falls back to ~/.claude/projects/). "
        "Use an absolute or ~-rooted path such as ~/workspace/data/memories."
    )
