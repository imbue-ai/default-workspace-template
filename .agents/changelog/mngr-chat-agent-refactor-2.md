Phase 2 of the chat-agent split (`docs/system/blueprint/chat-agent-split/`): the skills name a chat apart from the agent it runs on.

- The skills that open or message their own chat (caretaker, manage-layout, manage-scheduled-tasks, update-self's `surface-chat-tab`, now `--chat-id`) and the ones that file a permission request with the latchkey gateway (latchkey, file-sharing, github-sync, publish-template, migrate-workspace, minds-api) use `${MINDS_CHAT_ID:-$MNGR_AGENT_ID}`: the chat's id on an agent the chat app created, the agent's own id for one that is its own chat.

- `create_worker.py launch` stamps `lead_work_dir` (the lead's `MNGR_AGENT_WORK_DIR`) beside `lead_agent`, and `worker-reporting.md` has the worker write its report straight into that checkout, falling back to the repo's main worktree when the field is absent; the `mngr rsync` delivery for a lead in a worktree of its own is gone. `lead_agent` stays the dispatching agent's id: it is what `mngr transcript` reads, and mngr knows only agents.

- The launch-task skill names the launcher by its real path, `.agents/skills/launch-task/scripts/create_worker.py`.
