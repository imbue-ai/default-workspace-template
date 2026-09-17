# docs/system/

Internal documentation for the workspace machinery.

- `workspace-internals.md` - How the workspace template is put together:
  structure, create templates, and the creation lifecycle.
- `specs/` - Design specifications for workspace features.
- `blueprint/` - Implementation plans from feature work. The current direction for the
  workspace UI is `blueprint/workspace-app-model/plan-workspace-app-model.md`,
  with its contracts and per-phase specs beside it in the same folder.
  The plan that separates chats from agents (so a chat can switch harness) is
  `blueprint/chat-agent-split/plan-chat-agent-split.md`.
- `style_guide.md` - The code style guide (a symlink into the vendored mngr
  repo, which is its source of truth).

## The workspace layout migration

A workspace created before the workspace app model kept its projects and
layouts under the primary agent's `workspace_layout/` directory. The first
boot after the update carries them into the shell's state files with
`system/scripts/migrate_workspace_layouts.py` (bootstrap runs it at every
boot, and the update apply runs it once before its restart). It is guarded by
`data/.state/system_interface/migrated.json`, leaves the old directory
untouched, keeps any output that already exists, and only ever adds records to
the files and terminal apps' stores. To see what a run would write:

```bash
python3 system/scripts/migrate_workspace_layouts.py plan --json
```

To run it again after fixing a problem (the projects file and the seeds are
rewritten; the app stores still only gain records):

```bash
python3 system/scripts/migrate_workspace_layouts.py --force run
```

The spec is `blueprint/workspace-app-model/phase_09_migration.md`.
