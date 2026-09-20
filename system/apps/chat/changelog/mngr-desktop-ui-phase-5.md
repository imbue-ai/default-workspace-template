The chat app follows the desktop shell (phase 5 of `docs/system/blueprint/desktop-interface/plan-desktop-interface.md`):

- Opening a chat or a subagent view from inside the chat asks the shell for a path (`openPath`) rather than an address, so the desktop opens a window of the chat app at that path.

- The root relay now intercepts a `shell:open` of the chat's root path (`/` or `/?chat=<id>`) coming from an inner frame and selects that chat in place, forwarding every other open to the shell as before.

- The agent-side auto-open posts the desktop's `open` op (`{"op": "open", "args": {"app": "chat", "path": "/?chat=<id>", "client": <client id>}}`) instead of the tabbed shell's address op.

- The shell handshake test fixtures carry the desktop's window, desktop, and path fields.

- A chat started from inside a chat page is filed in no project: the shell's handshake now names a desktop where the tabbed shell named a project, and the desktop has no projects to file into.
