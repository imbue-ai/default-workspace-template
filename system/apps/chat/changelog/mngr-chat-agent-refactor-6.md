Phase 6 of the chat-agent split (`docs/system/blueprint/chat-agent-split/`): a chat can change account in place.

- Switching a chat to another account on its own harness and lane is now a rebind: the same agent is stopped, its binding repointed in its own state dir (for claude, the chat's session files move into the new account's folder first, so the transcript and `claude --resume` follow), its `account` label rewritten, and it is started again; the transcript, the tk steps, and the model settings stay. An account on another harness or lane is a handoff, as before.

- In the composer's provider menu every account but the chat's own is now a switch target (the "Launch a new chat?" prompt is gone). The confirm reads one line for a rebind ("Claude Code restarts on Anthropic 2 (Claude Code) and keeps this conversation.") and, for either kind of switch, offers "Start a new chat instead", which opens a new chat on that account with the typed message and leaves this chat alone.

- While the agent restarts, the held messages, the strip, and the placeholder read "Restarting Claude Code on Anthropic 2 (Claude Code)..."; there is no "Cancel switch" for a rebind, since the agent restarts as soon as the switch is confirmed. A restart that fails shows "Could not restart Claude Code" with mngr's reason, a retry on the accounts of the same harness and lane, and "Start a new chat instead".

- The `chats_updated` snapshot's `handoff` object gains `kind` (`handoff` or `rebind`), `target_label`, and the `restarting` phase; the switch route's answer gains `kind`; the 409s a converging chat answers name the account for a rebind. The chat record gains a `rebind` entry beside `handoff`.
