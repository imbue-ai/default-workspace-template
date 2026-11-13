Codex chats now run at the speed their fast-mode setting asks for. Before this, the agent terminal's codex TUI could undo the setting whenever it attached: on a new chat, and after every restart.

- Codex is bumped from 0.161.0 to 0.166.0 (`CODEX_VERSION` in `system/scripts/setup_system.sh`, and `[agent_types.codex]`'s `version`). Since 0.164.0, codex keeps a thread's speed tier when it rebuilds the thread, which it does when the TUI attaches and on restart.

- `[agent_types.codex]` no longer pins `service_tier = "default"`, and the `fast` create template no longer sets a codex tier. The TUI re-sent any configured tier when it attached, which undid a fast switch made before it attached, and turned an auto chat that had already dropped to standard speed back to fast after a restart.

- A codex chat that launches fast (fast mode on, or auto with its fast turns still to run) has fast mode turned on by the chat app with a model switch once its agent is up, both for a new chat and for a handoff's successor.

- A turn typed directly in the agent terminal on a model whose default is the fast tier (GPT-6 Sol) now runs fast, and the model bar shows it.
