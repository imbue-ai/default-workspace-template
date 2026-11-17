Codex chats now run at the speed their fast-mode setting asks for. Before this, the agent terminal's codex TUI could undo the setting whenever it attached: on a new chat, and after every restart. Codex keeps a thread's speed tier when it rebuilds the thread (when the TUI attaches, and on restart), so a tier set by a switch now holds.

- `[agent_types.codex]` no longer pins `service_tier = "default"`, and the `fast` create template no longer sets a codex tier. The TUI re-sent any configured tier when it attached, which undid a fast switch made before it attached, and turned an auto chat that had already dropped to standard speed back to fast after a restart.

- A codex chat that launches fast (fast mode on, or auto with its fast turns still to run) has fast mode turned on by the chat app with a model switch once its agent is up, both for a new chat and for a handoff's successor.

- A turn typed directly in the agent terminal on a model whose default is the fast tier (GPT-6 Sol) now runs fast, and the model bar shows it.
