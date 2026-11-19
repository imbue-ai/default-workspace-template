Claude records every change it makes to its memory folder, so the chat can show each one and undo it.

- `.claude/settings.json`: `system/scripts/agent_memory_change_log.py` (a stub for `workspace_bare_scripts.agent_memory_change_log`) runs as a UserPromptSubmit hook (`sync`, the turn's baseline) and a PostToolUse hook on file-writing and Bash calls (`post`, which records what the call changed and tells the agent). It records only; it is not a PreToolUse guard, and no other harness wires it, since memory is Claude's alone.
- The root workspace gains the `agent-memory` library (`system/libs/agent_memory`).
