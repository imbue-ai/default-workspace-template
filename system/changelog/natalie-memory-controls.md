`agent_memory_context.py` honours the memory settings the Agent Memory app keeps in `data/.apps/memories/settings.json`. While memory is paused, or turned off for a harness, pi's memory sections are replaced by a short notice that memory is off, and Claude's UserPromptSubmit hook adds the same notice to every message, then says once that memory is back on (announcing the notes saved meanwhile). A settings file that can't be read counts as off, since reading it as on would ignore a user who turned memory off.

Added `docs/system/specs/agent-memory.md`, the technical design for Agent Memory and shared memory across harnesses: how the notes, `agent_memory_context.py`, the pi extension, Claude's hooks and the app fit together, the decisions behind them, limitations, testing and next steps.

`agent_memory_context.py` review fixes:
- The index sync takes the same lock as the Agent Memory app and drops lines for note files that are gone, so a deleted fact can't come back into what chats start from.
- Claude's hook announces every delete and edit, and says how many saved notes it left off its list.
- A delete notice stops once the note is saved again.
- A settings file with a bad encoding counts as memory off.
- Note and index line endings are kept on disk.
- The hook's mark sits five seconds behind its clock, so a change recorded at the same moment isn't missed.
- The pi extension stamps notes written through a symlinked path, and logs why the script could not start.
- `CLAUDE.md` tells Claude chats that a "memory is off" notice overrides the standing instruction to use built-in memory.
