# data/.state/

Machine state the workspace services read and write:

- `apps.toml` - The registry of running apps and their ports.
- `oom_priority/` - The memory-pressure shed ledger and agent-pid registry.
- `browser-screenshots/` - Shared browser service state. (The browser's instance records are `data/.apps/browser/instances.json`; a `browser-fleet.json` here is an older copy of them, read only until the daemon first writes that file.)
- `initial_chat_created` - First-boot marker for the welcome chat.
- `last-restic-prune` - Backup maintenance timestamp.
- `user_timezone` - The last timezone the bootstrap applied, re-applied at boot when the fetch from the user's computer fails.
- `isolated-instances/` - State for temporarily booted app instances.
