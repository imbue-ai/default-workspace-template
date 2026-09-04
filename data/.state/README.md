# data/.state/

Machine state the workspace services read and write:

- `apps.toml` - The registry of running apps and their ports.
- `oom_priority/` - The memory-pressure shed ledger and agent-pid registry.
- `browser-screenshots/` - Shared browser service state. (The fleet manifest moved to `data/.apps/browser/instances.json`; a `browser-fleet.json` left here is the pre-move copy, read only until the daemon first writes the new path.)
- `initial_chat_created` - First-boot marker for the welcome chat.
- `last-restic-prune` - Backup maintenance timestamp.
- `isolated-instances/` - State for temporarily booted app instances.
