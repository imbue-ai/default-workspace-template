# data/.state/

Machine state the workspace services read and write:

- `apps.toml` - The registry of running apps and their ports.
- `oom_priority/` - The memory-pressure shed ledger and agent-pid registry.
- `browser-fleet.json`, `browser-screenshots/` - Shared browser service state.
- `initial_chat_created` - First-boot marker for the welcome chat.
- `last-restic-prune` - Backup maintenance timestamp.
- `isolated-instances/` - State for temporarily booted app instances.
- `user_knowledge.toml` - Whether the user has already used permissions, sharing, or inspirations, so the agent doesn't proactively suggest a feature they already know about. Written by `publish-inspiration`, `use-inspiration`, and `share-workspace`; read by any skill or agent judgment before making that kind of suggestion. See those skills for the schema.
