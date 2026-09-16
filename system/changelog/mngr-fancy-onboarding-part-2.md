- `system/scripts/seed_welcome_chat.py` opens the workspace's first chat on the conversation the Mind app had before the workspace existed: it posts the turns to the chat app's `POST /api/chats/seed`, retrying while the app is still coming up, and prints the chat's id. The Mind app runs it through `mngr exec` the moment a workspace is ready.

- `system/scripts/welcome_count.py` counts how many times the welcome skill has run (`data/.state/welcome/count`) so the skill can vary its greeting; `message_chat.py`'s JSON post helper is public (`post_json`) so the seed script shares it.

- `.mngr/settings.toml`: the `first` create template, stacked once per workspace, is replaced by `welcome` (sends `/welcome` to every chat that starts with no message) and `fast` (the fast-mode settings, stacked on every chat while the workspace's fast-mode turn limit is above zero).

- Carries the phase 7 and 8 changes of the chat-agent split (`mngr/chat-agent-refactor-8`) into this branch.

- `.mngr/settings.toml`: the codex and pi system prompts no longer inline a fixed `/welcome` reply; they point at the welcome skill, so those harnesses run the same count-varied greeting claude does.
