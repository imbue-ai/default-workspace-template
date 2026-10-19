# data/.state/

Machine state the workspace services read and write:

- `apps.toml` - The registry of running apps and their ports.
- `oom_priority/` - The memory-pressure shed ledger and agent-pid registry.
- `screen/` - The Screen's rendered `xorg.conf`, `neko.yaml`, `pulse.pa`, its control lease (`control.json`), the display modes added this stack life (`modes.json`) and the events its watcher appends (`events/screen/events.jsonl`). (The browser's instance records are `data/.apps/browser/instances.json`.)
- `workspace_main_branch_initialized` - First-boot marker for moving the workspace onto a clean `main` branch. (A workspace first booted by an older build has `initial_chat_created` instead, which counts the same.)
- `last-restic-prune` - Backup maintenance timestamp.
- `user_timezone` - The last timezone the bootstrap applied, re-applied at boot when the fetch from the user's computer fails.
- `isolated-instances/` - State for temporarily booted app instances.
