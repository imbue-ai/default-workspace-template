Two more harnesses can be launched from the workspace: OpenCode and Antigravity (`agy`). Both binaries are baked into the image at pinned versions, both mngr plugins are registered, and both get a New Tab tile beside Codex and Pi.

This is the launch path only. Neither harness reads its transcript yet, so a chat tab on one opens blank and its model bar is empty -- the point of this change is that `mngr create <name> --type opencode` and `--type antigravity` work at all, which they previously did not: the shared `chat` role template sets an output style, and neither plugin had anywhere to put one, so mngr rejected the create outright rather than launching an agent that would silently ignore its role.

Both harnesses also launch inside an OOM priority band, like Claude, Codex and Pi. Without one, earlyoom sheds an agent by raw kernel score rather than the user/worker tiering, so it could take a user's chat before a worker's build subprocess.
