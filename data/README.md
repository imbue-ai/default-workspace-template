# data/

Everything your workspace stores lives here. This folder is deliberately kept
out of git (its contents can be large or personal); the workspace's continuous
encrypted backup protects all of it.

The convention: **everything visible here is yours** -- organize, rename, or
delete it however you like. The dot-prefixed folders are workspace machinery.

Visible folders (starters -- reshape them freely):

- `documents/` - Documents your mind makes for you (reports, notes, exports).
- `my-project/` - An example per-project folder; make one per effort.
- `uploads/` - Files you attach to chat messages (recreated on demand if you
  rename it).
- `memories/` - Your mind's long-term memory notes.
- `system/` - Workspace configuration written at runtime (backup settings,
  GitHub sync settings).

Hidden folders (dot-prefixed; workspace machinery, safe to ignore):

- `.apps/` - Stored data belonging to each app in
  `/home/user/workspace/system/apps/`.
- `.skills/` - Stored data a skill keeps for itself across runs.
- `.tickets/` - The mind's internal task tracker records.
- `.tasks/` - Scratch space for work delegated between agents.
- `.state/` - Machine state: service registries, markers, and ledgers.
- `.secrets/` - Credentials injected by the minds app (backup and tunnel
  tokens). Never committed, never synced to GitHub.
