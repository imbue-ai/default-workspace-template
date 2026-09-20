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

