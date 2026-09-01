"""Tests that `autoMemoryDirectory` in `.claude/settings.json` actually takes effect.

Claude Code only honors an absolute or `~`-rooted `autoMemoryDirectory`. A
repo-relative value like `data/memories` is dropped *silently*: no warning, and
Claude falls back to `~/.claude/projects/<slug>/memory/`, so the `MEMORY.md`
maintained under `data/memories/` is never loaded into new chats. Nothing else
would catch that regression, so this test pins the invariant.
"""

from __future__ import annotations

import json
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CLAUDE_SETTINGS = _REPO_ROOT / ".claude" / "settings.json"


def test_auto_memory_directory_is_absolute_or_home_rooted_and_in_workspace_data() -> (
    None
):
    """A relative path here is silently ignored by Claude Code, killing memory."""
    settings = json.loads(_CLAUDE_SETTINGS.read_text())
    memory_dir = settings.get("autoMemoryDirectory")
    assert isinstance(memory_dir, str) and memory_dir, (
        f"settings.json sets no usable autoMemoryDirectory (got {memory_dir!r})"
    )
    assert memory_dir.startswith("~/") or Path(memory_dir).is_absolute(), (
        f"autoMemoryDirectory is the relative path {memory_dir!r}, which Claude "
        "Code silently ignores, so memory never loads -- use ~/workspace/data/memories"
    )
    assert memory_dir.endswith("/data/memories"), (
        f"autoMemoryDirectory is {memory_dir!r}; memory must live in the "
        "workspace's data/memories (gitignored, covered by the host-backup "
        "restic snapshot). Use ~/workspace/data/memories."
    )
