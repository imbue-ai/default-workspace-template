# data/

Everything your workspace stores lives here. This folder is deliberately kept
out of git (its contents can be large or personal); the workspace's continuous
encrypted backup protects all of it.

Visible folders:

- `creations/<name>/` - Stored data belonging to each creation in
  `/home/user/workspace/creations/`.
- `memories/` - Your mind's long-term memory notes.
- `uploads/` - Files you attach to chat messages.
- `chat-files/` - Files your mind shares back to you in chat as downloads.
- `chat-images/` - Images your mind shows you inline in chat.
- `system/` - Workspace configuration written at runtime (backup settings,
  GitHub sync settings).

Hidden folders (dot-prefixed; workspace machinery, safe to ignore):

- `.tickets/` - The mind's internal task tracker records.
- `.tasks/` - Scratch space for work delegated between agents.
- `.state/` - Machine state: service registries, markers, and ledgers.
- `.secrets/` - Credentials injected by the minds app (backup and tunnel
  tokens). Never committed, never synced to GitHub.
