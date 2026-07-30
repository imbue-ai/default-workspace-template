Chat now declines `/status` instead of sending it to the agent.

A chat message reaches an agent by being typed into its terminal, and `/status` replaces Claude
Code's input box with a full-screen view. Sending it left the agent unable to accept any further
message until someone closed that view from the terminal -- which a chat user cannot do. The
composer now shows a short notice explaining why, and keeps the typed message so nothing is lost.

The list of commands that behave this way lives in `models/claudeSlashCommands.ts`, since it is a
fact about Claude Code rather than about the chat. It starts with `/status`; other commands that
take over the input box will be added as each is confirmed.
