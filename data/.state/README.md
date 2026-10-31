# data/.state/

Machine state the workspace services read and write:

- `apps.toml` - The registry of running apps and their ports.
- `oom_priority/` - The memory-pressure shed ledger and agent-pid registry.
- `browser-screenshots/` - Shared browser service state. (The browser's instance records are `data/.apps/browser/instances.json`; a `browser-fleet.json` here is an older copy of them, read only until the daemon first writes that file.)
- `workspace_main_branch_initialized` - First-boot marker for moving the workspace onto a clean `main` branch. (A workspace first booted by an older build has `initial_chat_created` instead, which counts the same.)
- `last-restic-prune` - Backup maintenance timestamp.
- `user_timezone` - The user's IANA timezone, written once by the client that created the workspace and applied by the bootstrap at each boot.
- `profile/account.json` - The workspace profile snapshot: the workspace's account and owner, and the account-keyed settings its apps consume, materialized by a client (the Imbue Studio desktop or the web chrome) and never written from inside the workspace. Read by `system/libs/workspace_profile/`.
- `update-apply/` - The update flow's state: the armed schedule (`schedule.json`), the per-workspace skipped update target (`skipped-target.json`) and update-row dismissals (`dismissals.json`) the Imbue Studio app keeps here, the run record (`run.json`), the dispatch records (`last-dispatch.json`, `dispatch-attempt.json`, `watchdog.json`), the apply's own records (`marker.json`, `emergency.json`, `provision-incomplete.json`, `last-good.json`, `snapshots/`), and the app's installed dispatcher (`dispatcher.pyz`, `dispatcher.log`).
- `isolated-instances/` - State for temporarily booted app instances.
