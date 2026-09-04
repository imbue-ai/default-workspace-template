Switch the harness behind an existing chat without ending the conversation. Pick a different provider account from a chat's provider menu, confirm, and the chat keeps its identity, its place in the workspace, and its history while a fresh agent on the new harness takes over.

A switch runs as one ordered sequence with a single commit point: freeze the outgoing agent, archive its transcript, write the handover context, bring up the replacement, re-point the chat, then retire the old agent. Everything before the commit is reversible -- a replacement that fails to start is destroyed, the freeze is lifted, and the chat is exactly where it was. Nothing after it can lose the conversation.

The chat is held still for the duration. The freeze lives on the agent in mngr rather than in the app, so a turn cannot slip in from another window or from `mngr message` in a terminal during the window between the archive and the handover. Switching is refused up front, with the reason, for a chat that is mid-turn, has queued messages, already runs the target harness, or is already switching.

Progress is reported by the backend on the chat's row, so every open window (and a reload mid-switch) shows the same phase rather than only the window that clicked.

History spans the switch. Each retired agent's transcript is archived beside the saved layouts before `mngr destroy` takes its files, and a chat's events are read back as its archived segments followed by its live one. The replacement starts with a pointer to a context file describing the conversation so far.
