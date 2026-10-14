`agent_memory_context.py` honours the memory settings the Agent Memory app keeps in `data/.apps/memories/settings.json`. While memory is paused, or turned off for a harness, pi's memory sections are replaced by a short notice that memory is off, and Claude's UserPromptSubmit hook adds the same notice to every message, then says once that memory is back on (announcing the notes saved meanwhile). A settings file that can't be read counts as off, since reading it as on would ignore a user who turned memory off.

Added `docs/system/specs/agent-memory.md`, the technical design for Agent Memory and shared memory across harnesses: how the notes, `agent_memory_context.py`, the pi extension, Claude's hooks and the app fit together, the decisions behind them, limitations, testing and next steps.

A settings file with a bad encoding counts as memory off. `CLAUDE.md` tells Claude chats that a "memory is off" notice overrides the standing instruction to use built-in memory.
