Workers and leads address each other by agent id, and every in-workspace message to a chat goes through the chat app (phase 1 of `docs/system/blueprint/chat-agent-split/`).

- `create_worker.py launch` stamps `lead_agent` with the lead's agent id (`MNGR_AGENT_ID`) instead of its name, reads the worker's id back from `mngr create --format jsonl` and stamps it as `worker_agent_id`, and sends the task through `system/scripts/message_chat.py` by that id. A rename of the lead's chat no longer strands the worker's report or its `mngr transcript $LEAD_AGENT`.

- New `create_worker.py reply --task-file <task> -m "..."` sends a lead's gate answer or nudge to the worker's chat through the chat app; `lead-proxy.md`, `dead-worker-recovery.md`, and the update-self, migrate-workspace, fetch-process-show, and crystallize-creation skills use it instead of `mngr message <worker>`. A task file from before the stamp takes `--name`.

- `worker-reporting.md` makes the write into the lead's workspace the primary report delivery, with the id-addressed `mngr rsync` kept for a lead in a worktree of its own; the lead's reply arrives as a message, never through `mngr message` by name.

- The update-self task-file template authors `lead_agent` as `$MNGR_AGENT_ID` (the id an older, non-stamping launcher leaves in place), not `$MNGR_AGENT_NAME`.
