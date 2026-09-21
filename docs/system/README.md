# docs/system/

Internal documentation for the workspace machinery.

- `workspace-internals.md` - How the workspace template is put together:
  structure, create templates, and the creation lifecycle.
- `specs/` - Design specifications for workspace features.
- `blueprint/` - Implementation plans from feature work. The current direction for the
  workspace UI is `blueprint/desktop-interface/plan-desktop-interface.md`,
  with its contracts and geometry vectors beside it in the same folder; the
  `workspace-app-model/` plan it grew out of stays as history.
  The plan that separates chats from agents (so a chat can switch harness) is
  `blueprint/chat-agent-split/plan-chat-agent-split.md`.
  The launcher's next shape and the Getting Started app are
  `blueprint/launcher-and-getting-started/plan-launcher-and-getting-started.md`.
- `avatar-designs.md` - How to draw and register a desktop avatar design (the
  drawing format the shell validates, and the registration helper).
- `style_guide.md` - The code style guide (a symlink into the vendored mngr
  repo, which is its source of truth).

